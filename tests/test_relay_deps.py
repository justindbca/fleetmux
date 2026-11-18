"""Dependency advance and the merge segment (R2, R10; 04-design section 7)."""
from fleetmux.relay import deps


def rec(seg, state='pending', depends_on=(), delivered=None, base='b0'):
    return {'id': seg, 'state': state, 'depends_on': list(depends_on), 'base': base, 'merge_in': [],
            'delivered': delivered, 'seq': 3, 'owner': None, 'epoch': 0}


def delivered(seg, commit):
    return rec(seg, state='delivered', delivered={'branch': 'relay/demo/%s/e1-x' % seg, 'commit': commit,
                                                  'gate': 'pass', 'review': 'PASS'})


def test_blocked_not_claimable():
    assert deps.claimable(rec('M1', state='blocked', depends_on=['M3'])) is False
    assert deps.claimable(rec('M0')) is True
    assert deps.claimable(rec('M0', state='reclaimed')) is True
    assert deps.claimable(rec('M0', state='working')) is False


def test_delivery_releases_dependent():
    table = {'M3': delivered('M3', 'c3'), 'M1': rec('M1', state='blocked', depends_on=['M3'])}
    out = deps.advance(table)
    assert set(out) == {'M1'}
    assert out['M1']['state'] == 'pending' and out['M1']['base'] == 'c3' and out['M1']['merge_in'] == []
    assert out['M1']['seq'] == 4
    assert table['M1']['state'] == 'blocked'           # input untouched


def test_partial_deps_stay_blocked():
    table = {'A': delivered('A', 'ca'), 'B': rec('B', state='working'),
             'C': rec('C', state='blocked', depends_on=['A', 'B'])}
    assert deps.advance(table) == {}


def test_multi_dep_base_and_merge_in():
    table = {'A': delivered('A', 'ca'), 'B': delivered('B', 'cb'), 'D': delivered('D', 'cd'),
             'C': rec('C', state='blocked', depends_on=['B', 'A', 'D'])}
    out = deps.advance(table)
    assert out['C']['base'] == 'cb' and out['C']['merge_in'] == ['ca', 'cd']


def test_parked_or_missing_dependency_keeps_blocked():
    table = {'A': rec('A', state='parked'), 'C': rec('C', state='blocked', depends_on=['A', 'Z'])}
    assert deps.advance(table) == {}


def test_merge_segment_released_after_all_delivered():
    table = {'M0': delivered('M0', 'c0'), 'M1': delivered('M1', 'c1'), 'M3': delivered('M3', 'c3'),
             'merge': rec('merge', state='blocked', depends_on=['M0', 'M1', 'M3'])}
    out = deps.advance(table)
    assert out['merge']['state'] == 'pending'
    assert out['merge']['base'] == 'c0' and out['merge']['merge_in'] == ['c1', 'c3']
    assert deps.job_done(table) is False
    table['merge'] = delivered('merge', 'cm')
    assert deps.job_done(table) is True


def test_blocking_lists_open_dependencies():
    table = {'A': delivered('A', 'ca'), 'B': rec('B', state='working'),
             'C': rec('C', state='blocked', depends_on=['A', 'B'])}
    assert deps.waiting_on(table, 'C') == ['B']
