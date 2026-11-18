"""The fleet-superpowers inventory, validation and write side (docs/distributed-development.md, "Resource inventory").

Every test uses a temporary file; the real ~/.fleetmux is never read or written.
"""
import fcntl
import os
import stat
from pathlib import Path

import pytest
import yaml

from fleetmux import fleet_superpowers as fs

DOCS = Path(__file__).resolve().parents[1] / 'docs' / 'distributed-development.md'
NOW = '2030-01-02T09:00:00+00:00'


def doc_example():
    """The Complete example of docs/distributed-development.md, verbatim (comments and flow rows included)."""
    text = DOCS.read_text()
    start = text.index('# ~/.fleetmux/fleet-superpowers/fleet-superpowers.yaml on the field machine')
    return text[start:text.index('```', start)]


@pytest.fixture
def inv(tmp_path):
    path = tmp_path / 'inv' / 'fleet-superpowers.yaml'
    path.parent.mkdir()
    path.write_text(doc_example())
    return path


def outside(text, start_marker, end_marker):
    """The text before start_marker and from end_marker on: everything outside one entry."""
    a = text.index(start_marker)
    b = text.index(end_marker, a)
    return text[:a], text[b:]


# -- environment and isolation -------------------------------------------------------------------------------

def test_env_names_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv(fs.ENV, str(tmp_path / 'x.yaml'))
    assert fs.file_path() == tmp_path / 'x.yaml'
    assert fs.directory() == tmp_path


def test_suite_never_points_at_the_real_file():
    assert os.environ.get(fs.ENV)  # set by tests/conftest.py for every test
    assert fs.file_path() != Path.home() / '.fleetmux' / 'fleet-superpowers' / 'fleet-superpowers.yaml'


# -- validation ----------------------------------------------------------------------------------------------

def test_the_doc_example_is_valid(inv):
    assert fs.validate(fs.read(inv)) == []
    assert fs.validate(fs.read(inv), require_self=True) == []


@pytest.mark.parametrize('entry, needle', [
    ({'source': 'mine'}, 'source'),
    ({'source': 'copy', 'reach': [{'via': 'lan'}]}, 'reach'),
    ({'source': 'copy', 'reach': [{'via': '', 'ssh': 'user@home.example'}]}, 'via'),
    ({'source': 'copy', 'reach': [{'via': 'lan', 'ssh': '-oProxyCommand=x'}]}, 'ssh'),
    ({'source': 'copy', 'reach': {'via': 'lan', 'ssh': 'a'}}, 'reach'),
    ({'source': 'copy', 'addresses': [{'kind': 'moon', 'address': '192.0.2.1'}]}, 'addresses'),
    ({'source': 'copy', 'updated_at': 'yesterday'}, 'updated_at'),
    ({'source': 'copy', 'updated_at': '2030-01-01T10:00:00'}, 'updated_at'),
    ({'source': 'self', 'copied_at': NOW}, 'copied_at'),
    ({'source': 'copy', 'fleetmux': {'bin': 5}}, 'fleetmux'),
    ({'source': 'copy', 'clis': {'claude': {'version': '2.1.0', 'logged_in': 'yes'}}}, 'clis'),
    ({'source': 'copy', 'models': [{'id': 'opus', 'cli': 'claude', 'reachable': 1, 'checked_at': NOW}]}, 'models'),
    ({'source': 'copy', 'models': [{'id': 'opus', 'cli': 'claude', 'reachable': True, 'answered': '401'}]},
     'models'),
    ({'source': 'copy', 'models': [{'id': 'opus', 'cli': 'claude', 'reachable': True, 'source': 'probed'}]},
     'models'),
    ({'source': 'copy', 'can_start_claude': 'no'}, 'can_start_claude'),
    ({'source': 'copy', 'fleets': [{'name': 'main', 'running': 'yes'}]}, 'fleets'),
    ({'source': 'copy', 'hostname': 7}, 'hostname'),
])
def test_bad_machine_entries(entry, needle):
    problems = fs.validate_machine('home', entry)
    assert problems and any(needle in p for p in problems), problems


def test_minimal_copy_entry_is_valid():
    assert fs.validate_machine('home', {'source': 'copy', 'reach': [{'via': 'lan', 'ssh': 'user@home.example'}]}) == []


