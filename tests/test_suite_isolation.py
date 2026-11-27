"""The suite never reaches the operator's own Claude Code config.

``fleet.CLAUDE_JSON`` is bound to ``~/.claude.json`` at import, so changing HOME in a test does not move it; only the
module global does. ``fleet.trust_claude_dir`` reads it and writes it (plus ``.fleet.lock`` / ``.fleet-tmp`` next to
it) for every Claude slot a test starts in-process (``fleet.actor``, ``Fleet.spawn``, ``Fleet.spawn_advisor``).
tests/conftest.py points it at a per-test file; this checks that every test sees that, without calling anything
that would touch the file.
"""
import json
import os
import pwd
from pathlib import Path

from fleetmux import fleet

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def test_the_claude_config_the_fleet_trusts_dirs_in_is_a_per_test_file(tmp_path):
    assert fleet.CLAUDE_JSON != REAL_HOME / '.claude.json', fleet.CLAUDE_JSON
    assert tmp_path in fleet.CLAUDE_JSON.parents, (fleet.CLAUDE_JSON, tmp_path)
    assert not fleet.CLAUDE_JSON.exists()          # nothing in it until a test writes it


def test_a_test_that_sets_its_own_claude_config_still_wins(tmp_path, monkeypatch):
    own = tmp_path / 'own' / 'claude.json'
    monkeypatch.setattr(fleet, 'CLAUDE_JSON', own)   # as tests/test_fleet.py and others do
    assert fleet.CLAUDE_JSON == own


def test_trusting_an_agent_dir_writes_only_the_per_test_claude_json(tmp_path):
    """The write itself (fleet.trust_claude_dir, the C1 path of fleet.actor / Fleet.spawn) lands in the per-test file,
    with its lock beside it."""
    agent = tmp_path / 'run' / 'workspace' / 'worker-1'
    agent.mkdir(parents=True)
    fleet.trust_claude_dir(agent)
    assert tmp_path in fleet.CLAUDE_JSON.parents
    data = json.loads(fleet.CLAUDE_JSON.read_text())
    assert data['projects'][str(agent.resolve())]['hasClaudeMdExternalIncludesWarningShown'] is True, data
    assert Path(str(fleet.CLAUDE_JSON) + '.fleet.lock').exists()
