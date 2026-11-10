"""One configuration file (config/fleet.yaml) with one block per table, plus the private override."""
import pytest
import yaml

import test_fleet  # noqa: F401  (puts the checkout on sys.path)
from cmux_fleet import agent_registry, brake, cli_pairs, fleet_config, lifecycle


def test_shipped_file_has_exactly_the_blocks():
    shipped = yaml.safe_load(fleet_config.SHIPPED.read_text())
    assert list(shipped) == list(fleet_config.BLOCKS)
    assert shipped['accounts'] == [] and shipped['tools'] == {}  # real accounts live only in the private file
    assert not list(fleet_config.SHIPPED.parent.glob('*.yaml'))[1:]  # one file, no per-table files left


def test_cards_come_from_the_models_and_profiles_blocks():
    registry = agent_registry.AgentRegistry()
    assert registry.directory == agent_registry.PACKAGE_DIR / 'agents'  # role files stay files
    assert registry.get('claude-reviewer').role_file == 'reviewer.md'
    assert registry.get('claude-reviewer').model_card == 'opus'


def test_private_block_replaces_the_shipped_block_whole(tmp_path, monkeypatch):
    user = tmp_path / 'fleet.yaml'
    modes = fleet_config.block('lifecycle_modes')
    modes['common']['marker_for_this_test'] = 1
    user.write_text(yaml.safe_dump({'lifecycle_modes': modes, 'accounts': [{'id': 'a', 'meter': 'none'}]}))
    monkeypatch.setenv(fleet_config.USER_ENV, str(user))
    assert lifecycle.load_modes()['common']['marker_for_this_test'] == 1
    assert list(brake.pool_accounts()) == ['a']
    assert fleet_config.block('walls') == yaml.safe_load(fleet_config.SHIPPED.read_text())['walls']  # untouched


@pytest.mark.parametrize('name', ['cli_pairs', 'watchdog_prompts', 'models', 'profiles', 'capability_matrix', 'other'])
def test_private_file_may_not_override_gates_or_cards(tmp_path, monkeypatch, name):
    user = tmp_path / 'fleet.yaml'
    user.write_text(yaml.safe_dump({name: {}}))
    monkeypatch.setenv(fleet_config.USER_ENV, str(user))
    with pytest.raises(ValueError, match='may not be overridden here'):
        fleet_config.load()


def test_explicit_path_reads_blocks_or_a_bare_table(tmp_path):
    pairs = fleet_config.block('cli_pairs')
    bare, blocks = tmp_path / 'bare.yaml', tmp_path / 'blocks.yaml'
    bare.write_text(yaml.safe_dump(pairs))
    blocks.write_text(yaml.safe_dump({'cli_pairs': pairs}))
    assert cli_pairs.load_pairs(bare).rows == cli_pairs.load_pairs(blocks).rows == cli_pairs.load_pairs().rows
