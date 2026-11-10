"""The supervisor's "Daemon down" section gains one channel step (P6d): brief v2 §6.15 + approve-1 test-plan §4.3.

With ``cfg['channels']`` the section tells the supervisor, between step 3 (PROGRESS.md) and step 4 (the restart),
to run ``<same prefix> channel send --config <run>/config.json --event daemon_down --text …`` once, without
retrying and without letting a failure stop the restart. Without channels the section is byte-for-byte what it
was; a sub-fleet (``parent_config``) never gets the step. Pure text: no process, no network.
"""
import hashlib
import re
import shlex
from pathlib import Path

import pytest

from cmux_fleet import channels, fleet

RUN = '/tmp/fleetmux-p6d-run'
FLEET_PY = str(Path(fleet.__file__).resolve())
CHANNELS = [{'type': 'telegram', 'account': 'fleet-bot-1', 'push': list(channels.DEFAULT_PUSH)}]
# sha256 of daemon_down_section({'python': '/usr/bin/python3', 'run': RUN}, watched) with the fleet.py path
# replaced by <fleet.py>, taken on the frozen base 60ac389 (feat/channels) before P6d: {watched: digest}.
BASELINE = {True: 'd7a91c80727cbcdf34ba5de1972042535d36d2be777a9133dbc156354f81a67d',
            False: 'c046ccf785b20901d865dbe3f1150534616f1d93d90c2118c77dac56a6d45b04'}


def cfg(**extra):
    return dict({'python': '/usr/bin/python3', 'run': RUN}, **extra)


def normalized(text):
    return text.replace(FLEET_PY, '<fleet.py>')


def steps(text):
    """The numbered steps of the section: {n: line}."""
    return {int(m.group(1)): m.group(0) for m in re.finditer(r'(?m)^(\d+)\. .*$', text)}


def commands(text):
    return re.findall(r'`([^`]+)`', text)


def channel_step(text):
    found = [line for line in steps(text).values() if 'channel send' in line]
    assert len(found) == 1, steps(text)
    return found[0]


def channel_command(text):
    found = [c for c in commands(text) if ' channel send ' in ' ' + c + ' ']
    assert len(found) == 1, commands(text)
    return found[0]


def restart_command(text):
    found = [c for c in commands(text) if ' start --allow-development-v2 --resume ' in c]
    assert len(found) == 1, commands(text)
    return found[0]


@pytest.mark.parametrize('watched', [True, False])
def test_section_unchanged_without_channels_byte_for_byte(watched):
    text = fleet.daemon_down_section(cfg(), watched)
    assert hashlib.sha256(normalized(text).encode()).hexdigest() == BASELINE[watched]
    assert 'channel send' not in text


@pytest.mark.parametrize('watched', [True, False])
def test_section_with_channels_has_channel_send_step_between_3_and_4(watched):
    text = fleet.daemon_down_section(cfg(channels=CHANNELS), watched)
    command = channel_command(text)
    argv = shlex.split(command)
    i = argv.index('channel')
    assert argv[i:i + 2] == ['channel', 'send']
    assert argv[argv.index('--config') + 1] == RUN + '/config.json'
    assert argv[argv.index('--event') + 1] == 'daemon_down' and argv[argv.index('--text') + 1].strip()
    lines = text.splitlines()
    at = {name: next(n for n, line in enumerate(lines) if probe(line))
          for name, probe in (('progress', lambda l: 'PROGRESS.md' in l and re.match(r'\d+\. ', l)),
                              ('channel', lambda l: 'channel send' in l),
                              ('restart', lambda l: '--resume' in l and re.match(r'\d+\. ', l)))}
    assert at['progress'] < at['channel'] < at['restart']
    numbered = steps(text)
    progress_n = next(n for n, line in numbered.items() if 'PROGRESS.md' in line)
    channel_n = next(n for n, line in numbered.items() if 'channel send' in line)
    restart_n = next(n for n, line in numbered.items() if '--resume' in line)
    assert (progress_n, channel_n, restart_n) == (3, 4, 5)        # inserted after 3, the rest renumbered
    assert sorted(numbered) == list(range(1, len(numbered) + 1))


def test_channel_send_line_parses_with_shlex_same_prefix_as_restart():
    for watched in (True, False):
        text = fleet.daemon_down_section(cfg(channels=CHANNELS, fleet_home='/opt/fleet home',
                                             cmux_state_dir='/var/state dir'), watched)
        send, restart = shlex.split(channel_command(text)), shlex.split(restart_command(text))
        assert send[:send.index('channel')] == restart[:restart.index('start')]   # env + python + fleet.py
        assert FLEET_PY in send and 'CMUX_FLEET_HOME=/opt/fleet home' in send


def test_section_absent_for_subfleet_parent_config():
    for watched in (True, False):
        child = cfg(channels=CHANNELS, parent_config='/tmp/fleetmux-parent/config.json')
        assert 'channel send' not in fleet.daemon_down_section(child, watched)
        # the control: the same cfg without parent_config does carry the step, so its absence above is the rule
        assert 'channel send' in fleet.daemon_down_section(cfg(channels=CHANNELS), watched)


def test_step_says_failure_does_not_block_restart():
    for watched in (True, False):
        step = channel_step(fleet.daemon_down_section(cfg(channels=CHANNELS), watched))
        assert re.search(r'\bonce\b', step, re.I)
        assert re.search(r'fail', step, re.I)
        assert re.search(r"(do not|don't|never) retry", step, re.I)
        assert re.search(r'(go on|continue|carry on|still) (to|with)', step, re.I)
