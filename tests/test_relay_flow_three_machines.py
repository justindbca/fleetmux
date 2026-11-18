"""The relay flow across machines on port-1's `relay_kit.three_machines` fixture: one real local bare repo as the
hub, three temporary machine directories (laptop, desktop, server), each with its own clone, cache, state dir and
fake clock. The job's files are committed to the hub from the laptop's clone; every machine then runs `flow.Flow`
on the state layer (R2, R10, R11, R12)."""
import stat

import pytest

from fleetmux.relay import coordinator, jobfile
from relay_kit import git, isolated_git, remote_refs, three_machines  # noqa: F401
from test_relay_flow_git import Node, gate_delivers, lose_laptop, project_files

JOB = 'j'                                   # the job three_machines' Hubs are built for


@pytest.fixture
def w(three_machines):
    hub, _, ms = three_machines
    clone = ms['laptop'].clone
    for path, content in project_files(JOB).items():
        mode, text = content if isinstance(content, tuple) else (None, content)
        f = clone / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        if mode == 'exec':
            f.chmod(f.stat().st_mode | stat.S_IXUSR)
    git('add', '-A', cwd=clone)
    git('commit', '-q', '-m', 'relay job files', cwd=clone)
    git('push', '-q', 'origin', 'HEAD:main', cwd=clone)
    base = git('rev-parse', 'HEAD', cwd=clone)
    nodes = {n: Node(m, job=JOB) for n, m in ms.items()}
    for n in nodes.values():
        n.step()                                            # joined and running before the job exists
    doc = jobfile.load_job(clone, JOB)
    for seg, rec in coordinator.job_records(doc, ms['laptop'].settings, base=base).items():
        assert nodes['laptop'].store.cas_task(seg, None, rec) is True
    return {'hub': hub, 'base': base, 'n': nodes}


def rec(w, seg):
    return w['n']['server'].store.tasks()[seg][1]


def step_all(w, *names, dt=1.0, times=1):
    for _ in range(times):
        for name in names or ('laptop', 'desktop', 'server'):
            w['n'][name].step(dt)


def test_r2_dependent_waits_for_its_dependency_then_starts_from_its_commit(w):
    """R2: M1 depends on M3. While M3 works, no machine takes M1; once M3 is delivered through the gate, M1 is
    released with M3's commit as base and its preferred machine starts it with M3's work in the tree."""
    step_all(w, times=2)
    assert rec(w, 'M1')['state'] == 'blocked'
    assert all('M1' not in n.flow.owned for n in w['n'].values())
    sha = gate_delivers(w['n']['laptop'], 'M3', 1, 'src/core.rs', 'core\n')
    m1 = rec(w, 'M1')
    assert m1['state'] == 'pending' and m1['base'] == sha
    step_all(w, 'desktop')
    m1 = rec(w, 'M1')
    assert (m1['owner'], m1['state']) == ('desktop', 'working')
    assert (w['n']['desktop'].wt('M1', 1) / 'src' / 'core.rs').read_text() == 'core\n'


def test_r10_merge_segment_after_every_delivery(w):
    """R10: after M3, M0 and M1 are delivered (each pushed to its deploy branch on the hub), the merge segment is
    released, claimed once, and its worktree can merge every delivered commit."""
    step_all(w)
    lap, desk, srv = w['n']['laptop'], w['n']['desktop'], w['n']['server']
    gate_delivers(lap, 'M3', 1, 'src/core.rs', 'core\n')
    step_all(w, 'desktop')
    gate_delivers(srv, 'M0', 1, 'src/client.py', 'client\n')
    gate_delivers(desk, 'M1', 1, 'src/server.py', 'server\n')
    refs = remote_refs(w['hub'])
    assert {'refs/heads/relay/j/%s/delivered' % s for s in ('M0', 'M1', 'M3')} <= set(refs)
    step_all(w, times=3)
    merge = rec(w, 'merge')
    assert merge['state'] == 'working'
    holder = w['n'][merge['owner']]
    assert [s for _, s, _ in holder.executor.started].count('merge') == 1
    wt = holder.wt('merge', merge['epoch'])
    git('merge', '-q', '--no-edit', *merge['merge_in'], cwd=wt)
    assert all((wt / p).is_file() for p in ('src/core.rs', 'src/client.py', 'src/server.py'))


def test_r11_a_returning_machine_reads_first_and_settles(w):
    """R11: laptop is lost, M3 is taken over by desktop. When laptop wakes it is rejoining: it reads the table
    before writing any task, stops its old sub-fleet, keeps its tree, and only after settle_s is online and idle."""
    step_all(w)
    lap = w['n']['laptop']
    lose_laptop(w)
    assert rec(w, 'M3')['owner'] == 'desktop'
    before = {k: v for k, v in remote_refs(w['hub']).items() if '/tasks/' in k}
    lap.m.clock.jump_wall(3600)
    lap.step()
    assert lap.flow.state == 'rejoining' and lap.executor.stopped and 'M3' not in lap.flow.owned
    assert {k: v for k, v in remote_refs(w['hub']).items() if '/tasks/' in k} == before   # no task written
    assert lap.wt('M3', 1).is_dir()
    settle = lap.m.settings.hot('settle_s')
    for _ in range(5):
        step_all(w, dt=settle / 4)
    node = w['n']['server'].store.nodes()['laptop']
    assert node['state'] == 'online' and node['idle'] is True


def test_r12_returning_owner_salvages_and_the_new_owner_is_told(w):
    """R12: work laptop wrote while cut off goes to the salvage ref on the hub (create-only); desktop, the new
    owner, gets SALVAGE.md in its worktree and one relay.salvage notice."""
    step_all(w)
    lap, desk = w['n']['laptop'], w['n']['desktop']
    lose_laptop(w)
    (lap.wt('M3', 1) / 'src' / 'late.py').write_text('late = True\n')
    lap.m.clock.jump_wall(3600)
    lap.step()
    ref = 'refs/relay/j/salvage/M3/laptop-e1'
    salvage = remote_refs(w['hub'])[ref]
    assert git('show', '%s:src/late.py' % salvage, cwd=w['hub']) == 'late = True'
    step_all(w, 'desktop')
    note = desk.wt('M3', 2) / '.relay/j/M3/notes/SALVAGE.md'
    assert note.is_file() and 'salvage/M3/laptop-e1' in note.read_text()
    assert len(desk.kinds('relay.salvage')) == 1
    assert (lap.wt('M3', 1) / 'src' / 'late.py').is_file()                 # R26: nothing deleted
