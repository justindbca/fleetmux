"""Relay parameters: the `relay` block of fleetmux/config/fleet.yaml (no separate relay file, no code defaults)."""
import ast
import json
from pathlib import Path

import pytest
import yaml

import relay_kit  # noqa: F401  (puts the checkout on sys.path)
from test_fleet import FakeRedis
from fleetmux import fleet_config
from fleetmux.relay import params
from fleetmux.relay.errors import ConfigError

PKG = Path(params.__file__).resolve().parent


def test_relay_block_is_registered_and_overridable():
    assert 'relay' in fleet_config.BLOCKS and 'relay' in fleet_config.USER_BLOCKS
    shipped = yaml.safe_load(fleet_config.SHIPPED.read_text())['relay']
    assert shipped['enabled'] is False and shipped['state_remote'] == ''
    assert set(shipped['hot']) == set(params.HOT_KEYS)


def test_shipped_block_matches_the_interface_table():
    hot = yaml.safe_load(fleet_config.SHIPPED.read_text())['relay']['hot']
    assert hot == {'beat_s': 60, 'lost_after_s': 300, 'snapshot_s': 120, 'poll_s': 30, 'git_timeout_s': 20,
                   'max_tasks_per_node': 2, 'max_heavy_per_node': 1, 'disk_floor_heavy_gb': 40,
                   'disk_floor_any_gb': 15, 'score': {'cores': 1.0, 'mem_gb': 0.25, 'disk_free_gb': 0.02},
                   'offline_grace_s': 0, 'claim_stagger_s': 20, 'wake_gap_s': 180, 'settle_s': 600,
                   'budget_pause_gap_s': 60, 'write_back_seconds': 300}


def test_load_reads_the_shipped_block(no_private_file):
    s = params.load()
    assert s.enabled is False and s.state_remote == ''
    assert s.hot('beat_s') == 60 and s.hot('lost_after_s') == 300
    assert s.score_weights() == {'cores': 1.0, 'mem_gb': 0.25, 'disk_free_gb': 0.02}


def test_private_block_overlays_key_by_key(tmp_path, monkeypatch):
    private = tmp_path / 'private.yaml'
    private.write_text('relay:\n  enabled: true\n  hot:\n    beat_s: 30\n')
    monkeypatch.setenv('FLEETMUX_CONFIG', str(private))
    s = params.load()
    assert s.enabled is True
    assert s.hot('beat_s') == 30 and s.hot('lost_after_s') == 300      # the rest stays shipped


@pytest.mark.parametrize('key', ['beat_s', 'lost_after_s', 'score', 'settle_s', 'write_back_seconds'])
def test_every_hot_key_is_required(key):
    block = relay_kit.block()
    del block['hot'][key]
    with pytest.raises(ConfigError, match=key):
        params.load(block)


@pytest.mark.parametrize('overrides, needle', [
    ({'beat_s': 0}, 'beat_s'),
    ({'max_tasks_per_node': 0}, 'max_tasks_per_node'),
    ({'max_heavy_per_node': 1.5}, 'max_heavy_per_node'),
    ({'offline_grace_s': -1}, 'offline_grace_s'),
    ({'disk_floor_any_gb': 50, 'disk_floor_heavy_gb': 40}, 'disk_floor_any_gb'),
    ({'lost_after_s': 70, 'beat_s': 60, 'git_timeout_s': 20}, 'lost_after_s'),
    ({'poll_s': 300}, 'poll_s'),
    ({'score': {'cores': 1.0}}, 'score'),
    ({'score': {'cores': 1.0, 'mem_gb': 'x', 'disk_free_gb': 0}}, 'score'),
])
def test_bad_values_and_relations_rejected(overrides, needle):
    with pytest.raises(ConfigError, match=needle):
        params.load(relay_kit.block(**overrides))


def test_unknown_key_rejected():
    block = relay_kit.block()
    block['hot']['peer_lost_s'] = 300
    with pytest.raises(ConfigError, match='peer_lost_s'):
        params.load(block)


