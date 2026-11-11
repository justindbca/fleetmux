"""Hot worker roster (start --roster): add / graceful remove / reject, while the fleet runs.

Real private Redis v2 + real hook entry points, fake cmux and CLIs (same harness as the
Codex supervisor + Claude worker tests).
"""
import json
import os
import types
import uuid

import pytest

from test_v2_codex_claude import WORKERS, answer_turn, decision, mixed, no_provisioning, submit, typed  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from fleetmux import control_view, fleet, roster

REPORT = 'Done:\n- wrote part.md\nEvidence:\n- {w}/part.md\nNot done: none\nBlocked: none'


def write_roster(f, entries):
    """Write the roster yaml and make sure the next tick sees a new mtime."""
    path = f.run_dir / 'roster.yaml'
    lines = ['workers:'] + ['  - name: ' + e if isinstance(e, str) else
                            '  - {name: ' + e[0] + ', role: ' + e[1] + '}' for e in entries]
    path.write_text('\n'.join(lines) + '\n')
    stamp = getattr(f, 'test_stamp', 1_700_000_000_000_000_000) + 1_000_000_000
    f.test_stamp = stamp
    os.utime(path, ns=(stamp, stamp))
    return path


@pytest.fixture
def hot(mixed):  # noqa: F811
    f = mixed
    path = write_roster(f, WORKERS)
    f.cfg['roster'] = str(path)
    fleet.save(f.path, dict(fleet.load(f.path), roster=str(path)))
    f.roster_tick()  # same roster as the start: nothing changes
    assert f.workers == WORKERS and not audits(f, 'roster.changed')
    return f


def audits(f, event):
    return [fields for _, fields in f.r.xrange(f.v2.bus.audit_key) if fields.get('event') == event]


def disk_workers(f):
    return fleet.load(f.path)['workers']


def test_yaml_add_spawns_persists_and_takes_v2_delivery(hot, monkeypatch, tmp_path):
    f = hot
    latest = tmp_path / 'latest.json'
    fleet.save(latest, dict(fleet.load(f.path), config=str(f.path)))
    monkeypatch.setattr(fleet, 'LATEST', latest)
    (f.run_dir / 'roles').mkdir()
    (f.run_dir / 'roles' / 'reviewer.md').write_text('Review diffs only.')
    spawned = len(f.mgr.spawned)
    write_roster(f, WORKERS + [('reviewer-1', 'roles/reviewer.md')])
    f.tick()
    assert f.workers == WORKERS + ['reviewer-1'] and f.names[-1] == 'reviewer-1'
    assert len(f.mgr.spawned) == spawned + 1                     # only the new worker starts
    for cfg in (fleet.load(f.path), fleet.load(latest)):         # hooks read membership from disk
        assert cfg['workers'] == WORKERS + ['reviewer-1']
        assert cfg['worker_roles'] == {'reviewer-1': str((f.run_dir / 'roles' / 'reviewer.md').resolve())}
    info = f.agent('reviewer-1')
    assert info['label'] == 'reviewer-1' and info['state'] == 'starting' and info['generation'] == '0'
    assert f.r.hget(fleet.NAMES, 'reviewer-1') == 'reviewer-1'
    new_pane, peer = f.mgr.tabbed[-1]                               # a tab at the end of the worker column
    assert new_pane == info['pane'] and peer in [f.agent(w)['pane'] for w in WORKERS]
    assert 'Review diffs only.' in (fleet.agent_cwd(f.cfg, 'reviewer-1') / 'CLAUDE.md').read_text()
    assert 'reviewer-1' in control_view.snapshot(fleet.load(f.path))['slots']
    changed, = audits(f, 'roster.changed')
    assert json.loads(changed['results']) == {'reviewer-1': 'added'}
    # READY through the real Claude Stop hook, then a normal v2 delivery.
    f.test_threads['reviewer-1'] = str(uuid.uuid4())
    assert answer_turn(f, 'reviewer-1', 'READY', startup=True)['status'] == 'ready'
    submit(f)
    prompt = typed(f, 'supervisor')[-1]
    assert 'reviewer-1' in prompt.split('Workers you can dispatch to: ')[1].split('\n')[0]
    assert '[Roster change] Workers you can dispatch to now: ' in prompt and prompt.rstrip().endswith('reviewer-1')
    assert answer_turn(f, 'supervisor', decision(['reviewer-1']))['status'] == 'committed'
    f.pump()
    text, = typed(f, 'reviewer-1')
    assert 'Write reviewer-1/part.md' in text and f.v2.bus.slot_state('reviewer-1')['status'] == 'received'
    assert answer_turn(f, 'reviewer-1', REPORT.format(w='reviewer-1'))['status'] == 'committed'


