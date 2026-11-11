"""P7 (brief v2.1 #3): a service request too big for one worker becomes a task sub-fleet, on the §44 machinery.

The worker answers its request with the §44 escalate shape ({"escalate", "proposal", "workers"}); the program numbers
it M<n> (only then, Q-A), the supervisor answers it like any §44 split (approve with models / revise / reject), and the
existing approve -> rotate -> spawn -> adopt -> collect path runs. P7 adds only what service mode lacks: the intake,
the original request settled ``ok + delegated`` when the spawn is accepted, the ``<id>.done`` follow-up result,
``split_budget_h``, ``lifecycle=None``, request ownership / staleness, and the accepted-only settle before a resume
replays requests (S11).

Plan: split-1 v21-test-map.md / v21-approved-names.tsv (supervisor [M6], approved). Red (A2) until P7a / P7b land;
the two [baseline] tests (#11, #20) pass today. Only private test Redis, fake cmux, fake CLIs.
"""
import json
import time
import uuid
from pathlib import Path

import pytest

from test_escalate import ok_check  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_service_escalate import BKEY, DKEY, audits, escalations, request, result, supervisor_says, svc  # noqa: F401
from test_subfleet import (adopt_by_hook, child_daemon, die, given, hook, no_launch, spawn_m0, start_tree, step,
                           stop_child_like_daemon, tree)  # noqa: F401
from test_v2_codex_claude import answer_turn, typed
from fleetmux import fleet, runtime_v2, subfleet
from fleetmux.channels import envelope

ROOT = Path(__file__).resolve().parents[1]
PROPOSAL = [{'instruction': 'Fetch sources one to three into part-a.md', 'output': 'part-a.md', 'kind': 'research'},
            {'instruction': 'Fetch sources four and five into part-b.md', 'output': 'part-b.md', 'kind': 'research'}]
STATUSES = {'ok', 'invalid', 'skipped', 'timeout', 'stalled', 'failed', 'blocked', 'rejected'}


def split_value(reason='this request is too big: five sources to fetch and merge', proposal=None, **extra):
    return dict({'escalate': reason, 'proposal': [dict(p) for p in (PROPOSAL if proposal is None else proposal)], 'workers': 2}, **extra)


# ------------------------------------------------------------------ shared helpers
def items(f):
    return json.loads(f.r.get(fleet.ITEMS) or '{}')


def req(f, rid='r1'):
    return f.r.hgetall(fleet.REQ + rid)


def bad(f):
    return f.r.hget(fleet.SERVICE, 'bad_replies') or '0'


def mid_of(f, rid):
    """The M<n> the program gave request ``rid`` (Q-A), '' when none."""
    return next((k for k, v in items(f).items() if v.get('request') == rid), '')


def opened(f, rid='r1', slot='worker-1'):
    """The split record of ``rid``, asserted open (escalated) and owned by ``slot`` -- the first red point in A2."""
    key = mid_of(f, rid)
    rec = escalations(f).get(key) if key else None
    assert rec and rec['status'] == 'escalated' and rec['request'] == rid and rec['slot'] == slot, (key, escalations(f))
    return key, rec


def queued_to(f, slot, needle):
    """Messages queued to ``slot`` whose text has ``needle`` (from the v2 'queued' audit rows)."""
    out = []
    for x in audits(f, 'queued'):
        if x.get('slot') == slot and needle in (x.get('detail') or ''):
            out.append(f.v2.bus.message(x['message_id']))
    return out


def results(f):
    return [json.loads(x) for x in f.r.lrange(fleet.RESULTS, 0, -1)]


def result_ids(f):
    return [x.get('id') for x in results(f)]


def sf_files(f):
    folder = f.run_dir / 'subfleet-requests'
    return sorted(p for p in folder.glob('sf-*.json') if not p.name.endswith('.answer.json')) if folder.exists() else []


def sf_for(f, rid):
    return [fleet.load(p) for p in sf_files(f) if fleet.load(p).get('split') == rid]


def sub_runs(f):
    folder = f.run_dir / 'sub'
    return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.exists() else []


def last_audit(f):
    rows = f.r.xrevrange(f.v2.bus.audit_key, count=1)
    return rows[0][0] if rows else '0-0'


def audits_since(f, since, event=None):
    return [x for i, x in f.r.xrange(f.v2.bus.audit_key, min=since) if i != since
            and (event is None or x.get('event') == event)]


def routing_prompt(prompts, rid):
    """The supervisor's routing prompt for ``rid`` (v2 drops the '[Request <id>]' prefix: match its payload)."""
    needles = ('big job ' + rid + '"',) + (('what is in notes?',) if rid == 'r1' else ())
    return next(p for p in reversed(prompts) if 'needs routing' in p and any(x in p for x in needles))


# ------------------------------------------------------------------ svc (one service fleet, Codex supervisor)
def answer_open_supervisor_note(f):
    """Test harness. A split [escalate] note (and a refusal note) is its own supervisor delivery bound to the split
    request (#2 pins this). A scenario that next routes another request first answers that open note with an empty
    RUNNING decision, as a supervisor that has nothing to rule yet does; a routing delivery is left alone."""
    for _ in range(4):
        f.pump()
        if f.r.hget(f.v2.bus.lease_key('supervisor'), 'status') != 'received':
            return
        if 'needs routing' in (typed(f, 'supervisor') or [''])[-1]:
            return
        body = {'status': 'RUNNING', 'reason': 'nothing to rule yet', 'dispatch': [], 'skip': []}
        assert answer_turn(f, 'supervisor', json.dumps(body))['status'] == 'committed'


def route_svc(f, rid, to='worker-1', spec=None):
    """Submit ``rid`` and let the supervisor route it to ``to`` (its own routing prompt, not the latest one)."""
    spec = spec or {'id': rid, 'payload': {'question': 'big job ' + rid}, 'reply': {'required': ['answer']}}
    answer_open_supervisor_note(f)
    runtime_v2.post_event(f.v2.bus, dict(kind='request', request_id='cmd-' + rid, spec=spec), 'cmd-' + rid)
    assert f.v2.step(10)
    f.pump()
    prompt = routing_prompt(typed(f, 'supervisor'), rid)
    body = {'status': 'RUNNING', 'reason': 'route ' + rid, 'skip': [],
            'dispatch': [{'to': to, 'request_id': rid, 'instruction': 'Answer the question from the payload of ' + rid + '.'}]}
    assert answer_turn(f, 'supervisor', json.dumps(body), prompt=prompt)['status'] == 'committed'
    f.pump()
    assert f.agent(to)['state'] == 'busy' and f.agent(to)['request'] == rid


def ask_split(f, slot='worker-1', value=None):
    out = answer_turn(f, slot, json.dumps(value if value is not None else split_value()))
    assert out['status'] == 'committed'


def asked(f):
    request(f)
    ask_split(f)


