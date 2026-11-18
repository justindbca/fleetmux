"""The relay flow across three machines on one in-memory state store (R1, R2, R5, R6, R8-R13, R16).

The store has the git state layer's contract (versioned records, compare-and-swap); the workspace and the executor
are fakes, so nothing here touches git or starts a fleet."""
import copy
import json

import pytest

from fleetmux.relay import coordinator, deps, flow, jobfile, launch
from relay_flow_support import MACHINES, MemoryStore, params, write_job

SCENARIO = """\
job: demo
nodes: [laptop, desktop, server]
tasks:
  M0: {title: "client side", weight: light, needs: [], prefer: server}
  M3: {title: "protocol core", weight: heavy, needs: [rust], prefer: laptop}
  M1: {title: "server side", weight: heavy, needs: [rust], depends_on: [M3], prefer: desktop}
merge:
  depends_on: [M0, M1, M3]
"""


class Clock:
    """`t` is the monotonic clock, `w` the wall clock; a sleeping machine sees only `w` move."""

    def __init__(self):
        self.t = 1000.0
        self.w = 5000.0

    def mono(self):
        return self.t

    def wall(self):
        return self.w


class Workspace:
    def __init__(self, node, tmp_path):
        self.node = node
        self.tmp = tmp_path
        self.prepared = []
        self.notes = []
        self.n = 0
        self.local = {}

    def prepare(self, seg, rec, takeover):
        self.prepared.append((seg, rec['epoch'], takeover, rec.get('base'), list(rec.get('merge_in') or []),
                              (rec.get('checkpoint') or {}).get('commit')))
        self.local[seg] = ['work.py']
        return '/wt/%s/%s-e%d' % (self.node, seg, rec['epoch'])

    def snapshot(self, seg, rec, kind):
        self.n += 1
        return {'commit': 'c-%s-%s-%d' % (self.node, seg, self.n), 'snap': 's-%s-%s-%d' % (self.node, seg, self.n)}

    def progress(self, seg, rec):
        return {'path': 'notes/dev-1/PROGRESS.md', 'ok': True, 'done': 1, 'doing': ['y'], 'next': ['z'],
                'red_tests': []}

    def queue_path(self, seg, epoch):
        return self.tmp / ('queue-%s-%s.jsonl' % (self.node, seg))

    def salvage(self, seg, epoch):
        return {'commit': 'salv-%s-%s-e%d' % (self.node, seg, epoch), 'files': list(self.local.get(seg, []))}

    def publish(self, seg, epoch, branch, sha):
        return None                         # pushed

    def salvage_note(self, seg, epoch, name, content):
        self.notes.append((seg, name, content))


class World:
    """Three machines on one store; `gone` holds what the others judge a machine to be."""

    def __init__(self, tmp_path, job_yaml=SCENARIO, p=None, machines=None):
        self.store = MemoryStore()
        self.clock = Clock()
        self.p = p or params()
        self.judged = {n: 'online' for n in MACHINES}
        self.events = []
        self.machines = copy.deepcopy(machines or MACHINES)
        self.flows = {}
        for node in MACHINES:
            self.flows[node] = self.make_flow(node, Workspace(node, tmp_path), launch.FakeExecutor())
        for f in self.flows.values():
            f.step()                            # every machine joined and runs before the job is created
            assert f.state == 'online'
        write_job(tmp_path, job_yaml=job_yaml, segments=())
        doc = jobfile.load_job(tmp_path, 'demo')
        for seg, rec in coordinator.job_records(doc, self.p, base='b0').items():
            assert self.store.cas_task(seg, None, rec)

    def make_flow(self, node, ws, executor, settle_on_start=False):
        return flow.Flow(
            node, 'demo', store=self.store, workspace=ws, executor=executor,
            start_spec=lambda seg, rec, wt, takeover: {'name': launch.sub_fleet_name('demo', seg, rec['epoch']),
                                                      'seg': seg, 'takeover': takeover, 'worktree': wt},
            judge=lambda nodes, ok: dict(self.judged), params=self.p, clock=self.clock,
            notify=lambda kind, text, node=node: self.events.append((node, kind, text)),
            machine=lambda node=node: self.machines[node], settle_on_start=settle_on_start)

    def step(self, *nodes, dt=1.0):
        for node in nodes or ('laptop', 'desktop', 'server'):
            self.flows[node].step()
        self.clock.t += dt
        self.clock.w += dt

    def pass_time(self, seconds, asleep=()):
        """Time passes; machines not `asleep` kept their rounds going, so only the sleepers see a gap."""
        self.clock.t += seconds
        self.clock.w += seconds
        for node, f in self.flows.items():
            if node not in asleep and f.last_wall is not None:
                f.last_wall = self.clock.w

    def rec(self, seg):
        return self.store.tasks()[seg][1]

    def kinds(self, node, kind):
        return [t for n, k, t in self.events if n == node and k == kind]


