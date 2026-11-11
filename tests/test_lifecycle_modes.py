"""Lifecycle modes (D1): the fleet_modes.yaml table, start --lifecycle, cfg['lifecycle'] / cfg['mode']."""
import os
import shutil
from datetime import date

import pytest
import yaml

from fleetmux import cmux_manager, fleet, lifecycle, runtime_v2
from fleetmux import fleet_config
from test_pi import mock_start


def no_provisioning(monkeypatch):
    def forbidden():
        pytest.fail('lifecycle rejection must precede cmux/Redis provisioning')
    monkeypatch.setattr(cmux_manager, 'cmux_available', forbidden)


def with_lifecycle(args, name):
    args = list(args)
    args[args.index('--lifecycle') + 1] = name
    return args


@pytest.fixture
def table(tmp_path):
    path = tmp_path / 'fleet_modes.yaml'
    path.write_text(fleet_config.SHIPPED.read_text())
    return path


def touch_later(path, text):
    stat = path.stat()
    path.write_text(text)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))  # a distinct mtime even on coarse clocks


# ------------------------------------------------------------------ table
def test_packaged_table_has_six_modes_and_bases():
    table = lifecycle.load_modes()
    assert tuple(table['modes']) == lifecycle.MODE_NAMES
    assert {m: lifecycle.base_mode(m) for m in lifecycle.MODE_NAMES} == {
        'service': 'service', 'dev_task': 'task', 'program': 'task',
        'scheduled': 'task', 'review_loop': 'task', 'smoke': 'task'}
    assert table['require_mode_on_start'] is True


def test_common_policy_is_merged_into_every_mode():
    for name in lifecycle.MODE_NAMES:
        policy = lifecycle.get_mode(name)
        assert policy['on_blocked'] == {'hold_hours': 12, 'then': 'stop_keep_resumable', 'notify': ['tg', 'parent']}
        assert policy['close_workspace_after_stop'] is True
    assert lifecycle.mode_policy('scheduled', 'hard_timeout_min') == 150  # sector rotation runs on a 150-minute budget
    assert lifecycle.mode_policy('review_loop', 'max_rounds') == 3
    assert lifecycle.mode_policy('service', 'idle_stop_hours', 'never') == 'never'  # null -> default
    assert lifecycle.mode_policy('dev_task', 'review_step')['reviewer'] == 'opus'


def test_hot_reload_takes_effect_on_next_read(table):
    assert lifecycle.get_mode('smoke', table)['on_blocked']['hold_hours'] == 12
    touch_later(table, table.read_text().replace('hold_hours: 12', 'hold_hours: 3', 1))
    assert lifecycle.get_mode('smoke', table)['on_blocked']['hold_hours'] == 3


def test_broken_edit_keeps_last_good_table(table, capsys):
    assert lifecycle.base_mode('service', table) == 'service'
    touch_later(table, 'version: 1\nmodes: {service: {base: nonsense}}\n')
    assert lifecycle.base_mode('service', table) == 'service'
    assert 'keeping last good table' in capsys.readouterr().err


def test_broken_table_without_a_good_one_is_refused(tmp_path):
    bad = tmp_path / 'bad.yaml'
    bad.write_text('version: 1\nmodes: {smoke: {base: task}}\n')
    with pytest.raises(ValueError, match='missing: service'):
        lifecycle.load_modes(bad)


# ------------------------------------------------------------------ start resolution
@pytest.mark.parametrize('name,base', [('service', 'service'), ('dev_task', 'task'), ('smoke', 'task')])
def test_resolve_start_maps_lifecycle_to_mode(name, base):
    assert lifecycle.resolve_start(name) == (name, base)
    assert lifecycle.resolve_start(name, base) == (name, base)  # an agreeing --mode is fine


def test_resolve_start_refuses_missing_unknown_and_contradicting():
    with pytest.raises(ValueError, match='requires --lifecycle.*service, dev_task, program, scheduled, review_loop, smoke'):
        lifecycle.resolve_start(None)
    with pytest.raises(ValueError, match="unknown --lifecycle 'task'; choose one of"):
        lifecycle.resolve_start('task')
    with pytest.raises(ValueError, match='runs on --mode service, not --mode task'):
        lifecycle.resolve_start('service', 'task')