def replay_svc(f):
    """Test harness: the service branch of `start --resume`, step by step as runtime_v2.resume_run does it, on this
    fixture's fakes. Every slot's lease and queue are released (clear_slot). A worker that resume would reattach
    stays (idle); a hooked worker with a turn in flight is not reattachable and starts its next generation (respawn,
    READY), so a cancelled delivery never stands in front of the replay. Then every open request is requeued once.
    The fixture's fake Codex session cannot bring a respawned supervisor to READY, so the supervisor first answers
    any open note and is cleared in place instead of respawned."""
    answer_open_supervisor_note(f)
    f.mgr.has_cli_process = lambda pane, names=('codex',): pane in f.mgr.alive  # what resume_run asks the pane manager
    f.mgr.tab_panes = lambda: {}
    f.mgr.dock_control = lambda pane: True
    fresh = [w for w in f.workers if not f.v2.reattachable(w)]
    for slot in f.names:
        assert f.v2.clear_slot(slot, slot in f.workers and slot not in fresh)
    for slot in [w for w in f.workers if w not in fresh] + ['supervisor']:
        f.setf(slot, state='idle', bridge_available=0, busy_since=time.time(), report_retries=0)
    if fresh:
        f.v2.respawn(fresh, True)
        for slot in fresh:
            assert answer_turn(f, slot, 'READY', startup=True)['status'] == 'ready'
    # v2 transactions are idempotent by id: a fixed id would make every replay after the first a no-op
    f.v2.transaction('resume-requests-test-' + uuid.uuid4().hex, lambda: f.v2.requeue_requests())
    f.pump()


def reroute_svc(f, rid, to, escalations_=()):
    f.pump()
    prompt = routing_prompt(typed(f, 'supervisor'), rid)
    body = {'status': 'RUNNING', 'reason': 'route again', 'skip': [], 'escalations': list(escalations_),
            'dispatch': [{'to': to, 'request_id': rid, 'instruction': 'Answer the question from the payload of ' + rid + '.'}]}
    assert answer_turn(f, 'supervisor', json.dumps(body), prompt=prompt)['status'] == 'committed'
    f.pump()


def answer_split(f, key, decision, **extra):
    return supervisor_says(f, [dict({'task': key, 'decision': decision}, **extra)])


# ------------------------------------------------------------------ split_tree (two real v2 fleets, service parent)
@pytest.fixture
def split_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, cli_change_ok=False: None)
    yield from start_tree(tmp_path, monkeypatch, mode='service', lifecycle='service', max_attempts=2)


def tree_answer_open_supervisor_note(t):
    """Test harness (split_tree): like answer_open_supervisor_note, the supervisor answers an open [escalate] /
    refusal note with an empty RUNNING decision before the next routing prompt is looked up."""
    f = t.f
    for _ in range(4):
        step(t)
        if f.r.hget(f.v2.bus.lease_key('supervisor'), 'status') != 'received':
            return
        if 'needs routing' in (given(t, f, 'supervisor') or [''])[-1]:
            return
        body = {'status': 'RUNNING', 'reason': 'nothing to rule yet', 'skip': [], 'dispatch': []}
        assert hook(t, f, 'supervisor', json.dumps(body), prompt=given(t, f, 'supervisor')[-1])['status'] == 'committed'


def route(t, rid='r1', to='worker-1', spec=None):
    f = t.f
    tree_answer_open_supervisor_note(t)
    spec = spec or {'id': rid, 'payload': {'question': 'big job ' + rid}, 'reply': {'required': ['answer']}}
    runtime_v2.post_event(f.v2.bus, dict(kind='request', request_id='cmd-' + rid, spec=spec), 'cmd-' + rid)
    step(t)
    prompt = routing_prompt(given(t, f, 'supervisor'), rid)
    body = {'status': 'RUNNING', 'reason': 'route ' + rid, 'skip': [],
            'dispatch': [{'to': to, 'request_id': rid, 'instruction': 'Answer the question from the payload of ' + rid + '.'}]}
    assert hook(t, f, 'supervisor', json.dumps(body), prompt=prompt)['status'] == 'committed'
    step(t)
    assert f.r.hget(fleet.REQ + rid, 'worker') == to


def tree_answer(t, slot, value):
    f = t.f
    out = hook(t, f, slot, json.dumps(value), prompt=given(t, f, slot)[-1])
    step(t)
    return out


def tree_rule(t, escalations_=(), dispatch=()):
    f = t.f
    step(t)
    body = {'status': 'RUNNING', 'reason': 'ruling', 'skip': [], 'dispatch': list(dispatch),
            'escalations': list(escalations_)}
    out = hook(t, f, 'supervisor', json.dumps(body), prompt=given(t, f, 'supervisor')[-1])
    step(t)
    return out


def split_up(t, rid='r1', slot='worker-1', **approve):
    """Route ``rid`` to ``slot``, the worker asks to split it, the supervisor approves. Returns the M id."""
    route(t, rid, slot)
    tree_answer(t, slot, split_value())
    key, _ = opened(t.f, rid, slot)
    assert tree_rule(t, [dict({'task': key, 'decision': 'approve'}, **approve)])['status'] == 'committed'
    return key


def delegate(t, rid='r1', slot='worker-1'):
    """split_up + the daemon tick that spawns the sub-fleet and reads the accepted answer."""
    key = split_up(t, rid, slot)
    t.f.tick()
    step(t)
    return key


def ready_again(t):
    """A slot handed back by a sub-fleet boots its next generation: answer its startup READY."""
    f = t.f
    for slot in f.workers:
        if f.agent(slot).get('state') == 'starting':
            assert hook(t, f, slot, 'READY', prompt='startup')['status'] == 'ready'
    step(t)


def finish_child(t, key, outcome='completed', monkeypatch=None):
    """The child daemon of sub-fleet ``key`` runs, ends with ``outcome`` and is collected by the parent.
    ``cancelled``: the child daemon is gone without a result.json."""
    f = t.f
    rec = subfleet.record(f.run_dir / 'sub' / key)
    # §48: the role change reaches the adopted pane through its waiting Stop hook (never typed), as in test_subfleet
    cf, _ = adopt_by_hook(t, monkeypatch or t.monkeypatch, rec)
    step(t)
    if outcome == 'cancelled':
        monkeypatch.setattr(subfleet, 'LAUNCH_GRACE', 0)
        die(cf)
        subfleet.shutdown_redis(cf.cfg)
        f.tick()
        f.tick()
    else:
        cf.v2.finish(outcome, 'test end: ' + outcome)
        cf.write_result()
        f.tick()
        stop_child_like_daemon(cf)
        f.tick()
    step(t)
    ready_again(t)


def restart(t):
    """The parent daemon process dies and comes back with `start --resume` (in process)."""
    f = t.f
    f.v2.close()
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=t.sessions)
    t.fleets[t.fleets.index(f)] = again
    t.f = again
    again.resume_requested = True
    return again


def capture_child_specs(monkeypatch):
    """The task spec each spawned child is given (spawn_child posts it on the child's bus)."""
    specs = []
    real = runtime_v2.post_event

    def post(bus, event, key):
        if event.get('kind') == 'task':
            specs.append(event['spec'])
        return real(bus, event, key)
    monkeypatch.setattr(runtime_v2, 'post_event', post)
    return specs


# ================================================================== P7a: intake + supervisor answer
def test_worker_escalate_proposal_records_split_request_stays_dispatched(svc, ok_check):  # 1
    f = svc
    asked(f)
    assert result(f) is None
    assert req(f)['status'] == 'dispatched' and req(f)['worker'] == 'worker-1'
    rec = escalations(f).get('M0')
    assert rec, escalations(f)
    assert rec['slot'] == 'worker-1' and rec['request'] == 'r1' and rec['status'] == 'escalated' and rec['workers'] == 2
    assert [p['id'] for p in rec['proposal']] == ['M0.0', 'M0.1']
    assert 'Answer the question from the payload.' in rec['instruction']
    row = items(f)['M0']
    assert row['to'] == 'worker-1' and row['request'] == 'r1'
    assert f.agent('worker-1')['state'] == 'idle' and bad(f) == '0'
    assert audits(f, 'escalate.asked')
    assert [x for x in audits(f, 'split.asked') if x.get('request_id') == 'r1']


