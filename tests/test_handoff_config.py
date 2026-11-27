"""Handoff defaults come from fleet.yaml#handoff, never from Python (docs/design.md §22)."""
import ast
import inspect
import json
import os
import re
import time
from pathlib import Path

import pytest
import yaml

from test_fleet import cfx  # noqa: F401
from test_handoff_limits import deliver, roster, transcript
from fleetmux import fleet, fleet_config, join, midturn, named_subfleet

REPO = Path(__file__).resolve().parents[1]
SHIPPED = fleet_config.SHIPPED  # the repo file, taken before any test points SHIPPED at a copy
# None of these is a shipped or a former built-in value: a number left in Python cannot pass for the block's.
VALUES = {'ratio': .42, 'midturn_ratio': .66, 'handoff_timeout': 111, 'midturn_retry': 22, 'midturn_max_splits': 5}
MIDTURN = ('midturn_ratio', 'midturn_retry', 'midturn_max_splits')
CFG_KEYS = ('handoff_ratio', 'handoff_timeout') + MIDTURN
BARE_CFG = {'claude_window': 200000}


class Reached(Exception):
    """A start path went past the point where it must have refused: stop before anything is provisioned."""


def reached(*args, **kwargs):
    raise Reached()


def without_block(text, name):
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if re.match(r'[A-Za-z_][\w-]*\s*:', line):
            skip = line.split(':')[0].strip() == name
        if not skip:
            out.append(line)
    return ''.join(out)


class Block:
    """A temporary copy of the shipped fleet.yaml that stands in for it; every write gets its own stamp."""

    def __init__(self, path):
        self.path, self.writes = path, 0

    def raw(self, text):
        self.path.write_text(text, encoding='utf-8')
        self.writes += 1
        os.utime(self.path, ns=(1_700_000_000_000_000_000 + self.writes * 10**9,) * 2)

    def write(self, value):
        """Replace the handoff block by ``value``; None = no block at all."""
        text = without_block(SHIPPED.read_text(encoding='utf-8'), 'handoff')
        if value is not None:
            text = fleet_config._splice(text, 'handoff', value)
        self.raw(text)

    def set(self, **changes):
        self.write(dict(VALUES, **changes))


@pytest.fixture
def blk(tmp_path, monkeypatch):
    block = Block(tmp_path / 'shipped-fleet.yaml')
    block.set()
    monkeypatch.setattr(fleet_config, 'SHIPPED', block.path)
    for name in ('_handoff_good', '_handoff_noted'):  # this process has not read the block yet
        monkeypatch.setattr(fleet_config, name, None, raising=False)
    return block


def unset(f):
    """What Fleet.__init__ builds from a config.json that names no handoff or mid-turn value."""
    f.cfg = dict(fleet.DEFAULTS, **{k: v for k, v in f.cfg.items() if k not in CFG_KEYS})


def haiku(f, used):
    """A 200,000-token Claude worker with ``used`` tokens in its transcript."""
    f.cfg['worker_model'] = 'haiku'
    transcript(f, 'worker-1', used)


def parsed(*argv):
    return fleet.cli().parse_args(['start', '--lifecycle', 'service'] + list(argv))


# ------------------------------------------------------------------ the shipped block
def test_shipped_block_has_the_five_values_each_with_a_comment():
    text = SHIPPED.read_text(encoding='utf-8')
    assert yaml.safe_load(text).get('handoff') == {
        'ratio': .3, 'midturn_ratio': .8, 'handoff_timeout': 900, 'midturn_retry': 300, 'midturn_max_splits': 3}
    lines = re.search(r'^handoff:\n((?:[ \t].*\n|\n)+)', text, re.M).group(1).splitlines()
    for key in VALUES:
        at = next(i for i, line in enumerate(lines) if line.strip().startswith(key + ':'))
        assert '#' in lines[at] or lines[at - 1].strip().startswith('#'), key + ' has no comment'
    assert 'handoff' in fleet_config.BLOCKS and 'handoff' in fleet_config.USER_BLOCKS


def test_private_file_replaces_the_whole_block(blk, tmp_path, monkeypatch):
    private = tmp_path / 'private.yaml'
    private.write_text(yaml.safe_dump({'handoff': dict(VALUES, ratio=.55)}))
    monkeypatch.setenv(fleet_config.USER_ENV, str(private))
    assert fleet_config.handoff() == dict(VALUES, ratio=.55)
    private.write_text(yaml.safe_dump({'handoff': {'ratio': .25}}))  # no deep merge: four keys are missing
    assert fleet_config.handoff() == dict(VALUES, ratio=.55)


