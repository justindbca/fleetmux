"""Safeguards refusal = a wall (user 2026-11-23, brief section 1: switch to Astra on a refusal, Fable when Astra
cannot run, prefer the OpenAI family whenever safeguards appears; docs/design.md section 23).

The CLI's own refusal ("API Error: <model>'s safeguards flagged this message ...") is the walls class `safeguards`:
scope model_pool (only that model is skipped), kind safeguards. The slot walks its role's WHOLE chain: first the
models walls.safeguards_prefer names (OpenAI family, chain order), then the rest (chain order), skipping the flagged
model; nothing left -> stop. Only CLI-owned text counts (StopFailure fields, the CLI's own error line on a screen),
never a turn text (model output) or a report pasted into a pane.

Pure brake functions and v1 fleets on a fake Redis / fake cmux; the StopFailure path: tests/test_safeguards_stopfail.py.
"""
import json

import pytest
import yaml

from test_brake import CLAUDE_ACCOUNT_WALL, busy_worker, make  # noqa: F401
from test_brake import isolated_claude_trust, pools_file  # noqa: F401  (autouse fixtures)
from test_fleet import turn
from test_wall_chain import ASTRA, CACHE, DS_FREE, DS_PI, SOL, SUPERVISOR_CHAIN, TERRA, pi_bin  # noqa: F401
from fleetmux import brake, discover, fleet, fleet_config, job_chain

# The CLI's own text (claude CLI bundle: "API Error: ${model}'s safeguards flagged this message (...)").
INCIDENT = ("API Error: Opus 5.5's safeguards flagged this message (https://www.anthropic.com/legal/aup). This "
            "sometimes happens with safe, normal conversations. Claude Code can't respond to this message with Opus 5.5.")
BRIEF_TEXT = ("Opus 5.5's safeguards flagged this message (https://www.anthropic.com/legal/aup). This sometimes "
              "happens with safe, normal conversations. Claude Code can't respond to this message")
GLYPH = '\u23bf'  # the TUI's result / error glyph in front of the CLI's own error line
SCREEN_LINE = '  ' + GLYPH + '  ' + INCIDENT


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    path = tmp_path / 'discovered.yaml'
    path.write_text(yaml.safe_dump(CACHE))
    monkeypatch.setenv(discover.CACHE_ENV, str(path))
    return path


def prefer(c, m):
    return brake.preferred(m, ['gpt'])


def opus_supervisor(f):
    return dict(f.runtime_cfg('supervisor'), supervisor_cli='claude', supervisor_model='claude-opus-5-5')


def mark(f, cli, model, scope='model_pool'):
    f.r.hset(fleet.WALLS, mapping={cli + ':' + model: json.dumps({'cli': cli, 'model': model, 'scope': scope,
                                                                  'account': brake.account(cli, model), 'at': 0,
                                                                  'agent': 'worker-9'})})


def safeguards():
    return brake.classify_wall(INCIDENT, safeguards='cli')


# ------------------------------------------------------------------ the class (requirement 1)
def test_classify_returns_safeguards_kind_with_model_pool_scope():
    assert safeguards() == {'scope': 'model_pool', 'kind': 'safeguards', 'line': INCIDENT[:300]}
    assert brake.classify_wall(BRIEF_TEXT, safeguards='cli')['kind'] == 'safeguards'  # the brief's wording
    assert brake.wall_table()['safeguards'], 'the class lives in fleet.yaml#walls'


def test_a_turn_text_is_model_output_and_never_a_safeguards_wall():
    # A turn's text (Stop hook, v1 turn, startup reply) is written by the model: the sentence there is a quote.
    assert brake.classify_wall(INCIDENT) is None
    assert brake.classify_wall(BRIEF_TEXT) is None


def test_account_wall_still_wins_over_safeguards():
    wall = brake.classify_wall(CLAUDE_ACCOUNT_WALL + '\n' + INCIDENT, safeguards='cli')
    assert wall['scope'] == 'account' and wall.get('kind') != 'safeguards'


def test_screen_wall_sees_the_cli_own_error_line():
    screen = '\n'.join(['> look at the M3 report', '', '\u25cf Reading the report.', '', SCREEN_LINE, '',
                        '\u2500' * 40, '> ', '\u2500' * 40, '  ? for shortcuts'])
    wall = brake.screen_wall(screen)
    assert wall and wall['kind'] == 'safeguards' and wall['scope'] == 'model_pool'