def test_three_nodes_each_working(tmp_path):
    """R1, R14: three segments land on three machines at once; the unpinned heavy one on the strongest."""
    w = World(tmp_path, job_yaml=SCENARIO.replace(
        '  M1:', '  M4: {title: "bulk", weight: heavy, needs: []}\n  M1:'))
    w.step()
    assert (w.rec('M0')['owner'], w.rec('M3')['owner'], w.rec('M4')['owner']) == ('server', 'laptop', 'desktop')
    assert all(w.rec(s)['state'] == 'working' for s in ('M0', 'M3', 'M4'))
    assert w.rec('M1')['state'] == 'blocked' and w.rec('merge')['state'] == 'blocked'
    assert w.rec('coordinator')['owner'] == 'desktop'                      # R17: always on, strongest


def test_adopt_starts_sub_fleet_and_notifies(tmp_path):
    """R9: a claim starts the segment's sub-fleet from the rendered spec and tells the root supervisor."""
    w = World(tmp_path)
    w.step()
    ex = w.flows['laptop'].executor
    assert [(j, sg, sp['name'], sp['takeover'], sp['worktree']) for j, sg, sp in ex.started] == [
        ('demo', 'M3', 'relay-demo-M3-e1', False, '/wt/laptop/M3-e1')]
    assert w.kinds('laptop', 'relay.take')
    assert w.rec('M3')['branch'] == 'relay/demo/M3/e1-laptop'
    assert w.rec('M3')['owners'] == [{'node': 'laptop', 'epoch': 1, 'why': 'claim'}]
    assert w.flows['desktop'].executor.started == []                       # the coordinator role starts nothing


def test_blocked_segment_never_taken(tmp_path):
    """R2: no machine claims or takes a segment whose dependencies are not delivered."""
    w = World(tmp_path)
    for _ in range(5):
        w.step(dt=30)
    assert w.rec('M1')['state'] == 'blocked' and w.rec('M1')['owner'] is None
    w.flows['desktop']._read()
    assert w.flows['desktop'].check_take('M1') == 'blocked on M3'


def test_two_machines_race_one_wins(tmp_path):
    """R8: when two machines both decide to take the same segment, exactly one record lands."""
    w = World(tmp_path, p=params(claim_stagger_s=0))
    a, b = w.flows['desktop'], w.flows['server']
    a._read()
    b._read()
    a.judged = b.judged = dict(w.judged)
    assert a.take('M0') is True
    assert b.take('M0') is False
    assert w.rec('M0')['owner'] == 'desktop' and w.rec('M0')['epoch'] == 1
    assert b.executor.started == []


def test_stagger_lets_the_first_in_order_win(tmp_path):
    w = World(tmp_path)
    w.step('desktop')                       # desktop is second for M0 (prefer server): it waits
    assert w.rec('M0')['owner'] is None
    w.step('server')
    assert w.rec('M0')['owner'] == 'server'


