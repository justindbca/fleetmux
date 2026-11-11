"""A worker's Blocked: goes to the supervisor to rule on, not straight to BLOCKED (plan F' 8.4).

v2 (real private Redis, mixed Codex supervisor + Claude workers) for delivery-aware rules; the v1 contract
fleet (test_contracts) for a ruling recorded under its task row.
"""
import json

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, dispatch_one, mixed, submit, typed  # noqa: F401  (fixture)
from test_contracts import cf, inbox, item, report, sup, to_parallel, write  # noqa: F401
from fleetmux import fleet

KEY = 'blocked:worker-1'
STUCK = 'Done:\n- read the spec\nEvidence:\n- worker-1/notes.md\nNot done: section 3\nBlocked: need permission to change design §3'


def escalations(f):
    return fleet.Fleet.escalation_state(f)


def stuck(f, text=STUCK):
    dispatch_one(f)
    assert answer_turn(f, 'worker-1', text)['status'] == 'committed'
    f.pump()


def ruling(f, decision, note=None, dispatch=(), status='RUNNING'):
    esc = [dict({'task': KEY, 'decision': decision}, **({'note': note} if note is not None else {}))] if decision else []
    body = {'status': status, 'completion': 20, 'reason': 'r', 'dispatch': list(dispatch), 'escalations': esc}
    return answer_turn(f, 'supervisor', json.dumps(body))


def test_blocked_report_is_escalated_to_the_supervisor(mixed):
    f = mixed
    stuck(f)
    rec = escalations(f)[KEY]
    assert rec['kind'] == 'blocked' and rec['status'] == 'escalated' and 'design §3' in rec['reason']
    assert rec['message_id'] and f.v2.bus.status(rec['message_id']) == 'received'
    up = typed(f, 'supervisor')[-1]
    assert up.startswith('[worker-1 report]') and '[blocked] worker-1 says it is stuck on: need permission' in up
    assert '"task": "blocked:worker-1"' in up and f.task()['status'] == 'running'


@pytest.mark.parametrize('blocked', ['none', 'None.', '-', '无', 'n/a'])
def test_blocked_none_is_not_escalated(mixed, blocked):
    f = mixed
    stuck(f, STUCK.replace('need permission to change design §3', blocked))
    assert KEY not in escalations(f) and '[blocked]' not in typed(f, 'supervisor')[-1]


@pytest.mark.parametrize('decision,note', [('approve', 'only §3.2'), ('revise', 'keep §3, add §3a'), ('reject', 'skip it')])
def test_ruling_goes_to_the_worker(mixed, decision, note):
    f = mixed
    stuck(f)
    assert ruling(f, decision, note)['status'] == 'committed'
    assert f.task()['bad_replies'] == '0' and KEY not in escalations(f)
    f.pump()
    got = typed(f, 'worker-1')[-1]
    assert got.startswith('[Supervisor ruling] [M0] ' + decision + ': ' + note)  # §44: the auto id names the item and 'need permission' in got
    assert any(a.get('event') == 'blocked.ruled' for _, a in f.r.xrange(f.v2.bus.audit_key))


def test_revise_without_a_note_is_a_bad_reply(mixed):
    f = mixed
    stuck(f)
    ruling(f, 'revise')
    assert f.task()['bad_replies'] == '1' and escalations(f)[KEY]['status'] == 'escalated'


def test_dispatching_to_the_worker_answers_it(mixed):
    f = mixed
    stuck(f)
    ruling(f, None, dispatch=[{'to': 'worker-1', 'instruction': 'Write worker-1/part2.md without touching §3.'}])
    assert f.task()['bad_replies'] == '0' and KEY not in escalations(f)
    f.pump()
    assert not typed(f, 'worker-1')[-1].startswith('[Supervisor ruling]')


def test_a_consult_on_the_worker_answers_it(mixed):
    f = mixed
    submit(f)
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'r', 'dispatch': [
        {'to': 'worker-1', 'id': 'M0', 'instruction': 'Write worker-1/part.md with the findings.'}]}))
    f.pump()
    assert answer_turn(f, 'worker-1', STUCK)['status'] == 'committed'
    f.pump()
    assert escalations(f)[KEY]['item'] == 'M0'
    body = {'status': 'RUNNING', 'completion': 20, 'reason': 'r', 'dispatch': [],
            'consult': [{'worker': 'worker-1', 'task': 'M0', 'finding': 'cannot fetch the page twice'}]}
    answer_turn(f, 'supervisor', json.dumps(body))
    assert f.task()['bad_replies'] == '0' and KEY not in escalations(f)


