"""§44「监工直接开子 fleet」(user 2026-11-10: 监工把事务分级——一级派子 fleet、二级派 worker、三级自己回).

Switch ``supervisor_split`` (start ``--supervisor-split``, off by default). On: a service dispatch item may carry
``split`` ({reason, proposal, workers?, supervisor_profile?, worker_profiles?}); the worker in ``to`` becomes the
supervisor of a task sub-fleet through the existing P7 path (service_split_intake -> escalation_apply), without the
[escalate] question to the supervisor. Off: such a decision is invalid. A refusal by the program (limits) hands the
request to that worker as a normal dispatch.

Only private test Redis, fake cmux, fake CLIs (fixtures of test_service_split / test_service_escalate).
"""
import json

import pytest

from test_escalate import ok_check  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_service_escalate import audits, escalations, result, svc  # noqa: F401
from test_service_split import (PROPOSAL, answer_open_supervisor_note, bad, items, queued_to, req, routing_prompt,
                                sf_for, split_tree, tree_answer_open_supervisor_note)  # noqa: F401
from test_subfleet import given, hook, step
from test_v2_codex_claude import answer_turn, typed
from fleetmux import fleet, runtime_v2, subfleet

WORKERS = ['worker-1', 'worker-2']
SPLIT = {'reason': 'five sources to fetch and merge: two parts run in parallel', 'workers': 2,
         'proposal': [dict(p) for p in PROPOSAL]}


def post(f, rid='r1'):
    spec = {'id': rid, 'payload': {'question': 'big job ' + rid}, 'reply': {'required': ['answer']}}
    runtime_v2.post_event(f.v2.bus, dict(kind='request', request_id='cmd-' + rid, spec=spec), 'cmd-' + rid)
    assert f.v2.step(10)
    f.pump()


def route_split(f, split=None, to='worker-1'):
    body = {'status': 'RUNNING', 'reason': 'too big for one worker', 'skip': [],
            'dispatch': [{'to': to, 'instruction': 'Run the big job of the payload as a sub-fleet.',
                          'split': dict(SPLIT if split is None else split)}]}
    return answer_turn(f, 'supervisor', json.dumps(body))


# ------------------------------------------------------------------ parser
def test_parse_split_shape():
    def decision(split, **extra):
        item = dict({'to': 'worker-1', 'request_id': 'r1', 'instruction': 'Run the big job as a sub-fleet.',
                     'split': split}, **extra)
        return json.dumps({'status': 'RUNNING', 'dispatch': [item], 'skip': []})
    got = fleet.parse_service_decision(decision(SPLIT), WORKERS)
    assert got['dispatch'][0]['split']['workers'] == 2
    for split, why in (({'proposal': PROPOSAL}, 'one-sentence reason'),
                       (dict(SPLIT, proposal=[]), 'non-empty list'),
                       (dict(SPLIT, proposal=[{'instruction': 'short', 'output': 'x'}]), 'instruction'),
                       (dict(SPLIT, workers=0), 'positive integer'),
                       (dict(SPLIT, worker_profiles='opus'), 'worker_profiles'),
                       ('yes', 'must be an object')):
        with pytest.raises(ValueError, match=why):
            fleet.parse_service_decision(decision(split), WORKERS)
    with pytest.raises(ValueError, match='fresh'):
        fleet.parse_service_decision(decision(SPLIT, fresh=True), WORKERS)


# ------------------------------------------------------------------ switch off
def test_split_refused_when_switch_off(svc, ok_check):
    f = svc
    assert not f.cfg.get('supervisor_split')
    post(f)
    route_split(f)
    assert bad(f) == '1' and req(f)['status'] == 'routing'
    assert not [k for k in escalations(f) if k.startswith('M')]
    f.pump()
    assert 'supervisor_split is off' in typed(f, 'supervisor')[-1]