def test_self_count_rules():
    two = {'machines': {'a': {'source': 'self'}, 'b': {'source': 'self'}}}
    assert any('more than one' in p for p in fs.validate(two))
    none = {'machines': {'a': {'source': 'copy'}}}
    assert fs.validate(none) == []
    assert any('source: self' in p for p in fs.validate(none, require_self=True))


def test_blocks_and_write_back_setting_are_checked():
    assert any('quota' in p for p in fs.validate({'quota': [1]}))
    assert fs.validate({'hot': {'write_back_seconds': 60}}) == []
    assert any('write_back_seconds' in p for p in fs.validate({'hot': {'write_back_seconds': 0}}))
    assert any('hot' in p for p in fs.validate({'hot': 5}))


WORKFLOW = {
    'kind': 'workflow', 'summary': 'Route Claude through the home machine.',
    'applies_when': ['Claude sessions fail on the field machine.'],
    'check': {'machine': 'field', 'argv': ['python3', '-m', 'route.check']},
    'source': {'repo': '/srv/applets/route', 'ref': '4d5e6f7', 'from': 'home'},
    'applets': [{'machine': 'home', 'fleet': 'main',
                 'host': {'subfleet': 'route-home', 'supervisor_slot': 'route-host-1',
                          'supervisor_role': 'teams/host-supervisor.md', 'roster': 'teams/home.yaml'}},
                {'machine': 'field', 'fleet': 'field', 'roster_rows': 'teams/field-applet.yaml'}],
    'saved_on': ['home', 'field'],
}


def test_workflow_ok():
    assert fs.validate_workflow('r', WORKFLOW) == []


@pytest.mark.parametrize('change, needle', [
    ({'kind': 5}, 'kind'),
    ({'summary': ''}, 'summary'),
    ({'applies_when': 'always'}, 'applies_when'),
    ({'check': {'machine': 'field', 'argv': []}}, 'check'),
    ({'source': {'repo': '/srv/a', 'ref': 'main', 'from': 'home'}}, 'source.ref'),
    ({'source': {'ref': '4d5e6f7', 'from': 'home'}}, 'source.repo'),
    ({'source': {'repo': '/srv/a', 'ref': '4d5e6f7'}}, 'source.from'),
    ({'applets': []}, 'applets'),
    ({'applets': [{'machine': 'home'}]}, 'fleet'),
    ({'applets': [{'machine': 'home', 'fleet': 'main'}]}, 'host'),
    ({'applets': [{'machine': 'home', 'fleet': 'main', 'roster_rows': 'a.yaml',
                   'host': WORKFLOW['applets'][0]['host']}]}, 'host'),
    ({'applets': [{'machine': 'home', 'fleet': 'main', 'host': {'subfleet': 'x'}}]}, 'host'),
    ({'saved_on': 'home'}, 'saved_on'),
])
def test_bad_workflows(change, needle):
    problems = fs.validate_workflow('r', dict(WORKFLOW, **change))
    assert problems and any(needle in p for p in problems), problems


def test_other_kinds_are_kept_unchecked():
    assert fs.validate_workflow('x', {'kind': 'note', 'anything': 1}) == []


# -- writes keep every other byte --------------------------------------------------------------------------

def test_write_self_rewrites_only_the_self_entry(inv):
    before = inv.read_text()
    entry = dict(fs.machine('field', inv), hostname='field2.example', updated_at=NOW)
    assert fs.write_self(entry, path=inv) == 'field'
    after = inv.read_text()
    head_b, tail_b = outside(before, '  field:\n', '  home:\n')
    head_a, tail_a = outside(after, '  field:\n', '  home:\n')
    assert (head_a, tail_a) == (head_b, tail_b)  # comments, flow rows, other entries: byte for byte
    assert fs.machine('field', inv)['hostname'] == 'field2.example'
    assert fs.machine('field', inv)['source'] == 'self'


def test_write_self_names_a_new_self_entry(tmp_path):
    path = tmp_path / 'f.yaml'
    assert fs.write_self({'hostname': 'field.example', 'reach': []}, name='field', path=path) == 'field'
    assert fs.self_name(path) == 'field'
    with pytest.raises(fs.InventoryError, match='name'):
        fs.write_self({'hostname': 'x'}, path=tmp_path / 'g.yaml')


