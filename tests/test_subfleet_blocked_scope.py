"""A root supervisor's BLOCKED on a sub-fleet notice marks that sub-fleet; the root goes on (docs/design.md §24).

Incident (2026-11-16, root ops): the root daemon queued ``[sub-fleet stuck]`` about sub-fleet memsub; the root
supervisor answered BLOCKED (only a human can look); the program paused the whole root for 5 days, and queued jobs
never started. Now, at a root fleet (no ``parent_config``), a BLOCKED whose bound message is a ``[sub-fleet stuck]`` /
``[sub-fleet held]`` notice about one of this fleet's sub-fleets writes ``attention`` into that sub-fleet's record and
neither pauses the root (service) nor ends its task (task). Every other BLOCKED keeps today's behaviour; so does a
fleet that has a parent. Switch: ``fleet.yaml#daemon_watch.subfleet_blocked_marks_child`` (default on).

Private test Redis, fake cmux, run directories under tmp_path; no real ~/.fleetmux.
"""
import io
import json
import time
from contextlib import redirect_stdout
from pathlib import Path

from test_escalate import ok_check  # noqa: F401  (fixture)
from test_fleet import decision as v1_decision, fx, turn  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_service_escalate import audits, svc  # noqa: F401  (fixture)
from test_service_split import queued_to
from test_subfleet import no_launch
from test_v2_codex_claude import answer_turn, mixed  # noqa: F401  (fixture)
from test_v2_codex_claude import decision as task_decision, submit as submit_task
from fleetmux import fleet, fleet_config, lifecycle_close, pi_adapter, runtime_v2, subfleet

REASON = 'only a human can look at that sub-fleet; I may not run fleet commands'


def child(f, task_id='M7', state='running'):
    """A sub-fleet run directory of ``f`` with its record (no daemon, no Redis of its own)."""
    d = Path(f.run_dir) / 'sub' / task_id
    (d / 'workspace').mkdir(parents=True, exist_ok=True)
    fleet.save(d / 'config.json', {'run': str(d), 'parent_config': str(f.path), 'task_id': task_id})
    subfleet.save_record(d, {'task_id': task_id, 'state': state, 'slot': 'worker-2', 'created': time.time()})
    return d


def stuck_text(task_id, d):
    return ('[sub-fleet stuck] sub-fleet ' + task_id + ' (' + str(d) + '): paused for 1000 s. Facts: daemon beat '
            'never. Look at it (`fleetmux status --config ' + str(Path(d) / 'config.json') + '`).')


def held_note(f, d, task_id='M7'):
    folder = Path(f.run_dir) / 'subfleet-notices'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'held-test.json'
    fleet.save(path, {'kind': 'held', 'origin_run': str(d), 'task': task_id, 'slot': 'worker-1', 'label': 'mem-3',
                      'text': 'aborted/error/unfinished settlement; execution stop unconfirmed',
                      'reason': 'not_recovered'})
    return path


def service_blocked(f, text, reason=REASON):
    """``text`` is the supervisor's next message (a notice); it answers BLOCKED."""
    f.queue('supervisor', text)
    f.pump()
    out = answer_turn(f, 'supervisor', json.dumps({'status': 'BLOCKED', 'reason': reason, 'dispatch': [],
                                                   'skip': []}))
    assert out['status'] == 'committed', out
    f.pump()


def private_yaml(tmp_path, monkeypatch, body):
    path = tmp_path / 'private-fleet.yaml'
    path.write_text(body)
    monkeypatch.setenv(fleet_config.USER_ENV, str(path))


# ------------------------------------------------------------------ service root
def test_root_service_blocked_on_subfleet_stuck_marks_child_and_does_not_pause(svc):
    f = svc
    d = child(f)
    service_blocked(f, stuck_text('M7', d))
    assert not f.paused(), f.r.hgetall(fleet.SERVICE)
    att = subfleet.record(d)['attention']
    assert att['reason'] == REASON and att['notice'] == 'stuck' and att['by'] == 'supervisor BLOCKED'
    assert att['notice_text'].startswith('[sub-fleet stuck] sub-fleet M7')
    rows = audits(f, 'blocked.subfleet_marked')
    assert len(rows) == 1 and 'M7' in json.dumps(rows[0])