def test_yaml_remove_waits_for_busy_worker_then_retires(hot):
    f = hot
    submit(f)
    answer_turn(f, 'supervisor', decision(['worker-1']))
    f.pump()
    pane = f.agent('worker-1')['pane']
    write_roster(f, WORKERS[1:])
    f.tick()
    # Retiring: still running its delivery, counted as active, but no new work.
    assert 'worker-1' in f.workers and 'worker-1' in disk_workers(f) and pane in f.mgr.alive
    assert f.agent('worker-1')['state'] == 'busy' and f.others_active()
    assert 'worker-1' not in f.dispatch_targets() and 'worker-1' not in f.labels()
    with pytest.raises(ValueError, match='dispatch.to'):
        fleet.parse_decision(decision(['worker-1']), f.dispatch_targets(), True)
    out = f.workspace() / 'worker-1' / 'part.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('findings')
    assert answer_turn(f, 'worker-1', REPORT.format(w='worker-1'))['status'] == 'committed'
    f.tick()
    assert 'worker-1' not in f.workers and disk_workers(f) == WORKERS[1:]
    assert pane in f.mgr.killed and f.agent('worker-1')['state'] == 'retired'
    assert out.read_text() == 'findings'                         # workspace kept for artifact checks
    assert not f.paused()
    record, = [e for e in fleet.history.entries(f.run_dir) if e.get('slot') == 'worker-1']
    assert record['source'] == 'retired' and record['label'] == 'worker-1'
    assert 'worker-1' in [s for s, _, _ in f.history_slots()]   # its history stays browsable
    assert audits(f, 'roster.retired')
    # The report it already sent still reaches the supervisor, with the new roster.
    f.pump()
    followup = typed(f, 'supervisor')[-1]
    assert followup.startswith('[worker-1 report]') and '[Roster change]' in followup


def test_readding_a_retiring_worker_cancels_the_retire(hot):
    f = hot
    submit(f)
    answer_turn(f, 'supervisor', decision(['worker-1']))
    f.pump()
    write_roster(f, WORKERS[1:])
    f.tick()
    write_roster(f, WORKERS)
    f.tick()
    assert json.loads(audits(f, 'roster.changed')[-1]['results']) == {'worker-1': 'retire cancelled'}
    answer_turn(f, 'worker-1', REPORT.format(w='worker-1'))
    f.tick()
    assert f.workers == WORKERS and f.agent('worker-1')['state'] == 'idle'
    assert 'worker-1' in f.dispatch_targets() and f.agent('worker-1')['pane'] in f.mgr.alive


def test_role_change_applies_at_next_generation_without_respawn(hot):
    f = hot
    (f.run_dir / 'exit.md').write_text('Exit role v2.')
    spawned, pane = len(f.mgr.spawned), f.agent('worker-2')['pane']
    write_roster(f, ['worker-1', ('worker-2', 'exit.md')] + WORKERS[2:])
    f.tick()
    assert len(f.mgr.spawned) == spawned and f.agent('worker-2')['pane'] == pane
    assert 'Exit role v2.' not in (fleet.agent_cwd(f.cfg, 'worker-2') / 'CLAUDE.md').read_text()
    assert fleet.load(f.path)['worker_roles'] == {'worker-2': str((f.run_dir / 'exit.md').resolve())}
    assert 'next generation' in json.loads(audits(f, 'roster.changed')[-1]['results'])['worker-2']
    f.setf('worker-2', generation=1)  # the next spawn of this slot (handoff successor)
    f.spawn('worker-2', 'takeover')
    assert 'Exit role v2.' in (fleet.agent_cwd(f.cfg, 'worker-2') / 'CLAUDE.md').read_text()


