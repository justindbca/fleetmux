"""Roster `channels:` and run config (package P1a; brief §6.2, docs/design.md §53, docs/contracts.md §8).

Pure parsing through ``roster.parse_entries``; `start` through ``test_pi.mock_start`` (fake cmux / Redis
provisioning); refusals through ``no_provisioning``; hot reload on the private test Redis (service:
``test_runtime_v2.fx``, task: ``test_roster.hot``). The config dir is a temp dir; no network, no listener.

Baseline (``[基线]``) texts are written out: the ``split`` wording that `26392b9` (§44 long task split) added is
taken from that commit, every other text is the `4cd1156` original, unchanged since.
"""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (fixture: a v2 service fleet)
from test_v2_codex_claude import WORKERS, mixed, no_provisioning  # noqa: F401  (mixed: fixture)
from test_roster import hot, write_roster  # noqa: F401  (hot: fixture, a task fleet with a roster)
from test_pi import mock_start
from fleetmux import channels, fleet, night, roster

SERVICE_ONLY = '只挂本机常驻 service fleet'
BOT1 = 'channels:\n  - {type: telegram, account: fleet-bot-1}\n'


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    d = tmp_path / 'cfgdir'
    monkeypatch.setenv(channels.CONFIG_ENV, str(d))
    return d


def errors_of(text, tmp_path):
    with pytest.raises(ValueError) as exc:
        roster.parse_entries(text, tmp_path)
    return str(exc.value)


def audits(f, event):
    return [fields for _, fields in f.r.xrange(f.v2.bus.audit_key) if fields.get('event') == event]


def restamp(f, path):
    """A distinct mtime for the next roster_tick (same scheme as test_roster.write_roster)."""
    stamp = getattr(f, 'test_stamp', 1_700_000_000_000_000_000) + 1_000_000_000
    f.test_stamp = stamp
    os.utime(path, ns=(stamp, stamp))


W1 = 'workers:\n  - {name: worker-1}\n'


def test_roster_without_channels_parses_exactly_as_before(tmp_path):
    ok = ('workers:\n  - {name: worker-1}\n  - {name: worker-2, window: 1000000, handoff_ratio: 0.7}\n'
          'handoff: {codex: {ratio: 0.6}}\nlimits: {max_children: 2}\n')
    assert roster.parse_entries(ok, tmp_path) == (['worker-1', 'worker-2'], {}, {})
    assert errors_of('workers:\n  - {name: worker-1}\n  - {name: worker-1}\n', tmp_path) == \
        'workers[1]: duplicate worker name worker-1'
    assert errors_of(W1 + 'handoff: {claude: {ratio: 0}}\n', tmp_path) == \
        'handoff[claude]: ratio must be a number between 0 and 1'
    assert errors_of('workers:\n  - {name: worker-1, model: x}\n', tmp_path) == (  # 26392b9 added "split"
        'workers[0]: must be a mapping with "name" and optional "role", "profile", "writable", "window", '
        '"handoff_ratio", "split" only (pi workers also "pi_model", "pi_thinking")')
    assert errors_of(W1 + 'agents: []\n', tmp_path) == (  # 26392b9 added "split"; P1a must not change this text
        'the roster must be a mapping with only a "workers" list '
        '(and optional "limits", "handoff", "advisor", "subfleets", "tabs", "split", "max_busy")')  # main max_busy
    assert errors_of(W1 + 'limits: {max_depth: 0}\n', tmp_path) == \
        'limits: unknown key or not a positive integer: max_depth'


def test_channels_errors_join_the_roster_value_error(tmp_path):
    text = W1 + 'handoff: {claude: {ratio: 0}}\nchannels:\n  - {type: pigeon, account: a}\n'
    lines = errors_of(text, tmp_path).split('\n')
    assert any(line.startswith('channels[0]: type must be one of') for line in lines)
    assert any(line.startswith('handoff[claude]: ratio must be') for line in lines)


def test_channels_unknown_key_message_points_to_config_dir(tmp_path):
    text = W1 + 'channels:\n  - {type: telegram, account: fleet-bot-1, token: s3cr3t-value}\n'
    msg = errors_of(text, tmp_path)
    assert 'channels[0]' in msg and str(channels.config_dir()) in msg
    assert 's3cr3t-value' not in msg


def test_limits_accepts_split_budget_h_and_rejects_bad_values(tmp_path):
    assert roster.parse_limits({'split_budget_h': 2}) == {'split_budget_h': 2}
    for bad in (0, '4', True):
        with pytest.raises(ValueError, match='split_budget_h'):
            roster.parse_limits({'split_budget_h': bad})
    assert roster.parse_entries(W1 + 'limits: {split_budget_h: 2}\n', tmp_path)[0] == ['worker-1']
    with pytest.raises(ValueError, match='split_budget_h'):
        roster.parse_entries(W1 + 'limits: {split_budget_h: 0}\n', tmp_path)


