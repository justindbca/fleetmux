"""The relay flow on a real hub: one local bare repo, three temporary directories as three machines (R2, R4-R13).

Every machine runs `flow.Flow` on the state layer through `hubstore` (Hub, records, snapshots, materialize,
Observer, Beats). Only the executor is a fake: no fleet is started. Each machine has its own fake clock."""
import subprocess
import sys

import pytest

from fleetmux import fleet_superpowers as fs
from fleetmux.relay import coordinator, flow, hubstore, inventory, jobfile, launch
from fleetmux.relay.beats import Beats
from fleetmux.relay.liveness import Observer
from relay_kit import git, isolated_git, machine_info, machines, remote_refs, repo_fingerprint, tree_digest  # noqa: F401
from relay_flow_support import SPEC_YAML

JOB = 'demo'
JOB_YAML = """\
job: demo
nodes: [laptop, desktop, server]
tasks:
  M0: {title: "client side", weight: light, needs: [], prefer: server}
  M3: {title: "protocol core", weight: heavy, needs: [rust], prefer: laptop}
  M1: {title: "server side", weight: heavy, needs: [rust], depends_on: [M3], prefer: desktop}
merge:
  depends_on: [M0, M1, M3]
"""
INFO = {'laptop': machine_info(10, 32, 59, always_on=False, toolchains={'python': '3.12', 'rust': '1.83'}),
        'desktop': machine_info(16, 128, 132, toolchains={'python': '3.12', 'rust': '1.83'}),
        'server': machine_info(10, 16, 48, toolchains={'python': '3.12'})}


def project_files(job=JOB):
    files = {'README.md': 'demo\n', 'src/app.py': 'print(1)\n', '.gitignore': '*.ignored\n',
             '.relay/%s/job.yaml' % job: JOB_YAML.replace('job: demo\n', 'job: %s\n' % job, 1)}
    for seg in ('M0', 'M1', 'M3', 'merge'):
        d = '.relay/%s/%s/' % (job, seg)
        files[d + 'job.spec.yaml'] = SPEC_YAML.format(seg=seg).replace('.relay/demo/', '.relay/%s/' % job)
        files[d + 'brief.md'] = '# %s\n' % seg
        files[d + 'gate.sh'] = ('exec', '#!/bin/bash\n"$RELAY_PYTHON" -c "print(1)"\n')
    return files


class Node:
    def __init__(self, m, job=JOB):
        self.m = m
        self.notices = []
        self.store = hubstore.HubStore(m.hub, Beats(m.hub, node=m.name, fleet={'machine': m.name}, clock=m.clock,
                                                    settings=m.settings))
        self.ws = hubstore.GitWorkspace(m.hub, job=job, root=m.worktrees, state_dir=m.state_dir,
                                        settings=m.settings, run=subprocess.run)
        self.observer = Observer(m.settings, m.name, m.clock, None, job=job)
        self.executor = launch.FakeExecutor()
        self.inventory = m.root / 'fleet-superpowers.yaml'
        self.inventory.write_text('machines:\n  %s:\n    source: self\n' % m.name)
        self.flow = flow.Flow(m.name, job, store=self.store, workspace=self.ws, executor=self.executor,
                              start_spec=hubstore.start_spec(self.ws, job, relay_python=sys.executable),
                              judge=hubstore.observer_judge(self.observer, self.store), params=m.settings,
                              clock=m.clock, notify=lambda kind, text: self.notices.append((kind, text)),
                              machine=lambda: INFO[m.name], settle_on_start=False,
                              peer_sync=lambda nodes: inventory.sync_peers(nodes, m.name, path=self.inventory))

    def step(self, dt=1.0):
        self.flow.step()
        self.m.clock.advance(dt)

    def wt(self, seg, epoch):
        return self.ws.path(seg, epoch)

    def kinds(self, kind):
        return [t for k, t in self.notices if k == kind]


@pytest.fixture
def world(tmp_path):
    hub, base, ms = machines(tmp_path, job=JOB, files=project_files())
    nodes = {n: Node(m) for n, m in ms.items()}
    for n in nodes.values():
        n.step()                                            # joined and running before the job exists
    doc = jobfile.load_job(ms['laptop'].clone, JOB)
    store = nodes['laptop'].store
    for seg, rec in coordinator.job_records(doc, ms['laptop'].settings, base=base).items():
        assert store.cas_task(seg, None, rec) is True
    return {'hub': hub, 'base': base, 'n': nodes}