def test_missing_toolchain_never_taken_and_reported(tmp_path):
    """R16: M3 needs rust; with laptop gone no one can take it: it goes back to the table as `reclaimed`
    (04-design 6.5), nobody claims it, and the coordinator is told what is missing."""
    w = World(tmp_path)
    w.step()
    w.judged['laptop'] = 'lost'
    for _ in range(3):
        w.step('desktop', 'server', dt=30)
    m3 = w.rec('M3')
    assert (m3['state'], m3['owner'], m3['epoch']) == ('reclaimed', None, 2)
    assert w.kinds('desktop', 'relay.unplaceable') == ['M3 cannot be placed: toolchain: rust']
    assert w.kinds('server', 'relay.unplaceable') == []                  # only the coordinator's holder reports
    w.flows['server']._read()
    w.flows['server'].judged = dict(w.judged, server='online')
    assert w.flows['server'].check_take('M0') in ('already held here', None)
    assert w.flows['server'].check_take('M3') == 'toolchain: rust'


def scenario_world(tmp_path):
    machines = copy.deepcopy(MACHINES)
    machines['desktop']['toolchains']['rust'] = '1.83'
    return World(tmp_path, machines=machines)


def test_scenario_takeover_release_and_merge(tmp_path):
    """R8, R9, R10: laptop vanishes with M3; desktop takes it over from the last checkpoint, delivers, then
    takes M1 from M3's delivered commit; when all are delivered the merge segment is claimed and delivered."""
    w = scenario_world(tmp_path)
    w.step()
    assert w.flows['laptop'].checkpoint('M3', kind='phase', phase='design')
    assert w.rec('M3')['checkpoint']['phase'] == 'design'                 # R5
    w.judged['laptop'] = 'lost'
    w.step('desktop', 'server')
    m3 = w.rec('M3')
    assert (m3['owner'], m3['epoch'], m3['state']) == ('desktop', 2, 'working')
    assert m3['owners'][-1] == {'node': 'desktop', 'epoch': 2, 'why': 'takeover:lost'}
    assert m3['branch'] == 'relay/demo/M3/e2-desktop'
    prep = w.flows['desktop'].ws.prepared[-1]
    assert prep[:3] == ('M3', 2, True) and prep[5] == 'c-laptop-M3-1'     # from the last checkpoint
    assert w.flows['desktop'].executor.started[-1][2]['takeover'] is True
    assert w.rec('M0')['owner'] == 'server'                               # M0 is untouched
    assert w.flows['desktop'].deliver('M3', commit='c3')
    w.step('desktop', 'server')
    m1 = w.rec('M1')
    assert (m1['owner'], m1['state'], m1['base']) == ('desktop', 'working', 'c3')
    assert w.flows['server'].deliver('M0', commit='c0')
    assert w.flows['desktop'].deliver('M1', commit='c1')
    w.step('desktop', 'server')
    merge = w.rec('merge')
    assert merge['state'] == 'working' and merge['base'] == 'c0' and merge['merge_in'] == ['c1', 'c3']
    holder = w.flows[merge['owner']]
    assert holder.deliver('merge', commit='cm')
    assert deps.job_done({s: r for s, (_, r) in w.store.tasks().items()})


# ---------------------------------------------------------------- leave (R6)

def test_leave_hands_over_and_says_goodbye(tmp_path):
    w = scenario_world(tmp_path)
    w.step()
    w.flows['laptop'].leave('sleep')
    m3 = w.rec('M3')
    assert m3['state'] == 'handed_over' and m3['owner'] == 'laptop'
    assert m3['checkpoint']['kind'] == 'auto' and m3['checkpoint']['commit'] == 'c-laptop-M3-1'
    node = w.store.nodes()['laptop']
    assert node['state'] == 'leaving' and node['leaving_reason'] == 'sleep'
    kinds = [k for k, n in w.store.pushes if n in ('M3', 'laptop')]
    assert kinds[-2:] == ['task', 'node']                                 # hand-over before the goodbye


