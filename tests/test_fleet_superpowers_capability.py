"""Capability fields of ``machines.<self>`` (docs/distributed-development.md, "machines entry schema"):
``hardware`` (cores, perf_cores, mem_gb, disk_free_gb), ``always_on`` and ``toolchains``, measured by
``fleetmux superpowers refresh``. Every test uses the temporary inventory of tests/conftest.py
(FLEETMUX_SUPERPOWERS); system queries and version commands are stubs, toolchains are fake executables found on a
given search path.
"""
import re
import stat

import pytest
import yaml

from fleetmux import discover, fleet_superpowers as fs, fleet_superpowers_capability as cap
from fleetmux import fleet_superpowers_refresh as rf
from test_fleet_superpowers_cli import DISCOVERED, NOW, Probe
from test_fleet_superpowers_write import doc_example

HARDWARE = {'cores': 16, 'perf_cores': 12, 'mem_gb': 128.0, 'disk_free_gb': 132.4}


@pytest.fixture
def inv():
    path = fs.file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc_example())
    return path


@pytest.fixture
def measured(monkeypatch):
    """What capability.measure reports (a dict the test may change)."""
    facts = {'hardware': dict(HARDWARE), 'always_on': True, 'toolchains': {'python': '3.12.4'}}
    monkeypatch.setattr(cap, 'measure', lambda: {k: (dict(v) if isinstance(v, dict) else v) for k, v in facts.items()})
    return facts


def refresh(probe=None):
    return rf.refresh(probe=probe or Probe(), discovered=DISCOVERED, found={'instances': []}, now=NOW,
                      hostname='field.example', addresses=[], bin_=fs.DEFAULT_BIN, release='0123abc')


def stored(inv):
    return yaml.safe_load(inv.read_text())['machines']['field']


def drop_always_on(inv):
    """The field entry without always_on (it comes first in the documented example)."""
    inv.write_text(re.sub(r'^    always_on: .*\n', '', inv.read_text(), count=1, flags=re.M))
    assert 'always_on' not in fs.machine('field')


# -- refresh writes the fields ----------------------------------------------------------------------------

def test_refresh_writes_hardware_always_on_and_toolchains(inv, measured):
    drop_always_on(inv)
    measured['toolchains'] = {'rust': '1.83.0', 'python': '3.12.4'}
    _, entry = refresh()
    s = stored(inv)
    assert s == entry
    assert s['hardware'] == HARDWARE
    assert s['always_on'] is True
    assert s['toolchains'] == {'rust': '1.83.0', 'python': '3.12.4'}
    assert fs.validate(fs.read(inv), require_self=True) == []


def test_the_capability_keys_are_their_own_keys_after_the_rest(inv, measured):
    _, entry = refresh()
    assert set(cap.KEYS) <= set(entry)  # the existing keys are built as before; the three are added beside them
    assert {k for k in entry if k not in cap.KEYS} == set(rf.entry(None, DISCOVERED, {'instances': []}, Probe(), NOW,
                                                                    'h', [], fs.DEFAULT_BIN, 'r'))


def test_always_on_written_by_hand_survives_a_refresh(inv, measured):
    drop_always_on(inv)
    measured['always_on'] = False
    refresh()  # first refresh: the measured default (a laptop)
    assert stored(inv)['always_on'] is False
    inv.write_text(inv.read_text().replace('    always_on: false\n', '    always_on: true\n'))  # owner: stays on
    refresh()
    assert stored(inv)['always_on'] is True


def test_always_on_set_by_hand_while_refresh_measures_is_kept(inv, monkeypatch):
    drop_always_on(inv)

    def measure():  # the owner edits the file while refresh measures
        text = inv.read_text()
        inv.write_text(text.replace('  field:\n', '  field:\n    always_on: true\n', 1))
        return {'hardware': dict(HARDWARE), 'always_on': False, 'toolchains': {}}
    monkeypatch.setattr(cap, 'measure', measure)
    refresh()
    assert stored(inv)['always_on'] is True


