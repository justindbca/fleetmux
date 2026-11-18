"""The fleet-superpowers inventory, read side (docs/distributed-development.md, "Resource inventory").

Every test points the data root at a temporary HOME; the real ~/.fleetmux is never read or written.
"""
import pytest

from fleetmux import fleet_superpowers as fs, paths

EXAMPLE = """\
models: {}
quota: {}
machines:
  field:
    source: self
    updated_at: "2030-01-01T10:00:00+00:00"
    hostname: field.example
    reach: []
    fleetmux:
      release: 0123abc
  home:
    source: copy
    copied_at: "2030-01-01T10:05:00+00:00"
    hostname: home.example
    reach:
      - {via: lan, ssh: user@home.lan.example}
      - {via: overlay, ssh: user@home.example}
      - {via: jump, ssh: home-via-jump}
    fleetmux:
      bin: /opt/fleetmux/bin/fleetmux
      release: 0123abc
    can_start_claude: true
workflows:
  claude-via-home:
    kind: workflow
    summary: Route Claude sessions through the home machine.
"""


@pytest.fixture
def root(monkeypatch, tmp_path):
    """A temporary data root: HOME moves, so paths.root() and every default path move with it."""
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))
    monkeypatch.delenv(fs.ENV, raising=False)  # tests/conftest.py points it at a temporary file
    monkeypatch.setenv('FLEETMUX_HOME', str(tmp_path / 'user' / '.fleetmux' / 'fleets' / 'other'))
    return tmp_path / 'user' / '.fleetmux'


def write(root, text):
    path = root / 'fleet-superpowers' / 'fleet-superpowers.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_path_sits_next_to_fleets_under_the_data_root(root):
    assert paths.root() == root
    assert fs.directory() == root / 'fleet-superpowers'
    assert fs.file_path() == root / 'fleet-superpowers' / 'fleet-superpowers.yaml'
    assert fs.directory().parent == (root / 'fleets').parent  # FLEETMUX_HOME of a second fleet is not followed


def test_missing_file_reads_as_empty_blocks_and_creates_nothing(root):
    assert fs.read() == {'models': {}, 'machines': {}, 'quota': {}, 'workflows': {}}
    assert fs.machines() == {}
    assert fs.machine('home') is None
    assert fs.self_name() is None
    assert not (root / 'fleet-superpowers').exists()


def test_empty_file_and_empty_blocks_read_as_empty(root):
    write(root, '')
    assert fs.read() == {'models': {}, 'machines': {}, 'quota': {}, 'workflows': {}}
    write(root, 'machines:\nworkflows: {}\n')
    assert fs.read()['machines'] == {}
    assert fs.machines() == {}
    assert fs.self_name() is None


def test_read_returns_every_block(root):
    write(root, EXAMPLE)
    data = fs.read()
    assert list(data) == ['models', 'machines', 'quota', 'workflows']
    assert data['workflows']['claude-via-home']['kind'] == 'workflow'
    assert data['machines']['home']['hostname'] == 'home.example'


def test_read_keeps_an_unknown_block(root):
    write(root, 'machines: {}\nextra: {a: 1}\n')
    assert fs.read()['extra'] == {'a': 1}


def test_machines_lists_every_entry_in_file_order(root):
    write(root, EXAMPLE)
    found = fs.machines()
    assert list(found) == ['field', 'home']
    assert found['home']['source'] == 'copy'


def test_machine_keeps_reach_in_order(root):
    write(root, EXAMPLE)
    entry = fs.machine('home')
    assert [r['ssh'] for r in entry['reach']] == ['user@home.lan.example', 'user@home.example', 'home-via-jump']
    assert [r['via'] for r in entry['reach']] == ['lan', 'overlay', 'jump']
    assert entry['fleetmux'] == {'bin': '/opt/fleetmux/bin/fleetmux', 'release': '0123abc'}
    assert entry['can_start_claude'] is True


def test_machine_with_empty_reach_and_default_bin(root):
    write(root, EXAMPLE)
    entry = fs.machine('field')
    assert entry['reach'] == []
    assert entry['fleetmux'] == {'bin': '~/fleetmux-current/bin/fleetmux', 'release': '0123abc'}
    assert fs.DEFAULT_BIN == '~/fleetmux-current/bin/fleetmux'


def test_machine_with_no_reach_and_no_fleetmux_key(root):
    write(root, 'machines:\n  bare:\n    source: copy\n')
    assert fs.machine('bare') == {'source': 'copy', 'reach': [], 'fleetmux': {'bin': fs.DEFAULT_BIN}}


def test_machine_unknown_name_is_none(root):
    write(root, EXAMPLE)
    assert fs.machine('nowhere') is None


def test_self_name(root):
    write(root, EXAMPLE)
    assert fs.self_name() == 'field'
    write(root, 'machines:\n  home: {source: copy}\n')
    assert fs.self_name() is None


def test_self_name_refuses_two_self_entries(root):
    write(root, 'machines:\n  a: {source: self}\n  b: {source: self}\n')
    with pytest.raises(fs.InventoryError, match='more than one'):
        fs.self_name()


def test_explicit_path_overrides_the_default(root, tmp_path):
    other = tmp_path / 'elsewhere.yaml'
    other.write_text(EXAMPLE)
    assert fs.machine('home', path=other)['hostname'] == 'home.example'
    assert fs.self_name(path=other) == 'field'
    assert fs.machines(path=other)['field']['reach'] == []
    assert fs.read(path=other)['quota'] == {}
    assert not fs.file_path().exists()


def test_reading_never_writes(root):
    path = write(root, EXAMPLE)
    before = path.stat().st_mtime_ns, path.read_text()
    fs.read(), fs.machines(), fs.machine('home'), fs.self_name()
    assert (path.stat().st_mtime_ns, path.read_text()) == before
    assert sorted(p.name for p in path.parent.iterdir()) == ['fleet-superpowers.yaml']


@pytest.mark.parametrize('text', ['machines: [a, b]\n', '- a\n- b\n', 'machines: {a: [1]}\n', 'machines: {a: {reach: x}}\n',
                                  'machines: {a: {fleetmux: x}}\n', 'machines: {a\n'])
def test_malformed_file_raises(root, text):
    write(root, text)
    with pytest.raises(fs.InventoryError):
        fs.machines()