def test_peer_takes_handed_over_at_once_as_goodbye(tmp_path):
    w = scenario_world(tmp_path)
    w.step()
    w.flows['laptop'].leave('operator')
    w.judged['laptop'] = 'leaving'
    w.step('desktop', 'server')
    m3 = w.rec('M3')
    assert m3['owner'] == 'desktop' and m3['owners'][-1]['why'] == 'takeover:goodbye'
    assert w.kinds('desktop', 'relay.peer') == ['laptop is gone (goodbye)']


def test_offline_grace_delays_goodbye_takeover(tmp_path):
    w = World(tmp_path, p=params(offline_grace_s=100), machines={
        **MACHINES, 'desktop': dict(MACHINES['desktop'], toolchains={'rust': '1.83'})})
    w.step()
    w.flows['laptop'].leave('operator')
    w.judged['laptop'] = 'leaving'
    w.step('desktop', 'server', dt=50)
    assert w.rec('M3')['owner'] == 'laptop'
    w.step('desktop', 'server', dt=60)
    w.step('desktop', 'server')
    assert w.rec('M3')['owner'] == 'desktop'


def test_leave_step_failure_still_says_goodbye(tmp_path):
    w = scenario_world(tmp_path)
    w.step()

    def boom(seg):
        raise OSError('push timed out')
    w.flows['laptop'].ws.snapshot = boom
    w.flows['laptop'].leave('shutdown')
    assert w.rec('M3')['state'] == 'handed_over'
    assert w.store.nodes()['laptop']['state'] == 'leaving'


# ---------------------------------------------------------------- come back (R11, R12, R13)

def back_after_takeover(tmp_path):
    w = scenario_world(tmp_path)
    w.step()
    w.judged['laptop'] = 'lost'
    w.step('desktop', 'server')
    assert w.rec('M3')['owner'] == 'desktop'
    w.pass_time(2000, asleep=['laptop'])                  # laptop slept: a long gap since its last round
    w.judged['laptop'] = 'online'
    return w


def test_wake_marks_rejoining_and_pushes_no_task_ref_before_reading(tmp_path):
    w = back_after_takeover(tmp_path)
    w.store.fail_reads = True
    before = len(w.store.pushes)
    w.step('laptop')
    w.step('laptop')
    pushed = w.store.pushes[before:]
    assert pushed and all(k == 'node' for k, _ in pushed)                 # only its own node record
    assert w.store._nodes['laptop']['state'] == 'rejoining'
    assert w.flows['laptop'].executor.stopped == []                       # nothing decided before the read
    assert w.flows['laptop'].table_read is False and w.flows['laptop'].take('M0') is False
    w.store.fail_reads = False
    w.step('laptop')
    assert w.flows['laptop'].table_read is True and 'M3' not in w.flows['laptop'].owned


def test_taken_segment_stopped_and_salvaged(tmp_path):
    w = back_after_takeover(tmp_path)
    w.step('laptop')
    lap = w.flows['laptop']
    assert lap.executor.stopped == [{'fake': 1, 'job': 'demo', 'seg': 'M3', 'epoch': 1}]
    assert 'M3' not in lap.owned
    assert w.store.refs('salvage/M3/') == {'salvage/M3/laptop-e1': {'commit': 'salv-laptop-M3-e1',
                                                                    'files': ['work.py']}}
    assert w.kinds('laptop', 'relay.lost')
    writes = len([1 for k, n in w.store.pushes if k == 'task' and n == 'M3'])
    for _ in range(3):
        w.step('laptop', dt=30)
    assert len([1 for k, n in w.store.pushes if k == 'task' and n == 'M3']) == writes   # never writes M3 again
    assert w.rec('M3')['owner'] == 'desktop'