@pytest.mark.parametrize('text,reason', [
    ('workers: [ -\n', 'invalid YAML'),
    ('workers: []\n', 'at least one worker'),
    ('workers:\n  - name: worker-1\n  - name: worker-1\n', 'duplicate worker name worker-1'),
    ('workers:\n  - name: Worker_1\n', 'name must match'),
    ('workers:\n  - name: worker.2\n', 'no dots'),
    ('workers:\n  - name: supervisor\n', 'not be "supervisor"'),
    ('workers:\n  - {name: worker-1, role: nope.md}\n', 'role file not found: nope.md'),
    ('workers:\n  - {name: worker-1, model: x}\n', 'optional "role", "profile", "writable", "window",'),
    ('workers:\n  - {name: worker-1, handoff_ratio: 1.0}\n', 'workers[0]: handoff_ratio must be a number between 0 and 1'),
    ('workers:\n  - {name: worker-1, window: 0}\n', 'workers[0]: window must be a positive integer'),
    ('workers:\n  - {name: worker-1, window: true}\n', 'workers[0]: window must be a positive integer'),
    ('workers:\n  - name: worker-1\nhandoff: {claude: {ratio: 0}}\n', 'handoff[claude]: ratio must be a number'),
    ('workers:\n  - name: worker-1\nhandoff: {claude:opus: {window: 1.5}}\n', 'handoff[claude:opus]: window must be'),
    ('workers:\n  - name: worker-1\nhandoff: {gemini: {window: 10}}\n', 'handoff[gemini]: key must be'),
    ('workers:\n  - name: worker-1\nhandoff: {codex: {limit: 10}}\n', '"window", "ratio" and/or "midturn_ratio" only'),
    ('workers:\n  - {name: worker-1, profile: Bad_Id}\n', 'profile must be an agent card id'),
    ('workers:\n  - {name: worker-1, profile: no-such-card}\n', 'worker-1: unknown agent profile no-such-card'),
    ('agents: []\n', 'only a "workers" list'),
])
def test_invalid_roster_is_rejected_whole_and_roster_kept(hot, text, reason):
    f = hot
    path = write_roster(f, ['worker-1'])  # would retire five workers if it were accepted
    path.write_text(text)
    os.utime(path, ns=(f.test_stamp + 1, f.test_stamp + 1))
    f.tick()
    rejected, = audits(f, 'roster.rejected')
    assert any(reason in e for e in json.loads(rejected['errors'])) and rejected['level'] == 'error'
    assert f.workers == WORKERS and f.desired == WORKERS and disk_workers(f) == WORKERS
    assert not audits(f, 'roster.changed')
    f.tick()  # unchanged file: no repeated rejection
    assert len(audits(f, 'roster.rejected')) == 1


def test_previously_used_name_is_rejected(hot):
    f = hot
    (f.workspace() / 'old-1').mkdir()
    write_roster(f, WORKERS + ['old-1', 'fresh-1'])
    f.tick()
    rejected, = audits(f, 'roster.rejected')
    errors = json.loads(rejected['errors'])
    assert len(errors) == 1 and errors[0].startswith('old-1: name was already used')
    assert f.workers == WORKERS and 'fresh-1' not in disk_workers(f)  # whole change rejected


