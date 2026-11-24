"""Safeguards refusal through the StopFailure hook (docs/design.md section 23; incident 2026-11-23: a root fleet's
Claude supervisor pinned to claude-opus-5-5 got "API Error: Opus 5.5's safeguards flagged this message ..." on one
sub-fleet report; it was retried, regenerated and re-sent 23 times until the regeneration budget held the slot).

Now the StopFailure's CLI-owned fields (the transcript's API-error row, error_details, the error type) are read for
the walls class safeguards BEFORE the same-model retry and the regeneration counter: the delivery commits `walled`,
the brake pauses, the message is queued once for the next model on the whole chain (OpenAI family first), the slot
is never held and the 3-in-3600 s regeneration budget is not charged.

Private Redis, fake cmux, real hook entry points in-process; no real fleet, no real model.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, req, session  # noqa: F401  (fixture)
from test_claude_supervisor_hook import operator, supervisor  # noqa: F401  (fixture)
from test_claude_hook_delivery import delivered
from test_supervisor_regen import events, pending_texts, ready
from test_safeguards_wall import BRIEF_TEXT, INCIDENT
from test_wall_chain import ASTRA, CACHE, DS_PI, SOL
from fleetmux import brake, discover, fleet, pause_review


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    path = tmp_path / 'discovered.yaml'
    path.write_text(yaml.safe_dump(CACHE))
    monkeypatch.setenv(discover.CACHE_ENV, str(path))
    return path


def api_error_row(text, refusal=True, at=None):
    when = datetime.fromtimestamp(time.time() if at is None else at, timezone.utc)
    return {'type': 'assistant', 'isSidechain': False, 'isApiErrorMessage': True,
            'timestamp': when.isoformat().replace('+00:00', 'Z'),
            'message': {'model': '<synthetic>', 'role': 'assistant', 'stop_reason': 'refusal' if refusal else 'stop_sequence',
                        'content': [{'type': 'text', 'text': text}]}}


def transcript(f, slot, *rows):
    path = Path(f.agent(slot).get('transcript') or f.run_dir / ('transcript-' + slot + '.jsonl'))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as fh:
        for row in rows:
            fh.write(json.dumps(row) + '\n')
    return path


def flagged(f, slot='supervisor', api_text=INCIDENT, error='invalid_request', last=None, rows=None):
    """The slot's StopFailure hook for its in-flight turn (the CLI's refusal by default), then daemon steps."""
    raw = {'session_id': f.test_threads[slot] if hasattr(f, 'test_threads') else f.agent(slot)['thread'],
           'error': error, 'hook_event_name': 'StopFailure',
           'last_assistant_message': (api_text or '') if last is None else last}
    if rows is None:
        rows = [api_error_row(api_text)] if api_text else []
    raw['transcript_path'] = str(transcript(f, slot, *rows))
    assert fleet.claude_stop_failure(f.path, slot, int(f.agent(slot)['generation']), json.dumps(raw))
    for _ in range(5):
        f.v2.step(20)


def opus(f):
    f.cfg['supervisor_model'] = 'claude-opus-5-5'  # the incident's pinned id
    fleet.save(f.path, f.cfg)
    return f


def deliver(f, text='marker one'):
    assert '\u5df2\u6392\u961f' in operator(f, text)
    delivered(f, f.ready_hook)
    lease = f.v2.bus.slot_state('supervisor')
    assert lease.get('message_id')
    return lease


def mark(f, cli, model, scope='model_pool'):
    f.r.hset(fleet.WALLS, mapping={cli + ':' + model: json.dumps({'cli': cli, 'model': model, 'scope': scope,
                                                                  'account': brake.account(cli, model), 'at': 0,
                                                                  'agent': 'worker-9'})})


def requeued(f, slot, old_id):
    return [m for m in f.v2.bus.pending(slot) if f.v2.bus.message(m).get('wall_of') == old_id]


def no_preflight(f, monkeypatch, refuse=()):
    def check(launch, cli_change_ok=False, slot=None):
        if set(launch['overrides'].values()) & set(refuse):
            raise ValueError('test: refused')
    monkeypatch.setattr(f, 'preflight_profile', check)


def assert_walled_once(f, lease, marker='marker one'):
    msg = f.v2.bus.message(lease['message_id'])
    assert msg['status'] == 'committed' and msg.get('outcome') == 'walled', msg
    again = requeued(f, 'supervisor', lease['message_id'])
    assert len(again) == 1 and pending_texts(f, 'supervisor') == [f.v2.bus.message(again[0])['body']['text']]
    text = pending_texts(f, 'supervisor')[0]
    assert marker in text and 'Retried after an API error' not in text and 'Previous generation' not in text


def assert_never_held(f, slot='supervisor'):
    info = f.agent(slot)
    assert info['state'] != 'held' and not info.get('stopfail_at') and not info.get('regen_due'), info
    assert not events(f, 'execution.held') and not events(f, 'stop_failure.retry')
    assert not [e for e in events(f, 'audit') if 'supervisor_regen' in e.get('event', '')]
    assert not [x for _, x in f.r.xrange(f.v2.bus.audit_key) if str(x.get('event', '')).startswith('supervisor_regen')]


# ------------------------------------------------------------------ the hook's CLI-owned fields
def test_stop_failure_wall_reads_only_cli_owned_fields(tmp_path):
    path = tmp_path / 't.jsonl'
    path.write_text('\n'.join(json.dumps(r) for r in (
        {'type': 'assistant', 'isSidechain': False, 'message': {'content': [{'type': 'text', 'text': BRIEF_TEXT}]}},
        api_error_row('API Error: Connection refused (ECONNREFUSED)', refusal=False))) + '\n')
    assert fleet.claude_api_error(str(path)) == 'API Error: Connection refused (ECONNREFUSED)'
    path.write_text(json.dumps(api_error_row(INCIDENT)) + '\n'
                    + json.dumps({'type': 'assistant', 'isSidechain': True, 'isApiErrorMessage': True,
                                  'message': {'content': [{'type': 'text', 'text': 'API Error: other'}]}}) + '\n')
    assert fleet.claude_api_error(str(path)) == INCIDENT  # a subagent's row is not the session's own error
    assert fleet.stop_failure_wall({'api_text': INCIDENT})['kind'] == 'safeguards'
    assert fleet.stop_failure_wall({'details': BRIEF_TEXT, 'error': 'invalid_request'})['kind'] == 'safeguards'
    # text = last_assistant_message can be model output: never read alone (ruling M1 (b))
    assert fleet.stop_failure_wall({'text': BRIEF_TEXT, 'error': 'invalid_request'}) is None
    assert fleet.stop_failure_wall({'error': BRIEF_TEXT}) is None  # the clear-after-result hook's error may be that text
    # no API-error row of this turn could be read yet: the text counts only with the CLI's error type behind it
    assert fleet.stop_failure_wall({'text': INCIDENT, 'error_type': 'invalid_request'})['kind'] == 'safeguards'
    assert fleet.stop_failure_wall({'text': INCIDENT, 'error_type': 'server_error'}) is None
    assert fleet.stop_failure_wall({'text': INCIDENT, 'error_type': 'invalid_request',
                                    'api_text': 'API Error: Connection refused'}) is None
    # a row of an earlier turn is not this turn's error
    path.write_text(json.dumps(api_error_row(INCIDENT, at=time.time() - 600)) + '\n')
    assert fleet.claude_api_error(str(path), since=time.time() - 60) == ''
    assert fleet.claude_api_error(str(path)) == INCIDENT
    # other wall wording through StopFailure behaves as today (ruling Q5)
    assert fleet.stop_failure_wall({'api_text': "You've reached your Fable limit"}) is None


# ------------------------------------------------------------------ required tests 1, 7, 8 (root Claude supervisor)
@pytest.mark.parametrize('supervisor', ['task', 'service'], indirect=True)
def test_root_opus_supervisor_stop_failure_safeguards_switches_to_astra(supervisor):
    f = opus(supervisor)
    lease = deliver(f)
    flagged(f)
    assert_walled_once(f, lease)
    assert_never_held(f)
    assert f.paused()
    req_ = json.loads(f.agent('supervisor')['wall_downgrade'])
    assert (req_['cli'], req_['from'], req_['to_cli'], req_['to'], req_['kind']) == (
        'claude', 'claude-opus-5-5', 'pi', ASTRA, 'safeguards')  # astra although it is above opus
    rec = json.loads(f.r.hget(fleet.WALLS, 'claude:opus'))
    assert (rec['scope'], rec['reason']) == ('model_pool', 'safeguards')
    tripped = events(f, 'brake.tripped')
    assert tripped and 'safeguards' in tripped[-1].get('reason', json.dumps(tripped[-1]))


@pytest.mark.parametrize('supervisor', ['task', 'service'], indirect=True)
def test_blocked_message_is_requeued_once_and_reaches_only_the_new_generation(supervisor, monkeypatch):
    f = opus(supervisor)
    no_preflight(f, monkeypatch)
    lease = deliver(f)
    old_pane = f.agent('supervisor')['pane']
    flagged(f)
    again = requeued(f, 'supervisor', lease['message_id'])
    assert len(again) == 1
    f.tick()  # idle boundary: the switch runs as the slot's next generation
    sup = f.agent('supervisor')
    cfg = f.runtime_cfg('supervisor')
    assert sup['generation'] == '1' and old_pane in f.mgr.killed
    assert fleet.agent_cli(cfg, 'supervisor') == 'pi' and cfg['pi_model'] == ASTRA
    assert f.v2.bus.pending('supervisor') == again, 'still queued once, nothing handed to the old session'
    ready(f)
    f.tick()
    assert not f.paused()
    f.pump()
    now = f.v2.bus.slot_state('supervisor')
    assert now.get('message_id') == again[0] and int(f.v2.bus.message(again[0])['generation']) == 1
    assert 'marker one' in f.v2.bus.message(again[0])['body']['text']
    assert len([e for e in events(f, 'brake.wall_requeued') if e.get('message_id') == lease['message_id']]) == 1


@pytest.mark.parametrize('supervisor', ['task'], indirect=True)
def test_replayed_stop_failure_event_does_not_queue_twice(supervisor):
    f = opus(supervisor)
    lease = deliver(f)
    flagged(f)
    flagged(f)  # the same hook again (replay / duplicate)
    assert len(requeued(f, 'supervisor', lease['message_id'])) == 1 and len(f.v2.bus.pending('supervisor')) == 1


@pytest.mark.parametrize('supervisor', ['task', 'service'], indirect=True)
def test_safeguards_switch_leaves_regen_log_and_never_holds_the_slot(supervisor):
    f = opus(supervisor)
    f.cfg['max_attempts'] = 1  # the old path would go straight to regeneration
    spent = json.dumps([time.time() - 20, time.time() - 10])  # two of the three regenerations already used
    f.setf('supervisor', regen_log=spent)
    deliver(f)
    flagged(f)
    for _ in range(3):
        f.v2.tick()
    assert_never_held(f)
    assert f.agent('supervisor')['regen_log'] == spent and not f.agent('supervisor').get('regen_state')


# ------------------------------------------------------------------ required tests 2, 3, 4
@pytest.mark.parametrize('supervisor', ['task'], indirect=True)
@pytest.mark.parametrize('how', ['walled', 'preflight'])
def test_astra_walled_or_refused_takes_sol(supervisor, monkeypatch, how):
    f = opus(supervisor)
    if how == 'walled':
        mark(f, 'pi', ASTRA)
    no_preflight(f, monkeypatch, refuse=(ASTRA,))
    deliver(f)
    flagged(f)
    want = SOL if how == 'walled' else ASTRA
    assert json.loads(f.agent('supervisor')['wall_downgrade'])['to'] == want
    f.tick()
    assert f.runtime_cfg('supervisor')['pi_model'] == SOL and f.agent('supervisor')['generation'] == '1'
    if how == 'preflight':
        assert json.loads(f.r.hget(fleet.WALLS, 'pi:' + ASTRA))['scope'] == 'refused'


@pytest.mark.parametrize('supervisor', ['task'], indirect=True)
def test_no_openai_entry_usable_takes_fable_and_skips_opus(supervisor):
    f = opus(supervisor)
    mark(f, 'pi', ASTRA)
    mark(f, 'pi', SOL, 'refused')
    deliver(f)
    flagged(f)
    wall = json.loads(f.agent('supervisor')['wall'])
    assert (wall['action'], wall['to_cli'], wall['to']) == ('switch', 'claude', 'fable')
    assert [s.split(' (')[0] for s in wall['skipped']] == ['pi:' + ASTRA, 'pi:' + SOL]


@pytest.mark.parametrize('supervisor', ['task'], indirect=True)
def test_nothing_usable_stops_and_audit_names_safeguards(supervisor):
    f = opus(supervisor)
    for cli, model in (('pi', ASTRA), ('pi', SOL), ('claude', 'fable'), ('pi', DS_PI)):
        mark(f, cli, model)
    lease = deliver(f)
    flagged(f)
    wall = json.loads(f.agent('supervisor')['wall'])
    assert wall['action'] == 'stop' and wall['kind'] == 'safeguards' and 'safeguards' in wall['advice']
    assert 'claude:opus (model-pool wall)' not in wall['skipped'], 'the flagged entry is not a candidate'
    assert not f.agent('supervisor').get('wall_downgrade') and f.paused()
    tripped = events(f, 'brake.tripped')
    assert tripped and 'safeguards' in json.dumps(tripped[-1])
    assert_stopped_and_kept(f, lease)


def assert_stopped_and_kept(f, lease):
    """Ruling M1 (c): queued exactly once behind the pause, not lost, never redelivered to the walled model."""
    assert_walled_once(f, lease)
    assert_never_held(f)
    for _ in range(2):
        f.v2.tick()
        f.tick()
        f.pump()
    assert f.paused() and not f.v2.bus.slot_state('supervisor').get('message_id'), 'delivered behind the pause'
    assert len(f.v2.bus.pending('supervisor')) == 1
    verdict = pause_review.wall(f.v2, pause_review.classify(f), 'supervisor', {'pause_review_wall_retry_s': 1}, 10 ** 6)
    assert verdict['action'] == 'keep' and 'safeguards' in verdict['check'], verdict


@pytest.mark.parametrize('supervisor', ['task'], indirect=True)
def test_adopted_subfleet_supervisor_stops_keeps_the_message_and_records_safeguards(supervisor):
    f = opus(supervisor)
    lease = deliver(f)
    parent_run = f.run_dir.parent / 'parent-run'
    parent_run.mkdir()
    fleet.save(f.run_dir / 'parent.json', {'run': str(parent_run)})
    f.cfg.update(adopt={'slot': 'worker-1', 'hook': True}, parent_config=str(f.run_dir / 'parent.json'))
    flagged(f)
    wall = json.loads(f.agent('supervisor')['wall'])
    assert wall['action'] == 'stop' and 'adopted' in wall['advice'] and 'safeguards' in wall['advice']
    assert json.loads(f.r.hget(fleet.WALLS, 'claude:opus'))['reason'] == 'safeguards'  # its chain entry
    assert_stopped_and_kept(f, lease)
    notices = [json.loads(x.read_text()) for x in (parent_run / 'subfleet-notices').glob('held-*.json')]
    assert len(notices) == 1 and notices[0]['reason'] == 'safeguards' and 'safeguards' in notices[0]['text']


# ------------------------------------------------------------------ required test 6 (StopFailure side)
@pytest.mark.parametrize('supervisor', ['task'], indirect=True)
def test_short_reply_quoting_the_sentence_then_an_unrelated_api_error_is_not_a_wall(supervisor):
    f = opus(supervisor)
    deliver(f)
    flagged(f, api_text='API Error: Connection refused (ECONNREFUSED)', error='invalid_request', last=BRIEF_TEXT,
            rows=[api_error_row('API Error: Connection refused (ECONNREFUSED)', refusal=False)])
    sup = f.agent('supervisor')
    assert sup['state'] == 'held' and sup.get('stopfail_at') and not f.paused()  # the API-error retry, as today
    assert not f.r.hgetall(fleet.WALLS) and not sup.get('wall_downgrade')


@pytest.mark.parametrize('supervisor', ['task'], indirect=True)
def test_incident_payload_before_its_transcript_row_is_written_is_still_a_wall(supervisor):
    f = opus(supervisor)
    lease = deliver(f)
    flagged(f, api_text=None, error='invalid_request', last=INCIDENT, rows=[])  # the CLI writes its row later
    assert_walled_once(f, lease)
    assert json.loads(f.agent('supervisor')['wall_downgrade'])['to'] == ASTRA


@pytest.mark.parametrize('supervisor', ['task'], indirect=True)
def test_a_safeguards_row_of_an_earlier_turn_is_not_this_turns_error(supervisor):
    f = opus(supervisor)
    deliver(f)
    flagged(f, api_text=None, error='server_error', last='API Error: Overloaded',
            rows=[api_error_row(INCIDENT, at=time.time() - 3600)])
    sup = f.agent('supervisor')
    assert sup['state'] == 'held' and sup.get('stopfail_at') and not f.paused()  # the API-error retry, as today
    assert not f.r.hgetall(fleet.WALLS)