def test_write_self_refuses_another_name_or_a_copy(inv):
    with pytest.raises(fs.InventoryError, match='field'):
        fs.write_self({'hostname': 'x'}, name='other', path=inv)
    with pytest.raises(fs.InventoryError, match='copy'):
        fs.write_self({'hostname': 'x'}, name='home', path=inv)


def test_import_copy_replaces_the_section_and_keeps_our_reach(inv):
    before = inv.read_text()
    ours = fs.machine('home', inv)['reach']
    incoming = {'source': 'self', 'updated_at': NOW, 'hostname': 'home.example',
                'reach': [{'via': 'overlay', 'ssh': 'user@home.example'}]}  # one of ours: kept once
    out = fs.import_copy('home', incoming, now=NOW, path=inv)
    assert out == {'source': 'copy', 'copied_at': NOW, 'updated_at': NOW, 'hostname': 'home.example',
                   'reach': ours}
    raw = yaml.safe_load(inv.read_text())['machines']['home']
    assert raw == out  # old keys (clis, fleets ...) are gone: replaced whole, except reach
    assert inv.read_text().startswith(before[:before.index('  home:\n')])  # header and self entry unchanged
    assert incoming['source'] == 'self'  # the caller's dict is not changed


def test_import_copy_merges_reach_without_duplicates(tmp_path):
    path = tmp_path / 'f.yaml'
    fs.add_copy('home', 'home-lan', now=NOW, path=path)
    fs.add_copy('home', 'home-ovl', via='overlay', path=path)
    owner = [{'via': 'tunnel', 'ssh': 'home-relay'}, {'via': 'lan', 'ssh': 'home-lan'},
             {'via': 'overlay', 'ssh': 'home-lan'}]
    want = [{'via': 'lan', 'ssh': 'home-lan'}, {'via': 'overlay', 'ssh': 'home-ovl'},
            {'via': 'tunnel', 'ssh': 'home-relay'}, {'via': 'overlay', 'ssh': 'home-lan'}]
    assert fs.import_copy('home', {'reach': owner}, now=NOW, path=path)['reach'] == want
    assert fs.import_copy('home', {'reach': owner}, now=NOW, path=path)['reach'] == want  # stable on repeat
    assert fs.import_copy('home', {'reach': []}, now=NOW, path=path)['reach'] == want


def test_import_copy_refuses_the_self_name(inv):
    before = inv.read_text()
    with pytest.raises(fs.InventoryError, match='self'):
        fs.import_copy('field', {'hostname': 'field.example'}, path=inv)
    assert inv.read_text() == before


def test_import_copy_validates(inv):
    with pytest.raises(fs.InventoryError, match='reach'):
        fs.import_copy('home', {'reach': 'user@home.example'}, path=inv)


def test_add_copy_creates_a_minimal_entry(tmp_path):
    path = tmp_path / 'f.yaml'
    entry = fs.add_copy('home', 'user@home.example', now=NOW, path=path)
    assert entry == {'source': 'copy', 'copied_at': NOW, 'reach': [{'via': 'lan', 'ssh': 'user@home.example'}]}
    assert fs.machine('home', path)['reach'] == [{'via': 'lan', 'ssh': 'user@home.example'}]
    with pytest.raises(fs.InventoryError, match='already'):
        fs.add_copy('home', 'user@home.example', path=path)
    with pytest.raises(fs.InventoryError, match='ssh'):
        fs.add_copy('far', '-oProxyCommand=x', path=path)


def test_add_copy_appends_reach_entries_in_order(tmp_path):
    path = tmp_path / 'f.yaml'
    fs.add_copy('home', 'user@home.example', now=NOW, path=path)
    entry = fs.add_copy('home', 'home-overlay', via='overlay', now='2030-01-03T00:00:00+00:00', path=path)
    assert entry['copied_at'] == NOW  # only reach changes
    fs.add_copy('home', 'user@home.example', via='overlay', path=path)  # same ssh, another label: a new entry
    assert fs.machine('home', path)['reach'] == [{'via': 'lan', 'ssh': 'user@home.example'},
                                                 {'via': 'overlay', 'ssh': 'home-overlay'},
                                                 {'via': 'overlay', 'ssh': 'user@home.example'}]
    with pytest.raises(fs.InventoryError, match='already'):
        fs.add_copy('home', 'home-overlay', via='overlay', path=path)
    assert len(fs.machine('home', path)['reach']) == 3