# ------------------------------------------------------------------ (a) the block is the last layer
def test_ratio_without_any_override_comes_from_the_block(blk):
    lim = fleet.handoff_limits
    assert lim('claude', 'haiku', dict(BARE_CFG)) == (
        200000, .42, 'window: --claude-window; ratio: fleet.yaml#handoff')
    assert lim('codex', 'gpt-6', dict(BARE_CFG, handoff_ratio=None))[1:] == (
        .42, 'window: measured; ratio: fleet.yaml#handoff')


def test_explicit_flag_and_card_rank_above_the_block(blk):
    lim = fleet.handoff_limits
    flag = dict(BARE_CFG, handoff_ratio=.5)
    assert lim('claude', 'haiku', flag) == (200000, .5, 'window: --claude-window; ratio: --handoff-ratio')
    card = ('flashnext-coder', .35)
    assert lim('claude', 'haiku', flag, card=card)[1] == .35
    assert lim('claude', 'haiku', dict(BARE_CFG), card=card)[1] == .35


def test_agent_status_names_the_block(cfx, blk):
    unset(cfx)
    cfx.handoff_limits('worker-1')
    agent = cfx.agent('worker-1')
    assert agent['handoff_ratio'] == '0.42'
    assert agent['handoff_source'].endswith('; ratio: fleet.yaml#handoff')


def test_flags_default_to_none():
    args = parsed()
    assert args.handoff_ratio is None and args.handoff_timeout is None


def test_only_typed_flags_are_stored_in_the_run_config():
    assert fleet.handoff_flags(parsed()) == {}
    assert fleet.handoff_flags(parsed('--handoff-ratio', '0.6')) == {'handoff_ratio': .6}
    assert fleet.handoff_flags(parsed('--handoff-timeout', '60')) == {'handoff_timeout': 60.0}


def test_attach_forwards_a_typed_flag_even_at_the_former_default():
    overrides = join.start_settings(parsed('--handoff-ratio', '0.6', '--handoff-timeout', '900'), service=True)[0]
    assert overrides.get('handoff_ratio') == .6 and overrides.get('handoff_timeout') == 900


# ------------------------------------------------------------------ (b) hot: no restart
def test_edit_changes_the_next_delivery_check_without_restart(cfx, blk):
    unset(cfx)
    blk.set(ratio=.9)
    haiku(cfx, 100000)  # 100k < 0.9 x 200k
    assert deliver(cfx, 'worker-1') == 'busy'
    cfx.setf('worker-1', state='idle')  # turn finished
    blk.set(ratio=.42, handoff_timeout=222)  # same Fleet object; 100k >= 0.42 x 200k
    before = time.time()
    assert deliver(cfx, 'worker-1') == 'handoff'
    assert before + 222 <= float(cfx.agent('worker-1')['handoff_deadline']) <= time.time() + 222


def test_typed_timeout_ranks_above_the_block(cfx, blk):
    unset(cfx)
    cfx.cfg['handoff_timeout'] = 60
    haiku(cfx, 100000)
    before = time.time()
    assert deliver(cfx, 'worker-1') == 'handoff'
    assert before + 60 <= float(cfx.agent('worker-1')['handoff_deadline']) <= time.time() + 60


def test_other_ratio_readers_use_the_block(cfx, blk):
    unset(cfx)
    haiku(cfx, 100000)
    assert named_subfleet.over_threshold(cfx, 'worker-1') == (100000, 200000)
    blk.set(ratio=.9)
    assert named_subfleet.over_threshold(cfx, 'worker-1') is None
    assert cfx.handoff_ratio('worker-1') == .9


# ------------------------------------------------------------------ (c) a bad block keeps the last good values
BAD = {
    'not yaml': lambda b: b.raw('handoff: [unclosed\n'),
    'ratio above one': lambda b: b.set(ratio=1.5),
    'ratio zero': lambda b: b.set(ratio=0),
    'ratio is a bool': lambda b: b.set(ratio=True),
    'midturn_ratio one': lambda b: b.set(midturn_ratio=1),
    'timeout zero': lambda b: b.set(handoff_timeout=0),
    'retry is text': lambda b: b.set(midturn_retry='300'),
    'retry not finite': lambda b: b.set(midturn_retry=float('inf')),
    'splits is a fraction': lambda b: b.set(midturn_max_splits=1.5),
    'missing key': lambda b: b.write({k: v for k, v in VALUES.items() if k != 'ratio'}),
    'unknown key': lambda b: b.set(ratoi=.2),
    'not a mapping': lambda b: b.write([.3]),
    'block removed': lambda b: b.write(None),
}


