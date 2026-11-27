"""The switch baseline block of fleet-superpowers.yaml (fleetmux/baseline.py).

Every test uses a temporary inventory file; the real ~/.fleetmux is never read or written.
"""
import datetime

import pytest

from fleetmux import baseline
from fleetmux import fleet_superpowers as fs
from fleetmux import fleet_superpowers_hot as hot

MODELS = 'models:\n  version: 1\n  default: {supervisor: alpha, worker: beta}\n'
MACHINES = 'machines:\n  box-a:\n    source: self\n    hostname: box-a.local\n'
WORKFLOWS = 'workflows:\n  wf-one:\n    machine: box-a\n    applets: []\n'
GOOD = '''baseline:
  version: 1
  check_every_s: 120
  machines:
    box-a:
      root_service:
        advisor: on
        max_busy: 3
        supervisor_split: true
        worker_idle_reclaim_min: 30
        lease_reclaim: on
        worker_kit: true
        worker_domains: [infra, ops]
        kit_fleet: ops-kit
      job:
        lease_reclaim: on
  exceptions:
    - machine: box-a
      shape: root_service
      fleet: main
      switch: worker_idle_reclaim_min
      value: off
      reason: prerequisite not merged yet
      until: 2030-01-15
'''
NOW = datetime.datetime(2030, 1, 10, 12, 0)


def inventory(tmp_path, block=GOOD):
    path = tmp_path / 'fleet-superpowers.yaml'
    path.write_text(MODELS + MACHINES + WORKFLOWS + block)
    return path


def broken(tmp_path, block):
    path = inventory(tmp_path, block)
    loaded = baseline.load(path)
    assert loaded.block is None and loaded.error
    return loaded


# -- the block and the registry ------------------------------------------------------------------------------

def test_registry_lists_the_first_switches_with_where_reload_and_resume():
    assert set(baseline.REGISTRY) == {'advisor', 'max_busy', 'supervisor_split', 'worker_idle_reclaim_min',
                                      'lease_reclaim', 'worker_kit', 'worker_domains', 'kit_fleet'}
    adv, cap = baseline.REGISTRY['advisor'], baseline.REGISTRY['max_busy']
    assert (adv.where, adv.reload, adv.restore) == ('roster', 'hot', 'auto')
    assert (cap.where, cap.reload, cap.restore) == ('roster', 'hot', 'report')  # no roster writer for max_busy
    for key in ('supervisor_split', 'worker_idle_reclaim_min', 'lease_reclaim'):
        sw = baseline.REGISTRY[key]
        assert (sw.where, sw.reload, sw.resume_apply) == ('config', 'restart', True)
    for key in ('worker_kit', 'worker_domains', 'kit_fleet'):
        assert baseline.REGISTRY[key].resume_apply is False  # a new run, not a resume
    assert baseline.REGISTRY['worker_kit'].compare == 'set'
    assert baseline.SHAPES == ('root_service', 'job', 'named', 'attached', 'split')


def test_no_block_means_no_baseline(tmp_path):
    path = inventory(tmp_path, block='')
    loaded = baseline.load(path)
    assert loaded.block is None and loaded.error is None
    assert baseline.load(tmp_path / 'missing.yaml').block is None


def test_a_valid_block_is_read_with_its_values_normalised(tmp_path):
    block = baseline.load(inventory(tmp_path)).block
    assert block.version == 1 and block.check_every_s == 120
    root = block.table('box-a', 'root_service')
    assert root == {'advisor': True, 'max_busy': 3, 'supervisor_split': True, 'worker_idle_reclaim_min': 30,
                    'lease_reclaim': 'on', 'worker_kit': True, 'worker_domains': ['infra', 'ops'],
                    'kit_fleet': 'ops-kit'}
    assert block.table('box-a', 'job') == {'lease_reclaim': 'on'}
    assert block.table('box-a', 'named') == {} and block.table('other', 'root_service') == {}
    (exc,) = block.exceptions
    assert exc.value is None and exc.until == '2030-01-15'  # `off` = reclaim off = None, like the run config
    assert baseline.machine_name(inventory(tmp_path)) == 'box-a'


def test_check_every_s_defaults_when_missing(tmp_path):
    block = baseline.load(inventory(tmp_path, 'baseline:\n  version: 1\n')).block
    assert block.check_every_s == baseline.DEFAULT_CHECK_EVERY_S