def test_new_owner_gets_salvage_notice(tmp_path):
    w = back_after_takeover(tmp_path)
    w.step('laptop')
    w.step('desktop')
    notes = w.flows['desktop'].ws.notes
    assert notes and notes[0][:2] == ('M3', 'salvage/M3/laptop-e1')
    assert len(w.kinds('desktop', 'relay.salvage')) == 1
    w.step('desktop')
    assert len(w.kinds('desktop', 'relay.salvage')) == 1


def test_kept_when_still_owner(tmp_path):
    w = scenario_world(tmp_path)
    w.step()
    w.pass_time(2000, asleep=list(MACHINES))              # everyone was asleep; nobody took anything
    w.step('laptop')
    assert 'M3' in w.flows['laptop'].owned and w.flows['laptop'].executor.stopped == []
    assert w.rec('M3')['owner'] == 'laptop'


def test_short_wake_never_claims_and_is_not_a_candidate(tmp_path):
    w = back_after_takeover(tmp_path)
    w.store.cas_task('X', None, dict(jobfile.new_record('demo', 'X', {
        'title': 'x', 'weight': 'light', 'needs': [], 'depends_on': [], 'prefer': 'laptop'}, base='b0')))
    w.step('laptop', dt=60)                               # rejoining: X preferred to laptop, but not taken
    assert w.rec('X')['owner'] is None
    w.step('desktop', 'server', dt=1)
    assert w.rec('X')['owner'] in ('server', 'desktop')   # others do not count a rejoining machine
    w.pass_time(400, asleep=['laptop'])
    woke = w.clock.t
    w.step('laptop')                                      # sleeps again: the settle clock restarts
    assert w.flows['laptop'].state == 'rejoining'
    for _ in range(4):
        w.pass_time(120)
        w.step('laptop', dt=0)
    assert w.flows['laptop'].rejoin_since == woke
    assert w.flows['laptop'].state == 'rejoining'          # 481 s since the last wake < settle_s 600


def test_settled_online_idle_and_coordinator_told(tmp_path):
    w = back_after_takeover(tmp_path)
    w.step('laptop')
    w.step('desktop')
    for _ in range(4):
        w.clock.t += 150
        w.clock.w += 150
        w.step('laptop', 'desktop', 'server', dt=0)
    lap = w.store.nodes()['laptop']
    assert lap['state'] == 'online' and lap['idle'] is True
    assert w.flows['laptop'].state == 'online'
    w.step('desktop')
    assert 'laptop is online, idle' in w.kinds('desktop', 'relay.peer')


def test_refused_checkpoint_starts_rejoin(tmp_path):
    """A machine cut off from the network keeps working; its first refused CAS sends it through rejoin."""
    w = scenario_world(tmp_path)
    w.step()
    w.judged['laptop'] = 'lost'
    w.step('desktop', 'server')
    lap = w.flows['laptop']
    assert lap.checkpoint('M3', kind='auto') is False
    assert lap.state == 'rejoining'
    w.step('laptop')
    assert 'M3' not in lap.owned and w.store.refs('salvage/M3/')


def test_park_and_only_owner_delivers(tmp_path):
    w = World(tmp_path)
    w.step()
    assert w.flows['desktop'].deliver('M3', commit='x') is False
    assert w.flows['laptop'].park('M3', reason='needs a human') is True
    assert w.rec('M3')['state'] == 'parked' and w.rec('M3')['parked'] == {'reason': 'needs a human', 'by': 'laptop'}


def test_sleep_seen_on_wall_clock_when_monotonic_stood_still(tmp_path):
    """macOS: the monotonic clock stops during sleep; only the wall clock shows the gap. Settle counts awake time."""
    w = scenario_world(tmp_path)
    w.step()
    w.clock.w += 3600                                     # an hour asleep, monotonic unchanged
    for n in ('desktop', 'server'):
        w.flows[n].last_wall = w.clock.w
    w.step('laptop')
    lap = w.flows['laptop']
    assert lap.state == 'rejoining' and lap.rejoin_since == w.clock.t - 1
    w.clock.w += 600                                      # wall time alone does not settle it
    w.step('laptop', dt=0)
    assert lap.state == 'rejoining'