@pytest.mark.parametrize('screen', [
    # the harder case (supervisor ruling M1 (a)): the sentence pasted alone on an indented line of a delivered report
    GLYPH + '  Stop hook feedback:\n   [worker-1 report]\n   ' + BRIEF_TEXT + '\n   Done: m3.md',
    GLYPH + '  Stop hook feedback:\n   [worker-1 report]\n   ' + INCIDENT,  # with the CLI prefix, still indented
    '   ' + INCIDENT,
    '> ' + INCIDENT,  # typed into the prompt
    '{"status":"BLOCKED","reason":"' + INCIDENT + '"}',
    '\u25cf ' + INCIDENT,  # the model's own text bullet
    GLYPH + '  ' + BRIEF_TEXT,  # a tool result (cat report.md) starting with the sentence: no API Error prefix
], ids=['indented-alone', 'indented-with-prefix', 'indented', 'typed', 'json', 'model-bullet', 'tool-result'])
def test_screen_with_a_report_quoting_the_sentence_is_not_a_wall(screen):
    assert brake.screen_wall(screen + '\n> \n') is None


# ------------------------------------------------------------------ the order (requirement 2, parameters 6)
def test_openai_family_comes_from_safeguards_prefer_patterns():
    patterns = brake.wall_table()['safeguards_prefer']
    assert patterns == ['gpt']
    models = [m for row in CACHE['clis'].values() for m in row['models']]
    assert [m for m in models if brake.preferred(m, patterns)] == [ASTRA, SOL, TERRA]
    assert not brake.preferred(DS_PI, patterns) and not brake.preferred(DS_FREE, patterns)


def test_wall_action_prefer_walks_the_whole_chain_openai_first_in_chain_order():
    walled = [{'cli': 'claude', 'model': 'opus', 'scope': 'model_pool', 'account': 'claude'}]
    act = brake.wall_action('claude', 'claude-opus-5-5', SUPERVISOR_CHAIN, walled, None, prefer=prefer)
    assert (act['action'], act['to_cli'], act['to']) == ('switch', 'pi', ASTRA)  # above opus on the chain
    act = brake.wall_action('claude', 'claude-opus-5-5', SUPERVISOR_CHAIN, walled,
                            lambda c, m: 'refused' if m == ASTRA else None, prefer=prefer)
    assert act['to'] == SOL and act['skipped'] == ['pi:' + ASTRA + ' (refused)']
    act = brake.wall_action('claude', 'claude-opus-5-5', SUPERVISOR_CHAIN, walled,
                            lambda c, m: 'refused' if c == 'pi' else None, prefer=prefer)
    assert act['to'] == 'fable' and act['skipped'] == ['pi:' + ASTRA + ' (refused)', 'pi:' + SOL + ' (refused)']
    # without prefer: today's walk, in the given order
    act = brake.wall_action('claude', 'claude-opus-5-5', SUPERVISOR_CHAIN[3:], walled)
    assert act['to'] == SOL


def test_private_walls_block_without_the_keys_uses_the_shipped_ones_and_empty_turns_it_off(tmp_path, monkeypatch):
    path = tmp_path / 'private.yaml'
    path.write_text('walls:\n  account: ["Claude usage limit reached"]\n')
    monkeypatch.setenv(fleet_config.USER_ENV, str(path))
    table = brake.wall_table()
    assert table['safeguards'] and table['safeguards_screen'] and table['safeguards_prefer'] == ['gpt']
    assert brake.classify_wall(INCIDENT, safeguards='cli')['kind'] == 'safeguards'
    path.write_text('walls:\n  account: ["Claude usage limit reached"]\n  safeguards: []\n  safeguards_prefer: []\n')
    table = brake.wall_table()
    assert table['safeguards'] == [] and table['safeguards_prefer'] == []
    assert brake.classify_wall(INCIDENT, safeguards='cli') is None, 'present but empty: the operator turned it off'


# ------------------------------------------------------------------ v1 fleets: worker, record, sub-fleet supervisor
def test_worker_safeguards_takes_the_first_openai_model_of_the_worker_chain(tmp_path, pi_bin):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='claude-opus-5-5', **pi_bin)
    busy_worker(f)
    record = f.on_wall('worker-1', INCIDENT, 'stop_failure', wall=safeguards())
    assert 'safeguards' in record['reason'] and f.paused()
    req = json.loads(f.agent('worker-1')['wall_downgrade'])
    assert (req['to_cli'], req['to'], req['kind']) == ('pi', SOL, 'safeguards')
    f.setf('worker-1', state='idle')  # the turn ended (the StopFailure path releases the slot idle)
    f.tick()
    cfg = f.runtime_cfg('worker-1')
    assert f.agent('worker-1')['generation'] == '1' and fleet.agent_cli(cfg, 'worker-1') == 'pi'
    assert cfg['pi_model'] == SOL


