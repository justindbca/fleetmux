"""Transport v2 Codex supervisor + official Claude Code workers in service mode.

The missing piece was a renderer for UI-only result cards: a Codex TUI cannot show
them without making them a model turn. `tui_cards` renders them off-model into
`<run>/cards/<slot>.log` and the history page "Cards" view. Real private Redis +
real hook entry points, fake cmux/CLIs (the real TUI run is scripts/smoke_pair.sh).
"""
import json
import sys
import uuid

import pytest

from test_fleet import FakeMgr
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, no_message_files, typed
from cmux_fleet import cli_pairs, fleet, runtime_v2, tui_cards

WORKERS = ['worker-1', 'worker-2']


def test_codex_claude_service_verified_by_real_smoke_and_has_no_missing_adapter():
    cfg = dict(transport_version=2, supervisor_cli='codex', worker_cli='claude', mode='service',
               allow_development_v2=True)
    row = cli_pairs.load_pairs().lookup('codex', 'claude', 'service')
    assert row['status'] == 'verified' and row['adapter_missing'] == [] and 'smoke_pair.sh' in row['verified_by']
    assert runtime_v2.validate_config(cfg) == 2
    with pytest.raises(ValueError, match='allow-development-v2'):
        runtime_v2.validate_config(dict(cfg, allow_development_v2=False))


@pytest.fixture
def service(private_redis_v2, tmp_path):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    sessions = tmp_path / 'codex-home' / 'sessions'
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'],
               python=sys.executable, workers=list(WORKERS), mode='service', supervisor_cli='codex',
               worker_cli='claude', codex_home=str(tmp_path / 'codex-home'), transport_version=2,
               allow_development_v2=True, lifecycle='smoke', stall_timeout=0, v2_lease_ms=300000, max_busy=0)
    path = run / 'config.json'
    fleet.save(path, cfg)
    f = fleet.Fleet(path, r, FakeMgr(), sessions=sessions)
    f.test_sessions, f.test_claude = sessions, tmp_path / 'claude-projects'
    f.test_threads = {slot: str(uuid.uuid4()) for slot in ['supervisor'] + WORKERS}
    real_trust = fleet.trust_claude_dir
    fleet.trust_claude_dir = lambda cwd: None  # never touch the operator's ~/.claude.json
    try:
        f.start_all()
        r.set(f.v2.prefix + ':boot', 'running')
        for slot in f.names:
            assert answer_turn(f, slot, 'READY', startup=True)['status'] == 'ready'
        yield f
    finally:
        fleet.trust_claude_dir = real_trust
        f.v2.close()


def request(f, rid, message='Summarise the overnight fills'):
    spec = dict(id=rid, payload={'message': message}, reply={'required': ['reply'], 'allowed': ['reply']})
    runtime_v2.post_event(f.v2.bus, dict(kind='request', request_id='cmd-' + rid, spec=spec), 'cmd-' + rid)
    assert f.v2.step(10)
    f.pump()


def route(rid, to='worker-1'):
    return json.dumps({'status': 'RUNNING', 'skip': [],
                       'dispatch': [{'to': to, 'request_id': rid, 'instruction': 'Answer in the reply field.'}]})


def serve(f, rid, answer='{"reply": "fills ok"}'):
    request(f, rid)
    assert 'Reply with the service decision JSON from AGENTS.md.' in typed(f, 'supervisor')[-1]
    assert answer_turn(f, 'supervisor', route(rid))['status'] == 'committed'
    f.pump()
    body = typed(f, 'worker-1')[-1]
    assert 'Answer in the reply field.' in body and 'fleet_result' not in body   # Pi-only tool
    assert 'Your final reply must be exactly one JSON object with the keys: reply' in body
    return answer_turn(f, 'worker-1', answer)


def card_lines(f, slot='supervisor'):
    path = tui_cards.log_path(f.run_dir, slot)
    return path.read_text().splitlines() if path.exists() else []


