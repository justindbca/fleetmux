"""A slot's launch record (runtime_launch + installed_domains) carries across generations: a handoff
successor keeps model, --effort and domains whether the advisor is on or off; only a new advisor sheet
changes them. The advisor process is always a fake runner."""
import json
import os
from pathlib import Path

import pytest

from test_advisor import Runner, afx, item, report, send_decision, v2_advised, worker_text  # noqa: F401
from test_advisor_hotload import set_roster
from test_fleet import turn
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from cmux_fleet import fleet

SHEET = {'model': 'opus', 'effort': 'high', 'domains': ['research_web']}


def assert_inherited(f, generation):
    """Generation `generation` of worker-1 runs opus/high with research_web, laid out advised."""
    info = f.agent('worker-1')
    assert info['generation'] == str(generation)
    assert json.loads(info['installed_domains']) == ['research_web']
    snap = fleet.load(f.run_dir / 'runtime' / ('worker-1-g' + str(generation) + '.json'))
    argv = fleet.claude_argv(f.path, snap, 'worker-1', generation, 'READY?')
    assert argv[argv.index('--model') + 1] == 'opus' and argv[argv.index('--effort') + 1] == 'high'
    assert argv[argv.index('--setting-sources') + 1] == 'project' and '--strict-mcp-config' in argv
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    assert 'RULES OF research_web' in (cwd / 'CLAUDE.md').read_text()
    assert sorted(p.name for p in (cwd / '.claude' / 'skills').iterdir()) == ['web-fetch-fallback']


def v1_handoff(f, generation):
    """A context-full handoff (the same start_rotation the ratio check calls), draft written by the model."""
    f.start_rotation('worker-1', 90000, 100000)
    info = f.agent('worker-1')
    draft = Path(info['handoff_draft'])
    draft.write_text('## Done (facts)\n- notes\n## In progress\nnone\n## Next steps (in order)\n1. go on\n')
    os.utime(draft, ns=(int(info['handoff_mtime']) + 10**9,) * 2)
    f.handle(dict(turn('worker-1', 'handoff written', generation), msg_id=info['msg_id']))


@pytest.mark.parametrize('online', [False, True])
def test_v1_handoff_keeps_the_sheet_config_on_or_off(afx, online):
    f = afx
    f.advisor_runner = Runner(dict(SHEET))
    send_decision(f, [item()])
    f.pump()  # fingerprint change: Phase 5 respawn to worker-1.1 with the sheet
    f.handle(turn('worker-1', 'READY', 1))
    f.pump()
    report(f, 'worker-1', 1)
    assert_inherited(f, 1)
    if not online:
        assert f.set_advisor(False, 'user', write=True) is None and not f.advisor_online()
    calls = len(f.advisor_runner.calls)
    v1_handoff(f, 1)
    assert f.agent('worker-1')['label'] == 'worker-1.2'
    assert_inherited(f, 2)
    assert len(f.advisor_runner.calls) == calls  # no new sheet: nothing re-decided


def test_v1_never_advised_slot_hands_off_plain(afx):
    """The boundary: a slot whose record is the slot defaults stays plain after the advisor goes off."""
    f = afx
    f.setf('worker-1', installed_domains='')  # e.g. released with the defaults (release_default)
    assert f.set_advisor(False, 'user', write=True) is None
    send_decision(f, [item()])
    f.pump()
    report(f, 'worker-1')
    v1_handoff(f, 0)
    snap_path = f.run_dir / 'runtime' / 'worker-1-g1.json'
    assert not snap_path.exists()  # runs off the main config, like a fleet that never had the advisor
    argv = fleet.claude_argv(f.path, fleet.load(f.path), 'worker-1', 1, 'READY?')
    assert '--setting-sources' not in argv and argv[argv.index('--effort') + 1] == 'high'


@pytest.fixture
def v2adv(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    yield from v2_advised(private_redis_v2, tmp_path, monkeypatch, ['worker-1'])


def v2_sheet_launched(f):
    from test_v2_codex_claude import answer_turn, submit
    f.advisor_runner = Runner(dict(SHEET))
    submit(f)
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'id': 'M0', 'instruction': 'Research the semis rotation into worker-1/notes.md'}]}))
    f.pump()  # the held first generation is released with the sheet
    answer_turn(f, 'worker-1', 'READY', startup=True)
    f.pump()
    answer_turn(f, 'worker-1', 'Done:\n- notes\nEvidence:\n- worker-1/notes.md\nNot done: none\nBlocked: none')
    f.pump()
    assert_inherited(f, 0)


@pytest.mark.parametrize('online', [False, True])
def test_v2_handoff_keeps_the_sheet_config_on_or_off(v2adv, online):
    from test_v2_codex_claude import answer_turn
    f = v2adv
    v2_sheet_launched(f)
    if not online:
        set_roster(f, False)
        f.tick()
        assert not f.advisor_online()
    calls = len(f.advisor_runner.calls)
    f.start_rotation('worker-1', 90000, 100000)
    f.pump()
    assert answer_turn(f, 'worker-1', 'handoff written')['status'] == 'committed'
    f.v2.step(10)
    assert f.agent('worker-1')['label'] == 'worker-1.1'
    assert_inherited(f, 1)
    assert len(f.advisor_runner.calls) == calls


