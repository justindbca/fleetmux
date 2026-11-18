"""The coordinator role: a light task record that is no development work, prefers always-on machines and can be
taken over like a segment; configurable (R13, R17; 04-design section 9)."""
from fleetmux.relay import coordinator, jobfile
from relay_flow_support import MACHINES, node_rec, params, write_job

ALL_ONLINE = {n: 'online' for n in MACHINES}


def nodes():
    return {n: node_rec(n) for n in MACHINES}


def test_record_is_light_role_without_toolchain():
    rec = coordinator.record('demo', coordinator.conf(params()), base='abc')
    assert rec['id'] == 'coordinator' and rec['weight'] == 'light' and rec['needs'] == []
    assert rec['state'] == 'pending' and rec['dir'] == '.relay/demo/coordinator'
    assert rec['owner'] is None and rec['epoch'] == 0


def test_job_records_include_coordinator_when_enabled(tmp_path):
    write_job(tmp_path)
    doc = jobfile.load_job(tmp_path, 'demo')
    recs = coordinator.job_records(doc, params(), base='abc')
    assert 'coordinator' in recs and 'merge' in recs
    off = params(coordinator={'enabled': False, 'prefer': '', 'takeover': True})
    assert 'coordinator' not in coordinator.job_records(doc, off, base='abc')


def test_coordinator_prefers_always_on():
    """R17: laptop (not always on) has a higher score than server, yet the role goes to an always-on machine."""
    rec = coordinator.record('demo', coordinator.conf(params()), base='abc')
    order = coordinator.rank(rec, nodes(), ALL_ONLINE, params())
    assert order[0] == 'desktop' and order[-1] == 'laptop'
    judged = dict(ALL_ONLINE, desktop='lost')
    assert coordinator.rank(rec, nodes(), judged, params())[0] == 'server'


def test_coordinator_fixed_by_prefer():
    p = params(coordinator={'enabled': True, 'prefer': 'server', 'takeover': False})
    rec = coordinator.record('demo', coordinator.conf(p), base='abc')
    assert rec['prefer'] == 'server' and coordinator.conf(p)['takeover'] is False
    assert coordinator.rank(rec, nodes(), ALL_ONLINE, p)[0] == 'server'


def test_takeable_only_when_owner_gone_and_allowed():
    c = coordinator.conf(params())
    rec = dict(coordinator.record('demo', c, base='abc'), state='working', owner='desktop', epoch=1)
    assert coordinator.takeable(rec, {'desktop': 'online'}, c) is False
    assert coordinator.takeable(rec, {'desktop': 'lost'}, c) is True
    assert coordinator.takeable(rec, {'desktop': 'leaving'}, c) is True
    assert coordinator.takeable(rec, {'desktop': 'lost'}, dict(c, takeover=False)) is False


def test_conf_reads_the_relay_block_and_fills_defaults(tmp_path):
    """Read from the settings object's block (a cold key), defaults for what it leaves out."""
    class Settings:
        block = {'coordinator': {'prefer': 'server'}}
    assert coordinator.conf(Settings()) == {'enabled': True, 'prefer': 'server', 'takeover': True}
    assert coordinator.conf(type('S', (), {'block': {}})()) == coordinator.DEFAULTS


def test_role_is_not_development_work():
    assert coordinator.is_role('coordinator') and not coordinator.is_role('M1')


def test_holder_of_role():
    table = {'coordinator': {'id': 'coordinator', 'state': 'working', 'owner': 'server'}}
    assert coordinator.holder(table) == 'server'
    assert coordinator.holder({}) is None