@pytest.mark.parametrize('block, words', [
    ('baseline:\n  version: 1\n  colour: blue\n', 'colour'),
    ('baseline:\n  version: 2\n', 'version'),
    ('baseline:\n  version: 1\n  machines:\n    box-a:\n      root_task: {}\n', 'root_task'),
    ('baseline:\n  version: 1\n  machines:\n    box-a:\n      root_service: {stall_timeout: 5}\n', 'stall_timeout'),
    ('baseline:\n  version: 1\n  machines:\n    box-a:\n      root_service: {max_busy: -1}\n', 'max_busy'),
    ('baseline:\n  version: 1\n  machines:\n    box-a:\n      root_service: {lease_reclaim: maybe}\n',
     'lease_reclaim'),
    ('baseline:\n  version: 1\n  check_every_s: 0\n', 'check_every_s'),
])
def test_an_unknown_or_bad_key_is_an_error_not_ignored(tmp_path, block, words):
    assert words in broken(tmp_path, block).error


@pytest.mark.parametrize('row, words', [
    ('{machine: box-a, shape: root_service, switch: lease_reclaim, value: off, until: 2030-01-01}', 'reason'),
    ('{machine: box-a, shape: root_service, switch: lease_reclaim, value: off, reason: x}', 'until'),
    ('{machine: box-a, shape: root_service, switch: lease_reclaim, value: off, reason: x, until: soon}', 'until'),
    ('{machine: box-a, shape: root_service, switch: nope, value: off, reason: x, until: 2030-01-01}', 'nope'),
    ('{machine: box-a, shape: root_service, switch: lease_reclaim, value: off, reason: x, until: 2030-01-01, '
     'note: y}', 'note'),
    ('{machine: box-a, shape: root_service, switch: [x], value: off, reason: x, until: 2030-01-01}', 'switch'),
    ('{machine: box-a, shape: [x], switch: lease_reclaim, value: off, reason: x, until: 2030-01-01}', 'shape'),
])
def test_an_exception_needs_reason_and_until(tmp_path, row, words):
    assert words in broken(tmp_path, 'baseline:\n  version: 1\n  exceptions:\n    - ' + row + '\n').error


def test_an_exception_holds_until_its_date_then_expires(tmp_path):
    block = baseline.load(inventory(tmp_path)).block
    args = ('box-a', 'root_service', 'main', 'worker_idle_reclaim_min')
    assert block.exception(*args, now=NOW) is not None
    assert block.exception(*args, now=datetime.datetime(2030, 1, 15, 23, 59)) is not None  # the whole last day
    assert block.exception(*args, now=datetime.datetime(2030, 1, 16, 0, 0)) is None
    assert block.exception('box-a', 'root_service', 'other-fleet', 'worker_idle_reclaim_min', now=NOW) is None
    timed = inventory(tmp_path, GOOD.replace('until: 2030-01-15', 'until: "2030-01-10T11:00:00"'))
    assert baseline.load(timed).block.exception(*args, now=NOW) is None  # a time of day: compared as given


# -- the file is the only source -----------------------------------------------------------------------------

def test_the_block_is_reread_when_the_file_changes(tmp_path):
    path = inventory(tmp_path)
    assert baseline.load(path).block.table('box-a', 'root_service')['max_busy'] == 3
    path.write_text(path.read_text().replace('max_busy: 3', 'max_busy: 4  # edited'))
    assert baseline.load(path).block.table('box-a', 'root_service')['max_busy'] == 4


def test_writing_other_blocks_leaves_the_baseline_bytes_unchanged(tmp_path):
    path = inventory(tmp_path)
    before = baseline.block_text(path.read_text())
    assert before.startswith('baseline:\n') and 'until: 2030-01-15' in before
    fs.write_block('models', {'version': 1, 'default': {'supervisor': 'gamma', 'worker': 'delta'}}, path=path)
    fs.write_block('quota', {'pools': {}}, path=path)
    fs.write_self({'hostname': 'box-a.lan'}, name='box-a', path=path)
    after = path.read_text()
    assert baseline.block_text(after) == before
    assert fs.read(path)['models']['default']['supervisor'] == 'gamma'


def test_no_code_path_writes_the_block():
    assert 'baseline' not in hot.HOT_BLOCKS  # never loaded into Redis as a writable block, never flushed
    with pytest.raises(fs.InventoryError):
        fs.write_block('baseline', {'version': 1})
    assert not any(name.startswith('write') for name in dir(baseline))


# -- a broken block breaks only itself -----------------------------------------------------------------------

