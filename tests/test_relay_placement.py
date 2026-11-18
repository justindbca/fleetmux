"""Who takes a segment: toolchain match, disk floors, caps, `prefer`, capability order, staggered claim
(R1, R14, R15, R16; 04-design sections 2 and 10)."""
import copy

import pytest

from fleetmux.relay import placement, toolchain
from relay_flow_support import MACHINES, node_rec, params


def rec(seg, weight='heavy', needs=(), prefer=None, state='pending', owner=None):
    return {'id': seg, 'weight': weight, 'needs': list(needs), 'prefer': prefer, 'state': state, 'owner': owner,
            'depends_on': [], 'epoch': 0}


def nodes(**over):
    out = {n: node_rec(n) for n in MACHINES}
    for n, m in over.items():
        out[n]['machine'].update(m)
    return out


ALL_ONLINE = {n: 'online' for n in MACHINES}


def ranked(task, table=None, ns=None, judged=None, p=None):
    p = p or params()
    cands = placement.candidates(ns or nodes(), judged or ALL_ONLINE, table or {})
    return placement.rank(task, cands, p)


# ---------------------------------------------------------------- toolchains (R16)

@pytest.mark.parametrize('have,need,ok', [
    ({'rust': '1.83'}, 'rust', True),
    ({}, 'rust', False),
    ({'node': '18.2'}, 'node>=20', False),
    ({'node': '22.1'}, 'node>=20', True),
    ({'node': '20'}, 'node>=20', True),
    ({'node': '20.0.1'}, 'node>20', True),
    ({'python': '3.12.4'}, 'python==3.12', False),
    ({'go': 'go1.22.3'}, 'go>=1.21', True),
])
def test_toolchain_requirement(have, need, ok):
    assert (toolchain.missing(have, [need]) == []) is ok


def test_bad_requirement_rejected():
    with pytest.raises(ValueError):
        toolchain.parse_need('rust>=')


def test_toolchain_missing_not_eligible():
    assert 'desktop' not in ranked(rec('M3', needs=['rust']))
    assert ranked(rec('M3', needs=['rust'])) == ['laptop']


def test_toolchain_version_bound():
    assert 'desktop' not in ranked(rec('X', weight='light', needs=['node>=20']))
    assert 'laptop' in ranked(rec('X', weight='light', needs=['node>=20']))


def test_unplaceable_reason_names_missing_toolchain():
    ns = nodes()
    del ns['laptop']
    cands = placement.candidates(ns, ALL_ONLINE, {})
    task = rec('M3', needs=['rust'])
    assert placement.rank(task, cands, params()) == []
    assert placement.unplaceable_reason(task, cands, params()) == 'toolchain: rust'


# ---------------------------------------------------------------- capability, disk, caps (R14, R15)

def test_heavy_goes_to_strongest_without_prefer():
    assert ranked(rec('M4'))[0] == 'desktop'


def test_light_goes_to_least_busy():
    table = {'A': rec('A', state='working', owner='desktop'), 'B': rec('B', weight='light', state='working',
                                                                         owner='laptop')}
    assert ranked(rec('X', weight='light'), table=table)[0] == 'server'


def test_disk_floor_heavy_and_any():
    ns = nodes(desktop={'disk_free_gb': 30})          # below heavy floor 40, above any floor 15
    assert 'desktop' not in ranked(rec('H'), ns=ns)
    assert 'desktop' in ranked(rec('L', weight='light'), ns=ns)
    ns = nodes(desktop={'disk_free_gb': 10})          # below any floor
    assert 'desktop' not in ranked(rec('L', weight='light'), ns=ns)
    cands = placement.candidates(nodes(desktop={'disk_free_gb': 10}, laptop={'disk_free_gb': 10},
                                       server={'disk_free_gb': 10}), ALL_ONLINE, {})
    assert placement.unplaceable_reason(rec('L', weight='light'), cands, params()) == 'disk'


def test_caps_tasks_and_heavy():
    table = {'A': rec('A', state='working', owner='desktop')}
    assert 'desktop' not in ranked(rec('H'), table=table)                 # max_heavy_per_node 1
    assert 'desktop' in ranked(rec('L', weight='light'), table=table)
    table['B'] = rec('B', weight='light', state='working', owner='desktop')
    assert 'desktop' not in ranked(rec('L', weight='light'), table=table)   # max_tasks_per_node 2


def test_thresholds_read_from_params():
    p = params(disk_floor_heavy_gb=200)
    assert ranked(rec('H'), p=p) == []
    p = params(score={'cores': 0.0, 'mem_gb': 0.0, 'disk_free_gb': -1.0})
    assert ranked(rec('H', needs=['python']), p=p)[0] == 'server'        # least disk now scores highest


# ---------------------------------------------------------------- prefer

def test_prefer_first_when_eligible():
    assert ranked(rec('M0', weight='light', prefer='server'))[0] == 'server'
    assert ranked(rec('M1', prefer='laptop'))[0] == 'laptop'


def test_prefer_ineligible_falls_back():
    order = ranked(rec('M1', needs=['python'], prefer='server'), ns=nodes(server={'disk_free_gb': 20}))
    assert order[0] == 'desktop' and 'server' not in order


# ---------------------------------------------------------------- who counts as a candidate (R11)

def test_only_online_nodes_are_candidates():
    ns = nodes()
    ns['server']['state'] = 'rejoining'
    judged = dict(ALL_ONLINE, laptop='lost')
    assert ranked(rec('X', weight='light'), ns=ns, judged=judged) == ['desktop']


# ---------------------------------------------------------------- staggered claim (04-design 2.4.3)

def test_stagger_seconds():
    p = params(claim_stagger_s=20)
    assert placement.stagger_s(['a', 'b', 'c'], 'a', p) == 0
    assert placement.stagger_s(['a', 'b', 'c'], 'c', p) == 40
    assert placement.stagger_s(['a', 'b'], 'z', p) is None


def test_claim_queue_waits_for_rank():
    q = placement.ClaimQueue()
    assert q.due('M3', 'v1', 0, now=100.0) is True          # rank 1: at once
    assert q.due('M4', 'v1', 20, now=100.0) is False        # rank 2: 20 s later
    assert q.due('M4', 'v1', 20, now=119.0) is False
    assert q.due('M4', 'v1', 20, now=120.0) is True


def test_claim_queue_restarts_when_record_changes():
    q = placement.ClaimQueue()
    assert q.due('M4', 'v1', 20, now=100.0) is False
    assert q.due('M4', 'v2', 20, now=121.0) is False        # a new version of the record: wait again
    assert q.due('M4', 'v2', 20, now=141.0) is True
    q.forget('M4')
    assert q.due('M4', 'v2', 20, now=142.0) is False


def test_score_hook_replaces_the_weights():
    """The capability score comes from the state layer's scoring when the caller passes it in."""
    cands = placement.candidates(nodes(), ALL_ONLINE, {})
    by_disk_low = lambda machine, p: -machine['disk_free_gb']          # noqa: E731
    assert placement.rank(rec('H', needs=['python']), cands, params(), score=by_disk_low)[0] == 'server'
    assert placement.rank(rec('H', needs=['python']), cands, params())[0] == 'desktop'
