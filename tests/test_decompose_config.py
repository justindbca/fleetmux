"""The decompose block of fleetmux/config/fleet.yaml (docs/design.md §62): shipped on, fields as planned, allowed in
the private file, validated; the test suite runs the old path unless a test asks for the tree."""
import pytest
import yaml

from fleetmux import decompose, fleet_config

PLANNED = {'enabled': True, 'unit': 'minutes', 'split': {'k': 2, 'margin': 7.4, 'floor': 2},
           'cost': {'cold_start': 7.4, 'per_decision': 0.32, 'merge': 1.45, 'supervisor': 1.0, 'spawn': 0.3},
           'overrun_ratio': 5, 'returns_alert': 3, 'write_back_seconds': 300, 'session_minute_tokens': 9472}
# cold_start (margin follows it), per_decision and merge: p50 of measured task-mode distributions; supervisor 1.0 is
# the upper bound by definition (not measured); session_minute_tokens: provisional, display only


def test_the_shipped_block_is_on_with_the_planned_fields():
    shipped = fleet_config.block('decompose', fleet_config.SHIPPED)
    assert shipped == PLANNED
    assert decompose.validate_block(shipped) == []


def test_decompose_is_a_block_the_private_file_may_set(tmp_path, monkeypatch):
    assert 'decompose' in fleet_config.BLOCKS and 'decompose' in fleet_config.USER_BLOCKS
    private = tmp_path / 'fleet.yaml'
    private.write_text(yaml.safe_dump({'decompose': dict(PLANNED, enabled=False)}))
    monkeypatch.setenv(fleet_config.USER_ENV, str(private))
    assert decompose.block()['enabled'] is False  # the private block replaces the shipped one as a whole


@pytest.mark.parametrize('bad, error', [
    ({'colour': 1}, 'colour is not a decompose key'),
    ({'enabled': 'yes'}, 'enabled must be true or false'),
    ({'unit': 'tool_calls'}, 'unit must be minutes'),
    ({'split': {'k': 0}}, 'split.k must be a number'),
    ({'cost': {'merge': -1}}, 'cost.merge must be a number'),
    ({'overrun_ratio': 1}, 'overrun_ratio has a bad value'),
    ({'returns_alert': 2.5}, 'returns_alert has a bad value'),
])
def test_a_bad_block_is_refused_and_reads_as_off(bad, error, tmp_path, monkeypatch):
    assert any(error in e for e in decompose.validate_block(bad))
    private = tmp_path / 'fleet.yaml'
    private.write_text(yaml.safe_dump({'decompose': bad}))
    monkeypatch.setenv(fleet_config.USER_ENV, str(private))
    with pytest.raises(ValueError):
        decompose.block()


def test_tests_run_the_old_path_unless_they_ask(request):
    assert decompose.enabled() is False  # conftest: the suite proves the switch-off path is the baseline


def test_the_decompose_on_fixture_reads_the_shipped_switch(decompose_on):
    assert decompose.enabled() is True