def test_supervisor_receives_split_message_with_answer_format(svc, ok_check):  # 2
    f = svc
    asked(f)
    opened(f)
    msgs = queued_to(f, 'supervisor', 'asks to split M0')
    assert len(msgs) == 1 and msgs[0]['request_id'] == 'r1' and msgs[0]['kind'] == 'followup'
    text = msgs[0]['body']['text']
    for needle in ('[escalate] worker-1 asks to split M0 into 2 pieces run by a sub-fleet of 2 workers', '"task": "M0"',
                   'approve', 'supervisor_profile', 'Do not dispatch to worker-1 in the reply that approves',
                   '(request r1)'):
        assert needle in text, needle


def test_bad_proposal_or_limits_refused_to_both(svc, ok_check, monkeypatch):  # 3
    f = svc
    request(f)
    limit = 'max_children 3: this fleet already runs M1, M2, M3'
    cases = [(split_value(proposal=[]), 'proposal must be a non-empty list'),
             (split_value(proposal=[{'instruction': 'too short', 'output': 'x.md'}]), 'instruction (>= 12 chars)'),
             (split_value(workers=0), 'workers'),
             (split_value(), limit)]
    for n, (value, why) in enumerate(cases):
        if n == 3:
            monkeypatch.setattr(subfleet, 'check', lambda *a, **k: limit)
        ask_split(f, value=value)
        assert not [k for k in escalations(f) if k.startswith('M')] and not mid_of(f, 'r1'), (why, escalations(f))
        assert req(f).get('status') == 'dispatched' and req(f).get('worker') == 'worker-1'
        f.pump()
        got = typed(f, 'worker-1')[-1]
        assert '[escalate] your split request was refused by the program: ' in got and why in got, got
        assert 'Payload (JSON)' in got and 'yourself' in got
        assert queued_to(f, 'supervisor', '[escalate] refused for worker-1: ')
        assert bad(f) == '0'


def test_reply_keys_conflicting_with_split_keys_not_treated_as_split(svc, ok_check):  # 4
    v = split_value()
    assert fleet.service_split(v, {'required': ['answer'], 'allowed': ['answer', 'escalate', 'proposal']}) == ''
    assert fleet.service_split(v, {'required': ['answer']})
    assert fleet.service_split(dict(v, answer='x'), {'required': ['answer']}) == ''
    f = svc
    own = {'id': 'r1', 'payload': {'question': 'big job r1'},
           'reply': {'required': ['escalate'], 'allowed': ['escalate', 'proposal', 'workers']}}
    route_svc(f, 'r1', spec=own)
    ask_split(f)
    assert result(f)['status'] in ('ok', 'invalid') and not mid_of(f, 'r1')


def test_split_judge_off_refuses(svc, ok_check):  # 5
    f = svc
    base = fleet.load(f.path)
    for x in ('worker', 'off'):
        runtime_v2.validate_config(dict(base, split_judge=x))
    runtime_v2.validate_config({k: v for k, v in base.items() if k != 'split_judge'})
    for x in ('supervisor', '', 1, None):
        with pytest.raises(ValueError, match='split_judge must be worker or off'):
            runtime_v2.validate_config(dict(base, split_judge=x))
    f.cfg['split_judge'] = 'off'
    asked(f)
    assert not mid_of(f, 'r1') and not [k for k in escalations(f) if k.startswith('M')]
    f.pump()
    got = typed(f, 'worker-1')[-1]
    assert 'split_judge' in got and 'off' in got and 'Payload (JSON)' in got
    assert queued_to(f, 'supervisor', 'split_judge') and bad(f) == '0'
    f.cfg.pop('split_judge')  # unset = worker: the #1 path
    # the refusal left r1 with worker-1 to answer itself: it does, so the next request can go to worker-1
    assert answer_turn(f, 'worker-1', json.dumps({'answer': 'done'}))['status'] == 'committed'
    route_svc(f, 'r2')
    ask_split(f)
    opened(f, 'r2')


def test_parse_service_decision_allows_m_ids_keeps_dialog_blocked_rejects_others():  # 6
    workers = ['worker-1', 'worker-2']
    ok = {'status': 'RUNNING', 'dispatch': [], 'skip': []}

    def parse(*items_):
        return fleet.parse_service_decision(json.dumps(dict(ok, escalations=list(items_))), workers)
    for task in ('M0', 'M0.1'):
        assert parse({'task': task, 'decision': 'approve'})['escalations'][0]['task'] == task
    assert parse({'task': DKEY, 'decision': 'approve'}, {'task': BKEY, 'decision': 'revise', 'note': 'x'})
    for task in ('split:r1', 'approve:x', 'fx:channel:approve:p-1'):
        with pytest.raises(ValueError, match='M<n>'):
            parse({'task': task, 'decision': 'approve'})
    with pytest.raises(ValueError, match='supervisor_profile must be an agent card id'):
        parse({'task': 'M0', 'decision': 'approve', 'supervisor_profile': 5})


def test_approve_same_reply_dispatch_to_that_worker_is_format_error(svc, ok_check):  # 7
    f = svc
    asked(f)
    key, _ = opened(f)
    answer_open_supervisor_note(f)  # the approve below is given on r2's routing delivery (v2 binds a reply to it)
    spec = {'id': 'r2', 'payload': {'question': 'small job'}, 'reply': {'required': ['answer']}}
    runtime_v2.post_event(f.v2.bus, dict(kind='request', request_id='cmd-r2', spec=spec), 'cmd-r2')
    assert f.v2.step(10)
    supervisor_says(f, [{'task': key, 'decision': 'approve'}],
                    dispatch=[{'to': 'worker-1', 'request_id': 'r2', 'instruction': 'Answer the small question now.'}])
    assert bad(f) == '1'
    f.pump()
    assert 'approve ' + key + ' hands worker-1 over to a sub-fleet; do not dispatch to it in the same reply' \
        in typed(f, 'supervisor')[-1]
    assert escalations(f)[key]['status'] == 'escalated' and not sf_files(f)


def test_revise_returns_to_worker_as_same_request_record_cleared(svc, ok_check):  # 8
    f = svc
    asked(f)
    key, _ = opened(f)
    assert answer_split(f, key, 'revise', note='split by source type instead')['status'] == 'committed'
    assert bad(f) == '0' and key not in escalations(f) and key not in items(f)
    f.pump()
    got = typed(f, 'worker-1')[-1]
    assert '[escalate] the supervisor asks you to revise your split of ' + key + ': split by source type instead' in got
    assert 'Payload (JSON)' in got and 'AGENTS.md format' not in got
    assert req(f)['status'] == 'dispatched' and req(f)['worker'] == 'worker-1'
    assert answer_turn(f, 'worker-1', json.dumps({'answer': 'done'}))['status'] == 'committed'
    assert result(f)['status'] == 'ok' and result(f)['worker'] == 'worker-1'
    route_svc(f, 'r2')  # Q-B: a split revise needs no note (§44), never a bad reply
    ask_split(f)
    key2, _ = opened(f, 'r2')
    assert answer_split(f, key2, 'revise')['status'] == 'committed'
    assert bad(f) == '0' and key2 not in escalations(f)
    f.pump()
    assert 'revise your split of ' + key2 + ': -' in typed(f, 'worker-1')[-1]


