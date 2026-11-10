"""New starts use Redis mailboxes; legacy configs remain readable, never hot-flipped."""
from types import SimpleNamespace

import pytest

from cmux_fleet import cmux_manager, fleet, runtime_v2


def no_provisioning(monkeypatch):
    def forbidden():
        pytest.fail('transport rejection must precede cmux/Redis provisioning')
    monkeypatch.setattr(cmux_manager, 'cmux_available', forbidden)


def test_new_cli_defaults_to_v2_without_implicitly_granting_development_permission(monkeypatch):
    seen = []
    monkeypatch.setattr(fleet, 'start', seen.append)
    fleet.main(['start'])
    assert seen[0].transport_version == 2
    assert seen[0].allow_development_v2 is False


def test_new_cli_rejects_v1_before_any_provisioning(monkeypatch):
    no_provisioning(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        fleet.main(['start', '--transport-version', '1'])
    assert exc.value.code == 2


def test_programmatic_start_cannot_bypass_v1_rejection(monkeypatch):
    no_provisioning(monkeypatch)
    with pytest.raises(SystemExit, match='require Redis transport v2'):
        fleet.start(SimpleNamespace(transport_version=1))


def test_new_all_pi_start_still_requires_development_opt_in(monkeypatch):
    no_provisioning(monkeypatch)
    with pytest.raises(SystemExit, match='allow-development-v2'):
        fleet.main(['start', '--supervisor-cli', 'pi', '--worker-cli', 'pi'])


# codex+pi and codex+claude/service left this list when their real smokes verified them (cli_pairs.yaml);
# pi+claude left it when it became a listed pair (2026-10-13 default, tests/test_codex_via_pi.py);
# claude+claude/service left it when its real smoke verified it (2026-11-03).
@pytest.mark.parametrize('supervisor,worker,mode,reason', [
    ('pi', 'codex', 'task', 'mixed Pi/Codex fleets are unvalidated'),
    ('codex', 'codex', 'task', 'Codex workers are unvalidated'),
    ('claude', 'codex', 'task', 'Claude supervisor')])
def test_unsupported_cli_is_not_silently_replaced(monkeypatch, supervisor, worker, mode, reason):
    # 2026-10-12 user decision: v2 also admits Codex supervisor + Claude workers (task mode).
    no_provisioning(monkeypatch)
    with pytest.raises(SystemExit, match=reason):
        fleet.main(['start', '--supervisor-cli', supervisor, '--worker-cli', worker, '--mode', mode,
                    '--allow-development-v2'])


def test_legacy_config_validation_still_preserves_v1():
    assert runtime_v2.validate_config({}) == 1
    assert runtime_v2.validate_config({'transport_version': 1, 'worker_cli': 'claude'}) == 1