def test_retired_name_cannot_come_back(hot):
    f = hot
    write_roster(f, WORKERS[1:])
    f.tick()
    assert 'worker-1' not in f.workers
    write_roster(f, WORKERS)
    f.tick()
    assert 'already used' in audits(f, 'roster.rejected')[-1]['errors'] and 'worker-1' not in f.workers


def test_failed_spawn_then_removal_retires_without_pausing(hot):
    f = hot
    f.mgr.spawn_interactive_cli = lambda **kw: types.SimpleNamespace(success=False, pane_id=None, error='boom')
    write_roster(f, WORKERS + ['extra-1'])
    f.tick()
    assert 'spawn failed' in json.loads(audits(f, 'roster.changed')[-1]['results'])['extra-1']
    assert f.agent('extra-1')['state'] == 'dead'
    write_roster(f, WORKERS)
    f.tick()
    assert 'extra-1' not in f.workers and f.agent('extra-1')['state'] == 'retired' and not f.paused()


def test_roster_parse_resolves_role_relative_to_file(tmp_path):
    (tmp_path / 'roles').mkdir()
    (tmp_path / 'roles' / 'exit.md').write_text('x')
    (tmp_path / 'r.yaml').write_text('workers:\n  - name: exit\n    role: roles/exit.md\n  - name: reentry\n')
    assert roster.load(tmp_path / 'r.yaml') == (['exit', 'reentry'], {'exit': str((tmp_path / 'roles/exit.md').resolve())})