@pytest.mark.parametrize('case', sorted(BAD))
def test_invalid_block_keeps_the_last_good_values_and_logs_once(blk, capsys, case):
    assert fleet_config.handoff() == VALUES
    capsys.readouterr()
    BAD[case](blk)
    for _ in range(3):
        assert fleet_config.handoff() == VALUES
    assert capsys.readouterr().err.count('keeping the last good values') == 1
    blk.set(ratio=.2)  # fixed: the new values apply
    assert fleet_config.handoff()['ratio'] == .2
    BAD[case](blk)  # broken again: keeps 0.2 and says so again
    assert fleet_config.handoff()['ratio'] == .2
    assert capsys.readouterr().err.count('keeping the last good values') == 1


def test_unreadable_file_keeps_the_last_good_values(blk, tmp_path, monkeypatch):
    assert fleet.handoff_limits('claude', 'haiku', dict(BARE_CFG))[1] == .42
    monkeypatch.setattr(fleet_config, 'SHIPPED', tmp_path / 'not-there.yaml')
    assert fleet.handoff_limits('claude', 'haiku', dict(BARE_CFG))[1] == .42


def test_caller_cannot_change_the_last_good_values(blk):
    fleet_config.handoff()['ratio'] = .99
    blk.write(None)
    assert fleet_config.handoff() == VALUES


@pytest.mark.parametrize('case', ['block removed', 'ratio above one', 'not yaml'])
def test_first_read_without_a_good_block_is_an_error(blk, case):
    BAD[case](blk)
    with pytest.raises(ValueError, match='fleet.yaml#handoff'):
        fleet_config.handoff()


def test_new_start_refuses_without_the_block(blk, monkeypatch):
    blk.write(None)
    monkeypatch.setattr(fleet, 'resolve_supervisor', reached)  # nothing has been created up to here
    with pytest.raises(SystemExit, match='fleet.yaml#handoff'):
        fleet.start_new_run(parsed('--no-join'))


def test_resume_refuses_without_the_block(blk, tmp_path, monkeypatch):
    blk.write(None)
    run = tmp_path / 'old-run'
    run.mkdir()
    (run / 'config.json').write_text(json.dumps({'run': str(run), 'codex_queue': False}))
    monkeypatch.setattr(fleet.runtime_v2, 'validate_config', reached)  # before any provisioning
    with pytest.raises(SystemExit, match='fleet.yaml#handoff'):
        fleet.resume(run, None)


def test_daemon_refuses_to_start_without_the_block(blk, tmp_path, monkeypatch):
    blk.write(None)
    run = tmp_path / 'daemon-run'
    run.mkdir()
    (run / 'config.json').write_text(json.dumps({'run': str(run)}))
    monkeypatch.setattr(fleet, 'connect', reached)  # no Redis, no Fleet
    with pytest.raises(SystemExit, match='fleet.yaml#handoff'):
        fleet.daemon(str(run / 'config.json'))


# ------------------------------------------------------------------ (d) tables and worker entries still win
def test_roster_table_and_worker_override_win_over_the_block(cfx, blk):
    unset(cfx)
    roster(cfx, 'workers:\n  - name: worker-1\nhandoff:\n  claude: {ratio: 0.7, midturn_ratio: 0.75}\n')
    assert cfx.handoff_limits('worker-1')[1] == .7
    assert cfx.agent('worker-1')['handoff_source'].endswith('; ratio: roster claude')
    assert cfx.midturn_settings('worker-1') == {'midturn_ratio': .75, 'midturn_retry': 22, 'midturn_max_splits': 5}
    roster(cfx, 'workers:\n  - {name: worker-1, handoff_ratio: 0.9}\nhandoff:\n  claude: {ratio: 0.7}\n')
    assert cfx.handoff_limits('worker-1')[1] == .9
    assert cfx.agent('worker-1')['handoff_source'].endswith('; ratio: worker')
    roster(cfx, 'workers:\n  - name: worker-1\n')
    assert cfx.handoff_limits('worker-1')[1] == .42
    assert cfx.agent('worker-1')['handoff_source'].endswith('; ratio: fleet.yaml#handoff')