def rec(world, seg):
    store = world['n']['server'].store
    return store.tasks()[seg][1]


def step_all(world, *names, dt=1.0, times=1):
    for _ in range(times):
        for name in names or ('laptop', 'desktop', 'server'):
            world['n'][name].step(dt)


def test_segments_start_from_repo_files_on_the_hub(world):
    """R1, R4, R9: each machine claims its segment, builds the worktree from the hub alone and starts the
    sub-fleet with a spec rendered from the in-repo job.spec.yaml; the dependent segment stays blocked."""
    step_all(world)
    assert (rec(world, 'M3')['owner'], rec(world, 'M0')['owner'], rec(world, 'coordinator')['owner']) == (
        'laptop', 'server', 'desktop')
    assert rec(world, 'M1')['state'] == 'blocked'
    lap = world['n']['laptop']
    wt = lap.wt('M3', 1)
    assert git('rev-parse', '--abbrev-ref', 'HEAD', cwd=wt) == 'relay/demo/M3/e1-laptop'
    assert git('rev-parse', 'HEAD', cwd=wt) == world['base']
    assert git('rev-parse', 'relay/demo/M3/delivered', cwd=wt) == world['base']         # the gate needs it
    (job, seg, spec), = lap.executor.started
    assert (job, seg, spec['name']) == ('demo', 'M3', 'relay-demo-M3-e1')
    assert spec['deliver']['repo'] == str(wt) and spec['deliver']['test_cmd'][-1] == '.relay/demo/M3/gate.sh'
    assert spec['deliver']['feature_branch'] == 'relay/demo/M3/e1-laptop'


def test_delivery_releases_dependent_from_the_delivered_commit(world):
    """R2, R10: the gate's queue row delivers M3; M1 is released from that commit and taken by its preferred
    machine, whose new worktree holds M3's work."""
    step_all(world)
    lap = world['n']['laptop']
    wt = lap.wt('M3', 1)
    (wt / 'src' / 'core.rs').write_text('fn main() {}\n')
    git('add', '-A', cwd=wt)
    git('commit', '-q', '-m', 'feat: core', cwd=wt)
    git('push', '-q', 'origin', 'relay/demo/M3/e1-laptop', cwd=wt)
    sha = git('rev-parse', 'HEAD', cwd=wt)
    lap.ws.queue_path('M3', 1).parent.mkdir(parents=True, exist_ok=True)
    lap.ws.queue_path('M3', 1).write_text('{"status": "pending_approval", "sha": "%s", '
                                          '"deploy_branch": "relay/demo/M3/delivered"}\n' % sha)
    lap.step()
    m3 = rec(world, 'M3')
    assert m3['state'] == 'delivered' and m3['delivered']['commit'] == sha
    m1 = rec(world, 'M1')
    assert m1['state'] == 'pending' and m1['base'] == sha
    step_all(world, 'desktop')
    m1 = rec(world, 'M1')
    assert (m1['owner'], m1['state']) == ('desktop', 'working')
    assert (world['n']['desktop'].wt('M1', 1) / 'src' / 'core.rs').read_text() == 'fn main() {}\n'


def test_auto_snapshot_updates_record_and_leaves_worker_untouched(world):
    """R5: the automatic snapshot round checkpoints the record and pushes `snap/<seg>` without touching the
    worker's index, HEAD or files."""
    step_all(world)
    lap = world['n']['laptop']
    wt = lap.wt('M3', 1)
    (wt / 'src' / 'wip.py').write_text('x = 1\n')
    before = repo_fingerprint(wt)
    lap.step(dt=lap.m.settings.hot('snapshot_s'))
    lap.step()
    assert repo_fingerprint(wt) == before
    cp = rec(world, 'M3')['checkpoint']
    assert cp['kind'] == 'auto' and cp['commit'] == world['base'] and cp['snap']
    assert remote_refs(world['hub'])['refs/relay/demo/snap/M3'] == cp['snap']


def lose_laptop(world):
    """laptop stops; desktop and server keep beating until laptop is judged lost and M3 is taken over."""
    lost = world['n']['desktop'].m.settings.hot('lost_after_s')
    step_all(world, 'desktop', 'server', dt=lost / 3, times=4)
    step_all(world, 'desktop', 'server')