def test_reject_returns_to_worker_record_cleared(svc, ok_check):  # 9
    f = svc
    asked(f)
    key, _ = opened(f)
    f.setf('worker-1', split_restore=json.dumps({'launch': ''}))  # as if an approve with models had rotated it
    assert answer_split(f, key, 'reject', note='answer it yourself')['status'] == 'committed'
    assert bad(f) == '0' and key not in escalations(f) and key not in items(f)
    info = f.agent('worker-1')
    assert not info.get('split_restore') and json.loads(info['split_launch']) == {'launch': ''}
    f.pump()
    got = typed(f, 'worker-1')[-1]
    assert 'rejected splitting ' + key in got and 'answer it yourself' in got and 'Payload (JSON)' in got
    assert 'AGENTS.md format' not in got
    assert answer_turn(f, 'worker-1', json.dumps({'answer': 'done'}))['status'] == 'committed'
    assert result(f)['status'] == 'ok'


def test_second_escalate_same_request_replaces_pending_refuses_after_approve(svc, ok_check):  # 10
    f = svc
    asked(f)
    key, first = opened(f)
    # (1) Q-C: the request comes back to its current owner, which sends a new proposal: same M id, replaced
    replay_svc(f)
    reroute_svc(f, 'r1', 'worker-1')
    other = [{'instruction': 'Fetch every source into one table at sources.md', 'output': 'sources.md'},
             {'instruction': 'Merge the sources table into the final report', 'output': 'report.md'}]
    ask_split(f, value=split_value(proposal=other))
    assert [k for k in escalations(f) if k.startswith('M')] == [key] and mid_of(f, 'r1') == key
    rec = escalations(f)[key]
    assert rec['status'] == 'escalated' and [p['output'] for p in rec['proposal']] == ['sources.md', 'report.md']
    assert len(queued_to(f, 'supervisor', 'asks to split ' + key)) == 2
    # (2)(3) approved / rotating / delegated: never overwritten, never a second spawn. r1 stays dispatched to its
    # owner (an approve does not move the request); no replay + re-route here: main's 957ca1e refuses a dispatch to a
    # slot whose approved / rotating split holds it, so the owner's second escalate comes while r1 is still with it.
    for n, status in enumerate(('approved', 'rotating', 'delegated')):
        esc = escalations(f)
        esc[key] = dict(esc[key], status=status, rid='sf-test')
        f.save_escalations(esc)
        before = escalations(f)[key]
        assert req(f)['status'] == 'dispatched' and req(f)['worker'] == 'worker-1'
        if n == 0:  # worker-1 holds no delivery: its second escalate goes straight to the intake (as #24 (4))
            f.service_split_intake('worker-1', 'r1', split_value())
        else:  # worker-1 answers the previous refusal (r1 handed back to it) with the escalate shape again
            ask_split(f)
        assert escalations(f)[key] == before and not sf_files(f) and not sub_runs(f)
        f.pump()
        got = typed(f, 'worker-1')[-1]
        assert 'Payload (JSON)' in got and '[escalate]' in got and 'is already ' + status in got
        refused = [x for x in audits(f, 'split.duplicate_refused') if status in json.dumps(x)]
        assert refused, status


def test_runtime_settle_branch_untouched_for_blocked_and_normal_answers(svc):  # 11 [baseline]
    f = svc
    request(f)
    assert answer_turn(f, 'worker-1', json.dumps({'answer': 'the notes say hi'}))['status'] == 'committed'
    assert result(f)['status'] == 'ok'
    route_svc(f, 'r2')
    assert answer_turn(f, 'worker-1', json.dumps({'wrong': 'shape'}))['status'] == 'committed'
    assert result(f, 'r2')['status'] == 'invalid'
    route_svc(f, 'r3')
    assert answer_turn(f, 'worker-1', json.dumps({'blocked': 'the wiki needs a login'}))['status'] == 'committed'
    assert result(f, 'r3') is None and escalations(f)[BKEY]['kind'] == 'blocked'
    assert set(escalations(f)) == {BKEY}
    assert not [k for k, v in items(f).items() if 'request' in v]


# ================================================================== P7b: spawn, settle, follow-up, collect
def test_approve_requests_spawn_with_hours_4_default_and_roster_override(split_tree):  # 12
    t, f = split_tree, split_tree.f
    key = split_up(t)
    assert subfleet.limits(f.cfg)['split_budget_h'] == 4
    (spec,) = sf_for(f, 'r1')
    assert spec['hours'] == 4 and spec['split'] == 'r1' and 'lifecycle' in spec and spec['lifecycle'] is None
    assert spec['worker_profiles'] == []
    rec = escalations(f)[key]
    assert rec['status'] == 'approved' and rec['rid'] and req(f)['status'] == 'dispatched'
    path = f.run_dir / 'roster.yaml'
    path.write_text('workers:\n  - name: worker-1\n  - name: worker-2\nlimits:\n  split_budget_h: 2\n')
    f.cfg['roster'] = str(path)
    f.roster_tick()
    assert subfleet.limits(f.cfg)['split_budget_h'] == 2
    split_up(t, 'r2', 'worker-2')
    (spec2,) = sf_for(f, 'r2')
    assert spec2['hours'] == 2


def test_spawn_accepted_settles_original_ok_with_delegated(split_tree):  # 13
    t, f = split_tree, split_tree.f
    key = split_up(t)
    rid = escalations(f)[key]['rid']
    # a task-style approved record (no request) next to it: the §44 path, never a publish
    esc = escalations(f)
    esc['M7'] = {'slot': 'worker-2', 'status': 'approved', 'rid': 'sf-task', 'workers': 1, 'proposal': [],
                 'instruction': 'task-mode work', 'reason': 'x'}
    f.save_escalations(esc)
    fleet.save(subfleet.answer_path(f.run_dir, 'sf-task'), {'status': 'accepted', 'run': str(f.run_dir / 'nowhere')})
    f.tick()
    step(t)
    assert fleet.load(subfleet.answer_path(f.run_dir, rid))['status'] == 'accepted'
    got = result(f)
    assert got['status'] == 'ok' and got['worker'] == 'worker-1'
    assert got['result']['delegated'] == key and key in got['result']['reply'] and 'r1.done' in got['result']['reply']
    rec = escalations(f)[key]
    assert rec['status'] == 'delegated' and rec['run'] and f.agent('worker-1')['state'] == 'delegated'
    assert result_ids(f)[-1] == 'r1'
    assert not [x for x in results(f) if (x.get('result') or {}).get('delegated') == 'M7']
    f.tick()
    step(t)
    assert result_ids(f).count('r1') == 1  # tombstone: never published twice


def test_spawn_rejected_returns_to_pending_request_still_dispatched(split_tree):  # 14
    t, f = split_tree, split_tree.f
    key = split_up(t)
    rid = escalations(f)[key]['rid']
    f.setf('worker-1', state='busy')  # no longer idle when the spawn runs
    f.tick()
    step(t)
    assert fleet.load(subfleet.answer_path(f.run_dir, rid))['status'] == 'rejected'
    assert escalations(f)[key]['status'] == 'escalated' and req(f)['status'] == 'dispatched'
    assert result(f) is None and result(f, 'r1.done') is None
    texts = [m['body']['text'] for m in queued_to(f, 'supervisor', 'could not start')]
    assert texts and '[escalate] sub-fleet ' + key + ' could not start:' in texts[-1]
    assert key + ' waits for your answer again' in texts[-1]
    assert not sub_runs(f)