def test_add_copy_appends_to_a_synced_copy_and_keeps_the_rest(inv):
    before = inv.read_text()
    head, tail = outside(before, '  home:\n', 'workflows:')
    stored = fs.machine('home', inv)
    fs.add_copy('home', 'home-backup', via='tunnel', path=inv)
    after = fs.machine('home', inv)
    assert after['reach'] == stored['reach'] + [{'via': 'tunnel', 'ssh': 'home-backup'}]
    assert dict(after, reach=None) == dict(stored, reach=None)
    text = inv.read_text()
    assert text.startswith(head) and text.endswith(tail)


def test_remove_reach_removes_exactly_one_entry(tmp_path):
    path = tmp_path / 'f.yaml'
    fs.add_copy('home', 'user@home.example', now=NOW, path=path)
    fs.add_copy('home', 'home-overlay', via='overlay', path=path)
    fs.add_copy('home', 'user@home.example', via='overlay', path=path)
    with pytest.raises(fs.InventoryError, match='2 reach entries'):
        fs.remove_reach('home', 'user@home.example', path=path)
    with pytest.raises(fs.InventoryError, match='no reach entry'):
        fs.remove_reach('home', 'nowhere', path=path)
    with pytest.raises(fs.InventoryError, match='no machine'):
        fs.remove_reach('far', 'user@home.example', path=path)
    entry = fs.remove_reach('home', 'user@home.example', via='overlay', path=path)
    assert entry['reach'] == [{'via': 'lan', 'ssh': 'user@home.example'}, {'via': 'overlay', 'ssh': 'home-overlay'}]
    assert entry['copied_at'] == NOW
    fs.remove_reach('home', 'user@home.example', path=path)
    fs.remove_reach('home', 'home-overlay', path=path)
    assert fs.machine('home', path)['reach'] == []  # the machine stays, unreachable


def test_add_and_remove_reach_on_the_self_entry(inv):
    stored = fs.machine('field', inv)
    entry = fs.add_copy('field', 'user@field.example', via='overlay', path=inv)
    assert entry['source'] == 'self' and 'copied_at' not in entry
    assert dict(entry, reach=None) == dict(stored, reach=None)
    assert entry['reach'] == stored['reach'] + [{'via': 'overlay', 'ssh': 'user@field.example'}]
    assert fs.self_name(inv) == 'field'
    assert fs.remove_reach('field', 'user@field.example', path=inv)['reach'] == stored['reach']


def test_add_refuses_an_unknown_via_and_lists_the_kinds(tmp_path):
    path = tmp_path / 'f.yaml'
    for via in ('pigeon', 'jump', ''):
        with pytest.raises(fs.InventoryError, match='thunderbolt, lan, overlay, tunnel, tunnel-overseas'):
            fs.add_copy('home', 'user@home.example', via=via, path=path)
    assert not path.exists() or fs.machine('home', path) is None
    for via in ('thunderbolt', 'tunnel-overseas'):
        fs.add_copy('home', 'home-' + via, via=via, path=path)


def test_a_stored_entry_may_carry_another_label(inv):
    assert fs.validate_machine('x', {'source': 'copy', 'reach': [{'via': 'jump', 'ssh': 'a'}]}) == []
    assert fs.validate_machine('x', {'source': 'copy', 'reach': [{'ssh': 'a'}]}) != []


def test_workflows_save_get_delete(inv):
    before = inv.read_text()
    assert fs.workflow('claude-via-home', inv)['source']['from'] == 'home'
    assert list(fs.workflows(inv)) == ['claude-via-home']
    assert fs.save_workflow('route-b', WORKFLOW, path=inv) == WORKFLOW
    assert inv.read_text().startswith(before.rstrip('\n'))  # appended after the existing entry
    assert fs.workflow('route-b', inv) == WORKFLOW
    assert fs.delete_workflow('route-b', path=inv) is True
    assert inv.read_text() == before
    assert fs.delete_workflow('route-b', path=inv) is False
    assert fs.workflow('nothing', inv) is None
    with pytest.raises(fs.InventoryError, match='summary'):
        fs.save_workflow('bad', dict(WORKFLOW, summary=''), path=inv)