def test_run_config_table_wins_over_the_block(blk):
    cfg = dict(BARE_CFG, handoff={'claude:haiku': {'ratio': .65}})
    assert fleet.handoff_limits('claude', 'haiku', cfg)[1:] == (
        .65, 'window: --claude-window; ratio: config claude:haiku')


# ------------------------------------------------------------------ (e) mid-turn values
def test_midturn_values_come_from_the_block(blk):
    values, sources = midturn.resolve_settings('pi', 'deepseek', {})
    assert values == {k: VALUES[k] for k in MIDTURN}
    assert sources == dict.fromkeys(MIDTURN, 'fleet.yaml#handoff')
    blk.set(midturn_ratio=.5, midturn_retry=33, midturn_max_splits=0)
    assert midturn.resolve_settings('pi', 'deepseek', {})[0] == {
        'midturn_ratio': .5, 'midturn_retry': 33, 'midturn_max_splits': 0}


def test_midturn_overrides_rank_above_the_block(blk):
    table = {'pi': {'midturn_ratio': .7}}
    values, sources = midturn.resolve_settings('pi', 'x', {'midturn_retry': 44}, table)
    assert values == {'midturn_ratio': .7, 'midturn_retry': 44, 'midturn_max_splits': 5}
    assert sources == {'midturn_ratio': 'roster pi', 'midturn_retry': '--midturn-retry',
                       'midturn_max_splits': 'fleet.yaml#handoff'}


def test_daemon_midturn_settings_follow_an_edit(cfx, blk):
    unset(cfx)
    assert cfx.midturn_settings('worker-1') == {k: VALUES[k] for k in MIDTURN}
    blk.set(midturn_retry=44)
    assert cfx.midturn_settings('worker-1')['midturn_retry'] == 44


@pytest.mark.parametrize('func,names', [(midturn.decide, MIDTURN), (midturn.pause_message, ('midturn_retry',))])
def test_midturn_policy_functions_have_no_built_in_numbers(func, names):
    params = inspect.signature(func).parameters
    assert [n for n in names if params[n].default is not inspect.Parameter.empty] == []


# ------------------------------------------------------------------ (f) no number left in Python
def numeric(node):
    return isinstance(node, ast.Constant) and type(node.value) in (int, float)


def numeric_defaults(source):
    """Every place that gives one of the five names a number: a dict entry, a dict() keyword, a parameter
    default, or a ``.get(name, number)`` fallback."""
    found = []
    for node in ast.walk(ast.parse(source)):
        pairs = []
        if isinstance(node, ast.Dict):
            pairs = [(k.value, v) for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)]
        elif isinstance(node, ast.Call):
            pairs = [(k.arg, k.value) for k in node.keywords]
            if (isinstance(node.func, ast.Attribute) and node.func.attr == 'get' and len(node.args) == 2
                    and isinstance(node.args[0], ast.Constant)):
                pairs.append((node.args[0].value, node.args[1]))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            positional = a.posonlyargs + a.args
            pairs = [(p.arg, d) for p, d in zip(positional[len(positional) - len(a.defaults):], a.defaults)]
            pairs += [(p.arg, d) for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
        found += [(name, value.value, value.lineno) for name, value in pairs
                  if name in CFG_KEYS and numeric(value)]
    return found


@pytest.mark.parametrize('module', ['fleet.py', 'midturn.py'])
def test_the_five_numbers_are_not_defaults_in_python(module):
    assert numeric_defaults((REPO / 'fleetmux' / module).read_text(encoding='utf-8')) == []


def test_no_defaults_table_carries_the_five_keys():
    assert not set(CFG_KEYS) & set(fleet.DEFAULTS)
    assert not set(CFG_KEYS) & set(getattr(midturn, 'DEFAULTS', {}))


def test_no_reader_subscripts_the_optional_config_keys():
    """config.json holds them only when typed: a bare subscript would raise KeyError."""
    pattern = re.compile(r'''cfg\[['"]handoff_(ratio|timeout)['"]\]''')
    hits = [path.name + ':' + str(number) for path in sorted((REPO / 'fleetmux').rglob('*.py'))
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1) if pattern.search(line)]
    assert hits == []