def test_table_may_drop_the_requirement(table):
    touch_later(table, table.read_text().replace('require_mode_on_start: true', 'require_mode_on_start: false'))
    assert lifecycle.resolve_start(None, None, table) == (None, 'task')
    assert lifecycle.resolve_start(None, 'service', table) == (None, 'service')


def test_start_without_lifecycle_is_refused_before_provisioning(monkeypatch):
    no_provisioning(monkeypatch)
    with pytest.raises(SystemExit, match='requires --lifecycle <mode>; choose one of: service, dev_task'):
        fleet.main(['start', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--allow-development-v2'])


@pytest.mark.parametrize('argv,match', [
    (['--lifecycle', 'nightly'], "unknown --lifecycle 'nightly'"),
    (['--lifecycle', 'service', '--mode', 'task'], 'not --mode task'),
])
def test_start_refuses_bad_lifecycle_before_provisioning(monkeypatch, argv, match):
    no_provisioning(monkeypatch)
    with pytest.raises(SystemExit, match=match):
        fleet.main(['start', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--allow-development-v2'] + argv)


# ------------------------------------------------------------------ config passing (A: cli_pairs)
@pytest.mark.parametrize('name,base', [('smoke', 'task'), ('dev_task', 'task'), ('service', 'service')])
def test_start_writes_lifecycle_and_mode_into_config(tmp_path, monkeypatch, name, base):
    args, _ = mock_start(tmp_path, monkeypatch)
    fleet.main(with_lifecycle(args, name))
    latest = fleet.load(tmp_path / 'latest.json')
    cfg = fleet.load(latest['config'])
    assert (cfg['lifecycle'], cfg['mode']) == (name, base)
    assert cfg['claude_supervisor_hook_delivery'] is True  # new runs pin the supervisor's channel
    assert cfg['adopted_supervisor_hook_delivery'] is True  # and the adopted (sub-fleet) supervisor's
    assert (latest['lifecycle'], latest['mode']) == (name, base)


def a_pair_problem(cfg):
    """Stand-in for A's cli_pairs.pair_problem (worker-1/A-interface.md §2): codex+pi is
    unverified, so it is admitted only when cfg['lifecycle'] == 'smoke'; cfg['mode'] stays task|service."""
    assert cfg['mode'] in ('task', 'service')
    if (cfg.get('supervisor_cli'), cfg.get('worker_cli')) == ('codex', 'pi'):
        return None if cfg.get('lifecycle') == 'smoke' else 'codex+pi is unverified: only --lifecycle smoke'
    return None


def test_pair_check_sees_lifecycle_and_base_mode(monkeypatch):
    seen = []

    def check(cfg):
        seen.append({k: cfg.get(k) for k in ('lifecycle', 'mode', 'supervisor_cli', 'worker_cli')})
        return a_pair_problem(cfg)
    monkeypatch.setattr(runtime_v2, 'v2_cli_problem', check)

    class Reached(Exception):
        pass

    def reached():
        raise Reached
    monkeypatch.setattr(cmux_manager, 'cmux_available', reached)
    base = ['start', '--supervisor-cli', 'codex', '--worker-cli', 'pi', '--allow-development-v2']
    with pytest.raises(Reached):  # smoke admits the unverified pair -> provisioning is reached
        fleet.main(base + ['--lifecycle', 'smoke'])
    with pytest.raises(SystemExit, match='only --lifecycle smoke'):
        fleet.main(base + ['--lifecycle', 'dev_task'])
    assert seen == [{'lifecycle': 'smoke', 'mode': 'task', 'supervisor_cli': 'codex', 'worker_cli': 'pi'},
                    {'lifecycle': 'dev_task', 'mode': 'task', 'supervisor_cli': 'codex', 'worker_cli': 'pi'}]


def test_current_pair_rules_still_read_mode_from_lifecycle_base(monkeypatch, tmp_path):
    # v2_cli_problem keys on cfg['mode']: --lifecycle service must reach the pair table as mode
    # service. A x D integration: codex+claude/service is verified now, so a stub table forbids it.
    import yaml
    from fleetmux import cli_pairs
    table = fleet_config.block('cli_pairs')
    for item in table['pairs']:
        if (item['supervisor_cli'], item['worker_cli'], item['mode']) == ('codex', 'claude', 'service'):
            item.update(status='forbidden', reason='service base reached the pair table')
    path = tmp_path / 'cli_pairs.yaml'
    path.write_text(yaml.safe_dump(table))
    monkeypatch.setattr(cli_pairs, 'DEFAULT_PATH', path)
    no_provisioning(monkeypatch)
    with pytest.raises(SystemExit, match='service base reached the pair table'):
        fleet.main(['start', '--lifecycle', 'service', '--supervisor-cli', 'codex', '--worker-cli', 'claude',
                    '--allow-development-v2'])


# ------------------------------------------------------------------ clock gate (scheduled)
class Clock:
    def __init__(self, open_days):
        self.open_days, self.asked = open_days, []

    def is_trading_day(self, market, day):
        self.asked.append((market, day))
        return day in self.open_days


def test_clock_gate_only_for_gated_lifecycles():
    closed = Clock(set())
    assert lifecycle.clock_gate_problem('dev_task', clock=closed, now=date(2026, 12, 25)) is None
    assert lifecycle.clock_gate_problem(None, clock=closed) is None
    assert closed.asked == []
    problem = lifecycle.clock_gate_problem('scheduled', clock=closed, now=date(2026, 12, 25))
    assert 'US market closed on 2026-12-25' in problem and closed.asked == [('US', date(2026, 12, 25))]
    assert lifecycle.clock_gate_problem('scheduled', clock=Clock({date(2026, 10, 13)}), now=date(2026, 10, 13)) is None


def test_clock_gate_fails_closed_without_global_clock(monkeypatch, tmp_path):
    monkeypatch.setenv(lifecycle.CLOCK_ENV, str(tmp_path / 'missing'))
    assert 'global_clock is unavailable' in lifecycle.clock_gate_problem('scheduled', now=date(2026, 10, 13))


def test_clock_gate_loads_global_clock_from_env(monkeypatch, tmp_path):
    (tmp_path / 'global_clock.py').write_text(
        'def is_trading_day(market, d):\n    return d.weekday() < 5\n')
    monkeypatch.setenv(lifecycle.CLOCK_ENV, str(tmp_path))
    assert 'closed' in lifecycle.clock_gate_problem('scheduled', now=date(2026, 10, 17))  # Saturday
    assert lifecycle.clock_gate_problem('scheduled', now=date(2026, 10, 13)) is None


def test_scheduled_start_on_closed_day_launches_nothing(tmp_path, monkeypatch):
    args, ran = mock_start(tmp_path, monkeypatch)
    monkeypatch.setattr(lifecycle, '_clock_module', lambda: Clock(set()))
    with pytest.raises(SystemExit, match='market closed'):
        fleet.main(with_lifecycle(args, 'scheduled'))
    assert ran == [] and not (tmp_path / 'runs').exists() and not (tmp_path / 'latest.json').exists()


# ------------------------------------------------------------------ result.json
def test_result_json_records_lifecycle_mode(tmp_path):
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    fleet.save(run / 'config.json', {'lifecycle': 'smoke', 'mode': 'task'})
    fleet.save_result(run, {'id': 't1', 'status': 'completed', 'artifacts': '[]'})
    result = fleet.load(run / 'result.json')
    assert (result['mode'], result['base_mode'], result['status']) == ('smoke', 'task', 'completed')


def test_result_json_of_legacy_run_has_no_lifecycle(tmp_path):
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    fleet.save_result(run, {'id': 't1', 'status': 'blocked', 'artifacts': '[]'})
    result = fleet.load(run / 'result.json')
    assert (result['mode'], result['base_mode']) == (None, 'task')
