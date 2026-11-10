"""Service mode: the supervisor answers a routing request itself (`answer: [{request_id, result}]`, M0.1).

v1: Codex supervisor on the legacy transport (test_fleet's `sfx`, fake Redis). v2/tui: real private Redis, official
Codex supervisor + Claude workers (test_service_escalate's `svc`); there the daemon binds request_id to the delivery.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_fleet import last_to, req, results, sfx, submit  # noqa: F401  (sfx: fixture)
from test_fleet import answer as turn_v1
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_pi_v2_contract import PI_ROOT
from test_service_escalate import audits, result, svc  # noqa: F401  (svc: fixture)
from test_v2_codex_claude import answer_turn
from cmux_fleet import fleet, runtime_v2

WORKERS = ['worker-1']
REPLY = {'required': ['reply'], 'allowed': ['reply']}


def answered(rid, result_):
    return json.dumps({'status': 'RUNNING', 'reason': 'I know this one', 'dispatch': [], 'skip': [],
                       'answer': [{'request_id': rid, 'result': result_}]})


def bad_replies(f):
    return int(f.r.hget(fleet.SERVICE, 'bad_replies') or 0)


# ------------------------------------------------------------------ parser
def test_parser_takes_answer_and_checks_its_shape():
    ok = fleet.parse_service_decision(answered('r1', {'reply': 'hi'}), WORKERS)
    assert ok['answer'] == [{'request_id': 'r1', 'result': {'reply': 'hi'}}]
    assert fleet.parse_service_decision(json.dumps({'status': 'RUNNING'}), WORKERS)['answer'] == []
    for bad in ({}, [{'request_id': 'r1'}], [{'result': {}}], [{'request_id': 'r1', 'result': 'hi'}], ['r1']):
        with pytest.raises(ValueError):
            fleet.parse_service_decision(json.dumps({'status': 'RUNNING', 'answer': bad}), WORKERS)


@pytest.mark.parametrize('other', [
    {'dispatch': [{'to': 'worker-1', 'request_id': 'r1', 'instruction': 'Answer the payload question.'}]},
    {'skip': [{'request_id': 'r1', 'reason': 'nothing changed'}]}])
def test_parser_rejects_a_request_in_answer_and_dispatch_or_skip(other):
    body = dict({'status': 'RUNNING', 'answer': [{'request_id': 'r1', 'result': {'reply': 'x'}}]}, **other)
    with pytest.raises(ValueError, match='dispatch, skip or answer each request once'):
        fleet.parse_service_decision(json.dumps(body), WORKERS)
    body['answer'][0]['request_id'] = 'r2'  # different requests may mix
    fleet.parse_service_decision(json.dumps(body), WORKERS)


# ------------------------------------------------------------------ v1
def test_v1_answer_publishes_ok_from_the_supervisor(sfx):  # noqa: F811
    submit(sfx, req('r1'))
    sfx.pump()
    assert '[Request r1] A new request needs routing' in last_to(sfx, 'supervisor')
    turn_v1(sfx, 'supervisor', answered('r1', {'verdict': 'hold', 'param': {'trail_dd': 0.2}}))
    res = results(sfx)[-1]
    assert res['id'] == 'r1' and res['status'] == 'ok' and res['worker'] == 'supervisor'
    assert res['result'] == {'verdict': 'hold', 'param': {'trail_dd': 0.2}}
    assert not sfx.r.hgetall(fleet.REQ + 'r1') and bad_replies(sfx) == 0
    assert sfx.agent('exit')['state'] == sfx.agent('reentry')['state'] == 'idle'  # no worker turn spent


def test_v1_answer_that_misses_the_reply_schema_is_an_invalid_decision(sfx):  # noqa: F811
    submit(sfx, req('r2', reply={'required': ['verdict'], 'allowed': ['verdict', 'param']}))
    sfx.pump()
    turn_v1(sfx, 'supervisor', answered('r2', {'param': 1}))
    assert results(sfx) == [] and sfx.r.hget(fleet.REQ + 'r2', 'status') == 'routing' and bad_replies(sfx) == 1
    sfx.pump()
    fix = last_to(sfx, 'supervisor')
    assert 'not a valid service decision JSON' in fix and 'missing keys: verdict' in fix
    turn_v1(sfx, 'supervisor', answered('r2', {'verdict': 'ok', 'qty': 4}))
    assert results(sfx) == [] and bad_replies(sfx) == 2
    sfx.pump()
    assert 'keys not allowed: qty' in last_to(sfx, 'supervisor')


@pytest.mark.parametrize('other', [
    {'dispatch': [{'to': 'exit', 'request_id': 'r3', 'instruction': 'Check the last sell against the high'}]},
    {'skip': [{'request_id': 'r3', 'reason': 'nothing changed'}]}])
def test_v1_same_request_in_answer_and_dispatch_or_skip_is_rejected(sfx, other):  # noqa: F811
    submit(sfx, req('r3'))
    sfx.pump()
    body = dict({'status': 'RUNNING', 'answer': [{'request_id': 'r3', 'result': {'verdict': 1, 'param': 2}}]}, **other)
    turn_v1(sfx, 'supervisor', json.dumps(body))
    assert results(sfx) == [] and bad_replies(sfx) == 1
    assert sfx.r.hget(fleet.REQ + 'r3', 'status') == 'routing' and sfx.agent('exit')['state'] == 'idle'


def test_v1_answer_to_an_unknown_or_dispatched_request_publishes_nothing(sfx):  # noqa: F811
    submit(sfx, req('r4', to='exit'))  # a direct request: dispatched, never routing
    sfx.pump()
    assert sfx.r.hget(fleet.REQ + 'r4', 'status') == 'dispatched'
    turn_v1(sfx, 'supervisor', answered('r4', {'verdict': 1, 'param': 2}))
    turn_v1(sfx, 'supervisor', answered('nope', {'verdict': 1, 'param': 2}))
    assert results(sfx) == [] and bad_replies(sfx) == 0  # a valid decision; the stale answers are only logged
    assert sfx.r.hget(fleet.REQ + 'r4', 'status') == 'dispatched' and not sfx.r.get(fleet.RESULT + 'nope')


def test_v1_operator_request_answered_by_the_supervisor_is_still_reported(sfx):  # noqa: F811
    submit(sfx, req('r5', reply=REPLY))
    sfx.r.hset(fleet.REQ + 'r5', mapping={'operator': '1'})  # what operator_input marks on its request
    sfx.pump()
    turn_v1(sfx, 'supervisor', answered('r5', {'reply': 'the trail is fine'}))
    res = results(sfx)[-1]
    assert res['status'] == 'ok' and res['worker'] == 'supervisor' and res['result'] == {'reply': 'the trail is fine'}
    sfx.pump()
    report = last_to(sfx, 'supervisor')
    assert report.startswith('[Operator result r5]') and 'the trail is fine' in report
    assert 'Report the worker result to the operator' in report


# ------------------------------------------------------------------ v2 / tui
def routing(f, rid, reply=None, operator=False):
    spec = {'id': rid, 'payload': {'question': 'what is in notes?'}, 'reply': reply or {'required': ['answer']}}
    runtime_v2.post_event(f.v2.bus, dict(kind='request', request_id='cmd-' + rid, spec=spec), 'cmd-' + rid)
    assert f.v2.step(10)
    if operator:
        f.r.hset(fleet.REQ + rid, mapping={'operator': '1'})
    f.pump()
    assert f.r.hget(fleet.REQ + rid, 'status') == 'routing' and f.agent('supervisor')['state'] == 'busy'


def bound(result_, **extra):  # no request_id: the daemon binds the delivery's request
    return json.dumps(dict({'status': 'RUNNING', 'reason': 'answered directly', 'dispatch': [], 'skip': [],
                            'answer': [{'result': result_}]}, **extra))


def invalid_errors(f):
    return [e.get('error') for e in audits(f, 'candidate.invalid') if e.get('slot') == 'supervisor']


def test_v2_answer_without_request_id_is_bound_and_published(svc):  # noqa: F811
    routing(svc, 'r1')
    assert answer_turn(svc, 'supervisor', bound({'answer': 'notes say hi'}))['status'] == 'committed'
    res = result(svc, 'r1')
    assert res['status'] == 'ok' and res['worker'] == 'supervisor' and res['result'] == {'answer': 'notes say hi'}
    assert invalid_errors(svc) == [] and not svc.r.hgetall(fleet.REQ + 'r1')
    assert svc.agent('worker-1')['state'] == svc.agent('worker-2')['state'] == 'idle'


def test_v2_answer_plus_dispatch_breaks_the_one_item_rule(svc):  # noqa: F811
    routing(svc, 'r2')
    both = bound({'answer': 'x'}, dispatch=[{'to': 'worker-1', 'instruction': 'Answer the question from the payload.'}])
    assert answer_turn(svc, 'supervisor', both)['status'] == 'committed'  # settled; the decision is rejected
    assert invalid_errors(svc) == ['one dispatch, skip or answer per bound service request']
    assert result(svc, 'r2') is None and svc.r.hget(fleet.REQ + 'r2', 'status') == 'routing'
    assert svc.agent('worker-1')['state'] == 'idle'


def test_v2_answer_for_another_request_id_is_rejected(svc):  # noqa: F811
    routing(svc, 'r3')
    other = json.dumps({'status': 'RUNNING', 'dispatch': [], 'skip': [],
                        'answer': [{'request_id': 'r-other', 'result': {'answer': 'x'}}]})
    answer_turn(svc, 'supervisor', other)
    assert invalid_errors(svc) == ['service decision may route only its bound request'] and result(svc, 'r3') is None


def test_v2_answer_that_misses_the_reply_schema_publishes_nothing(svc):  # noqa: F811
    routing(svc, 'r4')
    answer_turn(svc, 'supervisor', bound({'reply': 'wrong key'}))
    assert result(svc, 'r4') is None and svc.r.hget(fleet.REQ + 'r4', 'status') == 'routing'
    assert bad_replies(svc) == 1


def test_v2_operator_request_answered_by_the_supervisor_reports_its_result(svc):  # noqa: F811
    routing(svc, 'r5', reply=REPLY, operator=True)
    answer_turn(svc, 'supervisor', bound({'reply': 'notes say hi'}))
    res = result(svc, 'r5')
    assert res['status'] == 'ok' and res['worker'] == 'supervisor' and res['result'] == {'reply': 'notes say hi'}
    cards = [json.loads(x['data']) for _, x in svc.r.xrange(svc.v2.prefix + ':ui:supervisor')]
    assert any('notes say hi' in json.dumps(c, ensure_ascii=False) for c in cards)


# ------------------------------------------------------------------ pi schema
def test_service_schema_source_declares_answer():
    src = (Path(__file__).resolve().parents[1] / 'cmux_fleet/pi_extension_v2.ts').read_text()
    assert ('answer: Type.Optional(Type.Array(Type.Object({ request_id: Type.Optional(Type.String()),\n'
            '      result: Type.Record(Type.String(), Type.Any()) }))),') in src


HARNESS = r"""
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { Value } from 'typebox/value';
const {default: register}=await import(pathToFileURL(process.argv[2]).href);
const tools=new Map();
register({on(){},registerTool:(t)=>tools.set(t.name,t),registerEntryRenderer(){},registerMessageRenderer(){},appendEntry(){},sendMessage(){}});
const schema=tools.get('fleet_decision').parameters;
const base={status:'RUNNING',dispatch:[],skip:[]};
assert.ok(Value.Check(schema,{...base,answer:[{result:{reply:'hi'}}]}),'answer without request_id (bound by code)');
assert.ok(Value.Check(schema,{...base,answer:[{request_id:'r1',result:{reply:'hi'}}]}),'answer with request_id');
assert.ok(!Value.Check(schema,{...base,answer:[{request_id:'r1'}]}),'result is required');
assert.ok(!Value.Check(schema,{...base,answer:[{result:'hi'}]}),'result is an object');
assert.ok(!Value.Check(schema,{...base,answer:{result:{}}}),'answer is a list');
console.log('service answer schema contract passed');
"""


def test_pi_service_supervisor_schema_accepts_answer(tmp_path):
    node = shutil.which('node')
    if not node or not (PI_ROOT / 'node_modules/typebox').exists():
        pytest.skip('installed Pi/Node dependencies needed for actual TS contract harness')
    root = Path(__file__).resolve().parents[1]
    extension = tmp_path / 'extension.ts'
    shutil.copy(root / 'cmux_fleet/pi_extension_v2.ts', extension)
    harness = tmp_path / 'harness.mjs'
    harness.write_text(HARNESS)
    (tmp_path / 'node_modules').symlink_to(PI_ROOT / 'node_modules', target_is_directory=True)
    env = dict(os.environ, CMUX_FLEET_PI_SLOT='supervisor', CMUX_FLEET_PI_MODE='service',
               CMUX_FLEET_PI_BRIDGE='["x","supervisor","0"]')
    out = subprocess.run([node, '--experimental-strip-types', str(harness), str(extension)],
                         env=env, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stdout + out.stderr
    assert 'service answer schema contract passed' in out.stdout