# ---------------------------------------------------------------- delivery seen in the dev_task gate queue (R10)

def test_gate_queue_row_delivers_and_releases_dependent(tmp_path):
    w = scenario_world(tmp_path)
    w.step()
    w.step('laptop')
    assert w.rec('M3')['state'] == 'working'
    (tmp_path / 'queue-laptop-M3.jsonl').write_text(json.dumps(
        {'status': 'pending_approval', 'sha': 'd3', 'deploy_branch': 'relay/demo/M3/delivered'}) + '\n')
    w.step('laptop')
    m3 = w.rec('M3')
    assert m3['state'] == 'delivered' and m3['delivered']['commit'] == 'd3'
    assert m3['delivered']['branch'] == 'relay/demo/M3/e1-laptop'
    assert w.rec('M1')['state'] == 'pending' and w.rec('M1')['base'] == 'd3'


# ---------------------------------------------------------------- own hand-over (04-design 8.2) and CLI leave

def test_own_hand_over_kept_when_nobody_took_it(tmp_path):
    w = scenario_world(tmp_path)
    w.step()
    w.flows['laptop'].leave('sleep')
    w.pass_time(2000, asleep=list(MACHINES))              # nobody took it while laptop slept
    pushes = len(w.store.pushes)
    w.step('laptop')
    m3 = w.rec('M3')
    assert (m3['owner'], m3['epoch'], m3['state']) == ('laptop', 1, 'working')
    lap = w.flows['laptop']
    assert 'M3' in lap.owned and lap.executor.stopped == [] and w.store.refs('salvage/') == {}
    assert ('task', 'M3') in w.store.pushes[pushes:]


def test_hand_over_by_one_shot_leave_stops_the_running_process(tmp_path):
    """`relay leave` from a shell marks the records; the running process sees its own hand-over and leaves too,
    instead of treating the segment as lost."""
    w = scenario_world(tmp_path)
    w.step()
    v, rec = w.store.tasks()['M3']
    assert w.store.cas_task('M3', v, dict(rec, state='handed_over', seq=rec['seq'] + 1))
    w.step('laptop')
    lap = w.flows['laptop']
    assert lap.state == 'leaving' and w.store.nodes()['laptop']['state'] == 'leaving'
    assert lap.executor.stopped == [] and w.store.refs('salvage/') == {}


# ---------------------------------------------------------------- restart (04-design 13)

def test_restart_derives_holdings_from_table_and_running_sub_fleets(tmp_path):
    """A restarted relay process reads the table first and finds its sub-fleets through the executor."""
    w = scenario_world(tmp_path)
    w.step()
    old = w.flows['laptop']
    handle = old.owned['M3']['handle']
    new = w.make_flow('laptop', old.ws, old.executor, settle_on_start=True)   # same root fleet, new process
    assert new.state == 'rejoining' and new.owned == {}
    w.flows['laptop'] = new
    w.step('laptop')
    assert new.owned['M3']['handle'] == handle and old.executor.stopped == []
    assert [s for _, s, _ in old.executor.started].count('M3') == 1          # not started twice


def test_restart_stops_a_sub_fleet_whose_segment_was_taken_meanwhile(tmp_path):
    w = scenario_world(tmp_path)
    w.step()
    old = w.flows['laptop']
    handle = old.owned['M3']['handle']
    w.judged['laptop'] = 'lost'
    w.step('desktop', 'server')                             # taken over while laptop's relay was down
    assert w.rec('M3')['owner'] == 'desktop'
    w.judged['laptop'] = 'online'
    new = w.make_flow('laptop', old.ws, old.executor, settle_on_start=True)
    w.flows['laptop'] = new
    w.step('laptop')
    assert old.executor.stopped == [handle] and 'M3' not in new.owned
    assert 'salvage/M3/laptop-e1' in w.store.salvages('M3')