def test_request_timeout_while_pending_marks_split_stale_no_spawn(svc, ok_check, monkeypatch):  # 15
    f = svc
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    # a) pending (escalated) and the request times out: swept on the next tick
    asked(f)
    key, _ = opened(f)
    f.v2.transaction('t-pub-1', lambda: f.v2.publish('r1', 'timeout', worker='worker-1', error='too slow'))
    f.tick()
    assert key not in escalations(f) and key not in items(f) and audits(f, 'split.stale')
    # b) approved, the request times out before the spawn runs: the spawn is refused, nothing starts
    route_svc(f, 'r2')
    ask_split(f)
    key, _ = opened(f, 'r2')
    answer_split(f, key, 'approve')
    rid = escalations(f)[key]['rid']
    f.v2.transaction('t-pub-2', lambda: f.v2.publish('r2', 'timeout', worker='worker-1', error='too slow'))
    f.tick()
    answer = fleet.load(subfleet.answer_path(f.run_dir, rid))
    assert answer['status'] == 'rejected' and 'no longer open' in answer['reason']
    assert not sub_runs(f) and f.agent('worker-1')['state'] != 'delegated' and key not in escalations(f)
    # c) the request moved to worker-2 before the approve: refused by the program, no strike
    route_svc(f, 'r3')
    ask_split(f)
    key, _ = opened(f, 'r3')
    replay_svc(f)
    # the resume cancelled the [escalate] note: the approve rides on the reply that re-routes r3 (v2 needs a delivery)
    reroute_svc(f, 'r3', 'worker-2', escalations_=[{'task': key, 'decision': 'approve'}])
    assert bad(f) == '0' and not sf_for(f, 'r3')
    assert queued_to(f, 'supervisor', 'approve of ' + key + ' refused by the program: request r3 is no longer worker-1')
    assert audits(f, 'split.approve_refused')
    # d) rotating and the request ends: no rotation, record gone, the slot's own model comes back next generation
    assert answer_turn(f, 'worker-2', json.dumps({'answer': 'done'}))['status'] == 'committed'
    route_svc(f, 'r4')
    ask_split(f)
    key, rec = opened(f, 'r4')
    esc = escalations(f)
    esc[key] = dict(rec, status='rotating', spec={'slot': 'worker-1', 'task_id': key, 'goal': 'g', 'workers': 2},
                    launch='{}', supervisor_profile='claude-opus', from_generation=int(f.agent('worker-1')['generation']))
    f.save_escalations(esc)
    f.setf('worker-1', split_restore=json.dumps({'launch': ''}))
    f.v2.transaction('t-pub-4', lambda: f.v2.publish('r4', 'timeout', worker='worker-1', error='too slow'))
    f.tick()
    info = f.agent('worker-1')
    assert info['state'] != 'handoff' and key not in escalations(f)
    assert not info.get('split_restore') and json.loads(info['split_launch']) == {'launch': ''}
    assert not [x for x in result_ids(f) if x.endswith('.done')]
    before = bad(f)
    answer_split(f, key, 'approve')  # a late answer to a swept split: logged only
    assert bad(f) == before and audits(f, 'escalate.stale_answer')


def test_child_completed_publishes_done_ok_failed_otherwise(split_tree, monkeypatch):  # 16
    t = split_tree
    for n, outcome in enumerate(('completed', 'failed', 'budget_exhausted', 'cancelled')):
        rid = 'r' + str(n + 1)
        key = delegate(t, rid)
        accepted = result(t.f, rid)
        assert accepted and accepted['result']['delegated'] == key
        finish_child(t, key, outcome, monkeypatch)
        f = t.f
        assert result_ids(f).count(rid + '.done') == 1, outcome
        done = result(f, rid + '.done')
        if outcome == 'completed':
            assert done['status'] == 'ok'
            assert {'reply', 'delegated', 'run', 'outcome'} <= set(done['result'])
        else:
            assert done['status'] == 'failed' and outcome in done['error']
        assert key not in escalations(f) and result(f, rid) == accepted


def test_done_result_id_maps_to_original_origin_id(split_tree):  # 17
    t = split_tree
    key = delegate(t)
    finish_child(t, key)
    f = t.f
    assert envelope.origin_id('r1.done') == 'r1'
    assert 'r1.done' in result_ids(f) and f.r.get(fleet.RESULT + 'r1.done')
    assert not f.r.keys('fx:channel:*')


def test_collect_notice_to_supervisor_uses_service_wording(split_tree):  # 18
    t = split_tree
    key = delegate(t)
    finish_child(t, key)
    texts = [m['body']['text'] for m in queued_to(t.f, 'supervisor', '[Sub-fleet ' + key + ' completed]')]
    assert texts
    assert 'Continue the current task' not in texts[-1]
    assert 'r1.done' in texts[-1] and 'service decision JSON' in texts[-1]


def test_external_done_id_refused_and_approve_refused_when_done_taken(svc, ok_check):  # 19
    f = svc
    asked(f)
    opened(f)
    with pytest.raises(ValueError, match='duplicate request id: r1.done'):
        f.request({'id': 'r1.done', 'payload': {'q': 'x'}, 'reply': {'required': ['answer']}})
    f.request({'id': 'r2.done', 'payload': {'q': 'x'}, 'reply': {'required': ['answer']}})  # an outside id
    answer_open_supervisor_note(f)  # r1's [escalate] note, then r2.done's routing delivery: the supervisor skips it
    body = {'status': 'RUNNING', 'reason': 'test id', 'dispatch': [], 'skip': [{'reason': 'an outside test id'}]}
    assert answer_turn(f, 'supervisor', json.dumps(body))['status'] == 'committed'
    route_svc(f, 'r2')
    ask_split(f)
    key, _ = opened(f, 'r2')
    answer_split(f, key, 'approve')
    assert bad(f) == '0' and not sf_for(f, 'r2') and escalations(f)[key]['status'] == 'escalated'
    assert queued_to(f, 'supervisor', 'approve of ' + key + ' refused by the program')
    assert f.request({'id': 'x.done', 'payload': {'q': 'x'}, 'reply': {'required': ['answer']}}) == 'x.done'


def test_task_mode_spawn_child_without_hours_unchanged(tree, monkeypatch):  # 20 [baseline]
    t, f = tree, tree.f
    specs = capture_child_specs(monkeypatch)
    deadline = float(f.task().get('deadline') or 0)
    want = max((deadline - time.time()) / 3600, 0.05) if deadline else 16
    rec, _ = spawn_m0(t)
    assert len(specs) == 1 and abs(specs[0]['hours'] - want) < 0.05
    ccfg = fleet.load(Path(rec['run']) / 'config.json')
    assert ccfg.get('lifecycle') == subfleet.child_lifecycle(f.cfg)


def test_published_statuses_subset_of_existing_set(split_tree, monkeypatch):  # 21
    t = split_tree
    for rid, outcome in (('r1', 'completed'), ('r2', 'failed')):
        key = delegate(t, rid)
        finish_child(t, key, outcome, monkeypatch)
    rows = results(t.f)
    assert [x for x in rows if x.get('id') == 'r1' and (x.get('result') or {}).get('delegated')]
    assert [x for x in rows if x.get('id') == 'r1.done']
    assert {x['status'] for x in rows} <= STATUSES