def test_refresh_measures_with_the_capability_probes(inv, monkeypatch):
    monkeypatch.setattr(cap, 'hardware_of', lambda: dict(HARDWARE, disk_free_gb=1.5))
    monkeypatch.setattr(cap, 'has_battery', lambda: True)
    monkeypatch.setattr(cap, 'toolchains_of', lambda search=None: {'node': '22.1.0'})
    drop_always_on(inv)
    refresh()
    s = stored(inv)
    assert s['hardware']['disk_free_gb'] == 1.5 and s['always_on'] is False and s['toolchains'] == {'node': '22.1.0'}


def test_capability_write_refuses_a_copy_entry(inv):
    with pytest.raises(fs.InventoryError):
        cap.write('home', facts={'hardware': dict(HARDWARE), 'always_on': True, 'toolchains': {}})


# -- measuring --------------------------------------------------------------------------------------------

def fake_tool(directory, name, output):
    path = directory / name
    path.write_text('#!/bin/sh\necho "' + output + '"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_toolchains_are_found_on_the_search_path_and_only_their_version_is_asked(tmp_path):
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    log = tmp_path / 'calls'
    (bin_ / 'rustc').write_text('#!/bin/sh\necho "$0 $*" >> ' + str(log) + '\necho "rustc 1.83.0 (90b35a623 2024)"\n')
    (bin_ / 'rustc').chmod(0o755)
    fake_tool(bin_, 'node', 'v22.1.0')
    found = cap.toolchains_of(str(bin_))
    assert found == {'rust': '1.83.0', 'node': '22.1.0'}  # missing tools (python, go) are left out
    assert log.read_text().split()[1:] == ['--version']


def test_toolchains_use_the_one_resolver_when_no_search_path_is_given(monkeypatch, tmp_path):
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    fake_tool(bin_, 'go', 'go version go1.22.3 darwin/arm64')
    asked = []
    monkeypatch.setenv('FLEETMUX_DISCOVERY', 'on')
    monkeypatch.setattr(discover, 'search_path', lambda warn=None: asked.append(1) or str(bin_))
    assert cap.toolchains_of() == {'go': '1.22.3'}
    assert asked == [1]


def test_toolchains_are_not_probed_while_discovery_is_off(monkeypatch):
    monkeypatch.setattr(discover, 'search_path', lambda warn=None: pytest.fail('probed with discovery off'))
    assert cap.toolchains_of() == {}


def test_a_version_command_that_fails_or_hangs_records_nothing(tmp_path):
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    (bin_ / 'node').write_text('#!/bin/sh\nexit 3\n')
    (bin_ / 'node').chmod(0o755)
    (bin_ / 'go').write_text('#!/bin/sh\necho no version here\n')
    (bin_ / 'go').chmod(0o755)
    assert cap.toolchains_of(str(bin_)) == {}


def runner(answers):
    def run(argv):
        key = ' '.join(argv)
        if key not in answers:
            return None
        return answers[key]
    return run


def test_hardware_on_macos_reads_sysctl(tmp_path):
    run = runner({'sysctl -n hw.ncpu': '10\n', 'sysctl -n hw.perflevel0.physicalcpu': '4\n',
                  'sysctl -n hw.memsize': str(32 * 1024 ** 3) + '\n'})
    hw = cap.hardware_of(run=run, platform='darwin', disk_path=tmp_path, cpu_count=lambda: 99,
                        disk_usage=lambda p: (0, 0, int(59.04 * 1024 ** 3)))
    assert hw == {'cores': 10, 'perf_cores': 4, 'mem_gb': 32.0, 'disk_free_gb': 59.0}


def test_hardware_without_performance_levels_counts_every_core(tmp_path):
    run = runner({'sysctl -n hw.ncpu': '8\n', 'sysctl -n hw.memsize': str(16 * 1024 ** 3) + '\n'})
    hw = cap.hardware_of(run=run, platform='darwin', disk_path=tmp_path, disk_usage=lambda p: (0, 0, 0))
    assert hw['cores'] == 8 and hw['perf_cores'] == 8 and hw['disk_free_gb'] == 0.0


def test_hardware_on_linux_reads_proc_meminfo(tmp_path):
    meminfo = tmp_path / 'meminfo'
    meminfo.write_text('MemTotal:       65843200 kB\nMemFree:  1 kB\n')
    hw = cap.hardware_of(run=runner({}), platform='linux', disk_path=tmp_path, cpu_count=lambda: 24,
                        disk_usage=lambda p: (0, 0, 40 * 1024 ** 3), meminfo=meminfo)
    assert hw == {'cores': 24, 'perf_cores': 24, 'mem_gb': 62.8, 'disk_free_gb': 40.0}


def test_disk_free_is_measured_on_the_given_path(tmp_path):
    seen = []
    cap.hardware_of(run=runner({'sysctl -n hw.ncpu': '4', 'sysctl -n hw.memsize': '0'}), platform='darwin',
                   disk_path=tmp_path, disk_usage=lambda p: seen.append(p) or (0, 0, 0))
    assert seen == [tmp_path]


def test_a_battery_means_a_laptop(tmp_path):
    assert cap.has_battery(run=runner({'pmset -g batt': "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t80%"}),
                          platform='darwin') is True
    assert cap.has_battery(run=runner({'pmset -g batt': "Now drawing from 'AC Power'\n"}), platform='darwin') is False
    supply = tmp_path / 'power_supply'
    (supply / 'AC').mkdir(parents=True)
    assert cap.has_battery(platform='linux', power_supply=supply) is False
    (supply / 'BAT0').mkdir()
    assert cap.has_battery(platform='linux', power_supply=supply) is True


def test_machine_facts_are_the_flat_node_status_shape(inv, measured):
    drop_always_on(inv)
    measured['toolchains'] = {'rust': '1.83.0'}
    refresh()
    assert cap.machine_facts(fs.machine('field')) == dict(HARDWARE, always_on=True, toolchains={'rust': '1.83.0'})


# -- validation -------------------------------------------------------------------------------------------

GOOD = {'source': 'self', 'hardware': dict(HARDWARE), 'always_on': False, 'toolchains': {'rust': '1.83.0'}}


def test_the_full_capability_entry_is_valid():
    assert fs.validate_machine('field', GOOD) == []


@pytest.mark.parametrize('change, word', [
    ({'hardware': [1]}, 'hardware'),
    ({'hardware': dict(HARDWARE, cores=0)}, 'hardware'),
    ({'hardware': dict(HARDWARE, cores=True)}, 'hardware'),
    ({'hardware': dict(HARDWARE, perf_cores=17)}, 'perf_cores'),
    ({'hardware': dict(HARDWARE, mem_gb=-1)}, 'hardware'),
    ({'hardware': dict(HARDWARE, disk_free_gb='lots')}, 'hardware'),
    ({'always_on': 'yes'}, 'always_on'),
    ({'toolchains': ['rust']}, 'toolchains'),
    ({'toolchains': {'rust': 1.83}}, 'toolchains'),
    ({'toolchains': {'': '1'}}, 'toolchains'),
])
def test_bad_capability_fields_are_named(change, word):
    problems = fs.validate_machine('field', dict(GOOD, **change))
    assert problems and all(word in p for p in problems), problems


def test_the_documented_example_carries_the_capability_fields():
    data = yaml.safe_load(doc_example())
    for name, entry in data['machines'].items():
        assert set(entry['hardware']) == {'cores', 'perf_cores', 'mem_gb', 'disk_free_gb'}, name
        assert isinstance(entry['always_on'], bool) and isinstance(entry['toolchains'], dict), name
    assert fs.validate(data) == []
