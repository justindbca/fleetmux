"""Night switch (docs/design.md §25): roster `profile` column, idle per-slot swap, night on|off, brief.

Real private Redis v2 + real Claude Stop hook, fake cmux and CLIs (the hot-roster harness). The
codex+pi night pair is not admitted by today's CLI matrix; its case uses a stub in the shape of
the combination table (cli_pairs.yaml rows) that feature A will provide.
"""
import json
import os
from pathlib import Path

import pytest
import yaml

from test_roster import REPORT, audits, hot, write_roster  # noqa: F401  (fixture)
from test_v2_codex_claude import WORKERS, answer_turn, decision, mixed, no_provisioning, submit, typed  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from fleetmux import fleet, night, roster, runtime_v2

DAY_CARD, NIGHT_CARD = None, 'claude-coder'  # same CLI (official Claude), other model: today's matrix admits it


def write_profiles(f, name, profiles):
    """A roster file with one row per worker and an optional profile column."""
    path = f.run_dir / name
    rows = ['workers:'] + ['  - {name: ' + w + (', profile: ' + profiles[w] if profiles.get(w) else '') + '}'
                           for w in WORKERS]
    path.write_text('\n'.join(rows) + '\n')
    stamp = getattr(f, 'test_stamp', 1_700_000_000_000_000_000) + 1_000_000_000
    f.test_stamp = stamp
    os.utime(path, ns=(stamp, stamp))
    return path


@pytest.fixture
def nightly(hot, tmp_path, monkeypatch):  # noqa: F811
    f = hot
    # Fake CLIs: the night card's preflight looks the official `claude` binary up on PATH; give it a stub
    # instead of depending on the host's install (the suite runs with a fixed PATH without it).
    stub = tmp_path / 'cli-bin'
    stub.mkdir()
    (stub / 'claude').write_text('#!/bin/sh\nexit 0\n')
    (stub / 'claude').chmod(0o755)
    monkeypatch.setenv('PATH', str(stub) + os.pathsep + os.environ.get('PATH', ''))
    night_path = write_profiles(f, 'night.yaml', {w: NIGHT_CARD for w in WORKERS})
    f.cfg['roster_night'] = str(night_path)
    fleet.save(f.path, dict(fleet.load(f.path), roster_night=str(night_path)))
    return f


def ready_all(f, slots):
    for w in slots:
        f.test_threads.setdefault(w, f.test_threads[w])
        assert answer_turn(f, w, 'READY', startup=True)['status'] == 'ready'


