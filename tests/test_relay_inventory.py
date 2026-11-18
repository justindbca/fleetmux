"""R20: after joining a job every machine's inventory holds the latest measured block of the others.

Each machine has its own temporary inventory file (what $FLEETMUX_SUPERPOWERS points at); the peers' blocks come
from their node records (`machine`), written with the inventory's own `import_copy` path."""
import yaml

from fleetmux import fleet_superpowers as fs
from fleetmux.relay import inventory
from relay_flow_support import MACHINES, node_rec


def inv(tmp_path, name, extra=''):
    p = tmp_path / name / 'fleet-superpowers.yaml'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('machines:\n  %s:\n    source: self\n    hostname: %s.local\n%s' % (name, name, extra))
    return p


def nodes():
    return {n: node_rec(n) for n in MACHINES}


def test_each_machine_holds_the_other_two(tmp_path):
    files = {n: inv(tmp_path, n) for n in MACHINES}
    for n in MACHINES:
        assert sorted(inventory.sync_peers(nodes(), n, path=files[n])) == sorted(set(MACHINES) - {n})
    for n in MACHINES:
        ms = fs.read(files[n])['machines']
        assert set(ms) == set(MACHINES)
        assert ms[n] == {'source': 'self', 'hostname': '%s.local' % n}                 # own entry untouched
        for peer in set(MACHINES) - {n}:
            m = MACHINES[peer]
            assert ms[peer]['source'] == 'copy' and ms[peer]['copied_at']
            assert ms[peer]['hardware'] == {k: m[k] for k in ('cores', 'perf_cores', 'mem_gb', 'disk_free_gb')}
            assert ms[peer]['always_on'] is m['always_on'] and ms[peer]['toolchains'] == m['toolchains']
        assert fs.validate(fs.read(files[n])) == []


def test_existing_copy_keeps_its_other_fields_and_updates_measures(tmp_path):
    path = inv(tmp_path, 'laptop', extra=('  desktop:\n    source: copy\n    copied_at: "2026-01-01T00:00:00+00:00"\n'
                                          '    hostname: desktop.lan\n    reach:\n    - {via: lan, ssh: desktop}\n'
                                          '    hardware: {cores: 1, perf_cores: 1, mem_gb: 1, disk_free_gb: 1}\n'))
    inventory.sync_peers({'desktop': node_rec('desktop')}, 'laptop', path=path)
    d = fs.read(path)['machines']['desktop']
    assert d['hostname'] == 'desktop.lan' and d['reach'] == [{'via': 'lan', 'ssh': 'desktop'}]
    assert d['hardware']['cores'] == 16 and d['hardware']['disk_free_gb'] == 132


def test_unchanged_measures_write_nothing(tmp_path):
    path = inv(tmp_path, 'laptop')
    assert inventory.sync_peers(nodes(), 'laptop', path=path)
    text = path.read_text()
    assert inventory.sync_peers(nodes(), 'laptop', path=path) == []
    assert path.read_text() == text


def test_a_record_naming_this_machines_self_entry_is_skipped(tmp_path):
    """A peer record must never overwrite this machine's own (`source: self`) entry, whatever it is named."""
    path = inv(tmp_path, 'laptop')
    recs = {'laptop': node_rec('laptop'), 'desktop': node_rec('desktop', machine={})}
    assert inventory.sync_peers(recs, 'other-name', path=path) == []
    assert yaml.safe_load(path.read_text())['machines'] == {'laptop': {'source': 'self', 'hostname': 'laptop.local'}}