def test_a_syntax_error_in_the_block_keeps_models_and_workflows_readable(tmp_path):
    path = inventory(tmp_path, 'baseline:\n  version: 1\n  machines: {box-a: [unclosed\n')
    data = fs.read(path)
    assert data['models']['default']['supervisor'] == 'alpha'
    assert fs.workflows(path) == {'wf-one': {'machine': 'box-a', 'applets': []}}
    assert fs.self_name(path) == 'box-a'
    loaded = baseline.load(path)
    assert loaded.block is None and 'baseline' in loaded.error


def test_a_syntax_error_elsewhere_still_raises(tmp_path):
    path = tmp_path / 'fleet-superpowers.yaml'
    path.write_text('models: {unclosed\n' + GOOD)
    with pytest.raises(fs.InventoryError):
        fs.read(path)
    assert baseline.load(path).block.check_every_s == 120  # the baseline's own text is read on its own


def test_a_bad_block_never_raises_from_load(tmp_path):
    for text in ('baseline: 7\n', 'baseline:\n  version: 1\n  machines: [a]\n', 'baseline:\n  - 1\n'):
        loaded = baseline.load(inventory(tmp_path, text))
        assert loaded.block is None and loaded.error


# -- the shapes and the precedence ---------------------------------------------------------------------------

@pytest.mark.parametrize('cfg, shape', [
    ({'mode': 'service'}, 'root_service'),
    ({'mode': 'task'}, None),
    ({'mode': 'task', 'parent_config': '/p', 'job': 'j-1'}, 'job'),
    ({'mode': 'service', 'parent_config': '/p', 'named': {'name': 'n'}}, 'named'),
    ({'mode': 'service', 'parent_config': '/p', 'named': {'name': 'n', 'attached': True}}, 'attached'),
    ({'mode': 'task', 'parent_config': '/p'}, 'split'),
])
def test_shape_of_a_run_config(cfg, shape):
    assert baseline.shape_of(cfg) == shape


def decide(tmp_path, current, typed, resume=False, now=NOW, fleet='main'):
    block = baseline.load(inventory(tmp_path)).block
    return block.resolve('box-a', 'root_service', fleet, current, typed, resume=resume, now=now)


def test_precedence_new_run(tmp_path):
    current = {'supervisor_split': False, 'worker_idle_reclaim_min': None, 'lease_reclaim': 'off'}
    out = decide(tmp_path, current, typed={'lease_reclaim', 'worker_idle_reclaim_min'})
    assert (out['supervisor_split'].source, out['supervisor_split'].value) == ('baseline', True)  # not typed
    assert out['lease_reclaim'].source == 'typed' and out['lease_reclaim'].value == 'off'
    assert out['lease_reclaim'].deviation  # typed against the baseline, no exception
    reclaim = out['worker_idle_reclaim_min']  # typed, but a live exception covers it: no report
    assert reclaim.source == 'exception' and not reclaim.deviation and reclaim.value is None
    untyped = decide(tmp_path, {'worker_idle_reclaim_min': 30}, typed=set())['worker_idle_reclaim_min']
    assert untyped.source == 'exception' and untyped.value is None  # the exception's value is the default
    expired = decide(tmp_path, current, typed={'worker_idle_reclaim_min'}, now=datetime.datetime(2030, 2, 1))
    assert expired['worker_idle_reclaim_min'].deviation  # after until: checked again


def test_precedence_resume(tmp_path):
    current = {'supervisor_split': False, 'lease_reclaim': 'on', 'kit_fleet': None, 'worker_idle_reclaim_min': None,
               'max_busy': 0}
    out = decide(tmp_path, current, typed={'lease_reclaim', 'supervisor_split'}, resume=True)
    split = out['supervisor_split']  # typed, but resume ignores it today (not in CONTINUE_APPLIED): baseline
    assert split.source == 'baseline' and split.apply and split.value is True
    assert out['lease_reclaim'].source == 'typed' and not out['lease_reclaim'].apply
    assert not out['worker_idle_reclaim_min'].apply  # under a live exception: left alone
    kit = out['kit_fleet']
    assert not kit.apply and kit.deviation and kit.new_run  # cannot be applied on resume: needs a new run
    assert not out['max_busy'].apply  # roster switch: the daemon's check, not resume
    typed_off = decide(tmp_path, dict(current, lease_reclaim='off'), typed={'lease_reclaim'}, resume=True)
    assert typed_off['lease_reclaim'].deviation and typed_off['lease_reclaim'].value == 'off'