def test_split_child_lifecycle_is_none_not_dev_task(split_tree, monkeypatch):  # 22
    t, f = split_tree, split_tree.f
    f.cfg['channels'] = [{'type': 'exec', 'account': 'phone'}]  # a channels parent: never inherited by a split child
    # P8 plan §4.1 (8): the parent's pinned security policy is inherited as is, never re-read by the child
    from fleetmux.channels import security
    source = f.run_dir.parent / 'security.yaml'
    source.write_text('confirm:\n  wait_s: 60\n')
    pinned, errors = security.parse_security(source.read_text())
    assert pinned and not errors
    f.cfg.update(security=pinned, security_source=str(source))

    def no_reread(*a, **k):
        raise AssertionError('a sub-fleet must inherit the pinned security policy, not parse security.yaml again')
    monkeypatch.setattr(security, 'parse_security', no_reread)
    key = delegate(t)
    ccfg = fleet.load(f.run_dir / 'sub' / key / 'config.json')
    assert ccfg['lifecycle'] is None and ccfg['mode'] == 'task' and 'channels' not in ccfg
    assert ccfg['security'] == f.cfg['security'] == pinned and ccfg['security_source'] == str(source)
    assert ccfg['worker_profiles'] == {}
    for k in ('python', 'worker_cli', 'transport_version'):
        assert ccfg.get(k) == f.cfg.get(k), k
    # control: a sub-fleet that is not a split (operator spawn) keeps the parent's child lifecycle
    f.cfg.pop('channels')
    rec = subfleet.spawn_child(f, 'worker-2', 'M5', 'Operator job: summarise the notes into one page', 1,
                               launch=no_launch)
    other = fleet.load(Path(rec['run']) / 'config.json')
    assert other.get('lifecycle') == subfleet.child_lifecycle(f.cfg) and 'channels' not in other
    assert other['security'] == pinned and other['security_source'] == str(source)  # same inheritance, no re-read


def test_agents_md_rows_present_in_both_files():  # 23
    agents = ROOT / 'fleetmux' / 'agents'
    worker = (agents / 'service_worker_v2.md').read_text(encoding='utf-8')
    supervisor = (agents / 'service_supervisor_v2.md').read_text(encoding='utf-8')
    assert '"escalate"' in worker and '"proposal"' in worker
    assert '[escalate]' in supervisor and 'M<n>' in supervisor


def test_service_resume_replays_request_second_escalate_dedupes(split_tree):  # 24
    t, f = split_tree, split_tree.f
    f.cfg['max_attempts'] = 3  # r1 is replayed twice below, (1) and (3); split_tree's max_attempts 2 allows one
    route(t)
    tree_answer(t, 'worker-1', split_value())
    key, rec = opened(f)
    # (1) escalated + replay: routing is not terminal, the record stays
    f.v2.transaction('resume-requests-1', lambda: f.v2.requeue_requests())
    step(t)
    assert req(f)['status'] == 'routing' and req(f)['worker'] == '' and escalations(f)[key] == rec
    # (2) routed back to the same worker, which asks again: one record, same M id (Q-C)
    tree_answer_open_supervisor_note(t)  # the [escalate] note of (1) is still the supervisor's open delivery
    prompt = routing_prompt(given(t, f, 'supervisor'), 'r1')
    body = {'status': 'RUNNING', 'reason': 'again', 'skip': [], 'dispatch': [
        {'to': 'worker-1', 'request_id': 'r1', 'instruction': 'Answer the question from the payload of r1.'}]}
    assert hook(t, f, 'supervisor', json.dumps(body), prompt=prompt)['status'] == 'committed'
    step(t)
    tree_answer(t, 'worker-1', split_value())
    assert [k for k in escalations(f) if k.startswith('M')] == [key] and mid_of(f, 'r1') == key
    # (3) a request that may not be replayed times out on resume: its split is stale
    route(t, 'r2', 'worker-2', spec={'id': 'r2', 'payload': {'question': 'big job r2'},
                                     'reply': {'required': ['answer']}, 'replay': False})
    tree_answer(t, 'worker-2', split_value())
    key2, _ = opened(f, 'r2', 'worker-2')
    f.v2.transaction('resume-requests-2', lambda: f.v2.requeue_requests())
    f.tick()
    step(t)
    assert not req(f, 'r2') and key2 not in escalations(f) and audits(f, 'split.stale') and not result(f, 'r2.done')
    # (4) R4 (3): r1 replayed and re-dispatched to worker-2; the stale owner is refused, the current one replaces
    tree_answer_open_supervisor_note(t)  # r2's [escalate] note from (3) is still the supervisor's open delivery
    prompt = routing_prompt(given(t, f, 'supervisor'), 'r1')
    body = {'status': 'RUNNING', 'reason': 'move', 'skip': [], 'dispatch': [
        {'to': 'worker-2', 'request_id': 'r1', 'instruction': 'Answer the question from the payload of r1.'}]}
    assert hook(t, f, 'supervisor', json.dumps(body), prompt=prompt)['status'] == 'committed'
    step(t)
    before = escalations(f)[key]
    f.service_split_intake('worker-1', 'r1', split_value())
    assert escalations(f)[key] == before and audits(f, 'split.stale_owner')
    tree_answer(t, 'worker-2', split_value(reason='worker-2 also finds r1 too big for one worker'))
    rec = escalations(f)[key]
    assert rec['slot'] == 'worker-2' and items(f)[key]['to'] == 'worker-2' and audits(f, 'split.owner_replaced')
    assert len(queued_to(f, 'supervisor', 'asks to split ' + key)) >= 3
    # (5) window W: the spawn was accepted, the daemon died before the escalations part of that tick
    assert tree_rule(t, [{'task': key, 'decision': 'approve'}])['status'] == 'committed'
    subfleet.process_requests(f)  # the subfleet part only
    assert fleet.load(subfleet.answer_path(f.run_dir, escalations(f)[key]['rid']))['status'] == 'accepted'
    esc = escalations(f)
    esc['M9'] = {'slot': 'worker-1', 'request': 'r9', 'status': 'rotating', 'proposal': [], 'workers': 2,
                 'instruction': 'x', 'reason': 'x', 'spec': {}, 'launch': '{}', 'supervisor_profile': 'claude-opus',
                 'from_generation': int(f.agent('worker-1')['generation'])}
    f.save_escalations(esc)
    since = last_audit(f)
    again = restart(t)
    again.v2.resume_run()
    got = result(again)
    assert got and got['status'] == 'ok' and got['result']['delegated'] == key
    assert 'r1' not in again.r.lrange(fleet.REQQ, 0, -1)
    assert not [x for x in audits_since(again, since, 'queued') if 'needs routing' in (x.get('detail') or '') and 'big job r1' in (x.get('detail') or '')]
    for event in ('result.late', 'execution.held', 'service.paused'):
        assert not audits_since(again, since, event), event
    assert escalations(again)[key]['status'] == 'delegated'
    # M9's request r9 is gone: resume cannot pin it, so it is dropped with a note (#33), never rotated here
    assert 'M9' not in escalations(again) and queued_to(again, 'supervisor', 'M9 approval lapsed on resume')
    assert again.agent('worker-1').get('state') != 'handoff'
    third = restart(t)
    third.v2.resume_run()
    assert result_ids(third).count('r1') == 1 and sub_runs(third) == [key]


def test_split_goal_and_proposal_frozen_at_intake(split_tree, monkeypatch):  # 25
    t, f = split_tree, split_tree.f
    specs = capture_child_specs(monkeypatch)
    route(t)
    tree_answer(t, 'worker-1', split_value())
    key, rec = opened(f)
    frozen = rec['proposal']
    assert [p['id'] for p in frozen] == [key + '.0', key + '.1']
    (fleet.agent_cwd(f.cfg, 'worker-1') / 'proposal-draft.md').write_text('a later draft the worker wrote')
    assert tree_rule(t, [{'task': key, 'decision': 'approve', 'note': 'keep the sources separate'}])['status'] == 'committed'
    (spec,) = sf_for(f, 'r1')
    assert spec['proposal'] == frozen
    assert 'Answer the question from the payload of r1.' in spec['goal']
    assert 'Supervisor note: keep the sources separate' in spec['goal']
    f.tick()
    step(t)
    assert specs, 'the sub-fleet was not started'
    for p in frozen:
        assert p['instruction'] in specs[-1]['goal'] and p['output'] in specs[-1]['goal']