def test_template_mentions_split_only_when_on(svc):
    f = svc
    off = f.role_scope('supervisor', f.runtime_cfg('supervisor'), {})
    assert '{{SUPERVISOR_SPLIT}}' not in off and 'dispatch[].split' not in off
    f.cfg['supervisor_split'] = True
    on = f.role_scope('supervisor', f.runtime_cfg('supervisor'), {})
    assert fleet.SUPERVISOR_SPLIT_DOC in on and '{{SUPERVISOR_SPLIT}}' not in on
    assert 'dispatch[].split' not in f.role_scope('worker-1', f.runtime_cfg('worker-1'), {})


def test_config_value_is_checked():
    with pytest.raises(ValueError, match='supervisor_split'):
        runtime_v2.validate_config({'supervisor_split': 'yes'})


# ------------------------------------------------------------------ switch on
def test_supervisor_split_is_approved_without_an_escalate_question(svc, ok_check):
    f = svc
    f.cfg['supervisor_split'] = True
    post(f)
    assert route_split(f)['status'] == 'committed'
    assert bad(f) == '0'
    assert req(f)['status'] == 'dispatched' and req(f)['worker'] == 'worker-1'
    key = next(k for k, v in items(f).items() if v.get('request') == 'r1')
    rec = escalations(f)[key]
    assert rec['slot'] == 'worker-1' and rec['request'] == 'r1' and rec['status'] in ('approved', 'rotating')
    assert [p['id'] for p in rec['proposal']] == [key + '.0', key + '.1'] and rec['workers'] == 2
    assert rec.get('message_id') == ''  # no [escalate] question went to the supervisor
    assert not queued_to(f, 'supervisor', 'asks to split')
    assert f.agent('worker-1')['state'] == 'idle'  # nothing was delivered to it as a normal request
    assert [x for x in audits(f, 'split.by_supervisor') if x.get('request_id') == 'r1']
    # a later supervisor reply that does not mention the split is fine (nothing is open for it)
    answer_open_supervisor_note(f)
    assert bad(f) == '0'


def test_refused_split_falls_back_to_a_normal_dispatch(svc, ok_check, monkeypatch):
    f = svc
    f.cfg['supervisor_split'] = True
    limit = 'max_children 3: this fleet already runs M1, M2, M3'
    monkeypatch.setattr(subfleet, 'check', lambda *a, **k: limit)
    post(f)
    assert route_split(f)['status'] == 'committed'
    assert bad(f) == '0'
    assert not [k for k in escalations(f) if k.startswith('M')]
    assert req(f)['status'] == 'dispatched' and req(f)['worker'] == 'worker-1'
    f.pump()
    got = typed(f, 'worker-1')[-1]
    assert limit in got and 'Payload (JSON)' in got and 'yourself' in got
    assert queued_to(f, 'supervisor', '[escalate] refused for worker-1: ')


def test_supervisor_split_spawns_and_delegates(split_tree):
    t, f = split_tree, split_tree.f
    f.cfg['supervisor_split'] = True
    tree_answer_open_supervisor_note(t)
    spec = {'id': 'r1', 'payload': {'question': 'big job r1'}, 'reply': {'required': ['answer']}}
    runtime_v2.post_event(f.v2.bus, dict(kind='request', request_id='cmd-r1', spec=spec), 'cmd-r1')
    step(t)
    prompt = routing_prompt(given(t, f, 'supervisor'), 'r1')
    body = {'status': 'RUNNING', 'reason': 'tier 1: a sub-fleet', 'skip': [],
            'dispatch': [{'to': 'worker-1', 'instruction': 'Run the big job of the payload as a sub-fleet.',
                          'split': dict(SPLIT)}]}
    assert hook(t, f, 'supervisor', json.dumps(body), prompt=prompt)['status'] == 'committed'
    step(t)
    key = next(k for k, v in items(f).items() if v.get('request') == 'r1')
    assert escalations(f)[key]['status'] == 'approved'
    (sf,) = sf_for(f, 'r1')
    assert sf['split'] == 'r1' and sf['lifecycle'] is None and len(sf['proposal']) == 2
    f.tick()
    step(t)
    got = result(f)
    assert got['status'] == 'ok' and got['result']['delegated'] == key and 'r1.done' in got['result']['reply']
