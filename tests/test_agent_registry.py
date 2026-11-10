import json

import pytest

from cmux_fleet.agent_registry import AgentRegistry, AgentRegistryError


def test_default_registry_loads_initial_cards():
    registry = AgentRegistry()
    assert [card.id for card in registry.cards] == [
        "claude-coder", "claude-fable", "claude-haiku", "claude-opus", "claude-reviewer",
        "claude-sonnet", "flashnext-coder", "researcher"
    ]
    assert registry.get("flashnext-coder").cli == "pi"
    assert registry.get("claude-reviewer").cli == "claude"
    assert "web-research" in registry.get("researcher").capabilities


@pytest.mark.parametrize("role_file", ["/tmp/role.md", "../role.md", "C:\\roles\\worker.md"])
def test_registry_rejects_unsafe_role_paths(tmp_path, role_file):
    card = {
        "id": "unsafe-card",
        "display_name": "Unsafe",
        "cli": "codex",
        "capabilities": ["planning"],
        "role_file": role_file,
    }
    (tmp_path / "unsafe.json").write_text(json.dumps(card))
    with pytest.raises(AgentRegistryError, match="role_file"):
        AgentRegistry(tmp_path)


def test_json_registry_uses_only_stdlib_format_support(tmp_path):
    card = {
        "id": "json-agent",
        "display_name": "JSON Agent",
        "cli": "codex",
        "capabilities": ["planning"],
    }
    (tmp_path / "agent.json").write_text(json.dumps(card))
    assert AgentRegistry(tmp_path).get("json-agent").display_name == "JSON Agent"


def test_optional_launch_mapping_is_data_only_and_backward_compatible(tmp_path):
    assert AgentRegistry().get("researcher").model is None
    assert AgentRegistry().get("claude-reviewer").profile is None
    card = {"id": "mapped-agent", "display_name": "Mapped", "cli": "claude",
            "capabilities": ["code-review"], "model": "sonnet", "profile": "review",
            "role_file": "worker.md"}
    (tmp_path / "mapped.json").write_text(json.dumps(card))
    loaded = AgentRegistry(tmp_path).get("mapped-agent").to_dict()
    assert all(loaded[field] == card[field] for field in card)


@pytest.mark.parametrize("field", ["model", "profile"])
@pytest.mark.parametrize("value", ["", 1, True, [], "unsafe\nvalue", "bad\x00value", "model\n", "\tprofile"])
def test_optional_launch_mapping_rejects_invalid_values(tmp_path, field, value):
    card = {"id": "mapped-agent", "display_name": "Mapped", "cli": "codex",
            "capabilities": ["planning"], field: value}
    (tmp_path / "mapped.json").write_text(json.dumps(card))
    with pytest.raises(AgentRegistryError, match=field):
        AgentRegistry(tmp_path)