def test_safeguards_record_is_model_pool_with_reason_and_skips_only_that_model(tmp_path, pi_bin):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='claude-opus-5-5', **pi_bin)
    busy_worker(f)
    f.on_wall('worker-1', INCIDENT, 'stop_failure', wall=safeguards())
    rec = json.loads(f.r.hget(fleet.WALLS, 'claude:opus'))
    assert (rec['scope'], rec['reason'], rec['account']) == ('model_pool', 'safeguards', 'claude')
    # Only that model: another Claude model of the same account stays usable for an ordinary wall.
    act = brake.wall_action('claude', 'fable', [('claude', 'opus'), ('claude', 'sonnet')], f.walled())
    assert act['to'] == 'sonnet' and act['skipped'] == ['claude:opus (model-pool wall)']
    assert json.loads(f.agent('worker-1')['wall'])['kind'] == 'safeguards'


def test_subfleet_supervisor_safeguards_stops_and_the_parent_decides(tmp_path):
    # docs/design.md section 67: a sub-fleet's supervisor never walks a chain in its own daemon, whatever the wall kind
    # and generation; it stops and the parent fleet's supervisor reopens it (heal). It used to switch to fable here.
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude')
    f.cfg['parent_config'] = str(tmp_path / 'parent.json')  # a child-owned (not adopted) sub-fleet supervisor
    assert not f.wall_chained('supervisor') and not f.wall_chained('supervisor', 'safeguards')
    act = f.wall_decide('supervisor', opus_supervisor(f), safeguards())
    assert act['action'] == 'stop' and 'safeguards' in act['advice'] and 'parent fleet decides' in act['advice']


def test_adopted_subfleet_supervisor_safeguards_stops_with_the_reason(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude')
    f.cfg.update(parent_config=str(tmp_path / 'parent.json'), adopt={'pane': f.agent('supervisor')['pane']})
    assert not f.wall_chained('supervisor', 'safeguards')  # generation 0: the parent's pane (fleet.py handoff rule)
    act = f.wall_decide('supervisor', opus_supervisor(f), safeguards())
    assert act['action'] == 'stop' and 'safeguards' in act['advice'] and 'adopted' in act['advice']


def test_flagged_again_on_the_new_model_walks_on_and_then_stops(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude')
    for cli, model in (('pi', ASTRA), ('pi', SOL), ('pi', DS_PI)):
        mark(f, cli, model, 'refused')  # no OpenAI / pi entry usable on this run
    first = f.wall_decide('supervisor', opus_supervisor(f), safeguards())
    assert (first['to_cli'], first['to']) == ('claude', 'fable')
    again = f.wall_decide('supervisor', dict(opus_supervisor(f), supervisor_model='fable'), safeguards())
    assert again['action'] == 'stop' and 'to' not in again
    assert 'claude:opus (model-pool wall)' in again['skipped'] and 'claude:fable' not in str(again.get('to'))
    assert {k for k in f.r.hgetall(fleet.WALLS)} >= {'claude:opus', 'claude:fable'}


def test_report_quoting_safeguards_on_a_normal_turn_is_not_a_wall(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='claude-opus-5-5')
    busy_worker(f)
    f.handle(dict(turn('worker-1', BRIEF_TEXT), msg_id='m1'))  # a short report line quoting the refusal
    assert not f.paused() and not f.agent('worker-1').get('wall')
    assert not f.r.hgetall(fleet.WALLS)


def test_wall_gate_trips_on_the_cli_line_and_not_on_a_pasted_report(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='claude-opus-5-5')
    busy_worker(f)
    pasted = GLYPH + '  Stop hook feedback:\n   [worker-2 report]\n   ' + BRIEF_TEXT + '\n> \n'
    assert f.wall_gate('worker-1', pasted) is None and not f.paused()
    record = f.wall_gate('worker-1', SCREEN_LINE + '\n> \n')
    assert record and record['wall']['kind'] == 'safeguards' and f.paused()
    assert json.loads(f.agent('worker-1')['wall_downgrade'])['kind'] == 'safeguards'
