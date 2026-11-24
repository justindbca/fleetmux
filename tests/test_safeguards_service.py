"""Safeguards refusal on service-mode worker requests (supervisor ruling Q6, docs/design.md section 23).

Today a service worker's StopFailure fails its request at once and never re-sends it (tests/test_service_stop_failure.py).
A safeguards refusal is a wall instead, through the same wall_commit as a usage wall: the request is not failed, the
instruction is queued once (behind WALL_RESUME) for the next model on the worker chain, the slot is not held. Both the
hook-channel worker (RuntimeV2.api_error_retry) and the clear-after-result worker (RuntimeV2.stop_failure) take it.

Real private Redis / the in-memory plan fixture; no real fleet, no real model.
"""
import json
import time
from pathlib import Path

import pytest
import yaml

from test_clear_after_result import SLOT, enable, event
from test_clear_after_result_redis import inflight
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2_planning import plan  # noqa: F401  (fixture)
from test_v2_codex_service import request, route, service  # noqa: F401  (fixture)
from test_safeguards_stopfail import api_error_row
from test_safeguards_wall import INCIDENT
from test_wall_chain import CACHE, SOL
from fleetmux import brake, discover, fleet


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    path = tmp_path / 'discovered.yaml'
    path.write_text(yaml.safe_dump(CACHE))
    monkeypatch.setenv(discover.CACHE_ENV, str(path))
    return path


def test_service_worker_request_safeguards_is_requeued_once_not_failed(service):
    f = service
    msg = inflight(f, 'r1')
    path = Path(f.agent(SLOT)['transcript'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as fh:
        fh.write(json.dumps(api_error_row(INCIDENT)) + '\n')
    raw = dict(session_id=f.agent(SLOT)['thread'], transcript_path=str(path), error='invalid_request',
               last_assistant_message=INCIDENT, hook_event_name='StopFailure')
    assert fleet.claude_stop_failure(f.path, SLOT, int(f.agent(SLOT)['generation']), json.dumps(raw))
    for _ in range(5):
        f.v2.step(20)
    assert f.r.get(fleet.RESULT + 'r1') is None, 'a safeguards refusal does not fail the request'
    assert f.v2.bus.message(msg['message_id']).get('outcome') == 'walled'
    pending = f.v2.bus.pending(SLOT)
    assert len(pending) == 1 and f.v2.bus.message(pending[0])['body']['text'].startswith(brake.WALL_RESUME)
    info = f.agent(SLOT)
    assert info['state'] != 'held' and not info.get('stopfail_at') and f.paused()
    req_ = json.loads(info['wall_downgrade'])
    assert (req_['kind'], req_['to_cli'], req_['to']) == ('safeguards', 'pi', SOL)


def test_clear_after_result_worker_safeguards_takes_the_wall_path(plan):
    f, v, r, env = enable(plan)
    f.pending_notify = []  # Fleet.__init__ state the bare plan fixture does not set (notify_later)
    v.handle(dict(event(env), kind='stop_failure', error='invalid_request', api_text=INCIDENT))
    assert f.r.get(fleet.RESULT + 'r1') is None, 'not failed: queued again for the next model'
    assert f.r.hget(v.bus.msg_key(env['message_id']), 'outcome') == 'walled'
    assert v.depth(SLOT) == 1 or len(v.bus.pending(SLOT)) == 1
    assert json.loads(f.agent(SLOT)['wall'])['kind'] == 'safeguards'