def test_takeover_from_last_checkpoint_is_byte_identical(world):
    """R7, R8, R9: laptop vanishes; desktop takes M3 over (epoch 2, takeover:lost) with a worktree byte-identical
    to laptop's last snapshot, and gets TAKEOVER.md; M0 on server is untouched."""
    step_all(world)
    lap = world['n']['laptop']
    wt = lap.wt('M3', 1)
    (wt / 'src' / 'wip.py').write_text('x = 1\n')
    assert lap.flow.checkpoint('M3', kind='phase', phase='design')
    lose_laptop(world)
    m3 = rec(world, 'M3')
    assert (m3['owner'], m3['epoch'], m3['owners'][-1]['why']) == ('desktop', 2, 'takeover:lost')
    new = world['n']['desktop'].wt('M3', 2)
    notes = ('.relay/demo/M3/TAKEOVER.md', '.relay/demo/M3/takeover.json')       # written for the new owner
    assert {k: v for k, v in tree_digest(new).items() if k not in notes} == tree_digest(wt)
    assert (new / '.relay/demo/M3/TAKEOVER.md').is_file()
    import json
    prev = json.loads((new / '.relay/demo/M3/takeover.json').read_text())['previous']
    assert (prev['node'], prev['judged']) == ('laptop', 'lost')
    started = world['n']['desktop'].executor.started[-1]
    assert started[1] == 'M3' and started[2]['goal'].startswith('First read .relay/demo/M3/TAKEOVER.md')
    assert rec(world, 'M0')['owner'] == 'server' and rec(world, 'M0')['epoch'] == 1


def test_return_salvages_and_settles(world):
    """R11, R12, R13: laptop wakes, reads the table first, stops M3 and pushes its extra work to the salvage ref;
    desktop gets SALVAGE.md; after settle_s laptop is online and idle, and the coordinator is told."""
    step_all(world)
    lap = world['n']['laptop']
    wt = lap.wt('M3', 1)
    lose_laptop(world)
    (wt / 'src' / 'late.py').write_text('late = True\n')               # written while cut off
    lap.m.clock.jump_wall(3600)
    lap.step()
    assert lap.flow.state == 'rejoining'
    assert lap.executor.stopped and 'M3' not in lap.flow.owned
    assert (wt / 'src' / 'late.py').read_text() == 'late = True\n'          # R12 / R26: the local tree is kept
    ref = 'refs/relay/demo/salvage/M3/laptop-e1'
    salvage = remote_refs(world['hub'])[ref]
    assert git('show', '%s:src/late.py' % salvage, cwd=world['hub']) == 'late = True'
    assert rec(world, 'M3')['owner'] == 'desktop'
    step_all(world, 'desktop')
    note = world['n']['desktop'].wt('M3', 2) / '.relay/demo/M3/notes/SALVAGE.md'
    assert note.is_file() and 'salvage/M3/laptop-e1' in note.read_text()
    assert world['n']['desktop'].kinds('relay.salvage')
    settle = lap.m.settings.hot('settle_s')
    for _ in range(5):
        step_all(world, dt=settle / 4)
    node = world['n']['server'].store.nodes()['laptop']
    assert node['state'] == 'online' and node['idle'] is True
    assert any('laptop is online, idle' in t for t in world['n']['desktop'].kinds('relay.peer'))


def test_leave_hands_over_and_peer_takes_at_once(world):
    """R6: `leave` snapshots and hands M3 over and says goodbye; desktop takes it without waiting lost_after_s."""
    step_all(world)
    lap = world['n']['laptop']
    lap.flow.leave('sleep')
    m3 = rec(world, 'M3')
    assert m3['state'] == 'handed_over' and m3['owner'] == 'laptop'
    node = world['n']['server'].store.nodes()['laptop']
    assert node['state'] == 'leaving' and node['leaving_reason'] == 'sleep'
    step_all(world, 'desktop', 'server')
    m3 = rec(world, 'M3')
    assert (m3['owner'], m3['owners'][-1]['why']) == ('desktop', 'takeover:goodbye')


def test_every_inventory_holds_the_other_machines(world):
    """R20: after joining and a round, each machine's inventory holds the measured blocks of the other two."""
    step_all(world)
    for name, node in world['n'].items():
        ms = fs.read(node.inventory)['machines']
        assert set(ms) == {'laptop', 'desktop', 'server'} and ms[name]['source'] == 'self'
        for peer in set(ms) - {name}:
            assert ms[peer]['source'] == 'copy'
            assert ms[peer]['toolchains'] == INFO[peer]['toolchains']
            assert ms[peer]['hardware']['cores'] == INFO[peer]['cores']
            assert ms[peer]['always_on'] is INFO[peer]['always_on']