def test_v2_resume_mid_rescue_respawn_starts_the_successor_with_the_sheet(v2adv):
    """start --resume while an advisor respawn was still handing off: rescue_launch is not lost, and the
    relay note / hint do not leak into a later rotation. The resumed daemon starts with the advisor off."""
    from cmux_fleet import profile_launch
    f = v2adv
    v2_sheet_launched(f)
    launch = json.loads(f.agent('worker-1')['runtime_launch'])
    launch['overrides']['worker_model'], launch['extras'] = 'sonnet', {'effort': 'max'}
    f.setf('worker-1', handoff_id='h-cut', rescue_launch=profile_launch.encode_launch(launch), rescue_relay=1,
           rescue_hint='check the links', advice_domains=json.dumps(['research_web', 'us_options']))
    set_roster(f, False)
    f.v2.close()
    f.mgr.dock_control = lambda pane: None  # the control tab re-docks after a resume
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=f.test_sessions)
    assert not again.advisor_online()
    again.resume_requested = True
    again.v2.resume_run()
    info = again.agent('worker-1')
    assert info['generation'] == '1' and not info.get('rescue_launch') and not info.get('rescue_relay')
    assert not info.get('rescue_hint') and json.loads(info['installed_domains']) == ['research_web', 'us_options']
    snap = fleet.load(again.run_dir / 'runtime' / 'worker-1-g1.json')
    argv = fleet.claude_argv(again.path, snap, 'worker-1', 1, 'READY?')
    assert argv[argv.index('--model') + 1] == 'sonnet' and argv[argv.index('--effort') + 1] == 'max'
    assert argv[argv.index('--setting-sources') + 1] == 'project'
    again.v2.close()


# ------------------------------------------------------------------ the record is replayed whatever the switch says
# (integ review P2-1: a never-advised slot that handed off while the advisor was online used to come back
# with --setting-sources project / --strict-mcp-config, zero domains, and installed_domains='[]' kept it so)
from test_advisor_hotload import cfx  # noqa: E402,F401  (fixture: v1 Claude workers, advisor off at start)

RECORD = ('runtime_launch', 'installed_domains', 'advice_domains')


def record(f, name='worker-1'):
    info = f.agent(name)
    return {k: info.get(k, '') for k in RECORD}


def assert_default(f, generation, name='worker-1'):
    """Generation `generation` of `name` is laid out with the slot defaults: user-level settings, no
    --effort, CLAUDE.md = the role scope, full kit copy, and no advised record written."""
    info = f.agent(name)
    assert info['generation'] == str(generation) and info.get('installed_domains', '') == ''
    snap_path = f.run_dir / 'runtime' / (name + '-g' + str(generation) + '.json')
    snap = fleet.load(snap_path) if snap_path.exists() else fleet.load(f.path)
    assert 'advisor' not in snap
    argv = fleet.claude_argv(f.path, snap, name, generation, 'READY?')
    assert '--setting-sources' not in argv and '--strict-mcp-config' not in argv and argv[argv.index('--effort') + 1] == 'high'
    cwd = fleet.agent_cwd(f.cfg, name)
    assert (cwd / 'CLAUDE.md').read_text() == (cwd / 'AGENTS.md').read_text()
    assert 'RULES OF' not in (cwd / 'CLAUDE.md').read_text()


@pytest.mark.parametrize('roster_card', [False, True])
def test_v1_never_advised_slot_hands_off_plain_while_online(cfx, roster_card):
    """roster_card: the slot runs a roster profile (a runtime_launch), so its successor has a snapshot --
    the snapshot used to copy the in-memory switch into the argv."""
    from cmux_fleet import profile_launch
    from cmux_fleet.agent_registry import AgentRegistry
    f = cfx
    if roster_card:
        reg = AgentRegistry()
        f.setf('worker-1', runtime_launch=profile_launch.encode_launch(
            profile_launch.resolve_launch(reg.get('claude-sonnet'), reg, {})))
    before = record(f)
    assert f.set_advisor(True, 'user') is None and f.advisor_online()
    assert record(f) == before
    # roster_card: handoff alone. A dispatch here is blocked by the pre-existing extras drift check (frozen
    # card launch has no extras, the same-fingerprint sheet's advice_launch carries effort) -- not this fix.
    if not roster_card:
        send_decision(f, [item()])  # advised: sheet = the slot's own runtime, no domains -> effect none
        f.pump()
        assert f.r.hgetall(fleet.ADVICE + f.agent('worker-1')['advice_ticket'])['effect'] == 'none'
        report(f, 'worker-1')
    v1_handoff(f, 0)
    assert f.agent('worker-1')['label'] == 'worker-1.1'
    assert_default(f, 1)
    assert record(f) == before