def test_start_roster_validation_precedes_provisioning(monkeypatch, tmp_path):
    no_provisioning(monkeypatch)
    base = ['start', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--allow-development-v2']
    good = tmp_path / 'good.yaml'
    good.write_text('workers:\n  - name: exit\n')
    with pytest.raises(SystemExit, match='replaces --workers'):
        fleet.main(base + ['--roster', str(good), '--workers', '2'])
    bad = tmp_path / 'bad.yaml'
    bad.write_text('workers:\n  - name: exit\n  - name: exit\n')
    with pytest.raises(SystemExit, match='duplicate worker name exit'):
        fleet.main(base + ['--roster', str(bad)])


def test_handoff_table_hot_reloads_into_status(hot):
    """docs/design.md §22: a handoff-only edit is applied by the running daemon and shown by status."""
    f = hot
    path = write_roster(f, WORKERS)
    path.write_text(path.read_text().replace('  - name: worker-2\n', '  - {name: worker-2, handoff_ratio: 0.8}\n')
                    + 'handoff:\n  claude: {window: 1000000}\n')
    os.utime(path, ns=(f.test_stamp + 1, f.test_stamp + 1))
    f.tick()
    changed, = audits(f, 'roster.changed')
    assert json.loads(changed['handoff'])['worker-2'] == {'before': {}, 'after': {'ratio': 0.8}}
    assert f.workers == WORKERS and disk_workers(f) == WORKERS  # nothing else changed
    slots = control_view.snapshot(fleet.load(f.path))['slots']
    assert slots['worker-2']['context']['handoff_ratio'] == 0.8
    assert slots['worker-1']['context']['handoff_window'] == '1000000'
    assert slots['worker-1']['context']['handoff_source'] == 'window: roster claude; ratio: --handoff-ratio'


def test_added_rows_on_a_capped_default_model_are_rejected_whole(hot):
    # Added rows run the fleet default: its model card (Fable: max_concurrent 1) caps them.
    from fleetmux.agent_registry import AgentRegistry
    f = hot
    f.cfg['worker_model'] = AgentRegistry().model('fable').model
    write_roster(f, WORKERS + ['fresh-1'])
    f.tick()
    rejected, = audits(f, 'roster.rejected')
    assert any('exceed max_concurrent 1' in e for e in json.loads(rejected['errors']))
    assert f.workers == WORKERS and 'fresh-1' not in disk_workers(f)


def test_rotation_whose_old_tab_survives_records_the_launch_it_ran(hot, monkeypatch):
    # model_usage: the surviving old tab holds the model of the launch it ran, even after an earlier
    # switch left retired_launch='' in the hash (that would read as the fleet default).
    from fleetmux import profile_launch
    f = hot
    ran = profile_launch.encode_launch({'profile_id': 'claude-coder', 'overrides': {}, 'role_text': ''})
    draft = f.run_dir / 'draft-h1.md'
    draft.write_text('x')
    f.setf('worker-1', runtime_launch=ran, retired_launch='', handoff_id='h1', handoff_draft=str(draft),
           handoff_mtime=0)
    monkeypatch.setattr(f.mgr, 'kill_pane', lambda pane: False)
    old = f.agent('worker-1')['pane']
    f.finish_rotation('worker-1')
    info = f.agent('worker-1')
    assert info['state'] == 'dead' and info['retired_pane'] == old and info['retired_launch'] == ran


# ------------------------------------------------------------------ worker names end with -<number> (§3, 2026-10-28)
def test_start_rejects_a_roster_worker_name_without_a_number(monkeypatch, tmp_path):
    no_provisioning(monkeypatch)
    base = ['start', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--allow-development-v2']
    bad = tmp_path / 'bad.yaml'
    bad.write_text('workers:\n  - name: worker-1\n  - name: proto\n  - name: term-iface\n')
    with pytest.raises(SystemExit) as exc:
        fleet.main(base + ['--roster', str(bad)])
    text = str(exc.value)
    assert 'workers[1] (proto): ' in text and 'rename it proto-1' in text and 'proto-1.1' in text
    assert 'workers[2] (term-iface): ' in text and 'rename it term-iface-1' in text
    assert 'worker-1)' not in text  # the default name complies


def test_start_rejects_worker_names_without_a_number(monkeypatch):
    no_provisioning(monkeypatch)
    base = ['start', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--allow-development-v2']
    with pytest.raises(SystemExit, match=r'--worker-names: rung: .*rename it rung-1'):
        fleet.main(base + ['--worker-names', 'exit-1,rung'])


def test_start_rejects_a_sub_fleet_roster_worker_without_a_number(monkeypatch, tmp_path):
    no_provisioning(monkeypatch)
    (tmp_path / 'sup.md').write_text('x')
    (tmp_path / 'child.yaml').write_text('workers:\n  - name: analyst\n')
    path = tmp_path / 'r.yaml'
    path.write_text('workers:\n  - name: worker-1\nsubfleets:\n'
                    '  - {name: ops, supervisor_slot: worker-1, supervisor_role: sup.md, roster: child.yaml}\n')
    base = ['start', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--allow-development-v2']
    with pytest.raises(SystemExit, match=r'subfleets\[0\] \(ops\): .*workers\[0\] \(analyst\): .*rename it analyst-1'):
        fleet.main(base + ['--roster', str(path)])


def test_hot_added_worker_without_a_number_is_rejected_whole(hot):
    f = hot
    write_roster(f, WORKERS + ['fresh'])
    f.tick()
    rejected, = audits(f, 'roster.rejected')
    errors = json.loads(rejected['errors'])
    assert errors == [e for e in errors if 'workers[6] (fresh)' in e] and 'rename it fresh-1' in errors[0]
    assert f.workers == WORKERS and 'fresh' not in disk_workers(f)
    write_roster(f, WORKERS + ['fresh-1'])
    f.tick()
    assert 'fresh-1' in f.workers and 'fresh-1' in disk_workers(f)


def test_a_suffixed_roster_name_rotates_to_name_dot_generation(hot):
    f = hot
    write_roster(f, WORKERS + ['proto-1'])
    f.tick()
    assert f.label('proto-1') == 'proto-1'
    draft = f.run_dir / 'draft-h1.md'
    draft.write_text('x')
    f.setf('proto-1', handoff_id='h1', handoff_draft=str(draft), handoff_mtime=0)
    f.finish_rotation('proto-1')
    info = f.agent('proto-1')
    assert info['generation'] == '1' and info['label'] == 'proto-1.1' and f.label('proto-1') == 'proto-1.1'
    assert f.r.hget(fleet.NAMES, 'proto-1.1') == 'proto-1'  # the new label routes to the same slot


LEGACY = ['proto', 'term-iface']  # a run started before the rule


@pytest.fixture
def legacy_names(monkeypatch):
    import test_v2_codex_claude
    monkeypatch.setattr(test_v2_codex_claude, 'WORKERS', LEGACY)


@pytest.fixture
def legacy(legacy_names, mixed):  # noqa: F811
    f = mixed
    assert f.workers == LEGACY
    path = write_roster(f, LEGACY)
    f.cfg['roster'] = str(path)
    fleet.save(f.path, dict(fleet.load(f.path), roster=str(path)))
    return f


def test_a_run_with_old_style_names_keeps_them_on_reload_and_only_new_names_are_checked(legacy):
    f = legacy
    f.roster_tick()  # the daemon's first tick after a resume re-reads the stored roster
    assert not audits(f, 'roster.rejected') and f.workers == f.desired == LEGACY
    roster.write_advisor(f.cfg['roster'], False, f.workers)  # `fleet advisor off` still writes that roster
    write_roster(f, LEGACY + ['extra'])
    f.tick()
    rejected, = audits(f, 'roster.rejected')
    errors = json.loads(rejected['errors'])
    assert len(errors) == 1 and errors[0].startswith('workers[2] (extra): ') and 'rename it extra-1' in errors[0]
    assert f.workers == LEGACY
    write_roster(f, LEGACY + ['extra-1'])
    f.tick()
    assert f.workers == LEGACY + ['extra-1'] and len(audits(f, 'roster.rejected')) == 1


# -- the supervisor grows / shrinks its own worker set (user 2026-10-28: "Supervisor 能够动态地开启 Worker
# 和关闭 Worker、卸载 Worker"): its prompt names the roster file, a rejected edit is reported back to it.
SUPERVISOR_PROMPTS = ('supervisor.md', 'supervisor_v2.md', 'service_supervisor.md', 'service_supervisor_v2.md',
                      'service_supervisor_tui_v2.md')


def supervisor_scope(f):
    return f.role_scope('supervisor', f.runtime_cfg('supervisor'), {})


def test_every_supervisor_prompt_explains_adding_and_retiring_workers():
    agents = fleet.HERE / 'agents'
    for name in SUPERVISOR_PROMPTS:
        text = (agents / name).read_text()
        assert '### Adding and retiring workers (the roster)' in text and '{{ROSTER}}' in text, name
        assert '[Roster change]' in text and '[Roster rejected]' in text, name
        assert '- {name: <role>-<n>, role: <path>}' in text, name
        assert 'editing the roster file is allowed' in text, name


def test_supervisor_prompt_names_the_roster_file_when_the_run_has_one(hot):
    scope = supervisor_scope(hot)
    assert '{{' not in scope and 'Adding and retiring workers (the roster)' in scope
    assert 'This run\'s roster file is `' + hot.cfg['roster'] + '`' in scope
    assert 'not available in this run' not in scope and 'At night' not in scope


def test_supervisor_prompt_names_both_files_of_a_day_night_pair(hot):
    night = hot.run_dir / 'night.yaml'
    hot.cfg['roster_night'] = str(night)
    scope = supervisor_scope(hot)
    assert '`' + hot.cfg['roster'] + '`' in scope and '`' + str(night) + '`' in scope
    assert 'make the same `workers:` change in both files' in scope


def test_supervisor_prompt_without_a_roster_says_adding_is_not_available(mixed):  # noqa: F811
    assert not mixed.cfg.get('roster')
    scope = supervisor_scope(mixed)
    assert '{{' not in scope and 'adding or retiring workers is not available in this run' in scope
    assert 'roster file is `' not in scope


def test_rejected_roster_change_is_told_to_the_supervisor_once(hot, monkeypatch):
    f = hot
    write_roster(f, WORKERS + ['fresh'])
    f.tick()
    assert f.workers == WORKERS and len(audits(f, 'roster.rejected')) == 1
    submit(f)
    prompt = typed(f, 'supervisor')[-1]
    note = prompt.split('[Roster rejected] ', 1)[1]
    assert note.startswith('workers[6] (fresh): a worker name must end with -<number>')
    assert 'the roster in effect is unchanged: ' + ', '.join(WORKERS) in note
    assert '[Roster change]' not in prompt
    assert not f.r.hget(fleet.ROSTER, 'rejected')     # delivered: gone
    f.tick()                                            # the same rejected file: not re-reported
    assert not f.r.hget(fleet.ROSTER, 'rejected') and len(audits(f, 'roster.rejected')) == 1
    sent = []
    monkeypatch.setattr(f.v2, 'queue', lambda name, text, **kw: sent.append(text))
    f.queue('supervisor', 'next message')
    assert sent == ['next message']


def test_rejection_fixed_before_the_next_message_reports_only_the_change(hot):
    f = hot
    write_roster(f, WORKERS + ['fresh'])
    f.tick()
    write_roster(f, WORKERS + ['fresh-1'])
    f.tick()
    assert f.workers == WORKERS + ['fresh-1']
    submit(f)
    prompt = typed(f, 'supervisor')[-1]
    assert '[Roster rejected]' not in prompt
    assert '[Roster change] Workers you can dispatch to now: ' in prompt and prompt.rstrip().endswith('fresh-1')


def _roster_with_max_busy(f, value):
    path = write_roster(f, WORKERS)
    path.write_text(path.read_text() + ('' if value is None else 'max_busy: ' + str(value) + '\n'))
    os.utime(path, ns=(f.test_stamp + 1, f.test_stamp + 1))
    f.test_stamp += 1
    return path


def test_max_busy_hot_reloads_from_the_roster_and_caps_the_next_pump(hot):
    """11-09 user: "最大5个这个也应该可以热载啊" — --max-busy was read once at daemon start."""
    f = hot
    assert f.cfg['max_busy'] == 0  # the start value (--max-busy)
    _roster_with_max_busy(f, 2)
    f.tick()
    assert f.cfg['max_busy'] == 2 and f.workers == WORKERS
    changed, = audits(f, 'roster.changed')
    assert json.loads(changed['handoff'])['max_busy'] == {'before': 0, 'after': 2}
    submit(f)
    answer_turn(f, 'supervisor', decision(WORKERS))
    f.pump()
    assert sum(bool(typed(f, w)) for w in WORKERS) == 2  # the reloaded cap is the one pump uses


def test_removing_max_busy_from_the_roster_restores_the_start_value(hot):
    f = hot
    _roster_with_max_busy(f, 3)
    f.tick()
    assert f.cfg['max_busy'] == 3
    _roster_with_max_busy(f, None)
    f.tick()
    assert f.cfg['max_busy'] == 0
    assert fleet.load(f.path)['max_busy'] == 0  # never written back: config.json keeps the start value


@pytest.mark.parametrize('value', ['-1', 'true', 'five', '1.5'])
def test_invalid_max_busy_rejects_the_whole_roster_and_keeps_the_cap(hot, value):
    f = hot
    _roster_with_max_busy(f, value)
    f.tick()
    rejected, = audits(f, 'roster.rejected')
    assert any('max_busy must be' in e for e in json.loads(rejected['errors']))
    assert f.cfg['max_busy'] == 0 and not audits(f, 'roster.changed')


def test_roster_parse_accepts_max_busy(tmp_path):
    raw = b'workers:\n  - name: worker-1\nmax_busy: 0\n'
    assert roster.parse(raw, tmp_path)[0] == ['worker-1']
    assert roster.max_busy(raw) == 0 and roster.max_busy(b'workers:\n  - name: worker-1\n') is None