def test_answer_and_dispatch_together_the_dispatch_answers_it(mixed):
    # Was a bad reply; a rotated supervisor did this twice and the strikes blocked a run (2026-10-28).
    f = mixed
    stuck(f)
    ruling(f, 'approve', 'go', dispatch=[{'to': 'worker-1', 'instruction': 'Write worker-1/part2.md please now.'}])
    assert f.task()['bad_replies'] == '0' and KEY not in escalations(f)
    f.pump()
    got = typed(f, 'worker-1')[-1]
    assert 'Write worker-1/part2.md please now.' in got and not got.startswith('[Supervisor ruling]')
    assert not any('[Supervisor ruling]' in x for x in typed(f, 'worker-1'))


def test_correction_goes_before_reports_already_queued_for_the_supervisor(mixed):
    # Queued behind them it arrived stale, about a reply already replaced (2026-10-28, the third strike).
    f = mixed
    stuck(f)
    f.queue('supervisor', '[worker-2 report] a later report that arrived meanwhile')
    ruling(f, 'revise')  # revise without a note: a bad reply
    assert f.task()['bad_replies'] == '1'
    f.pump()
    got = typed(f, 'supervisor')[-1]
    assert 'not a valid decision JSON' in got and 'a later report' not in got


def test_unanswered_blocked_is_sent_back_once_without_a_strike_then_counts(mixed):
    f = mixed
    stuck(f)
    ruling(f, None, dispatch=[{'to': 'worker-2', 'instruction': 'Write worker-2/part.md with the findings.'}])
    assert f.task()['bad_replies'] == '0' and escalations(f)[KEY]['nudged'] == 1
    f.pump()
    assert 'answer ' + KEY + ' first' in typed(f, 'supervisor')[-1]
    ruling(f, None, dispatch=[{'to': 'worker-2', 'instruction': 'Write worker-2/part.md with the findings.'}])
    assert f.task()['bad_replies'] == '1' and KEY in escalations(f)


def test_blocked_reply_still_ends_the_task(mixed):
    f = mixed
    stuck(f)
    ruling(f, None, status='BLOCKED')
    assert f.task()['status'] == 'blocked'


def test_contract_ruling_is_recorded_under_its_row_and_the_next_report_closes_it(cf):
    to_parallel(cf)
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [item('M0', 'worker-1', provides=['IF-M0-src'])]})
    cf.r.l.clear()
    cf.handle(fleet_turn('worker-1', 'Done:\n- half\nEvidence:\n- worker-1/x\nNot done: rest\n'
                                     'Blocked: the source site needs a login'))
    rec = cf.escalation_state()[KEY]
    assert rec['item'] == 'M0' and '[blocked] worker-1' in inbox(cf, 'supervisor')[-1]
    sup(cf, {'status': 'RUNNING', 'completion': 20, 'dispatch': [],
             'escalations': [{'task': KEY, 'decision': 'revise', 'note': 'use the mirror at b.example'}]})
    assert cf.task()['bad_replies'] == '0', inbox(cf, 'supervisor')
    msg = inbox(cf, 'worker-1')[-1]
    assert '[Supervisor ruling] [M0] revise: use the mirror at b.example' in msg
    state = cf.contract_state()
    assert state['tasks']['M0']['status'] == 'running' and state['owners']['worker-1'][-1] == 'M0'
    write(cf, 'worker-1/sources.md', '## Sources\n| https://a |\n')
    report(cf, 'worker-1', 'Interfaces: IF-M0-src@1 provided: ready')
    assert cf.contract_state()['tasks']['M0']['status'] == 'done'


def fleet_turn(name, text):
    from test_fleet import turn
    return turn(name, text)


def test_a_ruling_for_an_earlier_generation_is_dropped_as_stale(mixed):
    """Live trial fleet-20261108-205242-43db976b, 20:58:49: a ruling on what worker-1 g0 reported as Blocked was
    queued after the slot had moved on (g1 adopted as the sub-fleet supervisor) and reached worker-1.2 after the
    return, which had no context, reported Blocked and cost an extra "COMPLETED refused" round. A task-mode ruling
    is bound to the generation and label that asked, like a service ruling to its request: stale -> blocked.stale,
    never delivered."""
    f = mixed
    stuck(f)
    rec = escalations(f)[KEY]
    assert rec['generation'] == '0' and rec['label'] == 'worker-1'
    f.setf('worker-1', generation=1, label='worker-1.1')  # the slot moved on before the supervisor ruled
    assert ruling(f, 'reject', 'skip that line')['status'] == 'committed'
    assert f.task()['bad_replies'] == '0' and KEY not in escalations(f)
    assert not [m for m in f.v2.bus.pending('worker-1')
                if '[Supervisor ruling]' in f.v2.bus.message(m)['body']['text']]
    stale = [a for _, a in f.r.xrange(f.v2.bus.audit_key) if a.get('event') == 'blocked.stale']
    assert stale and stale[-1]['task'] == KEY and not any(
        a.get('event') == 'blocked.ruled' for _, a in f.r.xrange(f.v2.bus.audit_key))