def test_flow_style_block_is_refused_not_rewritten(tmp_path):
    path = tmp_path / 'f.yaml'
    path.write_text('machines: {home: {source: copy}}\n')
    with pytest.raises(fs.InventoryError, match='block style'):
        fs.add_copy('far', 'user@far.example', path=path)
    assert path.read_text() == 'machines: {home: {source: copy}}\n'


def test_empty_flow_block_is_filled(tmp_path):
    path = tmp_path / 'f.yaml'
    path.write_text('# keep me\nmachines: {}\nworkflows: {}\n')
    fs.add_copy('home', 'user@home.example', now=NOW, path=path)
    text = path.read_text()
    assert text.startswith('# keep me\nmachines:\n  home:\n') and text.endswith('workflows: {}\n')


def test_block_write_and_read(inv):
    before = inv.read_text()
    fs.write_block('quota', {'pools': {'p': {'at': 1}}}, path=inv)
    assert fs.read_block('quota', inv) == {'pools': {'p': {'at': 1}}}
    a, b = before.index('quota:'), before.index('\n', before.index('quota:'))
    assert inv.read_text()[:a] == before[:a]
    assert inv.read_text().endswith(before[b + 1:])
    with pytest.raises(fs.InventoryError):
        fs.write_block('machines', {}, path=inv)


# -- atomic and locked ---------------------------------------------------------------------------------------

def test_write_holds_the_lock_and_renames(inv, monkeypatch):
    seen = {}
    real_replace = os.replace

    def replace(src, dst):
        fd = os.open(fs.lock_path(inv), os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        seen['src'], seen['dst'] = Path(src), Path(dst)
        seen['tmp_text'] = Path(src).read_text()
        return real_replace(src, dst)
    monkeypatch.setattr(fs.os, 'replace', replace)
    fs.add_copy('far', 'user@far.example', now=NOW, path=inv)
    assert seen['dst'] == inv and seen['src'].parent == inv.parent and seen['src'] != inv
    assert seen['tmp_text'] == inv.read_text()
    assert stat.S_IMODE(inv.stat().st_mode) == 0o600


def test_failed_rename_leaves_the_file_and_no_temp(inv, monkeypatch):
    before = inv.read_text()

    def boom(src, dst):
        raise OSError('disk full')
    monkeypatch.setattr(fs.os, 'replace', boom)
    with pytest.raises(OSError):
        fs.add_copy('far', 'user@far.example', path=inv)
    assert inv.read_text() == before
    assert sorted(p.name for p in inv.parent.iterdir()) == sorted([inv.name, fs.lock_path(inv).name])


def test_a_held_lock_blocks_the_writer(inv):
    fd = os.open(fs.lock_path(inv), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(fs.InventoryError, match='locked'):
            fs.add_copy('far', 'user@far.example', path=inv, lock_timeout=0.2)
    finally:
        os.close(fd)
    fs.add_copy('far', 'user@far.example', path=inv)


def test_new_files_are_ascii():
    root = Path(__file__).resolve().parents[1]
    for path in (list((root / 'fleetmux').glob('*superpowers*.py')) + list((root / 'tests').glob('test_*superpowers*.py'))):
        path.read_bytes().decode('ascii')


def test_suite_never_reads_the_real_old_quota_file():
    assert not str(fs.old_location('quota')).startswith(str(Path.home() / '.fleetmux'))


def test_unquoted_timestamps_stay_strings(tmp_path):
    path = tmp_path / 'f.yaml'
    path.write_text('machines:\n  home:\n    source: copy\n    copied_at: 2030-01-01T10:00:00+00:00\n')
    assert fs.machine('home', path)['copied_at'] == '2030-01-01T10:00:00+00:00'
    fs.add_copy('far', 'user@far.example', now=NOW, path=path)  # the write path parses the same way
    assert fs.validate(fs.read(path)) == []


def test_a_bad_unrelated_entry_does_not_block_a_write(tmp_path):
    path = tmp_path / 'f.yaml'
    path.write_text('machines:\n  odd:\n    source: copy\n    reach: [{ssh: a}]\n')
    fs.write_block('quota', {'at': 1}, path=path)
    fs.add_copy('far', 'user@far.example', now=NOW, path=path)
    assert fs.read_block('quota', path) == {'at': 1}
    assert any('odd' in p for p in fs.validate(fs.read(path)))  # still reported by check
