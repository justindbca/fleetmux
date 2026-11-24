"""Human notices must have somewhere to go (docs/design.md section 24 "No route for human notices").

Incident (2026-11-16, root ops): every notice for a human went nowhere: channel tg -> no_channel, channel parent ->
no_initiator, for 5 days, and nothing said so. Now a root fleet with no route at all (no channels entry that pushes
run_closed / daemon_down, no FLEETMUX_TG_CMD / notify_commands.tg, no initiator) says so at start and in status, and
every notice that left on no leg is counted in ``<run>/notify-undelivered.json`` with the full text of the latest one.
A root without a lifecycle counts too (it never calls lifecycle_close.notify). Switch:
``fleet.yaml#daemon_watch.no_route_alarm`` (default on).

Private test Redis, fake cmux, run directories under tmp_path; no real ~/.fleetmux.
"""
import io
import json
import time
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

import pytest

from test_fleet import fx  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_service_escalate import audits, svc  # noqa: F401  (fixture)
from test_subfleet_blocked_scope import REASON, child, held_note, private_yaml
from fleetmux import fleet, fleet_config, lifecycle_close, notify_route, pi_adapter, subfleet


@pytest.fixture(autouse=True)
def no_route_env(monkeypatch):
    monkeypatch.delenv(lifecycle_close.TG_ENV, raising=False)
    monkeypatch.delenv(lifecycle_close.PARENT_ENV, raising=False)
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', None)


def counted(run):
    path = notify_route.path(run)
    return json.loads(path.read_text()) if path.exists() else {'count': 0}


def fake_fleet(tmp_path, **cfg):
    run = tmp_path / 'run'
    run.mkdir(exist_ok=True)
    base = {'run': str(run), 'lifecycle': 'dev_task'}
    base.update(cfg)
    return SimpleNamespace(cfg=base, service=True, task=lambda: {}, log=lambda *a, **k: None, notifier=None)


def stuck_again(f, monkeypatch):
    monkeypatch.setattr(subfleet, 'child_progress', lambda cfg, rec: {'paused': True})
    monkeypatch.setattr(subfleet, 'stuck_reasons', lambda f_, d_, rec, facts, after: ['paused for 1000 s'])
    f.subfleet_watch_at = {}
    subfleet.watch_children(f)


def status_out(f):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        fleet.status(f.cfg)
    return json.loads(out.getvalue()), err.getvalue()


# ------------------------------------------------------------------ route_problem
@pytest.mark.parametrize('cfg,policy,env', [
    ({'lifecycle': 'dev_task', 'channels': [{'kind': 'telegram', 'push': ['run_closed']}]}, {}, None),
    ({'channels': [{'kind': 'telegram'}]}, {}, None),  # default push carries daemon_down
    ({'lifecycle': 'dev_task'}, {}, 'cat >/dev/null'),
    ({'lifecycle': 'dev_task'}, {'notify_commands': {'tg': 'cat >/dev/null'}}, None),
    ({'lifecycle': 'dev_task', 'initiator': {'kind': 'tmux', 'id': 'x'}}, {}, None),
])
def test_route_problem_is_none_when_any_route_exists(tmp_path, monkeypatch, cfg, policy, env):
    if env:
        monkeypatch.setenv(lifecycle_close.TG_ENV, env)
    assert notify_route.route_problem(dict(cfg, run=str(tmp_path)), policy) is None


def test_route_problem_names_the_missing_routes(tmp_path, monkeypatch):
    text = notify_route.route_problem({'run': str(tmp_path), 'lifecycle': 'dev_task'}, {})
    assert text and 'nowhere' in text and 'channels' in text and 'initiator' in text
    # a root without a lifecycle never calls lifecycle_close.notify: only channels reach a human
    monkeypatch.setenv(lifecycle_close.TG_ENV, 'cat >/dev/null')
    assert notify_route.route_problem({'run': str(tmp_path)}, {})
    # a fleet with a parent reports upwards: never a problem here
    assert notify_route.route_problem({'run': str(tmp_path), 'parent_config': '/p/config.json'}, {}) is None
    monkeypatch.delenv(lifecycle_close.TG_ENV)
    # an initiator does not count when the mode does not notify the parent session
    assert notify_route.route_problem({'run': str(tmp_path), 'lifecycle': 'dev_task', 'initiator': {
        'kind': 'tmux', 'id': 'x'}}, {'notify_parent_session': False})


# ------------------------------------------------------------------ counting
def test_notify_with_every_leg_unsent_counts_once_and_keeps_the_full_last_text(tmp_path):
    f = fake_fleet(tmp_path)
    lifecycle_close.notify(f, 'subfleet_stuck', 'first notice', {})
    long_text = '[sub-fleet stuck] ' + 'x' * 5000
    recs = lifecycle_close.notify(f, 'slot_held', long_text, {})
    assert [r['status'] for r in recs] == ['no_channel', 'no_initiator']
    got = counted(f.cfg['run'])
    assert got['count'] == 2 and got['last']['event'] == 'slot_held' and got['last']['text'] == long_text
    assert got['last']['at'] > 0