def test_restart_starts_held_segment_whose_sub_fleet_is_gone(tmp_path):
    w = scenario_world(tmp_path)
    w.step()
    old = w.flows['laptop']
    new = w.make_flow('laptop', old.ws, launch.FakeExecutor(), settle_on_start=True)   # the root lost it too
    w.flows['laptop'] = new
    w.step('laptop')
    assert [s for _, s, _ in new.executor.started] == ['M3']


# ---------------------------------------------------------------- a record owned here but not started here

def test_run_process_starts_a_segment_taken_by_a_one_shot_command(tmp_path):
    w = scenario_world(tmp_path)
    for f in w.flows.values():
        f._push_node()
    v, rec = w.store.tasks()['M0']
    taken = dict(rec, owner='server', epoch=1, state='working', branch='relay/demo/M0/e1-server', seq=1,
                 owners=[{'node': 'server', 'epoch': 1, 'why': 'claim'}])
    assert w.store.cas_task('M0', v, taken)
    w.step('server')
    w.step('server')
    srv = w.flows['server']
    assert [s for _, s, _ in srv.executor.started].count('M0') == 1 and 'M0' in srv.owned


def test_one_shot_take_does_not_start_work(tmp_path):
    w = scenario_world(tmp_path)
    srv = w.flows['server']
    srv.start_work = False
    srv._read()
    srv._observe(w.clock.t)
    assert srv.take('M0') is True
    assert srv.executor.started == [] and w.rec('M0')['owner'] == 'server'


# ---------------------------------------------------------------- unknown write outcome

class UnknownStore(MemoryStore):
    """A push whose answer was lost: `apply` decides whether it landed; the caller always hears None."""

    def __init__(self, apply):
        super().__init__()
        self.apply = apply
        self.unknown = False

    def cas_task(self, task, expected, record, snap=None):
        if not self.unknown:
            return super().cas_task(task, expected, record, snap=snap)
        if self.apply:
            super().cas_task(task, expected, record, snap=snap)
        return None


@pytest.mark.parametrize('apply,won', [(True, True), (False, False)])
def test_unknown_outcome_settled_by_reading_back(tmp_path, apply, won):
    w = World(tmp_path)
    w.store.__class__ = UnknownStore
    w.store.apply, w.store.unknown = apply, True
    srv = w.flows['server']
    srv._read()
    srv._observe(w.clock.t)
    assert srv.take('M0') is won
    assert (w.rec('M0')['owner'] == 'server') is won
    assert (len(srv.executor.started) == 1) is won


def test_automatic_snapshot_round_updates_the_record(tmp_path):
    """R5: every snapshot_s the relay process checkpoints each working segment it holds (kind auto)."""
    w = scenario_world(tmp_path)
    w.step()
    assert w.rec('M3')['checkpoint'] is None
    w.step('laptop', dt=w.p['snapshot_s'])
    w.step('laptop')
    cp = w.rec('M3')['checkpoint']
    assert cp['kind'] == 'auto' and cp['commit'].startswith('c-laptop-M3') and cp['epoch'] == 1
    assert w.store.refs('snap/') == {'snap/M3': cp['snap']}
    assert w.rec('M3')['progress']['next'] == ['z']


# ---------------------------------------------------------------- run-path failures

class FlakyRunning(launch.FakeExecutor):
    """The root fleet's status cannot be read for the first `fail` calls."""

    def __init__(self, inner, fail):
        super().__init__()
        self.started, self.stopped, self.fail = inner.started, inner.stopped, fail

    def running(self, job):
        if self.fail:
            self.fail -= 1
            raise OSError('root fleet status unreadable')
        return super().running(job)