def test_v2_never_advised_slot_hands_off_plain_while_online(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    from test_advisor import v2_advised as make
    from test_v2_codex_claude import answer_turn
    gen = make(private_redis_v2, tmp_path, monkeypatch, ['worker-1'], on=False, worker_effort='medium')
    f = next(gen)
    answer_turn(f, 'worker-1', 'READY', startup=True)
    before = record(f)
    set_roster(f, True)
    f.tick()
    assert f.advisor_online() and record(f) == before
    f.start_rotation('worker-1', 90000, 100000)
    f.pump()
    assert answer_turn(f, 'worker-1', 'handoff written')['status'] == 'committed'
    f.v2.step(10)
    assert f.agent('worker-1')['label'] == 'worker-1.1'
    assert_default(f, 1)
    assert record(f) == before
    with pytest.raises(StopIteration):
        next(gen)


def test_advisor_toggles_never_alter_a_record(cfx):
    """worker-1 gets an advisor sheet (respawn), worker-2 keeps the defaults; on/off/on leaves both records."""
    f = cfx
    assert f.set_advisor(True, 'user') is None
    f.advisor_runner = Runner(dict(SHEET))
    send_decision(f, [item()])
    f.pump()
    f.handle(turn('worker-1', 'READY', 1))
    f.pump()
    report(f, 'worker-1', 1)
    assert_inherited(f, 1)
    saved = {w: record(f, w) for w in ('worker-1', 'worker-2')}
    assert saved['worker-2']['installed_domains'] == ''
    for on in (False, True, False):
        assert f.set_advisor(on, 'user', write=True) is None and f.advisor_online() is on
        assert {w: record(f, w) for w in ('worker-1', 'worker-2')} == saved


def test_v1_hotload_sheet_on_a_default_slot_becomes_its_record(cfx):
    """An applied sheet is the only thing that changes a record: a hot-load onto a default-layout slot
    makes the successor advised (sheet model / effort / domains), even with the advisor off by then."""
    f = cfx
    assert f.set_advisor(True, 'user') is None
    f.advisor_runner = Runner({'model': 'sonnet', 'effort': 'medium', 'domains': ['research_web']})
    send_decision(f, [item()])
    f.pump()
    assert f.r.hgetall(fleet.ADVICE + f.agent('worker-1')['advice_ticket'])['effect'] == 'hotload'
    assert json.loads(f.agent('worker-1')['installed_domains']) == ['research_web']
    report(f, 'worker-1')
    assert f.set_advisor(False, 'user') is None
    v1_handoff(f, 0)
    snap = fleet.load(f.run_dir / 'runtime' / 'worker-1-g1.json')
    argv = fleet.claude_argv(f.path, snap, 'worker-1', 1, 'READY?')
    assert argv[argv.index('--setting-sources') + 1] == 'project' and argv[argv.index('--effort') + 1] == 'medium'
    assert 'RULES OF research_web' in (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text()


def test_v1_rescue_respawn_writes_the_new_sheet_into_a_default_record(cfx):
    from test_advisor_rescue import respawn_sheet, run_respawn, worked
    f = cfx
    assert f.set_advisor(True, 'user') is None
    worked(f)  # pre-dispatch sheet = the slot's own runtime: record stays the defaults
    assert f.agent('worker-1').get('installed_domains', '') == ''
    f.advisor_runner = Runner(respawn_sheet())
    run_respawn(f)
    assert_inherited(f, 1)


def v2_resume(f):
    """start --resume with the page still there but the CLI gone from the pane: every slot is respawned."""
    f.v2.close()
    f.mgr.has_cli_process = lambda pane, clis: False
    f.mgr.dock_control = lambda pane: None  # the control tab re-docks after a resume
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=f.test_sessions)
    again.resume_requested = True
    again.v2.resume_run()
    return again


@pytest.mark.parametrize('legacy', [False, True])
def test_v2_resume_while_online_replays_a_default_record(private_redis_v2, tmp_path, monkeypatch, legacy):  # noqa: F811
    """A slot started with the defaults is resumed with the advisor on: the successor keeps the defaults.
    legacy: a run from before the launch record has no record fields at all -> the slot defaults, and
    those stay its record ('' = the default layout, so nothing new is written)."""
    from test_advisor import v2_advised as make
    from test_v2_codex_claude import answer_turn
    gen = make(private_redis_v2, tmp_path, monkeypatch, ['worker-1'], on=False, worker_effort='medium')
    f = next(gen)
    answer_turn(f, 'worker-1', 'READY', startup=True)
    if legacy:
        f.r.hdel(fleet.key('agent', 'worker-1'), *RECORD)
    set_roster(f, True)
    again = v2_resume(f)
    assert again.advisor_online()
    assert_default(again, 1)
    assert record(again) == {k: '' for k in RECORD}
    again.v2.close()


def test_v2_resume_of_a_never_released_held_slot_launches_with_defaults(v2adv):
    """Held for its first sheet, never released, then resumed: nothing ever launched with a sheet, so the
    record is the defaults (the held spawn wrote none) and the successor is not left marked held."""
    f = v2adv
    info = f.agent('worker-1')
    assert info['advice_hold'] == '1' and info['launch_ready'] == '0' and info.get('installed_domains', '') == ''
    again = v2_resume(f)
    assert again.advisor_online()
    assert again.agent('worker-1').get('advice_hold') == '0'
    assert_default(again, 1)
    again.v2.close()