def test_codex_routes_request_claude_answers_public_result(service):
    f = service
    assert serve(f, 'r1')['status'] == 'committed'
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['result'] == {'reply': 'fills ok'}
    assert f.agent('worker-1')['pane'] not in dict(getattr(f.mgr, 'bracketed', []))  # Stop-hook channel: never typed
    assert no_message_files(f)


def test_result_card_rendered_off_model_never_typed_or_queued(service):
    f = service
    serve(f, 'r1')
    sent = len(f.mgr.sent) + len(f.mgr.queued)  # neither pasted nor queued
    f.v2.render_cards()
    assert any('worker-1：fills ok' in line for line in card_lines(f))
    assert any('worker-1：fills ok' in line for line in card_lines(f, 'worker-1'))  # Claude worker too
    assert 'worker-1：fills ok' in (f.run_dir / 'history.html').read_text()
    assert len(f.mgr.sent) + len(f.mgr.queued) == sent and not f.v2.depth('supervisor')
    assert not any('fills ok' in text for text in typed(f, 'supervisor'))   # never in Codex context
    rendered = [x[1] for x in f.r.xrange(f.v2.bus.audit_key) if x[1].get('event') == 'ui.rendered']
    assert rendered and all(json.loads(x['detail'])['log'].endswith('.log') for x in rendered)


def test_renderer_cursor_survives_restart_without_duplicates(service):
    f = service
    serve(f, 'r1')
    f.v2.render_cards()
    before = card_lines(f)
    f.v2.render_cards()                                   # nothing new
    f.v2.cards_dirty = False
    restarted = runtime_v2.RuntimeV2(f)                   # same run, new daemon object
    try:
        restarted.render_cards()
    finally:
        restarted.close()
    assert card_lines(f) == before


def test_invalid_and_timeout_results_render_as_cards(service):
    f = service
    serve(f, 'r1', answer='{"reply": 7, "extra": 1}')    # schema failure -> invalid public result
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['status'] == 'invalid'
    f.cfg['request_timeout'] = 900                        # explicit run deadline (none by default, §43)
    request(f, 'r2')
    f.r.hset(fleet.REQ + 'r2', 'created', 0)              # long past its timeout
    f.v2.tick()
    assert json.loads(f.r.get(fleet.RESULT + 'r2'))['status'] == 'timeout'
    text = '\n'.join(card_lines(f))
    assert text.count('\n') >= 1 and len(card_lines(f)) >= 2


def test_service_smoke_gate_still_requires_development_flag():
    cfg = dict(transport_version=2, supervisor_cli='codex', worker_cli='claude', mode='service', lifecycle='smoke')
    with pytest.raises(ValueError, match='allow-development-v2'):
        runtime_v2.validate_config(cfg)


def test_tui_service_supervisor_routes_without_seeing_request_ids(service):
    # Real smoke finding (codex+pi/service): v2 strips the [Request <id>] header before typing, so
    # the official-CLI supervisor's role must not ask for request ids; the daemon binds the id.
    f = service
    role = (fleet.agent_cwd(f.cfg, 'supervisor') / 'AGENTS.md').read_text()
    assert '[Request <id>]' not in role and '"request_id"' not in role
    assert 'The daemon binds its id' in role
    assert 'judge each request yourself' in role and '"answer": []' in role  # M0: may answer simple requests
    request(f, 'r9')
    assert '[Request' not in typed(f, 'supervisor')[-1] and 'r9' not in typed(f, 'supervisor')[-1]
    bare = json.dumps({'status': 'RUNNING', 'skip': [],
                       'dispatch': [{'to': 'worker-1', 'instruction': 'Answer in the reply field.'}]})
    assert answer_turn(f, 'supervisor', bare)['status'] == 'committed'
    f.pump()
    assert 'Answer in the reply field.' in typed(f, 'worker-1')[-1]
    assert answer_turn(f, 'worker-1', '{"reply": "bound"}')['status'] == 'committed'
    assert json.loads(f.r.get(fleet.RESULT + 'r9'))['result'] == {'reply': 'bound'}
