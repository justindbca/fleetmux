"""Resident advisor tab in a service-mode main fleet (docs/design.md §27): the Studio ops fleet runs
`--mode service` with roster `advisor: on`; its advisor tab sits next to the supervisor like in task mode,
and a held first generation's consultation goes to that tab (never cancelled as a finished request).
Real private Redis, fake cmux, real Stop-hook entry points (no `claude` is started)."""
import json

from test_advisor_resident import SHEET, advisor_answers, consultations, logs, ready_advisor, resident
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, typed
from test_v2_codex_service import request, route
from cmux_fleet import fleet


def service_fleet(r, tmp_path, monkeypatch, workers, **extra):
    return resident(r, tmp_path, monkeypatch, workers, mode='service', lifecycle='smoke', **extra)


def test_service_main_fleet_starts_the_advisor_tab_next_to_the_supervisor(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    gen = service_fleet(private_redis_v2, tmp_path, monkeypatch, ['worker-1'])
    f = next(gen)
    try:
        assert f.service and f.advisor_enabled() and f.advisor_resident()
        assert f.names == ['supervisor', 'worker-1', 'advisor'] and f.workers == ['worker-1']
        info = f.agent('advisor')
        assert info['role'] == 'advisor' and info['label'] == 'advisor'
        assert (info['pane'], f.agent('supervisor')['pane']) in f.mgr.tabbed  # the supervisor's pane, not a worker's
        assert info['pane'] not in f.mgr.arranged[1]
        assert 'advisor' not in f.dispatch_targets()
        assert logs(f, 'advisor.started') or any(  # daemon log (legacy) or v2 audit
            json.loads(line).get('event') == 'advisor.started'
            for line in (f.run_dir / 'events.jsonl').read_text().splitlines()
            if (f.run_dir / 'events.jsonl').exists())
    finally:
        gen.close()


def test_service_held_worker_is_advised_by_the_resident_tab_then_answers_the_request(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    gen = service_fleet(private_redis_v2, tmp_path, monkeypatch, ['worker-1'])
    f = next(gen)
    try:
        assert f.agent('worker-1')['advice_hold'] == '1'  # an ordinary Claude worker waits for its first sheet
        request(f, 'r1')
        assert answer_turn(f, 'supervisor', route('r1'))['status'] == 'committed'
        f.pump()
        held = f.agent('worker-1')
        pending = json.loads(held['advice_pending'])  # queued for the tab, not the synchronous one-shot
        assert held['advice_hold'] == '1' and pending['kind'] == 'dispatch'
        ready_advisor(f)
        f.pump()  # service delivery must not cancel a consultation as "already terminal" (no REQ for its ticket)
        text, = consultations(f)
        assert text.startswith('[Advisor consultation ' + pending['ticket'] + ']')
        assert 'Answer in the reply field.' in json.loads(text.split('\n', 1)[1])['instruction']
        assert advisor_answers(f, json.dumps(SHEET))['status'] == 'committed'
        f.pump()
        info = f.agent('worker-1')
        assert info['advice_hold'] == '0' and info['launch_ready'] == '1'
        assert fleet.load(f.run_dir / 'runtime' / 'worker-1-g0.json')['worker_model'] == 'opus'
        audit = f.r.hgetall(fleet.ADVICE + pending['ticket'])
        assert audit['status'] == 'ok' and audit['effect'] == 'launch'
        assert answer_turn(f, 'worker-1', 'READY', startup=True)['status'] == 'ready'
        f.pump()
        assert 'Answer in the reply field.' in typed(f, 'worker-1')[-1]
        assert answer_turn(f, 'worker-1', '{"reply": "fills ok"}')['status'] == 'committed'
        assert json.loads(f.r.get(fleet.RESULT + 'r1'))['result'] == {'reply': 'fills ok'}
        assert not f.paused() and not f.r.lrange('fx:replies:advisor', 0, -1)
    finally:
        gen.close()


def test_sub_fleets_never_start_an_advisor_tab(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    gen = service_fleet(private_redis_v2, tmp_path, monkeypatch, ['worker-1'])
    f = next(gen)
    try:
        assert f.advisor_resident()
        f.cfg['parent_config'] = str(tmp_path / 'parent' / 'config.json')  # a (named) sub-fleet of a parent
        assert not f.advisor_resident()
        f.cfg.pop('parent_config')
        f.cfg['adopt'] = {'pane': 'x', 'slot': 'memx-ops'}
        assert not f.advisor_resident()
    finally:
        gen.close()