def test_partial_delivery_is_not_counted(tmp_path, monkeypatch):
    monkeypatch.setenv(lifecycle_close.TG_ENV, 'true')
    f = fake_fleet(tmp_path)
    recs = lifecycle_close.notify(f, 'slot_held', 'held', {})
    assert [r['status'] for r in recs] == ['sent', 'no_initiator']
    assert counted(f.cfg['run'])['count'] == 0


def test_a_child_fleet_is_never_counted(tmp_path):
    f = fake_fleet(tmp_path, parent_config='/p/config.json')
    lifecycle_close.notify(f, 'slot_held', 'held', {})
    assert counted(f.cfg['run'])['count'] == 0


def test_unmanaged_root_stuck_notice_is_counted_also_when_the_child_is_marked(svc, monkeypatch):
    f = svc
    d = child(f)
    stuck_again(f, monkeypatch)
    got = counted(f.cfg['run'])
    assert got['count'] == 1 and got['last']['event'] == 'subfleet_stuck'
    assert got['last']['text'].startswith('[sub-fleet stuck] sub-fleet M7')
    subfleet.save_record(d, dict(subfleet.record(d), attention={'reason': REASON, 'notice': 'stuck',
                                                                'at': time.time()},
                                 stuck_alert_at=str(time.time() - 4000)))
    stuck_again(f, monkeypatch)  # marked: not to the supervisor, and still nowhere for a human
    assert counted(f.cfg['run'])['count'] == 2


def test_managed_root_without_routes_counts_through_notify(svc, monkeypatch):
    f = svc
    child(f)
    monkeypatch.setattr(lifecycle_close, 'managed', lambda fl: fl is f)
    stuck_again(f, monkeypatch)
    got = counted(f.cfg['run'])
    assert got['count'] == 1 and got['last']['event'] == 'subfleet_stuck'
    rows = [json.loads(x) for x in (f.run_dir / 'notifications.jsonl').read_text().splitlines()]
    assert [r['status'] for r in rows] == ['no_channel', 'no_initiator']


def test_unmanaged_root_held_notice_is_counted(svc):
    f = svc
    d = child(f)
    f.held_notice(held_note(f, d))
    got = counted(f.cfg['run'])
    assert got['count'] == 1 and got['last']['event'] == 'slot_held'
    assert got['last']['text'].startswith('[sub-fleet held] sub-fleet M7')


@pytest.fixture
def channel_sends(monkeypatch):
    """channels.actor.send_direct recorded; it answers 'sent' (no real channel is ever reached)."""
    from fleetmux.channels import actor
    out = []
    monkeypatch.setattr(actor, 'send_direct', lambda cfg, item: out.append(item) or {'status': 'sent'})
    return out


def test_unmanaged_root_with_a_channel_sends_held_and_stuck_through_channel_alarm(svc, channel_sends, monkeypatch):
    """Ruling 28: a root without a lifecycle but with channels sends the notice through RuntimeV2.channel_alarm
    (event daemon_down); a sent notice is not counted."""
    f = svc
    f.cfg['channels'] = [{'kind': 'telegram'}]
    d = child(f)
    f.held_notice(held_note(f, d))
    stuck_again(f, monkeypatch)
    assert [x['event'] for x in channel_sends] == ['daemon_down', 'daemon_down']
    assert '[sub-fleet held] sub-fleet M7' in channel_sends[0]['text']
    assert '[sub-fleet stuck] sub-fleet M7' in channel_sends[1]['text']
    assert counted(f.cfg['run'])['count'] == 0
    rows = [json.loads(x) for x in (f.run_dir / 'notifications.jsonl').read_text().splitlines()]
    assert [(r['event'], r['status']) for r in rows] == [('slot_held', 'sent'), ('subfleet_stuck', 'sent')]


def test_unmanaged_root_whose_channels_do_not_push_daemon_down_counts(svc, channel_sends, monkeypatch):
    f = svc
    f.cfg['channels'] = [{'kind': 'telegram', 'push': ['result']}]
    assert notify_route.route_problem(f.cfg), 'a root without a lifecycle is reached only through daemon_down'
    d = child(f)
    f.held_notice(held_note(f, d))
    assert channel_sends == []
    got = counted(f.cfg['run'])
    assert got['count'] == 1 and got['last']['text'].startswith('[sub-fleet held] sub-fleet M7')
    stuck_again(f, monkeypatch)
    got = counted(f.cfg['run'])
    assert got['count'] == 2 and got['last']['text'].startswith('[sub-fleet stuck] sub-fleet M7')