def test_jobs_still_start_while_a_child_waits_for_a_human(svc, ok_check, monkeypatch):  # noqa: F811
    f = svc
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    d = child(f)
    service_blocked(f, stuck_text('M7', d))
    runtime_v2.post_event(f.v2.bus, {'kind': 'task', 'request_id': 'cmd-job', 'spec': {
        'goal': 'Survey the five vendor APIs and write a comparison report with a recommendation'}}, 'cmd-job')
    assert f.v2.step(10)
    f.pump()
    answer = runtime_v2.wait_reply(f.v2.bus, 'cmd-job', timeout=2)
    assert answer['status'] == 'accepted', answer
    f.tick()
    assert f.job_state()[answer['job_id']]['state'] != 'queued', f.job_state()


def test_blocked_on_regen_requeued_stuck_notice_is_classified_too(svc):
    f = svc
    d = child(f)
    note = runtime_v2.REGEN_NOTE.format(prev='supervisor-1', why='held', label='supervisor-1.1')
    service_blocked(f, note + stuck_text('M7', d))
    assert not f.paused() and subfleet.record(d)['attention']['reason'] == REASON


def test_blocked_on_other_notices_still_pauses(svc):
    """Every other BLOCKED pauses the root as before: a sub-fleet report, a finished sub-fleet, an operator
    instruction, and a stuck notice whose run directory is not one of this fleet's sub-fleets."""
    f = svc
    d = child(f)
    texts = ['[sub-fleet report] sub-fleet M7 (worker-1.1): wants to delete old builds - disk is full',
             '[Sub-fleet M9 failed] worker-2 ran sub-fleet M9 (attempt 1): budget. worker-2 is a worker again.',
             '[Operator instruction] stop routing to worker-2',
             stuck_text('M9', Path(f.run_dir).parent / 'elsewhere' / 'M9')]
    for text in texts:
        f.resume()
        service_blocked(f, text)
        assert f.paused(), text
    assert 'attention' not in subfleet.record(d)
    assert not audits(f, 'blocked.subfleet_marked')


def test_blocked_on_a_stuck_notice_for_a_gone_child_does_not_pause(svc):
    f = svc
    d = child(f, state='done')
    service_blocked(f, stuck_text('M7', d))
    assert not f.paused()
    assert 'attention' not in subfleet.record(d)
    assert audits(f, 'blocked.subfleet_gone')


def test_non_root_fleet_keeps_todays_pause(svc):
    """A fleet with a parent pauses as before: its parent hears about it, up to the root."""
    f = svc
    d = child(f)
    f.cfg['parent_config'] = str(Path(f.run_dir).parent / 'parent' / 'config.json')
    service_blocked(f, stuck_text('M7', d))
    assert f.paused()
    assert 'attention' not in subfleet.record(d)


def test_switch_off_keeps_todays_pause(svc, tmp_path, monkeypatch):
    f = svc
    private_yaml(tmp_path, monkeypatch, 'daemon_watch:\n  subfleet_blocked_marks_child: false\n')
    d = child(f)
    service_blocked(f, stuck_text('M7', d))
    assert f.paused()
    assert 'attention' not in subfleet.record(d)


# ------------------------------------------------------------------ status, re-report, clear
def test_attention_shows_in_status_and_clears_when_the_child_moves(svc, monkeypatch):
    f = svc
    d = child(f)
    service_blocked(f, stuck_text('M7', d))
    buf = io.StringIO()
    with redirect_stdout(buf):
        fleet.status(f.cfg)
    out = json.loads(buf.getvalue())
    assert out['attention'] == [dict(subfleet.record(d)['attention'], subfleet='M7', run=str(d))], out.get('attention')
    assert out['subfleets']['M7']['attention']['reason'] == REASON
    monkeypatch.setattr(subfleet, 'child_progress', lambda cfg, rec: {})
    monkeypatch.setattr(subfleet, 'stuck_reasons', lambda f_, d_, rec, facts, after: [])
    f.subfleet_watch_at = {}
    subfleet.watch_children(f)
    assert 'attention' not in subfleet.record(d)
    assert audits(f, 'subfleet.attention_cleared')
    buf = io.StringIO()
    with redirect_stdout(buf):
        fleet.status(f.cfg)
    assert 'attention' not in json.loads(buf.getvalue())


