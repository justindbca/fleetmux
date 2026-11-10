"""`codex queue` binding across codex-cli versions (fix/codex-queue-0161).

Root cause on macmini (codex-cli 0.161.0): `task_started` in the rollout has no `turn_attribution`, so
codex_queue.bound() saw trigger '' and ignored the supervisor's real answer (turn.ignored "trigger none"):
the dispatch was lost. 0.162.0 / 0.162.1 record turn_attribution.turn_trigger "queue". Everything else
the channel uses (notify payload keys, `codex queue` stdout, one turn per queued item) is the same.

Fix: `start` probes `codex --version` once and pins codex_queue in config.json; an older Codex is typed
(the pre-queue path) and audited queue.unsupported, never queued into turns that cannot be bound.

Fixtures in fixtures/codex_queue_versions/ are recorded from real TUIs (isolated CODEX_HOME, 2026-10-14):
rollout-<version>.jsonl keeps only the rows the binding reads, verbatim; notify-<version>.json are the
notify payloads of the same sessions; codex-queue-stdout.txt is the CLI's answer on both versions.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from test_fleet import write_rollout
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
import test_codex_notype_regression as reg
from test_codex_notype_regression import (claude_turn, drive, lease, startup_ready, submit, typed,  # noqa: F401
                                          paste_spy)
from cmux_fleet import codex_queue, fleet

FIX = Path(__file__).parent / 'fixtures' / 'codex_queue_versions'


def rows(version):
    return [json.loads(x) for x in (FIX / ('rollout-' + version + '.jsonl')).read_text().splitlines() if x.strip()]


def notifies(version):
    return [x['payload'] for x in json.loads((FIX / ('notify-' + version + '.json')).read_text())]


def thread_of(version):
    return rows(version)[0]['payload']['id']


def recorded_turn(version, marker):
    """The recorded rows (task_started .. task_complete) of the turn whose UserMessage contains `marker`."""
    all_rows = rows(version)
    turn = next(r['payload']['turn_id'] for r in all_rows if r['payload'].get('type') == 'item_completed'
                and r['payload']['item']['type'] == 'UserMessage'
                and marker in r['payload']['item']['content'][0]['text'])
    return turn, [r for r in all_rows if r['type'] == 'event_msg' and r['payload'].get('turn_id') == turn]


def replay(version, marker, *, turn, text, answer):
    """A recorded turn re-issued for a new delivery: same row shapes and keys (with or without
    turn_attribution), only the ids, the user text and the final answer replaced."""
    old, recorded = recorded_turn(version, marker)
    out = []
    for r in copy.deepcopy(recorded):
        p = r['payload']
        for k in ('turn_id', 'root_turn_id'):
            if p.get(k) == old:
                p[k] = turn
        if 'turn_attribution' in p:
            p['turn_attribution'] = dict(p['turn_attribution'], turn_id=turn, root_turn_id=turn)
        item = p.get('item') or {}
        if item.get('type') == 'UserMessage':
            item['content'][0]['text'] = text
        elif item.get('type') == 'AgentMessage':
            item['content'][0]['text'] = answer
        if p.get('type') == 'task_complete':
            p['last_agent_message'] = answer
        out.append(r)
    return out


# ------------------------------------------------------------------ the recorded facts (root cause)
def test_recorded_rollouts_differ_only_in_turn_attribution():
    started = {v: [r['payload'] for r in rows(v) if r['payload'].get('type') == 'task_started']
               for v in ('0.161.0', '0.162.1')}
    assert started['0.161.0'] and all('turn_attribution' not in p for p in started['0.161.0'])
    triggers = [p['turn_attribution']['turn_trigger'] for p in started['0.162.1']]
    assert triggers[0] == 'user' and 'queue' in triggers  # argv/typed turns "user", queued turns "queue"
    # The queued UserMessage itself has the same shape on both versions.
    for v, token in (('0.161.0', 'fx-lab0161q1'), ('0.162.1', 'fx-lab0162q1')):
        _, turn_rows = recorded_turn(v, token)
        users = [r['payload']['item'] for r in turn_rows if r['payload'].get('type') == 'item_completed'
                 and r['payload']['item']['type'] == 'UserMessage']
        assert len(users) == 1 and users[0]['content'][0]['text'].endswith(codex_queue.MARK.format(token))


def test_recorded_notify_payload_and_queue_stdout_are_the_same_on_both_versions():
    assert {tuple(sorted(p)) for p in notifies('0.161.0')} == {tuple(sorted(p)) for p in notifies('0.162.1')}
    lines = (FIX / 'codex-queue-stdout.txt').read_text().splitlines()
    assert lines and all(codex_queue.QUEUED.search(x) for x in lines)


def _bound_against_recording(tmp_path, version, token):
    _, recorded = recorded_turn(version, token)
    turn = recorded[0]['payload']['turn_id']
    text = next(r['payload']['item']['content'][0]['text'] for r in recorded
                if (r['payload'].get('item') or {}).get('type') == 'UserMessage')
    thread, sessions = thread_of(version), tmp_path / 'sessions'
    write_rollout(sessions, thread, tmp_path / 'cwd', rows=rows(version)[1:])
    rec = {'cq_delivery': 'd-1', 'cq_token': token, 'cq_text': text, 'cq_thread': thread, 'cq_generation': '0',
           'cq_state': 'queued', 'cq_queued_id': 'q', 'cq_turn': '', 'cq_queued_at': '0'}
    return codex_queue.bound(rec, thread, turn, sessions, 0)


def test_bound_on_recorded_0161_turn_fails_with_the_root_cause_named(tmp_path):
    ok, why = _bound_against_recording(tmp_path, '0.161.0', 'fx-lab0161q1')
    assert ok is False and 'no turn_attribution' in why  # was: "trigger none", indistinguishable from a typed turn


def test_bound_on_recorded_0162_queue_turn_binds(tmp_path):
    assert _bound_against_recording(tmp_path, '0.162.1', 'fx-lab0162q1') == (True, '')


# ------------------------------------------------------------------ capability probe
def _run(stdout='', rc=0, exc=None):
    def run(argv, **kw):
        assert argv[1:] == ['--version']
        if exc:
            raise exc
        return subprocess.CompletedProcess(argv, rc, stdout, '')
    return run


@pytest.mark.parametrize('out,expected', [
    ('codex-cli 0.161.0\n', (False, '0.161.0')),
    ('codex-cli 0.160.1\n', (False, '0.160.1')),
    ('codex-cli 0.162.0\n', (True, '0.162.0')),
    ('codex-cli 0.162.1\n', (True, '0.162.1')),
    ('codex-cli 0.170.0-alpha.2\n', (True, '0.170.0')),
    ('codex-cli 1.0.0\n', (True, '1.0.0')),
])
def test_capability_by_version(out, expected):
    ok, version, why = codex_queue.capability('/x/codex', run=_run(out))
    assert (ok, version) == expected
    assert bool(why) is (not ok)
    if not ok:
        assert 'turn_attribution' in why


@pytest.mark.parametrize('kw', [dict(stdout='1.1.0\n'), dict(stdout='', rc=0), dict(stdout='codex-cli 0.162.1', rc=2),
                                dict(exc=OSError('no such file')),
                                dict(exc=subprocess.TimeoutExpired(['codex'], 15))])
def test_capability_unreadable_is_unsupported(kw):
    ok, _, why = codex_queue.capability('/x/codex', run=_run(**kw))
    assert ok is False and why


def test_capability_without_a_binary():
    assert codex_queue.capability(None)[0] is False and codex_queue.capability('')[0] is False


def test_is_codex_follows_the_pinned_probe():
    base = {'supervisor_cli': 'codex', 'worker_cli': 'claude'}
    assert codex_queue.is_codex(base, 'supervisor') is True  # configs from before the probe: unchanged
    assert codex_queue.is_codex(dict(base, codex_queue=True), 'supervisor') is True
    assert codex_queue.is_codex(dict(base, codex_queue=False), 'supervisor') is False
    assert codex_queue.is_codex(dict(base, codex_queue=True), 'worker-1') is False  # not a Codex slot at all


# ------------------------------------------------------------------ start / resume pin the probe
class Reached(Exception):
    pass


def test_start_pins_the_probe_in_config(tmp_path, monkeypatch, capsys):
    from cmux_fleet import cmux_manager, redis_runtime
    probed, saved = [], {}
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(codex_queue, 'capability',
                        lambda codex, **kw: probed.append(codex) or (False, '0.161.0', 'too old (test)'))
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: type('M', (), {'session_exists': lambda self: False,
                                                                           'meta_path': tmp_path / 'cmux' / 'm.json'})())
    monkeypatch.setattr(fleet, 'prepare_codex_home', lambda home, profiles: None)
    monkeypatch.setattr(redis_runtime, 'prepare', lambda d, token: {})

    def save(path, cfg):
        if Path(path).name == 'config.json':
            saved.update(cfg)
            raise Reached
    monkeypatch.setattr(fleet, 'save', save)
    with pytest.raises(Reached):
        fleet.main(['start', '--lifecycle', 'dev_task', '--allow-development-v2', '--supervisor-cli', 'codex',
                    '--worker-cli', 'claude', '--codex', '/bin/echo', '--claude', '/bin/echo',
                    '--redis-server', '/bin/echo', '--runs-dir', str(tmp_path / 'runs'),
                    '--codex-home', str(tmp_path / 'codex-home')])
    assert probed == ['/bin/echo']
    assert saved['codex_queue'] is False and saved['codex_version'] == '0.161.0'
    assert 'typed delivery' in capsys.readouterr().err


def _resume_until_probe(tmp_path, monkeypatch, cfg):
    run = tmp_path / 'run'
    run.mkdir()
    fleet.save(run / 'config.json', dict(cfg, run=str(run), transport_version=2, allow_development_v2=True,
                                         supervisor_cli='codex', worker_cli='claude', workers=['worker-1']))
    probed = []
    monkeypatch.setattr(codex_queue, 'capability', lambda codex, **kw: probed.append(codex) or (False, '0.161.0', 'x'))
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: True)  # stops right after the config checks
    fleet.resume(run, type('A', (), {'max_attempts': None, 'roster': None})())
    return probed


def test_resume_probes_a_config_from_before_the_probe(tmp_path, monkeypatch):
    assert _resume_until_probe(tmp_path, monkeypatch, {'codex': '/x/codex'}) == ['/x/codex']


def test_resume_keeps_the_pinned_probe(tmp_path, monkeypatch):
    # Reattached TUIs still run the binary the run started with: never switch channel mid-run.
    assert _resume_until_probe(tmp_path, monkeypatch, {'codex': '/x/codex', 'codex_queue': True}) == []


# ------------------------------------------------------------------ end to end on a 0.161.0 fleet: typed fallback
def build_0161(r, tmp_path, monkeypatch):
    """reg.build with the start probe's answer for codex-cli 0.161.0 pinned in config.json."""
    real_save = fleet.save

    def save(path, cfg):
        if Path(path).name == 'config.json' and cfg.get('supervisor_cli') == 'codex' and 'codex_queue' not in cfg:
            cfg = dict(cfg, codex_queue=False, codex_version='0.161.0')
        return real_save(path, cfg)
    monkeypatch.setattr(fleet, 'save', save)
    f = reg.build(r, tmp_path, monkeypatch, ready=False)
    monkeypatch.setattr(fleet, 'save', real_save)
    return f


