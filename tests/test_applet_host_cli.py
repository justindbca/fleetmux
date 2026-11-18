"""``fleetmux applet`` commands of the process side (docs/applet-interface.md section 5) and the status tree row.

The repo's private test Redis, ``tmp_path`` and fake applet processes only.
"""
import io
import json
import threading
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (a v2 service fleet)
from test_applet_host import FakeProc, WORKERS, audits, write_roster
from fleetmux import applet_cli, applet_host, fleet

ROW = 'applets:\n  - {name: applet-a-1, command: [x], cwd: /, autostart: false}\n'


@pytest.fixture
def cli(fx, monkeypatch):
    f = fx
    write_roster(f, WORKERS + ROW)
    f.roster_tick()
    assert [s['name'] for s in f.applet_specs] == ['applet-a-1']
    monkeypatch.setattr(applet_host, '_signal', lambda *a, **k: None)
    monkeypatch.setattr(applet_host, 'clear_stale', lambda *a, **k: None)
    f.applet_spawn = lambda cfg, spec, token: FakeProc()
    f.config = str(Path(f.cfg['run']) / 'config.json')
    return f


def run(f, *argv, capsys=None):
    code = applet_cli.main(list(argv) + ['--config', f.config])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_list_status_and_unknown_names(cli, capsys):
    f = cli
    applet_host.tick(f)
    f.r.hset(applet_host.key('applet-a-1'), mapping={'phase': 'idle', 'hb_at': '1', 'pid': '7'})
    f.r.sadd(applet_host.key('applet-a-1') + ':leases', 'L-0000000a')
    f.r.hset(applet_host.key('applet-a-1') + ':lease:L-0000000a', mapping={'id': 'L-0000000a', 'state': 'active'})
    f.r.hset(applet_host.key('applet-a-1') + ':esc:applet-a-1.3', mapping={'class': 'plan'})
    code, out = run(f, 'list', capsys=capsys)
    assert code == 0
    [row] = out['applets']
    assert row['name'] == 'applet-a-1' and row['state'] == 'declared' and row['pid'] == '7'
    assert row['leases'] == 1 and row['phase'] == 'idle' and row['declared'] and row['hb_age_s'] > 0
    code, out = run(f, 'status', 'applet-a-1', capsys=capsys)
    assert code == 0 and out['hash']['state'] == 'declared'
    assert out['leases'] == [{'id': 'L-0000000a', 'state': 'active'}]
    assert out['escalations'][0]['class'] == 'plan'
    code, out = run(f, 'status', 'applet-none-1', capsys=capsys)
    assert code == 2 and out['status'] == 'rejected'


def test_start_stop_go_through_the_daemon(cli, capsys):
    f = cli
    applet_host.tick(f)
    result = {}

    def call(*argv):
        result['code'] = applet_cli.main(list(argv) + ['--config', f.config, '--wait', '10'])
    t = threading.Thread(target=call, args=('start', 'applet-a-1'))
    t.start()
    while t.is_alive():
        applet_host.tick(f)
        t.join(.05)
    out = json.loads(capsys.readouterr().out.strip())
    assert result['code'] == 0 and out == {'status': 'ok', 'applet': 'applet-a-1', 'state': 'starting',
                                           'pid': f.applet_runners['applet-a-1'].proc.pid}
    code, out = run(f, 'stop', 'applet-a-1', '--wait', '0', capsys=capsys)
    assert code == 3 and out['status'] == 'pending'
    applet_host.tick(f)
    assert f.r.get(applet_host.CTL_REPLY + out['request_id'])
    code, out = run(f, 'restart', 'applet-none-1', capsys=capsys)
    assert code == 2 and 'declares no applet' in out['reason']


def start_applet(f):
    applet_host.request(f.r, 'applet-a-1', 'start', wait=0)
    applet_host.tick(f)
    return f.applet_runners['applet-a-1'].proc


def test_stop_waits_for_the_process_to_end_and_returns_the_final_state(cli, capsys):
    f = cli
    proc = start_applet(f)
    result = {}

    def call():
        result['code'] = applet_cli.main(['stop', 'applet-a-1', '--config', f.config, '--wait', '10'])
    t = threading.Thread(target=call)
    t.start()
    while t.is_alive():
        applet_host.tick(f)
        if f.r.hget(applet_host.key('applet-a-1'), 'state') == 'stopping':
            proc.code = 0  # the applet ends on SIGTERM
        t.join(.05)
    out = json.loads(capsys.readouterr().out.strip())
    assert result['code'] == 0 and out == {'status': 'ok', 'applet': 'applet-a-1', 'state': 'exited', 'stopped': True}