def test_managed_root_with_channels_is_unchanged(svc, channel_sends, monkeypatch):
    """A managed root goes through lifecycle_close.notify (its tg leg: run_closed entries only), never
    channel_alarm, for these notices."""
    f = svc
    f.cfg['channels'] = [{'kind': 'telegram', 'push': ['daemon_down']}]
    monkeypatch.setattr(lifecycle_close, 'managed', lambda fl: fl is f)
    child(f)
    stuck_again(f, monkeypatch)
    assert channel_sends == []
    rows = [json.loads(x) for x in (f.run_dir / 'notifications.jsonl').read_text().splitlines()]
    assert [r['status'] for r in rows] == ['no_channel', 'no_initiator']
    assert counted(f.cfg['run'])['count'] == 1


def test_switch_off_sends_nothing_through_channel_alarm(svc, channel_sends, tmp_path, monkeypatch):
    f = svc
    private_yaml(tmp_path, monkeypatch, 'daemon_watch:\n  no_route_alarm: false\n')
    f.cfg['channels'] = [{'kind': 'telegram'}]
    d = child(f)
    f.held_notice(held_note(f, d))
    assert channel_sends == [] and counted(f.cfg['run'])['count'] == 0


def test_root_supervisor_held_without_channels_is_counted(svc):
    f = svc
    f.v2.held_escalate('supervisor', 'held (aborted); its turn is not proven stopped', {'held_s': 400})
    got = counted(f.cfg['run'])
    assert got['count'] == 1 and got['last']['event'] == 'supervisor_held' and 'supervisor' in got['last']['text']


# ------------------------------------------------------------------ start and status
def test_start_line_and_daemon_alarm(svc):
    f = svc
    err = io.StringIO()
    with redirect_stderr(err):
        notify_route.start_warning(f.cfg)
    assert 'human notices have nowhere to go' in err.getvalue()
    notify_route.startup_alarm(f)
    rows = audits(f, 'notify.no_route')
    assert len(rows) == 1 and 'nowhere' in json.dumps(rows[0])
    f.cfg['channels'] = [{'kind': 'telegram'}]
    err = io.StringIO()
    with redirect_stderr(err):
        notify_route.start_warning(f.cfg)
    assert err.getvalue() == ''


def test_status_v2_reports_no_route_count_and_last(svc):
    f = svc
    out, err = status_out(f)
    assert out['human_notice'] == {'route': 'none', 'problem': notify_route.route_problem(f.cfg),
                                   'undelivered': 0, 'last': None}
    assert 'human notices have nowhere to go' in err
    d = child(f)
    f.held_notice(held_note(f, d))
    out, err = status_out(f)
    assert out['human_notice']['undelivered'] == 1
    assert out['human_notice']['last']['text'].startswith('[sub-fleet held]')
    assert 'undelivered 1' in err
    f.cfg['channels'] = [{'kind': 'telegram'}]
    notify_route.path(f.cfg['run']).unlink()
    out, err = status_out(f)
    assert 'human_notice' not in out and 'nowhere' not in err


def test_status_v1_reports_no_route(fx, monkeypatch):  # noqa: F811
    f = fx
    monkeypatch.setattr(fleet, 'connect', lambda cfg, **kw: f.r)
    out, err = status_out(f)
    assert out['human_notice']['route'] == 'none' and 'human notices have nowhere to go' in err


# ------------------------------------------------------------------ switch
def test_switch_off_neither_warns_nor_counts(svc, tmp_path, monkeypatch):
    f = svc
    private_yaml(tmp_path, monkeypatch, 'daemon_watch:\n  no_route_alarm: false\n')
    assert notify_route.route_problem(f.cfg) is None
    d = child(f)
    f.held_notice(held_note(f, d))
    assert counted(f.cfg['run'])['count'] == 0
    out, err = status_out(f)
    assert 'human_notice' not in out


def test_switch_is_shipped_validated_and_never_exported_to_pi(tmp_path, monkeypatch):
    """Needs the shared files (step 2): fleet.yaml#daemon_watch.no_route_alarm and pi_adapter.HELD_KEYS."""
    assert fleet_config.block('daemon_watch')['no_route_alarm'] is True
    assert 'no_route_alarm' in pi_adapter.HELD_KEYS
    private_yaml(tmp_path, monkeypatch, 'daemon_watch:\n  no_route_alarm: 3\n')
    knobs, problem = notify_route.knobs()
    assert knobs['no_route_alarm'] is True and 'no_route_alarm' in problem
    private_yaml(tmp_path, monkeypatch, 'daemon_watch:\n  no_route_alarm: false\n')
    assert pi_adapter.daemon_watch() == {'check_interval_s': 120, 'stale_after_s': 120, 'realert_after_s': 600}
