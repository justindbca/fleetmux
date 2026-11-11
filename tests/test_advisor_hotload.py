"""Advisor hot on / off (advisor plan v0.7 §2.1 / §3.1 items 5-6): the switch is the roster's
`advisor: on|off` (default off), applied by roster_tick without a restart; the supervisor's decision
key and `fleet advisor on|off` only write the roster. The advisor process is always a fake runner."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from test_fleet import FakeMgr, FakeRedis, decision, turn
from test_kit import make_kit
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_advisor import afx  # noqa: F401  (fixture)
from test_advisor import Runner, item, report, send_decision, supervisor_texts, v2_advised, worker_text
from fleetmux import control_view, fleet, roster


def logs(f, kind):
    if f.v2:
        return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == kind]
    return [x for x in map(json.loads, f.r.l.get(fleet.LOG, [])) if x['kind'] == kind]


def set_roster(f, on):
    """The user edits the roster; bump mtime so the next tick sees it whatever the clock resolution."""
    path = roster.write_advisor(fleet.roster_file(f.cfg), on, f.workers)
    stamp = getattr(f, 'test_stamp', 1_700_000_000_000_000_000) + 1_000_000_000
    f.test_stamp = stamp
    os.utime(path, ns=(stamp, stamp))
    return path


@pytest.fixture
def cfx(tmp_path, monkeypatch):
    """Two Claude workers with a domain kit, started with the advisor off (no roster at all)."""
    monkeypatch.setattr(fleet, 'CLAUDE_JSON', tmp_path / 'claude.json')
    kit = make_kit(tmp_path / 'kit')
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = {'run': str(run), 'redis_socket': '/tmp/cmux-fleet-test.sock', 'python': sys.executable,
           'workers': ['worker-1', 'worker-2'], 'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 95000,
           'codex_home': str(tmp_path / 'codex-home'), 'worker_cli': 'claude', 'worker_kit': str(kit),
           'claude': '/bin/claude', 'claude_window': 200000, 'token': 'abcd1234',
           'worker_effort': 'medium'}  # the slot runtime = a medium sheet's fingerprint (no respawn on first advice)
    (run / 'config.json').write_text(json.dumps(cfg))
    f = fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr(), sessions=tmp_path / 'sessions')
    f.advisor_runner = Runner({'model': 'sonnet', 'effort': 'medium', 'domains': []})
    monkeypatch.setattr(f, 'preflight_profile', lambda launch: None)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: f.r)
    f.start_all()
    for name in f.names:
        f.handle(turn(name, 'READY'))
    f.handle({'kind': 'task', 'request_id': 'r1', 'spec': {'goal': 'research the sector rotation'}})
    return f


# ------------------------------------------------------------------ roster file
@pytest.mark.parametrize('line, on', [('advisor: on', True), ('advisor: off', False), ('advisor: "on"', True),
                                      ("advisor: 'off'", False), ('advisor: true', True), ('', False)])
def test_roster_advisor_values(tmp_path, line, on):
    raw = ('workers:\n  - name: worker-1\n' + line + '\n').encode()
    assert roster.parse(raw, tmp_path)[0] == ['worker-1'] and roster.advisor(raw) is on


@pytest.mark.parametrize('line', ['advisor: maybe', 'advisor: 1', 'advisor: [on]'])
def test_roster_bad_advisor_value_is_rejected(tmp_path, line):
    with pytest.raises(ValueError, match='advisor must be on or off'):
        roster.parse(('workers:\n  - name: worker-1\n' + line + '\n').encode(), tmp_path)


def test_write_advisor_keeps_the_rest_and_creates_a_missing_roster(tmp_path):
    own = tmp_path / 'team.yaml'
    own.write_text('# keep me\nworkers:\n  - name: exit   # the exit worker\nadvisor: off\nhandoff: {codex: {ratio: 0.5}}\n')
    roster.write_advisor(own, True)
    assert own.read_text() == '# keep me\nworkers:\n  - name: exit   # the exit worker\nadvisor: on\nhandoff: {codex: {ratio: 0.5}}\n'
    (tmp_path / 'run').mkdir()
    made = roster.write_advisor(tmp_path / 'run' / 'roster.yaml', True, ['worker-1', 'worker-2'])
    workers, _ = roster.load(made)
    assert workers == ['worker-1', 'worker-2'] and roster.advisor(made.read_bytes()) is True
    own.write_text('workers: []\n')
    with pytest.raises(ValueError, match='at least one worker'):  # never writes a roster the daemon rejects
        roster.write_advisor(own, True)
    assert own.read_text() == 'workers: []\n'


# ------------------------------------------------------------------ v1: default off
def test_default_off_is_the_pre_advisor_fleet(cfx):
    f = cfx
    f.advisor_runner = Runner(AssertionError('advisor must not be consulted'))
    assert not f.advisor_online() and 'advisor' not in f.cfg and 'advisor' not in fleet.load(f.path)
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    assert (cwd / 'CLAUDE.md').read_text() == (cwd / 'AGENTS.md').read_text()  # no core / domain rules
    assert not (f.run_dir / 'runtime').exists()  # every generation runs off the main config
    assert 'advice_hold' not in f.agent('worker-1') and f.agent('worker-1')['launch_ready'] == '1'
    send_decision(f, [item()])
    f.tick()
    f.pump()
    assert f.advisor_runner.calls == [] and f.agent('worker-1')['state'] == 'busy'
    assert f.r.hgetall(fleet.ADVICE_SEQ) == {} and 'advice_ticket' not in f.agent('worker-1')
    assert not (f.run_dir / 'roster.yaml').exists() and 'advisor' not in f.r.h.get(fleet.SERVICE, {})  # nothing written
    assert not logs(f, 'advisor.on') and not logs(f, 'advisor.off')


# ------------------------------------------------------------------ v1: user turns it on / off mid-run
def test_on_mid_run_next_dispatch_consults_running_workers_untouched(cfx):
    f = cfx
    send_decision(f, [item()])
    f.pump()
    assert f.agent('worker-1')['state'] == 'busy' and f.advisor_runner.calls == []
    set_roster(f, True)
    f.tick()
    assert f.advisor_online() and logs(f, 'advisor.on')[0]['who'] == 'user'
    assert f.r.h[fleet.SERVICE]['advisor'] == 'on' and f.r.h[fleet.SERVICE]['advisor_by'] == 'user'
    info = f.agent('worker-1')
    assert info['state'] == 'busy' and info['generation'] == '0' and 'advice_ticket' not in info  # untouched
    send_decision(f, [item('worker-2', 'Summarize the advisor plan into worker-2/sum.md')])
    f.pump()
    assert len(f.advisor_runner.calls) == 1 and f.advisor_runner.calls[0]['slot']['name'] == 'worker-2'
    assert f.agent('worker-2')['state'] == 'busy' and f.agent('worker-2')['advice_ticket']
    # a model / effort change goes through the Phase 5 idle-boundary respawn, no double opt-in
    report(f, 'worker-1')
    f.advisor_runner = Runner({'model': 'opus', 'effort': 'high', 'domains': ['research_web']})
    send_decision(f, [item(text='Research the memory rotation and write worker-1/mem.md')])
    f.pump()
    info = f.agent('worker-1')
    assert info['state'] == 'starting' and info['label'] == 'worker-1.1'
    snapshot = fleet.load(f.run_dir / 'runtime' / 'worker-1-g1.json')
    argv = fleet.claude_argv(f.path, snapshot, 'worker-1', 1, 'READY?')
    assert argv[argv.index('--model') + 1] == 'opus' and argv[argv.index('--setting-sources') + 1] == 'project'
    f.tick()
    assert len(logs(f, 'advisor.on')) == 1  # unchanged roster: no second switch


def test_off_mid_run_stops_consulting(cfx):
    f = cfx
    set_roster(f, True)
    f.tick()
    send_decision(f, [item()])
    f.pump()
    assert len(f.advisor_runner.calls) == 1
    report(f, 'worker-1')
    set_roster(f, False)
    f.tick()
    assert not f.advisor_online() and logs(f, 'advisor.off')[0]['who'] == 'user'
    send_decision(f, [item(text='Research the memory rotation and write worker-1/mem.md')])
    f.pump()
    assert len(f.advisor_runner.calls) == 1 and 'memory rotation' in worker_text(f, 'worker-1')
    assert f.agent('worker-1')['generation'] == '0'


def test_pending_advice_is_dropped_when_turned_off(cfx):
    f = cfx
    set_roster(f, True)
    f.tick()
    send_decision(f, [item()])  # registered for advice, not delivered yet
    assert f.r.llen(fleet.ADVICE_PENDING + 'worker-1') == 1
    set_roster(f, False)
    f.tick()  # the tick runs the roster first, then delivers
    f.pump()
    assert f.r.llen(fleet.ADVICE_PENDING + 'worker-1') == 0 and f.advisor_runner.calls == []
    assert f.agent('worker-1')['state'] == 'busy'


# ------------------------------------------------------------------ v1: the supervisor's decision key
def test_supervisor_key_writes_the_roster_and_advises_the_same_decision(cfx):
    f = cfx
    f.on_supervisor(decision(dispatch=[item()], advisor='on'))
    raw = (f.run_dir / 'roster.yaml').read_bytes()  # a run without --roster: created from its workers
    assert roster.advisor(raw) is True and roster.parse(raw, f.run_dir)[0] == ['worker-1', 'worker-2']
    assert f.advisor_online() and logs(f, 'advisor.on')[0]['who'] == 'supervisor'
    f.pump()
    assert len(f.advisor_runner.calls) == 1  # this decision's dispatch is already advised
    f.tick()  # the daemon's own write comes back through roster_tick: no second switch
    assert len(logs(f, 'advisor.on')) == 1 and f.workers == ['worker-1', 'worker-2']
    report(f, 'worker-1')
    f.on_supervisor(decision(dispatch=[item(text='Research the memory rotation and write worker-1/mem.md')],
                             advisor='off'))
    f.pump()
    assert roster.advisor((f.run_dir / 'roster.yaml').read_bytes()) is False
    assert logs(f, 'advisor.off')[0]['who'] == 'supervisor' and len(f.advisor_runner.calls) == 1
    assert 'memory rotation' in worker_text(f, 'worker-1')


def test_supervisor_key_edits_a_configured_roster_in_place(cfx, tmp_path):
    f = cfx
    own = tmp_path / 'team.yaml'
    own.write_text('# team\nworkers:\n  - name: worker-1\n  - name: worker-2\n')
    f.cfg['roster'] = str(own)
    f.tick()
    f.on_supervisor(decision(dispatch=[item()], advisor='on'))
    assert own.read_text() == '# team\nworkers:\n  - name: worker-1\n  - name: worker-2\nadvisor: on\n'
    assert not (f.run_dir / 'roster.yaml').exists()


@pytest.mark.parametrize('value', ['yes', True, 1])
def test_bad_advisor_key_is_a_bad_reply(cfx, value):
    f = cfx
    f.on_supervisor(decision(advisor=value))
    assert f.r.hget(fleet.TASK, 'bad_replies') == '1' and not f.advisor_online()
    assert any('advisor must be "on" or "off"' in t for t in f.r.l['fx:inbox:supervisor'])


def test_consult_while_off_gets_an_advisor_off_note(cfx):
    """Off: consult is answered with one [advisor off] note; with a dispatch in the same reply it rides
    on the next supervisor message, a consult-only reply gets it at once (nothing else would wake it)."""
    f = cfx
    f.on_supervisor(decision(dispatch=[dict(item(), id='M0')]))
    f.pump()
    report(f, 'worker-1')
    ask = {'worker': 'worker-1', 'task': 'M0', 'finding': 'two rounds of 403'}
    f.on_supervisor(decision(dispatch=[item('worker-2', 'Research the energy rotation and write worker-2/notes.md')],
                             consult=[ask]))
    assert f.r.llen(fleet.ADVICE_NOTES) == 1 and f.r.lrange(fleet.RESCUE, 0, -1) == []
    f.pump()
    report(f, 'worker-2')
    f.pump()
    note = next(t for t in supervisor_texts(f) if '[advisor off]' in t)
    assert '\n[advisor off] consult M0 (worker-1) ' in note and 'advisor: "on"' in note
    assert f.r.llen(fleet.ADVICE_NOTES) == 0
    before = len([t for t in supervisor_texts(f) if '[advisor off]' in t])
    f.on_supervisor(decision(dispatch=[], consult=[ask]))
    offs = [t for t in supervisor_texts(f) if '[advisor off]' in t]
    assert len(offs) == before + 1 and offs[-1].startswith('[advisor off] consult M0 (worker-1) ')
    assert f.r.llen(fleet.ADVICE_NOTES) == 0 and f.advisor_runner.calls == [] and f.task()['bad_replies'] == '0'


def test_bad_kit_refuses_the_switch_and_keeps_it_off(cfx):
    f = cfx
    (Path(f.cfg['worker_kit']) / 'domains' / 'fleet' / 'RULES.md').unlink()
    f.on_supervisor(decision(dispatch=[item()], advisor='on'))
    assert not f.advisor_online() and not (f.run_dir / 'roster.yaml').exists()  # roster not written
    assert any('advisor: "on" 未生效' in x for x in f.r.lrange(fleet.ADVICE_NOTES, 0, -1))
    set_roster(f, True)  # the user writes it anyway: rejected, logged, still off
    f.tick()
    assert not f.advisor_online() and logs(f, 'advisor.rejected')


# ------------------------------------------------------------------ `fleet advisor on|off`
def test_cli_only_edits_the_roster_of_the_latest_or_given_run(cfx, monkeypatch, tmp_path, capsys):
    f = cfx
    latest = tmp_path / 'latest.json'
    fleet.save(latest, dict(fleet.load(f.path), config=str(f.path)))
    monkeypatch.setattr(fleet, 'LATEST', latest)
    monkeypatch.setattr(fleet, 'request', lambda *a, **k: pytest.fail('the CLI never talks to the daemon'))
    fleet.main(['advisor', 'on', '--config', str(latest)])
    assert 'advisor on written to ' + str(f.run_dir / 'roster.yaml') in capsys.readouterr().out
    assert roster.advisor((f.run_dir / 'roster.yaml').read_bytes()) is True and not f.advisor_online()
    f.tick()  # the daemon applies it
    assert f.advisor_online() and logs(f, 'advisor.on')[0]['who'] == 'user'
    fleet.main(['advisor', 'off', '--config', str(f.path)])
    f.tick()
    assert not f.advisor_online() and 'advisor' not in fleet.load(f.path)
    (Path(f.cfg['worker_kit']) / 'domains' / 'fleet' / 'RULES.md').unlink()
    with pytest.raises(SystemExit, match='--worker-kit rejected'):
        fleet.main(['advisor', 'on', '--config', str(f.path)])
    assert roster.advisor((f.run_dir / 'roster.yaml').read_bytes()) is False


def test_sub_fleet_config_never_inherits_the_switch(cfx, tmp_path):
    from fleetmux import subfleet
    f = cfx
    set_roster(f, True)
    f.tick()
    child = subfleet.child_config(f, 'worker-1', 'M0', 1, tmp_path / 'child', 'pane-x', {}, {'redis_socket': 's'}, 'n')
    assert f.advisor_online() and 'advisor' not in child  # a sub-fleet starts off; its own roster turns it on


def test_online_at_start_is_audited(afx):
    on, = logs(afx, 'advisor.on')
    assert on['who'] == 'user' and on['at_start'] is True


# ------------------------------------------------------------------ control line
@pytest.mark.parametrize('daemon, shown', [({'paused': False, 'heartbeat_at': 1}, 'adv=off'),
                                           ({'paused': False, 'heartbeat_at': 1, 'advisor': 'on'}, 'adv=on')])
def test_control_state_line_shows_the_switch(daemon, shown):
    record = {'event': 'control.state', 'snapshot': {'run_id': 'r', 'socket': {'available': True},
                                                     'daemon': daemon, 'slots': {}}}
    assert control_view._render(record, 400).splitlines()[0].endswith(shown)


# ------------------------------------------------------------------ v2
@pytest.fixture
def v2off(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    yield from v2_advised(private_redis_v2, tmp_path, monkeypatch, ['worker-1'], on=False, worker_effort='medium')


def test_v2_off_at_start_spawns_with_defaults_immediately(v2off):
    f = v2off
    info = f.agent('worker-1')
    assert info['launch_ready'] == '1' and 'advice_hold' not in info and not f.advisor_online()
    assert not (f.run_dir / 'runtime').exists()
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    assert (cwd / 'CLAUDE.md').read_text() == (cwd / 'AGENTS.md').read_text()
    assert control_view.snapshot(f.cfg)['daemon']['advisor'] == 'off'


def test_v2_on_at_start_holds_then_off_releases_with_defaults(v2adv):
    from test_v2_codex_claude import answer_turn, submit, typed
    f = v2adv
    assert f.agent('worker-1')['advice_hold'] == '1' and f.agent('worker-1')['launch_ready'] == '0'
    assert control_view.snapshot(f.cfg)['daemon']['advisor'] == 'on'
    submit(f)
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Research the semis rotation into worker-1/notes.md'}]}))
    assert f.r.llen(fleet.ADVICE_PENDING + 'worker-1') == 1
    set_roster(f, False)
    f.tick()  # roster first: the held slot is released with the slot defaults, before any pump
    info = f.agent('worker-1')
    assert info['advice_hold'] == '0' and info['launch_ready'] == '1' and float(info['startup_deadline']) > time.time()
    assert f.r.llen(fleet.ADVICE_PENDING + 'worker-1') == 0 and logs(f, 'advice.released_default')
    snap = fleet.load(f.run_dir / 'runtime' / 'worker-1-g0.json')
    assert 'advisor' not in snap and 'worker_effort' not in snap
    argv = fleet.claude_argv(f.path, snap, 'worker-1', 0, 'READY?')
    assert '--setting-sources' not in argv and argv[argv.index('--effort') + 1] == 'high'
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    assert (cwd / 'CLAUDE.md').read_text() == (cwd / 'AGENTS.md').read_text()
    assert not (cwd / '.claude' / 'skills').exists()
    assert control_view.snapshot(f.cfg)['daemon']['advisor'] == 'off'
    answer_turn(f, 'worker-1', 'READY', startup=True)
    f.pump()
    text, = typed(f, 'worker-1')
    assert 'semis rotation' in text and f.advisor_runner.calls == []


def test_v2_supervisor_turns_it_on_and_the_next_dispatch_hotloads(v2off):
    from test_v2_codex_claude import answer_turn, submit, typed
    f = v2off
    answer_turn(f, 'worker-1', 'READY', startup=True)
    f.advisor_runner = Runner({'model': 'sonnet', 'effort': 'medium', 'domains': ['research_web']})
    submit(f)
    assert answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 5, 'reason': 'go',
        'advisor': 'on', 'dispatch': [{'to': 'worker-1', 'instruction': 'Research the semis rotation into worker-1/notes.md'}]}
    ))['status'] == 'committed'
    assert f.advisor_online() and logs(f, 'advisor.on')[0]['who'] == 'supervisor'
    f.pump()
    assert len(f.advisor_runner.calls) == 1
    text = typed(f, 'worker-1')[-1]
    assert text.lstrip().startswith('## Domain rules: research_web') and 'semis rotation' in text
    assert f.agent('worker-1')['generation'] == '0'  # the running generation stays; only domains hot-load


@pytest.fixture
def v2adv(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    yield from v2_advised(private_redis_v2, tmp_path, monkeypatch, ['worker-1'])