def lose_and_return_laptop(world):
    """laptop is lost, M3 goes to desktop (epoch 2); laptop wakes with late work, salvages it, desktop is told."""
    step_all(world)
    lap = world['n']['laptop']
    lose_laptop(world)
    (lap.wt('M3', 1) / 'src' / 'late.py').write_text('late = True\n')
    lap.m.clock.jump_wall(3600)
    lap.step()
    step_all(world, 'desktop')


def test_gate_preconditions_hold_after_takeover_and_salvage_note(world):
    """The dev_task gate (lifecycle_deliver.deliver) refuses a missing deploy branch and a worktree with tracked
    changes (`status --porcelain --untracked-files=no`). TAKEOVER.md, takeover.json and SALVAGE.md are new,
    untracked files and the deploy branch is made at `base` when the worktree is prepared: the new owner's
    worktree passes both checks."""
    lose_and_return_laptop(world)
    wt = world['n']['desktop'].wt('M3', 2)
    seg = wt / '.relay/demo/M3'
    assert (seg / 'TAKEOVER.md').is_file() and (seg / 'notes' / 'SALVAGE.md').is_file()
    assert git('rev-parse', '--verify', '--quiet', 'refs/heads/' + launch.deploy_branch_of(JOB, 'M3'), cwd=wt)
    assert git('status', '--porcelain', '--untracked-files=no', cwd=wt) == ''


def test_nothing_is_deleted_across_takeover_return_and_leave(world):
    """R26: through a takeover, a return with salvage and a leave, no worktree, file, hub ref or deploy branch
    the relay made is removed; the returning machine's tree keeps its late work."""
    step_all(world)
    seen_refs = set(remote_refs(world['hub']))
    lose_and_return_laptop(world)
    seen_refs |= set(remote_refs(world['hub']))
    desk = world['n']['desktop']
    desk.flow.leave('sleep')
    step_all(world, 'laptop', 'server', times=3)
    lap = world['n']['laptop']
    old = lap.wt('M3', 1)
    assert (old / 'src' / 'late.py').read_text() == 'late = True\n' and (old / 'README.md').is_file()
    assert (desk.wt('M3', 2) / '.relay/demo/M3/TAKEOVER.md').is_file()
    assert seen_refs <= set(remote_refs(world['hub']))
    branch = 'refs/heads/' + launch.deploy_branch_of(JOB, 'M3')
    for wt in (old, desk.wt('M3', 2)):
        assert git('rev-parse', '--verify', '--quiet', branch, cwd=wt)


def test_checkpoint_alerts_when_the_snapshot_skipped_secret_files(world):
    """Files whose names match relay.secret_globs are left out of the snapshot (snap.skipped). The record is
    still checkpointed, and the supervisor is told once which files stay only on this machine."""
    step_all(world)
    lap = world['n']['laptop']
    wt = lap.wt('M3', 1)
    (wt / '.env').write_text('TOKEN=x\n')
    (wt / 'src' / 'wip.py').write_text('x = 1\n')
    assert lap.flow.checkpoint('M3', kind='phase', phase='design')
    snap = rec(world, 'M3')['checkpoint']['snap']
    assert git('show', '%s:src/wip.py' % snap, cwd=world['hub']) == 'x = 1'
    assert git('cat-file', '-e', '%s:.env' % snap, cwd=world['hub'], check=False) == ''
    alerts = lap.kinds('relay.snapshot')
    assert len(alerts) == 1 and '.env' in alerts[0] and 'M3' in alerts[0]


