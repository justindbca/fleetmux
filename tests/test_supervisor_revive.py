"""§36 the daemon revives the supervisor: frozen in flight, lost bridge heartbeat (pi), CLI exited (Claude / Codex).
Each goes through the existing rotation; the successor's first message carries the summary from the logs and
`supervisor.revived` is audited. Real private Redis (test fixture) + fake cmux, pi supervisor bridge simulated."""
import time
import uuid

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, session, settle  # noqa: F401  (fixture)
from cmux_fleet import runtime_v2


def audits(f, name):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == name]


def screens(f):
    f.cfg.update(watch_interval=0)
    f.mgr.stats = {}
    f.mgr.capture_output = lambda pane, lines=2000: 'pi> working…'
    f.mgr.process_states = lambda pane: f.mgr.stats.get(pane, ['Ss', 'S+'])
    f.mgr.owns = lambda pane: pane in f.mgr.alive


def decided_then_busy(f):
    """One decision applied (committed), then a second operator message in flight to the supervisor."""
    event(f, dict(session(f, 'supervisor'), kind='operator_input', text='让 worker-1 回复 OK', request_id='click-1'))
    f.pump()
    settle(f, 'supervisor', receive(f, 'supervisor'),
           {'status': 'RUNNING', 'dispatch': [{'to': 'worker-1', 'instruction': 'Reply OK in the reply field.'}], 'skip': []})
    event(f, dict(session(f, 'supervisor'), kind='operator_input', text='再让 worker-2 回复 OK', request_id='click-2'))
    f.pump()
    env = receive(f, 'supervisor')
    assert env and f.v2.bus.slot_state('supervisor')['status'] == 'received'
    return env


def first_message(f):
    return open(f.mgr.spawned[-1]['prompt_file']).read()


def assert_revived(f, old_pane, in_flight):
    sup = f.agent('supervisor')
    assert sup['generation'] == '1' and old_pane in f.mgr.killed
    prompt = first_message(f)
    assert prompt.startswith('[Same-task takeover]') and '[Revive summary]' in prompt
    assert 'Reply OK in the reply field' in prompt  # the last decision the daemon applied
    revived, = audits(f, 'supervisor.revived')
    assert revived['label'] == sup['label'] and float(revived['stuck_s']) >= 0 and revived['stuck_since']
    assert 'Reply OK in the reply field' in revived['decisions']
    if in_flight:
        assert in_flight in prompt and 'NOT applied' in prompt
        assert revived['in_flight'] == in_flight and revived['applied'] == 'false'
    else:
        assert 'No message was in flight' in prompt and revived['in_flight'] == ''


def test_frozen_supervisor_in_flight_is_revived_with_the_summary(fx):  # noqa: F811
    f = fx
    screens(f)
    env = decided_then_busy(f)
    old = f.agent('supervisor')['pane']
    f.mgr.stats[old] = ['Ss', 'U+']  # stuck in the kernel
    now = time.time()
    f.v2.watchdog.tick(now)
    f.v2.watchdog.tick(now + 1)
    assert audits(f, 'watch.frozen_restarted')
    assert_revived(f, old, env['message_id'])


def test_pi_supervisor_whose_bridge_stopped_beating_is_revived(fx):  # noqa: F811
    f = fx
    screens(f)
    event(f, dict(session(f, 'supervisor'), kind='operator_input', text='让 worker-1 回复 OK', request_id='click-1'))
    f.pump()
    settle(f, 'supervisor', receive(f, 'supervisor'),
           {'status': 'RUNNING', 'dispatch': [{'to': 'worker-1', 'instruction': 'Reply OK in the reply field.'}], 'skip': []})
    old = f.agent('supervisor')['pane']
    key = runtime_v2.heartbeat_key(f.v2.bus, 'supervisor')
    f.v2.bus.r.set(key, '0')
    f.v2.tick()
    assert f.agent('supervisor')['bridge_hb_g'] == '0'
    f.v2.bus.r.delete(key)  # pi and its bridge are gone
    f.v2.tick()
    assert audits(f, 'link.restarted') and not f.paused()
    assert_revived(f, old, None)


def tui_supervisor(f, monkeypatch, live):
    monkeypatch.setitem(f.cfg, 'supervisor_cli', 'claude')
    f.mgr.has_cli_process = lambda pane, names=('codex',): live[0]
    f.next_liveness = 0


def test_official_cli_supervisor_whose_cli_exited_is_marked_lost_after_two_probes(fx, monkeypatch):  # noqa: F811
    f = fx
    live = [False]
    tui_supervisor(f, monkeypatch, live)
    monkeypatch.setattr(f.v2.watchdog, 'tick', lambda now: None)  # the restart itself is the link-lost path above
    f.v2.tick()
    assert f.agent('supervisor')['state'] == 'idle' and f.agent('supervisor')['cli_missing'] == '1'
    live[0] = True  # a slow probe, then the CLI is there: the count starts over
    f.next_liveness = 0
    f.v2.tick()
    assert f.agent('supervisor')['cli_missing'] == '0'
    live[0] = False
    for _ in range(2):
        f.next_liveness = 0
        f.v2.tick()
    sup = f.agent('supervisor')
    assert sup['state'] == 'held' and sup['link_lost'] == '1' and not f.paused()
    held, = [x for x in audits(f, 'execution.held') if x.get('slot') == 'supervisor']
    assert 'supervisor CLI exited' in held['error']


def test_cli_probe_leaves_a_pi_supervisor_and_a_starting_one_alone(fx, monkeypatch):  # noqa: F811
    f = fx
    f.mgr.has_cli_process = lambda pane, names=('codex',): False
    for _ in range(3):  # pi: its bridge heartbeat is the signal, not a CLI process name
        f.next_liveness = 0
        f.v2.tick()
    assert f.agent('supervisor')['state'] == 'idle' and not f.agent('supervisor').get('cli_missing')
    tui_supervisor(f, monkeypatch, [False])
    f.setf('supervisor', runtime_ready=0)  # not READY yet: startup has its own deadline
    for _ in range(3):
        f.next_liveness = 0
        f.v2.tick()
    assert f.agent('supervisor')['state'] == 'idle' and not f.agent('supervisor').get('link_lost')