def started_config(tmp_path, monkeypatch, roster_text):
    tmp_path.mkdir(parents=True, exist_ok=True)
    args, _ = mock_start(tmp_path, monkeypatch)
    args[args.index('--lifecycle') + 1] = 'service'
    path = tmp_path / 'r.yaml'
    path.write_text(roster_text)
    fleet.main(args + ['--roster', str(path)])
    return fleet.load(tmp_path / 'latest.json')


def test_start_pins_channels_into_config(tmp_path, monkeypatch):
    plain = started_config(tmp_path / 'plain', monkeypatch, W1)
    assert 'channels' not in plain
    cfg = started_config(tmp_path / 'with', monkeypatch, W1 + BOT1)
    expected, errors = channels.parse_channels([{'type': 'telegram', 'account': 'fleet-bot-1'}])
    assert not errors and expected[0]['push'] == list(channels.DEFAULT_PUSH)
    assert cfg['channels'] == expected


def test_hot_reload_updates_cfg_channels_and_bad_edit_keeps_old(fx, tmp_path):  # noqa: F811
    f = fx
    path = f.run_dir / 'roster.yaml'
    workers = 'workers:\n  - name: worker-1\n  - name: worker-2\n'
    path.write_text(workers)
    restamp(f, path)
    f.cfg['roster'] = str(path)
    fleet.save(f.path, dict(fleet.load(f.path), roster=str(path)))
    f.roster_tick()  # same workers: nothing changes
    assert 'channels' not in f.cfg
    path.write_text(workers + BOT1)  # only channels changed
    restamp(f, path)
    f.roster_tick()
    want, _ = channels.parse_channels([{'type': 'telegram', 'account': 'fleet-bot-1'}])
    assert f.cfg.get('channels') == want
    assert fleet.load(f.path).get('channels') == want  # persist_roster wrote it
    path.write_text(workers + 'channels:\n  - {type: pigeon, account: fleet-bot-1}\n')
    restamp(f, path)
    f.roster_tick()
    rejected = audits(f, 'roster.rejected')
    assert len(rejected) == 1 and 'channels[0]' in rejected[0]['errors']
    assert f.cfg['channels'] == want and fleet.load(f.path)['channels'] == want
    # R3: a night on / off must not drop or change the channels
    day, nite = tmp_path / 'day.yaml', tmp_path / 'night.yaml'
    day.write_text(workers + BOT1)
    for other in (workers, workers + 'channels:\n  - {type: telegram, account: fleet-bot-2}\n'):
        nite.write_text(other)
        with pytest.raises(ValueError, match='"channels:"'):
            night.check_rosters({}, day, nite)
    nite.write_text(workers + BOT1)
    night.check_rosters({}, day, nite)


def test_task_mode_subfleet_and_v1_refuse_channels(hot, monkeypatch, tmp_path):  # noqa: F811
    # (1) task mode: refused at start, before any provisioning ...
    no_provisioning(monkeypatch)
    path = tmp_path / 'r.yaml'
    path.write_text(W1 + BOT1)
    base = ['start', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--allow-development-v2']
    with pytest.raises(SystemExit, match=SERVICE_ONLY):
        fleet.main(base + ['--lifecycle', 'dev_task', '--roster', str(path)])
    # ... and on a hot reload of a running task fleet: rejected whole, cfg unchanged
    f = hot
    live = write_roster(f, WORKERS)
    live.write_text(live.read_text() + BOT1)
    restamp(f, live)
    f.roster_tick()
    rejected = audits(f, 'roster.rejected')
    assert len(rejected) == 1 and SERVICE_ONLY in rejected[0]['errors']
    assert 'channels' not in f.cfg and 'channels' not in fleet.load(f.path) and f.workers == WORKERS
    # (2) a named sub-fleet's roster: refused where the parent roster checks it
    (tmp_path / 'sup.md').write_text('x')
    (tmp_path / 'child.yaml').write_text('workers:\n  - name: analyst-1\n' + BOT1)
    parent = tmp_path / 'parent.yaml'
    parent.write_text('workers:\n  - name: worker-1\nsubfleets:\n'
                      '  - {name: ops, supervisor_slot: worker-1, supervisor_role: sup.md, roster: child.yaml}\n')
    with pytest.raises(SystemExit, match=r'subfleets\[0\] \(ops\): .*' + SERVICE_ONLY):
        fleet.main(base + ['--roster', str(parent)])
    # (3) v1: the existing early refusals, no channels-specific wording, no provisioning
    with pytest.raises(SystemExit):
        fleet.main(['start', '--transport-version', '1', '--roster', str(path)])
    with pytest.raises(SystemExit, match='new fleet instances require Redis transport v2'):
        fleet.start(SimpleNamespace(transport_version=1, resume=None, kit_fleet=None, max_attempts=None,
                                    roster=str(path)))


def test_channels_count_capped_at_8_via_roster(tmp_path):
    rows = ''.join('  - {type: telegram, account: bot-' + str(i) + '}\n' for i in range(9))
    msg = errors_of(W1 + 'channels:\n' + rows, tmp_path)
    assert 'at most 8' in msg