def test_delivered_commit_reaches_the_hub_without_the_worker_pushing(world):
    """R2, R10: the worker only commits; the gate queues the commit. The relay pushes it to the deploy branch on
    the code remote before recording the delivery, so another machine can start the dependent from it."""
    step_all(world)
    lap = world['n']['laptop']
    wt = lap.wt('M3', 1)
    (wt / 'src' / 'core.rs').write_text('fn main() {}\n')
    git('add', '-A', cwd=wt)
    git('commit', '-q', '-m', 'feat: core', cwd=wt)                    # committed, never pushed by the worker
    sha = git('rev-parse', 'HEAD', cwd=wt)
    lap.ws.queue_path('M3', 1).parent.mkdir(parents=True, exist_ok=True)
    lap.ws.queue_path('M3', 1).write_text('{"status": "pending_approval", "sha": "%s", '
                                          '"deploy_branch": "relay/demo/M3/delivered"}\n' % sha)
    lap.step()
    assert remote_refs(world['hub'])['refs/heads/relay/demo/M3/delivered'] == sha
    assert rec(world, 'M3')['state'] == 'delivered' and rec(world, 'M1')['base'] == sha
    step_all(world, 'desktop')
    assert (world['n']['desktop'].wt('M1', 1) / 'src' / 'core.rs').read_text() == 'fn main() {}\n'


def test_two_machines_race_one_segment_and_one_wins(world):
    """R8 on the hub: desktop and server read the same version of M0 and both take it; the hub's compare-and-swap
    lets exactly one through, the other is refused (raced) and neither starts the segment twice."""
    desk, srv = world['n']['desktop'], world['n']['server']
    assert desk.flow._read() and srv.flow._read()
    won = [srv.flow.take('M0'), desk.flow.take('M0')]
    assert won == [True, False]
    m0 = rec(world, 'M0')
    assert (m0['owner'], m0['epoch']) == ('server', 1) and 'M0' not in desk.flow.owned
    step_all(world, 'desktop', 'server')
    started = [s for n in (desk, srv) for _, s, _ in n.executor.started if s == 'M0']
    assert started == ['M0'] and srv.executor.started[-1][1] == 'M0'


def gate_delivers(node, seg, epoch, path, text):
    """The worker commits; the dev_task gate queues the commit; the node's next round delivers it."""
    wt = node.wt(seg, epoch)
    (wt / path).parent.mkdir(parents=True, exist_ok=True)
    (wt / path).write_text(text)
    git('add', '-A', cwd=wt)
    git('commit', '-q', '-m', 'feat: %s' % seg, cwd=wt)
    sha = git('rev-parse', 'HEAD', cwd=wt)
    q = node.ws.queue_path(seg, epoch)
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text('{"status": "pending_approval", "sha": "%s", "deploy_branch": "%s"}\n'
                 % (sha, launch.deploy_branch_of(node.flow.job, seg)))
    node.step()
    return sha


def test_merge_segment_released_after_all_deliveries_and_claimed(world):
    """R10 on the hub: M3, M0 and then M1 (released from M3's commit) are delivered through the gate; the merge
    segment is then released, claimed by one machine, started with the job's merged branch as its deploy branch,
    and its worktree holds every segment's work."""
    step_all(world)
    lap, desk, srv = world['n']['laptop'], world['n']['desktop'], world['n']['server']
    gate_delivers(lap, 'M3', 1, 'src/core.rs', 'core\n')
    step_all(world, 'desktop')
    assert (rec(world, 'M1')['owner'], rec(world, 'M1')['state']) == ('desktop', 'working')
    gate_delivers(srv, 'M0', 1, 'src/client.py', 'client\n')
    gate_delivers(desk, 'M1', 1, 'src/server.py', 'server\n')
    assert [rec(world, s)['state'] for s in ('M0', 'M1', 'M3')] == ['delivered'] * 3
    assert rec(world, 'merge')['state'] in ('pending', 'working')
    step_all(world, times=3)
    merge = rec(world, 'merge')
    assert merge['state'] == 'working' and merge['owner'] in ('laptop', 'desktop', 'server')
    holder = world['n'][merge['owner']]
    started = [spec for _, s, spec in holder.executor.started if s == 'merge']
    assert len(started) == 1 and started[0]['deliver']['deploy_branch'] == 'relay/demo/merged'
    wt = holder.wt('merge', merge['epoch'])
    merge_in = merge.get('merge_in') or []
    assert len(merge_in) == 2 and 'git merge ' + ' '.join(merge_in) in started[0]['goal']
    for sha in merge_in:                    # the goal's `git merge` needs these commits in the worktree
        assert subprocess.run(['git', 'cat-file', '-e', sha + '^{commit}'], cwd=wt).returncode == 0
    git('merge', '-q', '--no-edit', *merge_in, cwd=wt)
    assert all((wt / p).is_file() for p in ('src/core.rs', 'src/client.py', 'src/server.py'))