def test_restart_retries_when_running_sub_fleets_cannot_be_read(tmp_path):
    """Without the executor's list of running sub-fleets the restarted process cannot tell a live sub-fleet from a
    gone one: it stays rejoining, starts nothing, and reads again next round. Never a second sub-fleet."""
    w = scenario_world(tmp_path)
    w.step()
    old = w.flows['laptop']
    ex = FlakyRunning(old.executor, fail=2)
    new = w.make_flow('laptop', old.ws, ex, settle_on_start=False)
    w.flows['laptop'] = new
    w.step('laptop')
    w.step('laptop')
    assert new.state == 'rejoining' and new.owned == {}
    assert [s for _, s, _ in ex.started].count('M3') == 1
    w.step('laptop')
    assert new.state == 'online' and new.owned['M3']['handle'] is not None
    assert [s for _, s, _ in ex.started].count('M3') == 1 and ex.stopped == []


class RefusingStart(launch.FakeExecutor):
    def start(self, job, seg, spec):
        raise launch.ExecutorError('the root fleet refused the job: no free slot')


def test_failed_start_parks_the_segment_with_the_reason(tmp_path):
    """A sub-fleet that cannot be started is parked with the reason and reported once; it is not retried every
    round."""
    w = scenario_world(tmp_path)
    lap = w.flows['laptop']
    lap.executor = RefusingStart()
    w.step()
    w.step('laptop')
    m3 = w.rec('M3')
    assert m3['owner'] == 'laptop' and m3['state'] == 'parked' and 'no free slot' in m3['parked']['reason']
    notes = [t for n, k, t in w.events if n == 'laptop' and 'M3' in t and 'start' in t]
    assert len(notes) == 1 and 'no free slot' in notes[0]


def test_node_record_pushed_on_beat_or_change_not_every_round(tmp_path):
    """The node record goes out when its content changes (state, tasks, idle, role) and otherwise once per
    beat_s, not on every poll_s round; a change is pushed in the same round."""
    w = scenario_world(tmp_path)
    for _ in range(5):
        w.step()                                       # claims and starts settle
    lap = w.flows['laptop']
    pushes = []
    real = w.store.put_node
    w.store.put_node = lambda node, rec: (pushes.append((node, w.clock.t, copy.deepcopy(rec))), real(node, rec))[1]
    poll, beat = w.p['poll_s'], w.p['beat_s']
    rounds = 10
    for _ in range(rounds):
        w.step('laptop', dt=poll)
    mine = [t for n, t, _ in pushes if n == 'laptop']
    assert 0 < len(mine) < rounds
    assert len(mine) <= rounds * poll // beat + 1
    assert all(b - a >= beat for a, b in zip(mine, mine[1:]))
    seg = sorted(s for s in lap.owned if s != 'coordinator')[0]
    pushes.clear()
    assert lap.park(seg, reason='needs a person')
    w.step('laptop', dt=1.0)
    assert [r for n, _, r in pushes if n == 'laptop' and any(t['state'] == 'parked' for t in r['tasks'])]


def test_delivery_waits_until_the_commit_is_on_the_hub(tmp_path):
    """A gate row whose commit cannot be pushed to the deploy branch is not recorded yet: the dependents stay
    blocked (nobody could start from it), the owner is told, and the next round tries again."""
    w = scenario_world(tmp_path)
    w.step()
    w.step('laptop')
    lap = w.flows['laptop']
    answers = ['rejected: hub unreachable', None]
    lap.ws.publish = lambda seg, epoch, branch, sha: answers.pop(0)
    (tmp_path / 'queue-laptop-M3.jsonl').write_text(json.dumps(
        {'status': 'pending_approval', 'sha': 'd3', 'deploy_branch': 'relay/demo/M3/delivered'}) + '\n')
    w.step('laptop')
    assert w.rec('M3')['state'] == 'working' and w.rec('M1')['state'] == 'blocked'
    assert any('not pushed' in t and 'hub unreachable' in t for t in w.kinds('laptop', 'relay.take'))
    w.step('laptop')
    assert w.rec('M3')['state'] == 'delivered' and w.rec('M1')['base'] == 'd3'