def test_marked_child_is_not_re_reported_to_the_supervisor(svc, monkeypatch):
    f = svc
    d = child(f)
    sent = []
    monkeypatch.setattr(lifecycle_close, 'managed', lambda fl: fl is f)
    monkeypatch.setattr(lifecycle_close, 'notify', lambda fl, event, text, policy=None: sent.append(event))
    monkeypatch.setattr(subfleet, 'child_progress', lambda cfg, rec: {'paused': True})
    reasons = {'now': ['paused for 1000 s']}
    monkeypatch.setattr(subfleet, 'stuck_reasons', lambda f_, d_, rec, facts, after: reasons['now'])
    subfleet.save_record(d, dict(subfleet.record(d), attention={'reason': REASON, 'notice': 'stuck', 'at': time.time()},
                                 stuck_alert_at=str(time.time() - 4000)))
    f.subfleet_watch_at = {}
    subfleet.watch_children(f)
    assert not queued_to(f, 'supervisor', '[sub-fleet stuck]'), 'a marked sub-fleet is not sent to the supervisor again'
    assert sent == ['subfleet_stuck'], 'the user channel still hears it'
    reasons['now'] = []  # it moves: the mark is cleared
    f.subfleet_watch_at = {}
    subfleet.watch_children(f)
    reasons['now'] = ['paused for 1000 s']  # stuck again: a new episode, the supervisor hears it again
    f.subfleet_watch_at = {}
    subfleet.watch_children(f)
    assert len(queued_to(f, 'supervisor', '[sub-fleet stuck]')) == 1
    assert sent == ['subfleet_stuck', 'subfleet_stuck']


# ------------------------------------------------------------------ task root (v2 and v1)
def test_task_root_blocked_on_subfleet_held_keeps_the_task_running(mixed):
    f = mixed
    submit_task(f)
    assert answer_turn(f, 'supervisor', task_decision(['worker-1']))['status'] == 'committed'
    d = child(f)
    f.held_notice(held_note(f, d))
    f.pump()
    out = answer_turn(f, 'supervisor', json.dumps({'status': 'BLOCKED', 'completion': 10, 'reason': REASON,
                                                   'dispatch': []}))
    assert out['status'] == 'committed', out
    assert f.task()['status'] == 'running', f.task()
    att = subfleet.record(d)['attention']
    assert att['notice'] == 'held' and att['reason'] == REASON


def test_task_root_blocked_on_anything_else_still_ends_the_task(mixed):
    f = mixed
    submit_task(f)
    assert answer_turn(f, 'supervisor', task_decision(['worker-1']))['status'] == 'committed'
    f.queue('supervisor', '[Operator instruction] stop')
    f.pump()
    answer_turn(f, 'supervisor', json.dumps({'status': 'BLOCKED', 'completion': 10, 'reason': REASON, 'dispatch': []}))
    assert f.task()['status'] == 'blocked', f.task()


def test_v1_transport_classifies_from_last_msg(fx):  # noqa: F811
    f = fx
    f.pump()  # the task prompt reaches the supervisor first
    f.handle(turn('supervisor', v1_decision()))
    d = child(f)
    f.held_notice(held_note(f, d))
    f.pump()
    assert f.agent('supervisor').get('last_msg', '').startswith('[sub-fleet held]'), f.agent('supervisor')
    f.handle(turn('supervisor', v1_decision(status='BLOCKED', dispatch=[])))
    assert f.task()['status'] == 'running', f.task()
    assert subfleet.record(d)['attention']['notice'] == 'held'


# ------------------------------------------------------------------ parameter
def test_switch_is_validated_and_never_exported_to_pi(tmp_path, monkeypatch):
    assert fleet_config.block('daemon_watch')['subfleet_blocked_marks_child'] is True
    assert 'subfleet_blocked_marks_child' in pi_adapter.HELD_KEYS
    private_yaml(tmp_path, monkeypatch, 'daemon_watch:\n  subfleet_blocked_marks_child: false\n')
    assert pi_adapter.daemon_watch() == {'check_interval_s': 120, 'stale_after_s': 120, 'realert_after_s': 600}
