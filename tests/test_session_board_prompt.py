"""Every supervisor prompt carries the cross-session collision section, and the fleet fills in the board command.

User 2026-11-08: the supervisor every fleetmux run starts coordinates all collisions on the session board —
first come, first served; the later session waits; once the first is done the supervisor releases it with a note."""
import shlex

from test_v2_codex_claude import mixed, no_provisioning  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from cmux_fleet import fleet

SUPERVISOR_PROMPTS = ('supervisor.md', 'supervisor_v2.md', 'service_supervisor.md', 'service_supervisor_v2.md',
                      'service_supervisor_tui_v2.md')
HEADING = '### Cross-session collisions (the session board)'


def test_every_supervisor_prompt_explains_the_session_board():
    agents = fleet.HERE / 'agents'
    for name in SUPERVISOR_PROMPTS:
        text = (agents / name).read_text()
        assert HEADING in text and text.count(HEADING) == 1, name
        assert '{{SESSION_BOARD}} list' in text and '{{SESSION_BOARD}} note' in text, name
        assert 'first come, first served' in text, name
        assert 'editing the roster file is allowed' in text and 'session-board commands' in text, name  # carve-out


def test_the_section_is_the_same_in_every_supervisor_prompt():
    agents = fleet.HERE / 'agents'
    sections = set()
    for name in SUPERVISOR_PROMPTS:
        text = (agents / name).read_text()
        body = text.split(HEADING, 1)[1]
        sections.add(body.split('\n## ', 1)[0].split('\n### ', 1)[0])
    assert len(sections) == 1


def test_contracts_supervisor_does_not_repeat_it():
    """contracts_supervisor.md is appended to a base supervisor prompt that already has the section, and
    contract_doc() only fills {{WORKSPACE}}: a copy there would duplicate it and leak the raw placeholder."""
    text = (fleet.HERE / 'agents' / 'contracts_supervisor.md').read_text()
    assert HEADING not in text and '{{SESSION_BOARD}}' not in text


def test_supervisor_prompt_names_the_board_command(mixed):  # noqa: F811
    scope = mixed.role_scope('supervisor', mixed.runtime_cfg('supervisor'), {})
    command = shlex.join([mixed.cfg['python'], str(fleet.HERE / 'session_board.py')])
    assert '{{' not in scope and HEADING in scope
    assert '`' + command + ' list`' in scope and '`' + command + ' note ' in scope