def real_gate(f, monkeypatch):
    """BC review r1 P1-01: the night swap runs the REAL preflight_profile (its CLI-change refusal included);
    only the host-side pi checks (binary on PATH, --version, isolated home) are faked. Returns the kwargs seen."""
    real_run, calls = fleet.subprocess.run, []
    monkeypatch.setattr(fleet.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(fleet.subprocess, 'run', lambda argv, *a, **k: (
        fleet.subprocess.CompletedProcess(argv, 0, '1.1.0\n', '') if argv[-1:] == ['--version'] else real_run(argv, *a, **k)))
    (f.run_dir / 'pi-home').mkdir(exist_ok=True)
    f.cfg['pi_home'], f.cfg['allow_unsandboxed_pi'] = str(f.run_dir / 'pi-home'), True
    gate = fleet.Fleet.preflight_profile.__get__(f)
    monkeypatch.setattr(f, 'preflight_profile', lambda launch, **kw: (calls.append(kw), gate(launch, **kw))[1])
    return calls


# ------------------------------------------------------------------ roster file
def test_roster_profile_column_parses_and_is_optional(tmp_path):
    path = tmp_path / 'r.yaml'
    path.write_text('workers:\n  - {name: exit, profile: flashnext-coder}\n  - name: reentry\n')
    assert roster.load_entries(path) == (['exit', 'reentry'], {}, {'exit': 'flashnext-coder'})
    assert roster.load(path) == (['exit', 'reentry'], {})  # old two-value API unchanged


def test_night_rosters_must_name_the_same_slots(tmp_path):
    cfg = dict(fleet.DEFAULTS, transport_version=2, supervisor_cli='codex', worker_cli='claude',
               allow_development_v2=True, mode='task')
    day, other = tmp_path / 'day.yaml', tmp_path / 'night.yaml'
    day.write_text('workers:\n  - name: worker-1\n  - name: worker-2\n')
    other.write_text('workers:\n  - {name: worker-1, profile: claude-coder}\n  - {name: night-2, profile: claude-coder}\n')
    with pytest.raises(ValueError, match='same worker names'):
        night.check_rosters(cfg, day, other)
    other.write_text('workers:\n  - {name: worker-1, profile: claude-coder}\n  - {name: worker-2, profile: claude-coder}\n')
    assert night.check_rosters(cfg, day, other)['night'][2] == {'worker-1': 'claude-coder', 'worker-2': 'claude-coder'}


def test_todays_matrix_refuses_a_flashnext_night_under_codex_claude(tmp_path, pairs, monkeypatch):
    """No loosening: codex supervisor + pi worker stays refused until the combination table admits it.
    (Integration with A: the packaged table now lists codex+pi/task as verified after a real smoke, so
    the refusal is checked on a table where the row is still unverified, and admission on A's own table.)"""
    cfg = dict(fleet.DEFAULTS, transport_version=2, supervisor_cli='codex', worker_cli='claude',
               allow_development_v2=True, mode='task')
    day, other = tmp_path / 'day.yaml', tmp_path / 'night.yaml'
    day.write_text('workers:\n  - name: worker-1\n')
    other.write_text('workers:\n  - {name: worker-1, profile: flashnext-coder}\n')
    pairs(('codex', 'claude', 'task', 'verified'),
          ('codex', 'pi', 'task', 'unverified', (), {'reason': 'transport v2 mixed Pi/Codex fleets are unvalidated'}))
    with pytest.raises(ValueError, match='mixed Pi/'):
        night.check_rosters(cfg, day, other)
    from fleetmux import cli_pairs
    monkeypatch.setattr(cli_pairs, 'DEFAULT_PATH', None)
    night.check_rosters(cfg, day, other)  # A's packaged table (codex+pi/task verified) admits the night pair


# ------------------------------------------------------------------ start / CLI
def test_start_checks_night_roster_before_provisioning(monkeypatch, tmp_path, pairs):
    no_provisioning(monkeypatch)
    # D: start needs --lifecycle. A: codex+pi/task is verified in the packaged table, so the refusal
    # below is checked on a table where that row is still unverified (same message as before A).
    pairs(('codex', 'claude', 'task', 'verified'),
          ('codex', 'pi', 'task', 'unverified', (), {'reason': 'transport v2 mixed Pi/Codex fleets are unvalidated'}))
    base = ['start', '--lifecycle', 'dev_task', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--allow-development-v2']
    day = tmp_path / 'day.yaml'
    day.write_text('workers:\n  - name: exit-1\n')
    bad = tmp_path / 'bad.yaml'
    bad.write_text('workers:\n  - {name: other, profile: claude-coder}\n')
    with pytest.raises(SystemExit, match='needs --roster'):
        fleet.main(base + ['--workers', '1', '--night-roster', str(bad)])
    with pytest.raises(SystemExit, match='same worker names'):
        fleet.main(base + ['--roster', str(day), '--night-roster', str(bad)])
    pi = tmp_path / 'pi.yaml'
    pi.write_text('workers:\n  - {name: exit-1, profile: flashnext-coder}\n')
    with pytest.raises(SystemExit, match='mixed Pi/'):
        fleet.main(base + ['--roster', str(day), '--night-roster', str(pi)])


def test_night_cli_writes_the_trigger_file_only_after_validation(nightly):
    f = nightly
    cfg = fleet.load(f.path)
    assert fleet.night_cmd(cfg, 'status') == {'on': False, 'roster': cfg['roster']}
    out = fleet.night_cmd(cfg, 'on')
    assert out['on'] is True and out['brake_card'] == 'night' and out['roster'] == cfg['roster_night']
    assert json.loads((f.run_dir / 'night.json').read_text())['on'] is True
    assert not list(f.run_dir.glob('night.json.*.tmp'))
    Path_night = f.run_dir / 'night.yaml'
    Path_night.write_text('workers:\n  - {name: worker-1, profile: claude-coder}\n')  # names differ now
    with pytest.raises(SystemExit, match='refused, nothing changed'):
        fleet.night_cmd(cfg, 'off')
    assert json.loads((f.run_dir / 'night.json').read_text())['on'] is True
    with pytest.raises(SystemExit, match='no night roster'):
        fleet.night_cmd(dict(cfg, roster_night=None), 'on')


# ------------------------------------------------------------------ daemon
def test_night_on_swaps_idle_slots_waits_for_busy_and_briefs_once(nightly):
    f = nightly
    submit(f)
    assert answer_turn(f, 'supervisor', decision(['worker-1']))['status'] == 'committed'
    f.pump()
    busy_pane = f.agent('worker-1')['pane']
    old = {w: f.agent(w)['pane'] for w in WORKERS}
    spawned = len(f.mgr.spawned)
    fleet.night_cmd(fleet.load(f.path), 'on')
    f.tick()
    on, = audits(f, 'night.on')
    assert on['brake_card'] == 'night' and on['supervisor'] == 'unchanged'
    assert f.r.hgetall(night.NIGHT)['on'] == '1' and f.r.hget(night.NIGHT, 'brake_card') == 'night'
    # The busy worker finishes its turn first; every idle slot is now its next generation.
    assert f.agent('worker-1')['label'] == 'worker-1' and busy_pane in f.mgr.alive
    for w in WORKERS[1:]:
        info = f.agent(w)
        assert info['label'] == w + '.1' and info['roster_profile'] == NIGHT_CARD
        assert json.loads(info['runtime_launch'])['profile_id'] == NIGHT_CARD
        assert json.loads(info['runtime_launch'])['overrides'] == {'worker_cli': 'claude', 'worker_model': 'opus'}
        assert old[w] in f.mgr.killed and info['launch_ready'] == '1' and info['state'] == 'starting'
        assert f.r.hget(fleet.NAMES, w + '.1') == w
    assert len(f.mgr.spawned) == spawned + 5
    assert f.mgr.spawned[-1]['metadata']['profile'] == 'opus'           # the card's model, not the run default
    note = audits(f, 'roster.profile_switched')[0]['handoff']
    assert 'build M0 parts' in open(note).read()                  # the machine handoff skeleton
    assert json.loads(audits(f, 'roster.changed')[-1]['results'])['worker-2'].startswith('profile claude-coder')
    assert not [t for t in typed(f, 'supervisor') if '[Night switch' in t]
    # worker-1 reports, then it is swapped too.
    out = f.workspace() / 'worker-1' / 'part.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('findings')
    assert answer_turn(f, 'worker-1', REPORT.format(w='worker-1'))['status'] == 'committed'
    f.tick()
    assert f.agent('worker-1')['label'] == 'worker-1.1' and busy_pane in f.mgr.killed
    f.tick()
    assert not [t for t in typed(f, 'supervisor') if '[Night switch' in t]  # not before every successor is READY
    ready_all(f, WORKERS)
    f.tick()
    # Queued for the supervisor right behind worker-1's report.
    queued = [f.v2.bus.message(m)['body']['text'] for m in f.v2.bus.pending('supervisor')]
    assert queued[0].startswith('[worker-1 report]')
    text, = [t for t in queued if t.startswith('[Night switch ' + f.task()['id'] + ']')]
    assert '- worker-1.1: claude-coder (claude)' in text and 'worker-1: [M0] Write worker-1/part.md' in text  # §44 auto id
    assert 'Dispatch only work that is still missing' in text
    f.tick()
    assert len(audits(f, 'night.brief')) == 1


def test_pending_swap_holds_delivery_until_the_new_generation_is_ready(nightly):
    f = nightly
    submit(f)
    assert answer_turn(f, 'supervisor', decision(['worker-3']))['status'] == 'committed'
    # The roster changed but the tick has not run yet: the old generation gets nothing new.
    f.cfg['worker_profiles'] = {'worker-3': NIGHT_CARD}
    sent = len(typed(f, 'worker-3'))
    f.pump()
    assert len(typed(f, 'worker-3')) == sent and f.agent('worker-3')['state'] == 'idle'
    f.profile_swap_tick()
    assert f.agent('worker-3')['label'] == 'worker-3.1'
    assert f.inbox_depth('worker-3') == 1                                # queue kept on the slot
    f.pump()
    assert not typed(f, 'worker-3') or 'Write worker-3' not in typed(f, 'worker-3')[-1]
    ready_all(f, ['worker-3'])
    f.pump()
    assert 'Write worker-3/part.md' in typed(f, 'worker-3')[-1]


def test_added_worker_starts_on_its_roster_profile_without_a_swap(hot):
    f = hot
    path = f.run_dir / 'roster.yaml'
    path.write_text('workers:\n' + ''.join('  - name: ' + w + '\n' for w in WORKERS)
                    + '  - {name: extra-1, profile: claude-coder}\n')
    os.utime(path, ns=(f.test_stamp + 5, f.test_stamp + 5))
    f.tick()
    info = f.agent('extra-1')
    assert info['label'] == 'extra-1' and info['roster_profile'] == 'claude-coder' and info['generation'] == '0'
    assert f.mgr.spawned[-1]['metadata']['profile'] == 'opus'
    assert fleet.load(f.path)['worker_profiles'] == {'extra-1': 'claude-coder'}
    f.tick()
    assert not audits(f, 'roster.profile_switched')


def test_night_off_swaps_back_to_the_run_default(nightly):
    f = nightly
    fleet.night_cmd(fleet.load(f.path), 'on')
    f.tick()
    ready_all(f, WORKERS)
    fleet.night_cmd(fleet.load(f.path), 'off')
    f.tick()
    assert audits(f, 'night.off') and f.r.hget(night.NIGHT, 'brake_card') == 'day'
    for w in WORKERS:
        info = f.agent(w)
        assert info['label'] == w + '.2' and info['runtime_launch'] == '' and info['roster_profile'] == ''
    assert f.mgr.spawned[-1]['metadata']['profile'] == f.cfg['worker_model']
    ready_all(f, WORKERS)
    f.tick()
    assert audits(f, 'night.brief_skipped')                              # no running task: nothing to brief


def test_live_profile_task_defers_the_roster_swap_and_keeps_delivering(nightly):
    f = nightly
    submit(f)
    f.r.hset(fleet.TASK, mapping={'apply_profiles_live': 'true'})
    f.cfg['allow_profile_live_switch'] = True
    fleet.night_cmd(fleet.load(f.path), 'on')
    f.tick()
    assert all(f.agent(w)['label'] == w for w in WORKERS)
    assert len(audits(f, 'roster.profile_deferred')) == len(WORKERS)
    f.tick()
    assert len(audits(f, 'roster.profile_deferred')) == len(WORKERS)    # logged once per slot
    # The task goes on with the day runtime: a deferred slot still takes its delivery.
    monkey = fleet.profile_runtime.prepare_profile
    fleet.profile_runtime.prepare_profile = lambda *a, **k: fleet.profile_runtime.ProfileApplication()
    try:
        assert answer_turn(f, 'supervisor', decision(['worker-1']))['status'] == 'committed'
        f.pump()
    finally:
        fleet.profile_runtime.prepare_profile = monkey
    assert 'Write worker-1/part.md' in typed(f, 'worker-1')[-1] and f.agent('worker-1')['label'] == 'worker-1'
    # Night off while deferred: the flag clears, so a later deferral is audited again.
    fleet.night_cmd(fleet.load(f.path), 'off')
    f.tick()
    assert all(f.agent(w).get('roster_swap_deferred') == '0' for w in WORKERS)


def test_failed_preflight_blocks_and_keeps_the_old_runtime(nightly, monkeypatch):
    f = nightly
    submit(f)
    monkeypatch.setattr(f, 'preflight_profile', lambda launch, **kw: (_ for _ in ()).throw(ValueError('claude missing')))
    pane = f.agent('worker-1')['pane']
    fleet.night_cmd(fleet.load(f.path), 'on')
    f.tick()
    assert f.task()['status'] == 'blocked' and 'claude missing' in f.task()['reason']
    assert pane in f.mgr.alive and f.agent('worker-1')['label'] == 'worker-1'


# ------------------------------------------------------------------ CLI pair check (feature A's table)
@pytest.fixture
def pairs(monkeypatch, tmp_path):
    """A's fleetmux.cli_pairs if merged, else the v1.2 stub; returns write(rows) -> table path.

    Under the stub runtime_v2.v2_cli_problem is pointed at the stub too (A v1.2: its body is
    `return cli_pairs.pair_problem(cfg)`), so validate_config judges the same table."""
    import sys
    import fleetmux
    module = night.pair_module()
    if module is None:
        import cli_pairs_stub as module
        monkeypatch.setitem(sys.modules, 'fleetmux.cli_pairs', module)
        monkeypatch.setattr(fleetmux, 'cli_pairs', module, raising=False)
        monkeypatch.setattr(runtime_v2, 'v2_cli_problem', lambda cfg: module.pair_problem(cfg))
    path = tmp_path / 'cli_pairs.yaml'

    def write(*rows):
        pairs_ = [dict(supervisor_cli=sup, worker_cli=wrk, mode=mode, status=status, adapter_missing=list(missing),
                       verified_by=None, **extra) for sup, wrk, mode, status, missing, extra in
                  ((r + ((), {}))[:6] if len(r) == 4 else (r + ({},))[:6] if len(r) == 5 else r for r in rows)]
        path.write_text(yaml.safe_dump({'version': 1, 'default': {}, 'pairs': pairs_}))
        return path
    monkeypatch.setattr(module, 'DEFAULT_PATH', path)
    write.module = module
    return write


def v2cfg(**extra):
    return dict(fleet.DEFAULTS, **dict(dict(transport_version=2, supervisor_cli='codex', worker_cli='claude',
                                            allow_development_v2=True, mode='task'), **extra))


def refusal(cfg, card='flashnext-coder'):
    with pytest.raises(ValueError) as exc:
        night.resolve_launches(cfg, {'worker-1': card})
    return str(exc.value)


def test_pair_check_forbidden_and_unlisted_are_refused_even_under_smoke(pairs):
    pairs(('codex', 'claude', 'task', 'verified'),
          ('codex', 'pi', 'task', 'forbidden', (), {'reason': 'night pair forbidden by operator'}))
    for cfg in (v2cfg(), v2cfg(lifecycle='smoke')):
        err = refusal(cfg)
        assert 'worker-1: profile flashnext-coder: codex+pi/task [forbidden]: night pair forbidden' in err
    pairs(('codex', 'claude', 'task', 'verified'))  # codex+pi not listed: treated as forbidden
    err = refusal(v2cfg(lifecycle='smoke'))
    assert 'codex+pi/task [unlisted]' in err and 'mixed Pi/Codex' in err


def test_pair_check_adapter_missing_is_refused_even_under_smoke(pairs):
    pairs(('codex', 'claude', 'task', 'verified'),
          ('codex', 'pi', 'task', 'unverified', ('pi_bridge_x', 'night_brief'), {'reason': 'needs adapters'}))
    err = refusal(v2cfg(lifecycle='smoke'))
    assert 'codex+pi/task [adapter_missing]: needs adapters [adapter missing: pi_bridge_x, night_brief]' in err


def test_pair_check_unverified_needs_smoke(pairs):
    pairs(('codex', 'claude', 'task', 'verified'),
          ('codex', 'pi', 'task', 'unverified', (), {'reason': 'night pair unverified'}))
    err = refusal(v2cfg())
    assert '[unverified]: night pair unverified; an unverified pair may run only with --lifecycle smoke' in err
    assert refusal(v2cfg(lifecycle='night')) == err  # only the smoke lifecycle admits it
    launch = night.resolve_launches(v2cfg(lifecycle='smoke'), {'worker-1': 'flashnext-coder'})['worker-1']
    assert launch['overrides']['worker_cli'] == 'pi'
    pairs(('codex', 'claude', 'task', 'verified'), ('codex', 'pi', 'task', 'verified'))
    assert night.resolve_launches(v2cfg(), {'worker-1': 'flashnext-coder'})['worker-1']['overrides']['worker_cli'] == 'pi'


def test_pair_check_adopt_exception_is_claude_claude_task_or_service(pairs):
    """Sub-fleet (design §19, service: §29): an adopted Claude supervisor passes before the row is consulted."""
    pairs(('claude', 'claude', 'task', 'unverified', ('claude_supervisor',)))
    sub = v2cfg(supervisor_cli='claude', adopt={'hook': True})  # §48: an adopted Claude pane is on the hook channel
    assert night.resolve_launches(sub, {'worker-1': 'claude-coder'})['worker-1']['overrides']['worker_cli'] == 'claude'
    assert '[adapter_missing]' in refusal(dict(sub, adopt=False), 'claude-coder')
    service = night.resolve_launches(dict(sub, mode='service'), {'worker-1': 'claude-coder'})  # §29: allowed
    assert service['worker-1']['overrides']['worker_cli'] == 'claude'
    assert '[unlisted]' in refusal(dict(sub, mode='service', adopt=False), 'claude-coder')  # without adopt: no
    assert 'claude+pi/task [unlisted]' in refusal(sub)                          # other worker CLI: no exception


def test_pair_check_judges_every_slot_on_one_table_and_reports_a_broken_table(pairs, monkeypatch):
    path = pairs(('codex', 'claude', 'task', 'verified'), ('codex', 'pi', 'task', 'verified'))
    tables = []
    real = pairs.module.pair_problem
    monkeypatch.setattr(pairs.module, 'pair_problem', lambda cfg, table=None: tables.append(table) or real(cfg, table))
    night.resolve_launches(v2cfg(), {'worker-1': 'flashnext-coder', 'worker-2': 'flashnext-coder'})
    snapshot = [t for t in tables if t is not None]  # validate_config's own re-read passes None
    assert len(snapshot) == 2 and snapshot[0] is snapshot[1]
    path.write_text('version: 2\npairs: []\n')
    with pytest.raises(ValueError, match='cli pair table: .*version: 1'):
        night.resolve_launches(v2cfg(), {'worker-1': 'flashnext-coder'})


def test_flashnext_night_is_rejected_whole_until_the_pair_is_admitted(hot, pairs):
    f = hot
    pairs(('codex', 'claude', 'task', 'verified'),
          ('codex', 'pi', 'task', 'unverified', (), {'reason': 'mixed Pi/Codex fleets are unvalidated'}))
    path = write_profiles(f, 'roster.yaml', {'worker-2': 'flashnext-coder'})
    f.cfg['roster'] = str(path)
    f.tick()
    rejected, = audits(f, 'roster.rejected')
    assert any('worker-2: profile flashnext-coder: codex+pi/task [unverified]' in e and 'lifecycle smoke' in e
               for e in json.loads(rejected['errors']))
    assert f.agent('worker-2')['label'] == 'worker-2' and not f.cfg.get('worker_profiles')


def test_flashnext_night_swaps_to_pi_when_the_table_admits_codex_pi(hot, monkeypatch, pairs):
    f = hot
    pairs(('codex', 'claude', 'task', 'verified'), ('codex', 'pi', 'task', 'verified'))
    calls = real_gate(f, monkeypatch)
    launch = night.resolve_launches(f.cfg, {'worker-2': 'flashnext-coder'})['worker-2']
    with pytest.raises(ValueError, match='cannot change the worker CLI'):  # other paths: still refused
        f.preflight_profile(launch)
    path = write_profiles(f, 'roster.yaml', {'worker-2': 'flashnext-coder'})
    f.cfg['roster'] = str(path)
    f.tick()
    assert calls[-1] == {'cli_change_ok': True}  # the night swap went through the real gate, flag on
    assert not audits(f, 'profile.blocked') and f.task().get('status') != 'blocked'
    switched, = audits(f, 'roster.profile_switched')
    assert switched['agent'] == 'worker-2' and switched['cli'] == 'pi'
    info = f.agent('worker-2')
    assert info['label'] == 'worker-2.1' and info['roster_profile'] == 'flashnext-coder'
    assert f.mgr.spawned[-1]['metadata']['cli_type'] == 'pi'
    assert f.mgr.spawned[-1]['metadata']['profile'].startswith('flashnext/')
    assert not runtime_v2.tui_slot(f.v2.slot_cfg('worker-2'), 'worker-2')   # bridge slot, not typed into
    assert runtime_v2.tui_slot(f.v2.slot_cfg('worker-1'), 'worker-1')       # the day slots stay Claude TUIs
    switched, = audits(f, 'roster.profile_switched')
    assert switched['cli'] == 'pi'
    # The table is re-read: demoting the row to adapter_missing refuses the next roster even under smoke.
    pairs(('codex', 'claude', 'task', 'verified'), ('codex', 'pi', 'task', 'unverified', ('night_brief',)))
    f.cfg['lifecycle'] = 'smoke'
    write_profiles(f, 'roster.yaml', {'worker-3': 'flashnext-coder', 'worker-2': 'flashnext-coder'})
    f.tick()
    assert '[adapter_missing]' in audits(f, 'roster.rejected')[-1]['errors']


# ------------------------------------------------------------------ card limits (feature C's model cards)
FLASH = 'flashnext-coder'


def with_cards():
    return pytest.importorskip('fleetmux.model_usage', reason="feature C (model cards) is not merged")


def flash_ok(pairs):
    pairs(('codex', 'claude', 'task', 'verified'), ('codex', 'pi', 'task', 'verified'))


def test_without_model_cards_max_concurrent_is_not_enforced(pairs):
    """B alone (C not merged): three FlashNext slots pass; the cap only exists once C is in."""
    if night.optional_module('model_usage') is not None:
        pytest.skip('feature C is merged: the cap is enforced (see the tests below)')
    flash_ok(pairs)
    slots = ['worker-1', 'worker-2', 'worker-3']
    assert set(night.resolve_launches(v2cfg(), {s: FLASH for s in slots}, slots)) == set(slots)


def test_card_limits_reject_a_whole_roster_over_max_concurrent_or_off_host(pairs, tmp_path):
    with_cards()
    flash_ok(pairs)
    day, other = tmp_path / 'day.yaml', tmp_path / 'night.yaml'
    day.write_text('workers:\n  - name: worker-1\n  - name: worker-2\n  - name: worker-3\n')
    other.write_text('workers:\n' + ''.join('  - {name: worker-%d, profile: %s}\n' % (i, FLASH) for i in (1, 2, 3)))
    with pytest.raises(ValueError, match=r'model flashnext: 3 slots \(worker-1, worker-2, worker-3\) exceed max_concurrent 2'):
        night.check_rosters(v2cfg(), day, other)
    other.write_text('workers:\n  - {name: worker-1, profile: %s}\n  - name: worker-2\n  - name: worker-3\n' % FLASH)
    assert night.check_rosters(v2cfg(), day, other)['night'][2] == {'worker-1': FLASH}
    with pytest.raises(ValueError, match='profile flashnext-coder runs only on'):
        night.check_rosters(v2cfg(host='macmini'), day, other)


def test_daemon_rejects_an_over_cap_night_roster_whole(hot, pairs):
    with_cards()
    f = hot
    flash_ok(pairs)
    f.cfg['roster'] = str(write_profiles(f, 'roster.yaml', {w: FLASH for w in WORKERS[:3]}))
    f.tick()
    rejected, = audits(f, 'roster.rejected')
    assert any('exceed max_concurrent 2' in e for e in json.loads(rejected['errors']))
    assert not f.cfg.get('worker_profiles') and not audits(f, 'roster.profile_switched')


def hold_flash(f, name, roster_profile=FLASH, state=None):
    """``name``'s current generation runs FlashNext (as a live profile task or an earlier roster would leave it)."""
    from fleetmux import profile_launch
    launch = night.resolve_launches(f.cfg, {name: FLASH})[name]
    f.setf(name, runtime_launch=profile_launch.encode_launch(launch), roster_profile=roster_profile,
           **({'state': state} if state else {}))


def test_swap_waits_for_a_free_model_slot_holds_its_queue_and_retries(hot, pairs, monkeypatch):
    with_cards()
    f = hot
    flash_ok(pairs)
    real_gate(f, monkeypatch)
    hold_flash(f, 'worker-1', state='busy')   # still on FlashNext, mid-turn: cannot swap back yet
    f.cfg['roster'] = str(write_profiles(f, 'roster.yaml', {'worker-2': FLASH, 'worker-3': FLASH}))
    f.tick()
    assert f.agent('worker-2')['label'] == 'worker-2.1'                # 2nd FlashNext slot: admitted
    assert f.agent('worker-3')['label'] == 'worker-3'                  # 3rd: waits, no spawn
    waits = audits(f, 'roster.capacity_wait')
    assert len(waits) == 1 and waits[0]['agent'] == 'worker-3'
    assert 'max_concurrent 2 for model flashnext reached (running on worker-1, worker-2)' in waits[0]['reason']
    assert 'worker-3' in f.profile_swaps() and f.agent('worker-3').get('roster_swap_deferred') != '1'  # queue held
    assert not audits(f, 'profile.blocked') and f.task().get('status') != 'blocked'
    f.tick()
    assert len(audits(f, 'roster.capacity_wait')) == 1                 # audited once per reason
    f.setf('worker-1', state='idle')
    f.tick()                                                           # worker-1 back to the run default frees a slot
    assert f.agent('worker-1')['label'] == 'worker-1.1' and f.agent('worker-3')['label'] == 'worker-3.1'
    assert not f.agent('worker-3').get('roster_capacity_wait')
    assert [a['agent'] for a in audits(f, 'roster.profile_switched')] == ['worker-2', 'worker-1', 'worker-3']


def test_same_slot_reuse_needs_no_extra_model_slot(hot, pairs, monkeypatch):
    """worker-1 already runs FlashNext; making that its roster card does not count it twice."""
    with_cards()
    f = hot
    flash_ok(pairs)
    real_gate(f, monkeypatch)
    hold_flash(f, 'worker-1', roster_profile='')   # e.g. left by a live profile task
    hold_flash(f, 'worker-2')                      # cap 2 is now full
    f.cfg['roster'] = str(write_profiles(f, 'roster.yaml', {'worker-1': FLASH, 'worker-2': FLASH}))
    f.tick()
    assert not audits(f, 'roster.rejected') and not audits(f, 'roster.capacity_wait')
    assert f.agent('worker-1')['label'] == 'worker-1.1'
    assert f.model_usage().counts().get('flashnext') == 2


def test_host_mismatch_at_swap_time_blocks_instead_of_swapping(hot, pairs, monkeypatch):
    with_cards()
    f = hot
    flash_ok(pairs)
    real_gate(f, monkeypatch)
    f.setf('worker-2', state='busy')   # roster accepted while the slot is mid-turn
    f.cfg['roster'] = str(write_profiles(f, 'roster.yaml', {'worker-2': FLASH}))
    f.tick()
    assert f.cfg['worker_profiles'] == {'worker-2': FLASH} and not audits(f, 'roster.rejected')
    f.cfg['host'] = 'macmini'          # then the fleet's host no longer matches the card
    f.setf('worker-2', state='idle')
    f.tick()
    blocked, = audits(f, 'profile.blocked')
    assert blocked['agent'] == 'worker-2' and 'profile flashnext-coder runs only on' in blocked['reason']
    assert f.agent('worker-2')['label'] == 'worker-2' and not audits(f, 'roster.profile_switched')


def test_night_swapped_slot_takes_its_card_handoff_ratio_through_the_one_resolver(hot, monkeypatch, pairs):
    """Integration B x C x §22: a night swap only changes the slot's card; the handoff threshold then
    comes from the single §22 resolver (card layer 3b), not from a second night-specific rule."""
    f = hot
    pairs(('codex', 'claude', 'task', 'verified'), ('codex', 'pi', 'task', 'verified'))
    real_gate(f, monkeypatch)
    f.cfg['roster'] = str(write_profiles(f, 'roster.yaml', {'worker-2': 'flashnext-coder'}))
    f.tick()
    assert f.agent('worker-2')['runtime_profile'] == 'flashnext-coder'
    window, ratio, source = f.handoff_limits('worker-2')
    assert ratio == .5 and source.endswith('ratio: card flashnext-coder')  # FlashNext card: 0.5
    assert f.handoff_limits('worker-1')[1] == float(f.cfg['handoff_ratio'])  # day slot: fleet default


def test_night_roster_must_declare_the_same_subfleets(tmp_path):
    """design §29 d) (user 10-14): the night roster's ``subfleets:`` section equals the day roster's
    (compared with paths resolved), else the pair is refused as a whole with the differing names."""
    cfg = dict(fleet.DEFAULTS, transport_version=2, supervisor_cli='codex', worker_cli='claude',
               allow_development_v2=True, mode='task')
    (tmp_path / 'sup.md').write_text('x')
    (tmp_path / 'child.yaml').write_text('workers:\n  - name: a\n')
    (tmp_path / 'n').mkdir()
    sub = ('subfleets:\n  - {name: ops, supervisor_slot: worker-1, supervisor_role: SUP, roster: CHILD'
           + ', autostart: AUTO}\n')
    day, other = tmp_path / 'day.yaml', tmp_path / 'n' / 'night.yaml'
    day.write_text('workers:\n  - name: worker-1\n'
                   + sub.replace('SUP', 'sup.md').replace('CHILD', 'child.yaml').replace('AUTO', 'false'))
    # same targets through other relative paths: accepted
    other.write_text('workers:\n  - {name: worker-1, profile: claude-coder}\n'
                     + sub.replace('SUP', '../sup.md').replace('CHILD', '../child.yaml').replace('AUTO', 'false'))
    night.check_rosters(cfg, day, other)
    other.write_text('workers:\n  - {name: worker-1, profile: claude-coder}\n'
                     + sub.replace('SUP', '../sup.md').replace('CHILD', '../child.yaml').replace('AUTO', 'true'))
    with pytest.raises(ValueError, match=r'same "subfleets:".*differing: ops'):
        night.check_rosters(cfg, day, other)
    other.write_text('workers:\n  - {name: worker-1, profile: claude-coder}\n')
    with pytest.raises(ValueError, match=r'same "subfleets:".*differing: ops'):
        night.check_rosters(cfg, day, other)