def test_stop_wait_is_bounded_by_the_stop_grace_and_can_be_skipped(cli, capsys, monkeypatch):
    f = cli
    start_applet(f)
    waits = []
    monkeypatch.setattr(applet_host, 'request', lambda r, name, action, wait=30: {
        'status': 'ok', 'applet': name, 'state': 'stopping'})
    monkeypatch.setattr(applet_host, 'wait_stopped', lambda r, name, wait: waits.append(wait) or 'stopping')
    code, out = run(f, 'stop', 'applet-a-1', capsys=capsys)
    assert waits == [30 + applet_cli.STOP_WAIT_EXTRA_S]  # the row's stop_grace_s (default 30) plus a margin
    assert code == 3 and out['status'] == 'pending' and out['state'] == 'stopping' and out['stopped'] is False
    code, out = run(f, 'stop', 'applet-a-1', '--no-wait-exit', capsys=capsys)
    assert code == 0 and out == {'status': 'ok', 'applet': 'applet-a-1', 'state': 'stopping'} and len(waits) == 1


def test_wait_stopped_polls_until_a_held_state_or_the_bound(cli):
    f = cli
    clock = [0.0]
    naps = []

    def sleep(s):
        naps.append(s)
        clock[0] += s
    f.r.hset(applet_host.key('applet-a-1'), 'state', 'stopping')
    assert applet_host.wait_stopped(f.r, 'applet-a-1', 2, clock=lambda: clock[0], sleep=sleep) == 'stopping'
    assert clock[0] >= 2 and len(naps) < 20
    f.r.hset(applet_host.key('applet-a-1'), 'state', 'exited')
    assert applet_host.wait_stopped(f.r, 'applet-a-1', 2, clock=lambda: clock[0], sleep=sleep) == 'exited'


def test_send_appends_one_json_item(cli, capsys, monkeypatch, tmp_path):
    f = cli
    item = tmp_path / 'item.json'
    item.write_text('{"kind": "branch", "ref": "feat/x"}')
    code, out = run(f, 'send', 'applet-a-1', str(item), capsys=capsys)
    assert code == 0 and out['queued'] == 1
    monkeypatch.setattr('sys.stdin', io.StringIO('[1, 2]'))
    code, out = run(f, 'send', 'applet-a-1', '-', capsys=capsys)
    assert code == 0 and out['queued'] == 2
    assert [json.loads(x) for x in f.r.lrange(applet_host.key('applet-a-1') + ':inbox', 0, -1)] == [
        {'kind': 'branch', 'ref': 'feat/x'}, [1, 2]]
    monkeypatch.setattr('sys.stdin', io.StringIO('nope'))
    code, out = run(f, 'send', 'applet-a-1', '-', capsys=capsys)
    assert code == 2


def test_param_sets_a_hot_value_and_audits_it(cli, capsys):
    f = cli
    code, out = run(f, 'param', 'applet-a-1', 'poll_s', '12', capsys=capsys)
    assert code == 0 and out['value'] == 12
    assert f.r.hget(applet_host.key('applet-a-1') + ':params', 'poll_s') == '12'
    row = audits(f, 'applet.param_changed')[-1]
    assert row['applet'] == 'applet-a-1' and row['key'] == 'poll_s' and row['value'] == '12'
    code, out = run(f, 'param', 'applet-a-1', 'poll_s', '{bad', capsys=capsys)
    assert code == 2


def test_dispatch_table_takes_more_commands():
    calls = []
    applet_cli.register('probe', lambda cfg, r, args: (0, calls.append(args.name) or {'status': 'ok'}),
                        lambda p: p.add_argument('name'), 'a test command')
    try:
        args = applet_cli.parser().parse_args(['probe', 'applet-a-1', '--config', '/nowhere.json'])
        assert args.applet_cmd == 'probe' and args.name == 'applet-a-1'
        assert set(applet_cli.COMMANDS) >= {'list', 'status', 'start', 'stop', 'restart', 'send', 'param'}
    finally:
        applet_cli.COMMANDS.pop('probe')


def test_unreachable_host_is_rejected(tmp_path, capsys):
    code = applet_cli.main(['list', '--config', str(tmp_path / 'missing.json')])
    assert code == 2 and json.loads(capsys.readouterr().out)['status'] == 'rejected'


def test_fleetmux_routes_applet_to_its_own_command_line(monkeypatch):
    seen = []
    monkeypatch.setattr(applet_cli, 'main', lambda argv: seen.append(argv) or 0)
    with pytest.raises(SystemExit) as exc:
        fleet.main(['applet', 'list', '--config', 'x'])
    assert exc.value.code == 0 and seen == [['list', '--config', 'x']]
    assert 'applet' in fleet.cli().format_help()


def test_status_tree_has_an_applets_row(fx, capsys, monkeypatch):
    f = fx
    f.r.hset(applet_host.key('applet-a-1'), mapping={'state': 'up', 'phase': 'p'})
    monkeypatch.setattr('fleetmux.control_view.snapshot', lambda cfg: {'agents': {}})
    fleet.status(f.cfg)
    out = json.loads(capsys.readouterr().out)
    assert out['applets'] == [{'name': 'applet-a-1', 'state': 'up', 'phase': 'p', 'leases': []}]