def test_parent_stop_publishes_done_failed_for_running_split(split_tree):  # 26
    t = split_tree
    # A) delegated, the child still runs, the parent stops explicitly (collect(notify=False))
    key = delegate(t)
    assert subfleet.stop_children(t.f, wait=0)
    f = t.f
    assert result_ids(f).count('r1.done') == 1
    done = result(f, 'r1.done')
    assert done['status'] == 'failed' and key in done['error']
    assert not any(w in done['error'] for w in ('已通知', '已送达', 'sent'))
    ready_again(t)
    # B) the child wrote result.json (completed) but was not collected yet when the parent stops
    key2 = delegate(t, 'r2', 'worker-2')
    cf = child_daemon(t, subfleet.record(f.run_dir / 'sub' / key2))
    cf.v2.finish('completed', 'merged')
    cf.write_result()
    die(cf)
    assert subfleet.stop_children(f, wait=0)
    assert result_ids(f).count('r2.done') == 1 and result(f, 'r2.done')['status'] == 'ok'
    ready_again(t)
    # C) stop / collect again after the follow-up went out: never a second one
    subfleet.stop_children(f, wait=0)
    for name in sub_runs(f):
        subfleet.collect(f, f.run_dir / 'sub' / name, notify=False)
    assert result_ids(f).count('r1.done') == 1 and result_ids(f).count('r2.done') == 1
    # control: a delegated sub-fleet that is no split (operator spawn) gets no follow-up result
    count = len(results(f))
    subfleet.spawn_child(f, 'worker-1', 'M7', 'Operator job: summarise the notes into one page', 1, launch=no_launch)
    subfleet.stop_children(f, wait=0)
    assert len(results(f)) == count


def test_service_approve_with_models_rotates_then_adopts_and_settles(split_tree):  # 27
    t, f = split_tree, split_tree.f
    assert not f.cfg.get('allow_profile_live_switch')
    original = f.agent('worker-1').get('runtime_launch', '')
    route(t)
    tree_answer(t, 'worker-1', split_value())
    key, _ = opened(f)
    assert tree_rule(t, [{'task': key, 'decision': 'approve', 'supervisor_profile': 'claude-opus',
                          'worker_profiles': ['claude-haiku', 'claude-sonnet']}])['status'] == 'committed'
    assert escalations(f)[key]['status'] == 'rotating' and req(f)['status'] == 'dispatched' and result(f) is None
    f.tick()  # idle slot: the handoff turn starts
    assert f.agent('worker-1')['state'] == 'handoff'
    step(t)
    ask = given(t, f, 'worker-1')[-1]
    assert 'sub-fleet ' + key in ask
    assert hook(t, f, 'worker-1', 'handoff written', prompt=ask)['status'] in ('committed', 'handoff')
    step(t)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and json.loads(info['runtime_launch'])['profile_id'] == 'claude-opus'
    assert result(f) is None and req(f)['status'] == 'dispatched'  # not settled before the spawn is accepted
    assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
    f.tick()  # the spawn request is written
    f.tick()  # ... executed and accepted
    step(t)
    assert escalations(f)[key]['status'] == 'delegated' and f.agent('worker-1')['state'] == 'delegated'
    got = result(f)
    assert got['status'] == 'ok' and got['result']['delegated'] == key
    rec = subfleet.record(f.run_dir / 'sub' / key)
    ccfg = fleet.load(Path(rec['run']) / 'config.json')
    assert ccfg['worker_profiles'] == {'worker-1': 'claude-haiku', 'worker-2': 'claude-sonnet'}
    finish_child(t, key)
    assert result(f, 'r1.done')['status'] == 'ok'
    back = f.agent('worker-1')
    assert back['generation'] == '2' and back.get('runtime_launch', '') == original and not back.get('split_restore')


def test_service_intake_assigns_next_free_m_id_and_rewrites_proposal_ids(svc, ok_check):  # 28
    f = svc
    asked(f)
    assert opened(f)[0] == 'M0' and [p['id'] for p in escalations(f)['M0']['proposal']] == ['M0.0', 'M0.1']
    f.service_split_intake('worker-1', 'r1', split_value())  # the same request again: the same M id
    assert mid_of(f, 'r1') == 'M0' and [k for k in escalations(f) if k.startswith('M')] == ['M0']
    route_svc(f, 'r2')
    ask_split(f, value=split_value(proposal=[dict(p, id=x) for p, x in zip(PROPOSAL, ('a', 'b'))]))
    assert opened(f, 'r2')[0] == 'M1' and [p['id'] for p in escalations(f)['M1']['proposal']] == ['M1.0', 'M1.1']
    f.r.set(fleet.ITEMS, json.dumps(dict(items(f), M2={'to': 'worker-2', 'instruction': 'a leftover task id'})))
    route_svc(f, 'r3')
    ask_split(f)
    assert opened(f, 'r3')[0] == 'M3'
    f.cfg['task_id'] = 'M4'  # inside sub-fleet M4 the ids are M4.<n>
    f.cfg['limits'] = {'max_depth': 3}  # M4.0 is level 3; the default max_depth 2 refuses any split inside a sub-fleet
    route_svc(f, 'r4')
    ask_split(f)
    key, rec = opened(f, 'r4')
    assert key == 'M4.0' and [p['id'] for p in rec['proposal']] == ['M4.0.0', 'M4.0.1']


def test_split_settle_accepted_touches_only_accepted_service_splits(split_tree):  # 29
    t, f = split_tree, split_tree.f
    route(t)
    assert tree_answer(t, 'worker-1', {'blocked': 'stand-in: frees worker-1, r1 stays dispatched'})['status'] == 'committed'
    assert req(f)['status'] == 'dispatched' and BKEY in escalations(f)
    base = req(f)
    for n, rid in enumerate(('r2', 'r3', 'r4')):
        spec = dict(json.loads(base['spec']), id=rid)
        f.r.hset(fleet.REQ + rid, mapping=dict(base, spec=json.dumps(spec), worker='worker-2'))
        f.r.rpush(fleet.REQQ, rid)
    rec = lambda req_id, status, **kw: dict({'slot': 'worker-1', 'request': req_id, 'status': status,  # noqa: E731
                                             'workers': 2, 'proposal': [], 'instruction': 'Answer ' + req_id,
                                             'reason': 'too big'}, **kw)
    esc = escalations(f)
    esc.update(M0=rec('r1', 'approved', rid='sf-a'), M1=rec('r2', 'approved', rid='sf-b', slot='worker-2'),
               M2=rec('r3', 'approved', rid='sf-c', slot='worker-2'),
               M3=rec('r4', 'rotating', slot='worker-2', spec={}, launch='{}', supervisor_profile='claude-opus',
                      from_generation=int(f.agent('worker-2')['generation'])),
               M4={'slot': 'worker-2', 'status': 'approved', 'rid': 'sf-e', 'workers': 1, 'proposal': [],
                   'instruction': 'task-mode work', 'reason': 'x'})
    f.save_escalations(esc)
    f.r.set(fleet.ITEMS, json.dumps({'M0': {'to': 'worker-1', 'request': 'r1'}, 'M1': {'to': 'worker-2', 'request': 'r2'},
                                     'M2': {'to': 'worker-2', 'request': 'r3'}, 'M3': {'to': 'worker-2', 'request': 'r4'},
                                     'M4': {'to': 'worker-2', 'instruction': 'task-mode work'}}))
    run = str(f.run_dir / 'sub' / 'M0')
    fleet.save(subfleet.answer_path(f.run_dir, 'sf-a'), {'status': 'accepted', 'run': run})
    fleet.save(subfleet.answer_path(f.run_dir, 'sf-b'), {'status': 'rejected', 'reason': 'worker-2 is busy'})
    fleet.save(subfleet.answer_path(f.run_dir, 'sf-e'), {'status': 'accepted', 'run': run + '-task'})
    others = {k: v for k, v in escalations(f).items() if k != 'M0'}
    reqs = {rid: req(f, rid) for rid in ('r2', 'r3', 'r4')}
    queue = [x for x in f.r.lrange(fleet.REQQ, 0, -1) if x != 'r1']
    inbox = {s: f.v2.bus.pending(s) for s in f.names}
    states = {s: f.agent(s).get('state') for s in f.names}
    contract = f.contract_state()
    f.split_settle_accepted()
    got = result(f)  # the target: settled as usual (its publish side effects are allowed)
    assert got['status'] == 'ok' and got['worker'] == 'worker-1' and got['result']['delegated'] == 'M0'
    assert escalations(f)['M0']['status'] == 'delegated'
    assert {k: v for k, v in escalations(f).items() if k != 'M0'} == others  # everything unrelated: unchanged
    assert {rid: req(f, rid) for rid in ('r2', 'r3', 'r4')} == reqs
    assert [x for x in f.r.lrange(fleet.REQQ, 0, -1) if x != 'r1'] == queue
    assert {s: f.v2.bus.pending(s) for s in f.names} == inbox
    assert {s: f.agent(s).get('state') for s in f.names if s != 'worker-1'} == {
        s: v for s, v in states.items() if s != 'worker-1'}
    assert f.agent('worker-2').get('state') != 'handoff' and f.contract_state() == contract
    assert not [x for x in results(f) if (x.get('result') or {}).get('delegated') == 'M4']
    f.split_settle_accepted()  # a second call: nothing more
    assert result_ids(f).count('r1') == 1