def test_hot_reads_the_store_each_time_and_keeps_last_good_value():
    r = FakeRedis()
    hot = params.RedisHot(r)
    block = relay_kit.block()
    params.seed(hot, block)
    s = params.load(block, hot=hot)
    assert s.hot('beat_s') == 60
    r.hset(params.REDIS_KEY, mapping={'beat_s': json.dumps(45)})
    assert s.hot('beat_s') == 45                                       # next read sees the change
    r.hset(params.REDIS_KEY, mapping={'beat_s': json.dumps(-3)})
    assert s.hot('beat_s') == 45                                       # a breaking value is not used
    assert params.changed(hot, block) == {'beat_s': -3}
    with pytest.raises(KeyError):
        s.hot('no_such_key')


def _literals(path):
    """Numeric literals of a module other than 0, 1, 2 and those bound to a module-level UPPER_CASE name."""
    tree = ast.parse(path.read_text())
    named = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id.isupper() for t in node.targets):
            named.update(id(n) for n in ast.walk(node.value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float) and id(node) not in named:
            if node.value not in (0, 1, 2):
                out.append(f'{path.name}:{node.lineno}: {node.value!r}')
    return out


def test_no_relay_yaml_and_no_threshold_literal_in_code():
    assert not list(PKG.rglob('relay.yaml'))
    offenders = [x for path in sorted(PKG.glob('*.py')) if path.name != 'params.py' for x in _literals(path)]
    assert offenders == []


@pytest.fixture
def no_private_file(tmp_path, monkeypatch):
    monkeypatch.setenv('FLEETMUX_CONFIG', str(tmp_path / 'absent.yaml'))


def test_coordinator_block_has_defaults_and_is_read(no_private_file):
    shipped = yaml.safe_load(fleet_config.SHIPPED.read_text())['relay']
    assert shipped['coordinator'] == {'enabled': True, 'prefer': '', 'takeover': True}
    s = params.load()
    assert s.coordinator == {'enabled': True, 'prefer': '', 'takeover': True}


def test_private_coordinator_overlays_key_by_key(tmp_path, monkeypatch):
    private = tmp_path / 'private.yaml'
    private.write_text('relay:\n  coordinator:\n    prefer: server\n')
    monkeypatch.setenv('FLEETMUX_CONFIG', str(private))
    assert params.load().coordinator == {'enabled': True, 'prefer': 'server', 'takeover': True}


@pytest.mark.parametrize('coord, needle', [
    ({'enabled': 'yes', 'prefer': '', 'takeover': True}, 'coordinator.enabled'),
    ({'enabled': True, 'prefer': None, 'takeover': True}, 'coordinator.prefer'),
    ({'enabled': True, 'prefer': 'bad name/x', 'takeover': True}, 'coordinator.prefer'),
    ({'enabled': True, 'prefer': '', 'takeover': 1}, 'coordinator.takeover'),
    ({'enabled': True, 'prefer': ''}, 'coordinator.takeover'),
    ({'enabled': True, 'prefer': '', 'takeover': True, 'role': 'x'}, 'coordinator.role'),
])
def test_bad_coordinator_values_rejected(coord, needle):
    block = relay_kit.block()
    block['coordinator'] = coord
    with pytest.raises(ConfigError, match=needle):
        params.load(block)


def test_one_accessor_for_daemon_and_flow_code(no_private_file):
    """fleet_config.relay() is the only call the daemon and the flow code need: the validated relay block."""
    s = fleet_config.relay()
    assert isinstance(s, params.Settings)
    assert s.enabled is False and s.state_remote == ''
    assert s.coordinator == {'enabled': True, 'prefer': '', 'takeover': True}
    assert s.hot('beat_s') == 60
    hot = params.DictHot(relay_kit.block()['hot'])
    assert fleet_config.relay(hot=hot).store is hot


def test_shipped_relay_block_has_the_whole_key_table():
    shipped = yaml.safe_load(fleet_config.SHIPPED.read_text())['relay']
    assert set(shipped) == set(params.COLD_KEYS) | {'hot'}
    assert set(shipped['hot']) == set(params.HOT_KEYS)


def test_accessor_reads_the_private_block_and_refuses_a_bad_one(tmp_path, monkeypatch):
    private = tmp_path / 'private.yaml'
    monkeypatch.setenv('FLEETMUX_CONFIG', str(private))
    private.write_text('relay:\n  enabled: true\n')
    assert fleet_config.relay().enabled is True
    private.write_text('relay:\n  enabled: "yes"\n')
    with pytest.raises(ValueError, match='relay.enabled'):
        fleet_config.relay()


def test_write_back_puts_changed_good_hot_values_into_the_private_file_only(tmp_path, monkeypatch):
    private = tmp_path / 'private.yaml'
    private.write_text('gui: {theme: dark}\nrelay:\n  enabled: true\n  hot:\n    poll_s: 25\n')
    monkeypatch.setenv('FLEETMUX_CONFIG', str(private))
    shipped_before = fleet_config.SHIPPED.read_bytes()
    block = params.file_block()
    hot = params.RedisHot(FakeRedis())
    params.seed(hot, block)
    assert params.write_back(hot, block) == {}                         # nothing changed: file untouched
    hot.set('beat_s', 45)
    hot.set('settle_s', -1)                                            # a breaking value is never written
    assert params.write_back(hot, block) == {'beat_s': 45}
    data = yaml.safe_load(private.read_text())
    assert data['gui'] == {'theme': 'dark'} and data['relay']['enabled'] is True
    assert data['relay']['hot'] == {'poll_s': 25, 'beat_s': 45}
    assert fleet_config.SHIPPED.read_bytes() == shipped_before
    assert params.load().hot('beat_s') == 45


def test_write_back_creates_the_private_file_and_refuses_the_shipped_one(tmp_path, monkeypatch):
    private = tmp_path / 'sub' / 'fleet.yaml'
    monkeypatch.setenv('FLEETMUX_CONFIG', str(private))
    block = relay_kit.block()
    hot = params.DictHot(block['hot'])
    hot.set('poll_s', 40)
    assert params.write_back(hot, block) == {'poll_s': 40}
    assert yaml.safe_load(private.read_text()) == {'relay': {'hot': {'poll_s': 40}}}
    with pytest.raises(ConfigError, match='shipped'):
        params.write_back(hot, block, path=fleet_config.SHIPPED)


def test_limits_formerly_in_code_are_cold_keys_with_defaults(no_private_file):
    """Q47: the sysctl probe timeout and the notice / refusal-reason lengths are relay parameters."""
    shipped = yaml.safe_load(fleet_config.SHIPPED.read_text())['relay']
    assert (shipped['probe_timeout_s'], shipped['notify_max'], shipped['reason_max']) == (5, 1000, 200)
    s = params.load()
    assert (s.probe_timeout_s, s.notify_max, s.reason_max) == (5, 1000, 200)


@pytest.mark.parametrize('key, bad', [('probe_timeout_s', 0), ('notify_max', 0), ('reason_max', 2.5),
                                      ('notify_max', True)])
def test_bad_limit_values_rejected(key, bad):
    block = relay_kit.block()
    block[key] = bad
    with pytest.raises(ConfigError, match=key):
        params.load(block)


THRESHOLD_SUFFIXES = ('_S', '_MAX', '_MIN', '_LIMIT', '_TIMEOUT', '_SECONDS')


def test_no_threshold_named_constant_in_code():
    """The UPPER_CASE exemption of the literal guard covers format constants only: a module-level constant whose
    name reads as a limit (..._S, ..._MAX, ...) is a parameter and belongs in the relay block (Q47)."""
    bad = []
    for path in sorted(PKG.glob('*.py')):
        if path.name == 'params.py':
            continue
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.isupper() and t.id.endswith(THRESHOLD_SUFFIXES):
                        bad.append(f'{path.name}:{node.lineno}: {t.id}')
    assert bad == []