def turn_0161(f, marker, text, answer, inputs):
    """A finished 0.161.0 supervisor turn (recorded shape, no turn_attribution) and its real notify."""
    thread = reg.session(f, 'supervisor')
    turn = str(uuid.uuid4())
    write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'),
                  rows=replay('0.161.0', marker, turn=turn, text=text, answer=answer))
    payload = dict(notifies('0.161.0')[0], **{'thread-id': thread, 'turn-id': turn, 'input-messages': inputs,
                                              'last-assistant-message': answer,
                                              'cwd': str(fleet.agent_cwd(f.cfg, 'supervisor'))})
    gen = str(f.agent('supervisor')['generation'])
    return drive(f, lambda: fleet.notify(f.path, 'supervisor', gen, json.dumps(payload), f.test_sessions))


@pytest.fixture
def old_codex(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    f = build_0161(private_redis_v2, tmp_path, monkeypatch)
    yield f
    f.v2.close()


def test_0161_fleet_types_the_task_and_the_answer_settles_dispatches_and_reaches_the_worker(old_codex):
    f = old_codex
    assert f.cfg['codex_queue'] is False
    assert any(a.get('event') == 'queue.unsupported' and a.get('slot', a.get('agent')) == 'supervisor'
               or (a.get('event') == 'queue.unsupported' and 'supervisor' in json.dumps(a)) for a in reg.audit(f))
    assert turn_0161(f, 'You are a probe', 'startup prompt', 'READY', ['startup prompt'])['status'] == 'ready'
    for w in reg.WORKERS:
        assert claude_turn(f, w, 'READY', prompt='startup prompt')['status'] == 'ready'
    submit(f)
    sup_pane = f.agent('supervisor')['pane']
    body = reg.body_of(f)
    assert typed(f, 'supervisor', pane=sup_pane)[-1] == body  # typed, as before the queue channel
    assert f.q.items == [] and not f.r.hget(fleet.key('agent', 'supervisor'), 'cq_token')
    assert any(a.get('event') == 'tui.typed' for a in reg.audit(f))
    assert not any(a.get('event') == 'tui.queued' for a in reg.audit(f))
    out = turn_0161(f, 'fx-lab0161q1', body, reg.plan(['worker-1']), ['startup prompt', body])
    assert out['status'] == 'committed', out
    assert not any(a.get('event') == 'turn.ignored' for a in reg.audit(f))
    f.pump()
    f.v2.step(20)
    assert lease(f, 'worker-1')['status'] == 'received'
    assert any('worker-1/part.md' in t for t in typed(f, 'worker-1'))


def test_0162_fleet_still_queues_and_binds_on_the_recorded_shape(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    f = reg.build(private_redis_v2, tmp_path, monkeypatch)
    try:
        submit(f)
        _, text = reg.last(f)
        thread, turn = reg.session(f, 'supervisor'), str(uuid.uuid4())
        write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'),
                      rows=replay('0.162.1', 'fx-lab0162q1', turn=turn, text=text, answer=reg.plan(['worker-1'])))
        payload = dict(notifies('0.162.1')[0], **{'thread-id': thread, 'turn-id': turn,
                                                  'input-messages': ['startup prompt', text],
                                                  'last-assistant-message': reg.plan(['worker-1'])})
        out = drive(f, lambda: fleet.notify(f.path, 'supervisor', '0', json.dumps(payload), f.test_sessions))
        assert out['status'] == 'committed', out
        assert reg.cq(f, 'cq_state') == 'bound' and reg.cq(f, 'cq_turn') == turn
        reg.assert_no_input_box(f)
        f.pump()
        f.v2.step(20)
        assert lease(f, 'worker-1')['status'] == 'received'
    finally:
        f.v2.close()


def test_0161_rollout_on_a_forced_queue_channel_is_ignored_loudly_not_as_trigger_none(sup):  # noqa: F811
    """A misconfigured run (queue forced on with an old binary) still fails closed, with the cause named."""
    f = sup
    submit(f)
    _, text = reg.last(f)
    out = turn_0161(f, 'fx-lab0161q1', text, reg.plan(['worker-1']), ['startup prompt', text])
    assert out['status'] == 'ignored' and 'no turn_attribution' in out['reason'], out


sup = reg.sup  # noqa: F811  (fixture re-export)