# ================================================================== resume pins an approved / rotating split (缺陷 3)
def resume_after(t):
    """The parent daemon dies and comes back with `start --resume`; the respawned workers answer READY."""
    since = last_audit(t.f)
    again = restart(t)
    again.v2.resume_run()
    step(t)
    ready_again(t)
    return again, since


def test_resume_pins_an_approved_split_and_the_spawn_goes_on(split_tree):  # 30
    t, f = split_tree, split_tree.f
    key = split_up(t)
    assert escalations(f)[key]['status'] == 'approved' and sf_for(f, 'r1')  # spawn request written, not yet run
    again, since = resume_after(t)
    assert not audits_since(again, since, 'request.replayed')
    assert req(again)['status'] == 'dispatched' and req(again)['worker'] == 'worker-1'
    assert [x for x in audits_since(again, since, 'split.pinned') if x.get('request_id') == 'r1']
    assert not [x for x in audits_since(again, since, 'queued') if 'needs routing' in (x.get('detail') or '')
                and 'big job r1' in (x.get('detail') or '')]
    again.tick()
    step(t)
    assert escalations(again)[key]['status'] == 'delegated'
    got = result(again)
    assert got['status'] == 'ok' and got['result']['delegated'] == key
    assert not queued_to(again, 'supervisor', 'could not start')


def test_resume_keeps_the_model_of_a_rotating_split_not_yet_handed_off(split_tree, monkeypatch):  # 31
    t, f = split_tree, split_tree.f
    key = split_up(t, supervisor_profile='claude-opus')
    rec = escalations(f)[key]
    assert rec['status'] == 'rotating' and not rec.get('rotating_at') and rec['launch']
    assert rec['from_generation'] == int(f.agent('worker-1')['generation'])
    # worker-1's session is gone (e.g. the machine rebooted): resume starts its next generation (respawn), the
    # case where split_rotate's first branch never runs and only a split_launch written before respawn keeps the pick
    pane, alive = f.agent('worker-1')['pane'], f.mgr.has_cli_process
    monkeypatch.setattr(f.mgr, 'has_cli_process', lambda p, *a, **k: p != pane and alive(p, *a, **k))
    again, since = resume_after(t)
    assert not audits_since(again, since, 'request.replayed')
    assert req(again)['status'] == 'dispatched' and req(again)['worker'] == 'worker-1'
    info = again.agent('worker-1')
    assert int(info['generation']) > rec['from_generation']
    assert json.loads(info['runtime_launch'])['profile_id'] == 'claude-opus'
    again.tick()  # the generation changed and is READY: the spawn request is written
    assert sf_for(again, 'r1')
    again.tick()
    step(t)
    assert escalations(again)[key]['status'] == 'delegated' and result(again)['result']['delegated'] == key


def test_resume_pinned_split_refused_spawn_reopens_with_the_worker(split_tree):  # 32
    t, f = split_tree, split_tree.f
    key = split_up(t)
    again, since = resume_after(t)
    again.setf('worker-1', state='busy')  # no longer idle when the spawn runs
    again.tick()
    step(t)
    assert escalations(again)[key]['status'] == 'escalated'
    assert req(again)['status'] == 'dispatched' and req(again)['worker'] == 'worker-1'
    assert queued_to(again, 'supervisor', 'could not start')
    again.setf('worker-1', state='idle')
    # the supervisor revises (escalation_apply, as on_supervisor runs it: this fixture's respawned Codex supervisor
    # cannot answer a hook after a real resume_run, see replay_svc)
    again.escalation_apply({}, None, key, escalations(again)[key], {'decision': 'revise', 'note': 'three pieces, not two'})
    got = [m['body']['text'] for m in queued_to(again, 'worker-1', 'revise your split of ' + key)]
    assert got and 'three pieces, not two' in got[-1] and 'Payload (JSON)' in got[-1]
    assert key not in escalations(again) and req(again)['worker'] == 'worker-1'


def test_resume_drops_an_approved_split_whose_request_ended_and_tells_the_supervisor(split_tree):  # 33
    t, f = split_tree, split_tree.f
    key = split_up(t)
    f.v2.transaction('t-pub-r1', lambda: f.v2.publish('r1', 'timeout', worker='worker-1', error='too slow'))
    again, since = resume_after(t)
    assert key not in escalations(again) and key in json.loads(again.r.get(fleet.SPLIT_STALE) or '{}')
    texts = [m['body']['text'] for m in queued_to(again, 'supervisor', 'approval lapsed on resume')]
    assert texts and '[escalate] ' + key + ' approval lapsed on resume: ' in texts[-1]


# ================================================================== the routing board shows a held split (缺陷 4)
def test_worker_board_marks_an_approved_or_rotating_split(split_tree):  # 34
    t, f = split_tree, split_tree.f
    key = split_up(t)
    route(t, 'r2', 'worker-2')
    board = next(x for x in routing_prompt(given(t, f, 'supervisor'), 'r2').splitlines() if x.startswith('- worker-1:'))
    assert 'split ' + key + ' approved' in board and 'do not route' in board, board
    esc = escalations(f)
    esc[key]['status'] = 'rotating'
    f.save_escalations(esc)
    line = next(x for x in f.worker_board().splitlines() if x.startswith('- worker-1:'))
    assert 'split ' + key + ' rotating' in line and 'do not route' in line, line
    esc[key]['status'] = 'delegated'
    f.save_escalations(esc)
    f.setf('worker-1', state='delegated')
    line = next(x for x in f.worker_board().splitlines() if x.startswith('- worker-1:'))
    assert 'split ' not in line and 'delegated' in line, line
