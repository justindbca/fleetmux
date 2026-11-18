"""`fleetmux relay init|join|status|leave|checkpoint|deliver|take|park` (03-interfaces section 3; R2, R5, R6,
R16, R18, R21). The backend is replaced by in-memory fakes: no git, no fleet."""
import copy
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fleetmux.relay import cli, launch
from relay_flow_support import MACHINES, MemoryStore, params, write_job

ROOT = Path(__file__).resolve().parents[1]


class Workspace:
    def __init__(self, node):
        self.node = node
        self.n = 0

    def prepare(self, seg, rec, takeover):
        return '/wt/%s/%s' % (self.node, seg)

    def snapshot(self, seg, rec, kind):
        self.n += 1
        return {'commit': 'c%d' % self.n, 'snap': 's%d' % self.n}

    def progress(self, seg, rec):
        return {'path': 'notes/dev-1/PROGRESS.md', 'ok': True, 'done': 1, 'doing': ['b'], 'next': ['c'],
                'red_tests': []}

    def queue_path(self, seg, epoch):
        return '/nonexistent/queue-%s-%s.jsonl' % (self.node, seg)

    def salvage(self, seg, epoch):
        return None

    def publish(self, seg, epoch, branch, sha):
        return None                         # pushed

    def salvage_note(self, seg, epoch, name, content):
        pass


class Backend:
    """What the CLI needs from the machine: shared by the three test machines except node name and machine."""

    def __init__(self, store, node, judged, state_dir, machines=None):
        self._store = store
        self.node = node
        self.judged = judged
        self.state_dir = state_dir
        self.machines = machines or MACHINES
        self.executor = launch.FakeExecutor()
        self.ws = Workspace(node)
        self.seen = {}

    def node_name(self):
        return self.node

    def params(self):
        return params()

    def store(self, job):
        return self._store

    def workspace(self, job):
        return self.ws

    def make_executor(self, job):
        return self.executor

    def start_spec(self, job):
        return lambda seg, rec, wt, takeover: {'name': launch.sub_fleet_name(job, seg, rec['epoch'])}

    def judge(self, job):
        return lambda nodes, ok: dict(self.judged)

    def machine(self):
        return copy.deepcopy(self.machines[self.node])

    def head(self, path):
        return 'b0'

    def observations(self, job):
        return self.seen

    def hub(self, job):
        return 'hub.example/demo'


@pytest.fixture
def world(tmp_path):
    store = MemoryStore()
    judged = {n: 'online' for n in MACHINES}
    repo = tmp_path / 'repo'
    write_job(repo)
    machines = {n: Backend(store, n, judged, tmp_path / ('state-' + n)) for n in MACHINES}
    return {'store': store, 'judged': judged, 'repo': repo, 'm': machines}


def run(world, node, *argv):
    out, err = io.StringIO(), io.StringIO()
    b = world['m'][node]
    code = cli.main(list(argv), backend=b, state_dir=b.state_dir, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def joined(world):
    for n in MACHINES:
        assert run(world, n, 'join', 'demo', '--repo', str(world['repo']))[0] == 0


def init(world):
    return run(world, 'laptop', 'init', 'demo', '--from', str(world['repo'] / '.relay/demo/job.yaml'))


def test_module_entry_help_lists_commands():
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    done = subprocess.run([sys.executable, '-m', 'fleetmux.relay', '--help'], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0
    for cmd in ('init', 'join', 'status', 'leave', 'checkpoint', 'deliver', 'take', 'park'):
        assert cmd in done.stdout


def test_fleetmux_relay_dispatch(monkeypatch):
    from fleetmux import fleet
    seen = {}
    monkeypatch.setattr(cli, 'main', lambda argv: seen.setdefault('argv', argv) and 0)
    with pytest.raises(SystemExit):
        fleet.main(['relay', 'status', 'demo'])
    assert seen['argv'] == ['status', 'demo']


def test_without_git_layer_commands_fail_loudly(tmp_path):
    """Until the git state layer is wired in, a state command exits non-zero and says why."""
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(['status', 'demo'], state_dir=tmp_path, out=out, err=err,
                    backend_factory=lambda state_dir: (_ for _ in ()).throw(cli.Unavailable('no git state layer')))
    assert code == cli.EXIT_UNAVAILABLE and 'no git state layer' in err.getvalue()


def test_init_creates_records_and_refuses_twice(world):
    joined(world)
    code, out, _ = init(world)
    assert code == 0
    tasks = {s: r for s, (_, r) in world['store'].tasks().items()}
    assert tasks['M1']['state'] == 'blocked' and tasks['M3']['state'] == 'pending'
    assert set(tasks) == {'M0', 'M1', 'M3', 'merge', 'coordinator'}
    code, _, err = init(world)
    assert code == 1 and 'already' in err


def test_init_refuses_machine_paths(world):
    (world['repo'] / '.relay/demo/M0/brief.md').write_text('look in /Users/someone/x\n')
    code, _, err = init(world)
    assert code == 1 and 'absolute' in err
    assert world['store'].tasks() == {}


def test_join_records_membership_and_pushes_node(world):
    code, _, _ = run(world, 'desktop', 'join', 'demo', '--repo', str(world['repo']))
    assert code == 0
    member = json.loads((world['m']['desktop'].state_dir / 'jobs' / 'demo.json').read_text())
    assert member == {'job': 'demo', 'repo': str(world['repo']), 'node': 'desktop', 'returning': False}
    assert world['store'].nodes()['desktop']['state'] == 'online'


def test_join_again_is_a_return_that_settles_first(world):
    """A first join is online at once; a machine whose node record exists comes back as rejoining."""
    run(world, 'desktop', 'join', 'demo', '--repo', str(world['repo']))
    code, out, _ = run(world, 'desktop', 'join', 'demo', '--repo', str(world['repo']))
    assert code == 0 and '(rejoining)' in out
    assert world['store'].nodes()['desktop']['state'] == 'rejoining'
    member = json.loads((world['m']['desktop'].state_dir / 'jobs' / 'demo.json').read_text())
    assert member['returning'] is True


def test_take_refuses_missing_toolchain_and_blocked(world):
    joined(world)
    init(world)
    code, _, err = run(world, 'desktop', 'take', 'M3')
    assert code == 1 and 'toolchain: rust' in err
    code, _, err = run(world, 'laptop', 'take', 'M1')
    assert code == 1 and 'blocked on M3' in err
    assert world['store'].tasks()['M3'][1]['owner'] is None


def test_take_then_checkpoint_deliver_releases_dependent(world):
    joined(world)
    init(world)
    assert run(world, 'laptop', 'take', 'M3')[0] == 0
    rec = world['store'].tasks()['M3'][1]
    assert rec['owner'] == 'laptop' and rec['epoch'] == 1
    assert world['m']['laptop'].executor.started == []                     # the relay process starts it
    assert run(world, 'laptop', 'checkpoint', 'M3', '--phase', 'design')[0] == 0
    rec = world['store'].tasks()['M3'][1]
    assert rec['checkpoint']['kind'] == 'phase' and rec['checkpoint']['phase'] == 'design'      # R5
    assert rec['progress']['next'] == ['c']
    code, _, err = run(world, 'desktop', 'deliver', 'M3', '--commit', 'x')
    assert code == 1 and 'not held' in err
    assert run(world, 'laptop', 'deliver', 'M3', '--commit', 'c3')[0] == 0
    assert world['store'].tasks()['M3'][1]['state'] == 'delivered'
    m1 = world['store'].tasks()['M1'][1]
    assert m1['state'] == 'pending' and m1['base'] == 'c3'


def test_take_refuses_segment_held_by_live_owner(world):
    joined(world)
    init(world)
    run(world, 'server', 'take', 'M0')
    code, _, err = run(world, 'desktop', 'take', 'M0')
    assert code == 1 and 'held by server' in err


def test_park(world):
    joined(world)
    init(world)
    run(world, 'server', 'take', 'M0')
    assert run(world, 'server', 'park', 'M0', '--reason', 'needs a login')[0] == 0
    rec = world['store'].tasks()['M0'][1]
    assert rec['state'] == 'parked' and rec['parked'] == {'reason': 'needs a login', 'by': 'server'}


def test_leave_hands_over_and_says_goodbye(world):
    joined(world)
    init(world)
    run(world, 'laptop', 'take', 'M3')
    assert run(world, 'laptop', 'leave', '--reason', 'sleep')[0] == 0
    assert world['store'].tasks()['M3'][1]['state'] == 'handed_over'
    node = world['store'].nodes()['laptop']
    assert node['state'] == 'leaving' and node['leaving_reason'] == 'sleep'
    world['judged']['laptop'] = 'leaving'
    _, out, _ = run(world, 'desktop', 'status', 'demo')
    assert [l for l in out.splitlines() if l.startswith('laptop')][0].split()[1] == 'leaving'


def test_status_same_on_three_nodes(world):
    """R18: the same state remote gives every machine the same task table."""
    joined(world)
    init(world)
    run(world, 'laptop', 'take', 'M3')
    run(world, 'server', 'take', 'M0')
    outs = [run(world, n, 'status', 'demo')[1] for n in MACHINES]
    tables = [o[o.index('TASK'):] for o in outs]
    assert tables[0] == tables[1] == tables[2]
    text = outs[0]
    assert text.splitlines()[0].startswith('job demo')
    m3 = [l for l in text.splitlines() if l.startswith('M3 ')][0].split()
    assert m3[:4] == ['M3', 'working', 'laptop', '1']
    m1 = [l for l in text.splitlines() if l.startswith('M1 ')][0].split()
    assert m1[1] == 'blocked' and m1[-1] == 'M3'
    lap = [l for l in text.splitlines() if l.startswith('laptop')][0]
    assert 'M3 (working, e1' in lap


def test_status_shows_takeover_note(world):
    joined(world)
    init(world)
    world['m']['desktop'].machines = dict(MACHINES, desktop=dict(MACHINES['desktop'],
                                                                 toolchains={'rust': '1.83'}))
    run(world, 'laptop', 'take', 'M3')
    world['judged']['laptop'] = 'lost'
    assert run(world, 'desktop', 'take', 'M3')[0] == 0
    _, out, _ = run(world, 'server', 'status', 'demo')
    m3 = [l for l in out.splitlines() if l.startswith('M3 ')][0]
    assert 'taken over from laptop (lost)' in m3


def test_segment_command_needs_a_joined_job(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    b = Backend(MemoryStore(), 'laptop', {}, tmp_path)
    code = cli.main(['take', 'M3'], backend=b, state_dir=tmp_path, out=out, err=err)
    assert code == 1 and 'join' in err.getvalue()


def test_run_loop_starts_work_and_leaves_on_stop(world, monkeypatch):
    """`run --node`: reads the table first, rounds until SIGTERM, starts the sub-fleets itself, then leaves."""
    from fleetmux.relay import flow as flow_mod
    joined(world)
    init(world)
    handlers = {}
    monkeypatch.setattr(cli.signal, 'signal', lambda sig, fn: handlers.__setitem__(sig, fn))
    real_step = flow_mod.Flow.step

    rounds = []

    def two_rounds(self):
        real_step(self)
        rounds.append(self.state)
        if len(rounds) == 2:
            handlers[cli.signal.SIGTERM]()
    monkeypatch.setattr(flow_mod.Flow, 'step', two_rounds)

    class FakeTime:                                         # the loop waits poll_s between rounds
        now = 0.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def time(cls):
            return 1e9 + cls.now

        @classmethod
        def sleep(cls, s):
            cls.now += s
    monkeypatch.setattr(cli, 'time', FakeTime)
    code, out, _ = run(world, 'laptop', 'run', '--node', 'laptop')
    assert code == 0
    b = world['m']['laptop']
    assert rounds == ['online', 'online']                  # a first join does not settle
    assert [s for _, s, _ in b.executor.started] == ['M3']
    assert json.loads((b.state_dir / 'jobs' / 'demo.json').read_text())['returning'] is True
    assert world['store'].tasks()['M3'][1]['state'] == 'handed_over'
    assert world['store'].nodes()['laptop']['leaving_reason'] == 'shutdown'


class FakeApplet:
    """Stands in for the applet SDK: `rounds` passes (sleeping what each returns on the fake clock), then exits
    like Applet.run (0 on a stop, 1 on a crash)."""
    rounds = 12
    code = 0
    made = []

    heartbeat_s = 10.0
    stale_after_s = 60.0

    def __init__(self):
        import threading
        self.notices, self.audits, self.beats = [], [], []
        self.stopping = threading.Event()
        self.thread_started = None
        FakeApplet.made.append(self)

    @classmethod
    def from_env(cls, *, start=True):
        app = cls()
        app.thread_started = start              # the SDK's free-running heartbeat thread
        return app

    def beat(self):
        self.beats.append(FakeTime.now)

    def notify(self, text, **_):
        self.notices.append(text)

    def audit(self, event, **detail):
        self.audits.append((event, detail))

    def run(self, step):
        for _ in range(self.rounds):
            FakeTime.now += step(self)
        sys.exit(self.code)


class StubBeat:
    """Stands in for relay_applet.LoopHeartbeat: records every tick (and beats the fake applet)."""
    made = []

    def __init__(self, applet):
        self.applet = applet
        self.ticks = []
        StubBeat.made.append(self)

    def tick(self):
        self.ticks.append(FakeTime.now)
        self.applet.beat()
        return True


def use_stub_heartbeat(monkeypatch, applet_cls):
    """`run` must get its applet from relay_applet.loop_heartbeat(), never build the SDK itself."""
    import fleetmux.applet
    from fleetmux import relay_applet

    class NoDirectSdk:
        @classmethod
        def from_env(cls, *a, **k):
            raise AssertionError('run built the applet SDK itself instead of relay_applet.loop_heartbeat()')
    StubBeat.made = []
    monkeypatch.setattr(fleetmux.applet, 'Applet', NoDirectSdk)
    monkeypatch.setattr(relay_applet, 'loop_heartbeat', lambda env=None: StubBeat(applet_cls.from_env(start=False)))


def run_applet(world, monkeypatch, code):
    joined(world)
    init(world)
    FakeApplet.made, FakeApplet.code = [], code
    FakeTime.now = 0.0
    monkeypatch.setattr(cli, 'time', FakeTime)
    use_stub_heartbeat(monkeypatch, FakeApplet)
    monkeypatch.setenv(cli.APPLET_NAME, 'relay')
    monkeypatch.setattr(cli.signal, 'signal', lambda *_: None)
    with pytest.raises(SystemExit) as exc:
        run(world, 'laptop', 'run', '--node', 'laptop')
    assert exc.value.code == code
    return FakeApplet.made[0]


def test_run_under_the_applet_notifies_once_and_leaves_on_a_clean_stop(world, monkeypatch):
    """Under the root fleet the notices go to the supervisor through the SDK, each once, with an audit line; the
    view store is kept beside the membership; a clean stop leaves with reason `shutdown`."""
    app = run_applet(world, monkeypatch, 0)
    takes = [t for t in app.notices if 'M3' in t]
    assert len(takes) == 1 and 'sub-fleet started' in takes[0]
    assert ('relay.take' in [e for e, _ in app.audits])
    assert (world['m']['laptop'].state_dir / 'view-demo.json').is_file()
    assert world['store'].nodes()['laptop']['leaving_reason'] == 'shutdown'


def test_run_under_the_applet_does_not_leave_after_a_crash(world, monkeypatch):
    """A crash (exit 1) keeps the segments: the restarted process settles and resumes them."""
    run_applet(world, monkeypatch, 1)
    assert world['store'].nodes()['laptop'].get('leaving_reason') is None
    assert world['store'].tasks()['M3'][1]['state'] == 'working'


def test_notices_are_not_repeated_after_a_restart(world, monkeypatch):
    """The key of a notice is saved before it is sent: the same notice from a new process is not sent again."""
    from fleetmux.relay import events as ev, knobs
    b = world['m']['laptop']
    store = ev.ViewStore(b.state_dir / 'view-demo.json', cli._Clock())
    sent = []
    note = cli.job_notifier(ev.Events(sent.append, lambda *a, **k: None, store, settings=knobs.Limits(params())), 'demo')
    note('relay.lost', 'M3 (epoch 2) is now taken by desk')
    store2 = ev.ViewStore(b.state_dir / 'view-demo.json', cli._Clock())
    note2 = cli.job_notifier(ev.Events(sent.append, lambda *a, **k: None, store2, settings=knobs.Limits(params())), 'demo')
    note2('relay.lost', 'M3 (epoch 2) is now taken by desk')
    note2('relay.lost', 'M3 (epoch 3) is now taken by tower')
    assert sent == ['M3 (epoch 2) is now taken by desk', 'M3 (epoch 3) is now taken by tower']


# ---------------------------------------------------------------- the run process under the applet host
# The host reads exit 0 as done, 75 as restart (not a crash), 78 as held, anything else as a crash.

class FakeTime:
    now = 0.0

    @classmethod
    def monotonic(cls):
        return cls.now

    @classmethod
    def time(cls):
        return 1e9 + cls.now

    @classmethod
    def sleep(cls, s):
        cls.now += s


class LoopApplet(FakeApplet):
    """Calls the round like Applet.run, sleeps what it returns on the fake clock, runs `hooks[i]` before round i,
    and stops (exit 0) after `max_rounds`. A SystemExit from the round leaves as in Applet.run."""
    max_rounds = 200
    hooks = {}

    def __init__(self):
        super().__init__()
        self.delays = []

    def run(self, step):
        for i in range(self.max_rounds):
            if i in self.hooks:
                self.hooks[i]()
            delay = step(self)
            self.delays.append(delay)
            FakeTime.now += delay
        sys.exit(0)


def run_loop_applet(world, monkeypatch, node='laptop', hooks=None, max_rounds=200):
    FakeApplet.made = []
    LoopApplet.hooks, LoopApplet.max_rounds = dict(hooks or {}), max_rounds
    FakeTime.now = 0.0
    use_stub_heartbeat(monkeypatch, LoopApplet)
    monkeypatch.setattr(cli, 'time', FakeTime)
    monkeypatch.setenv(cli.APPLET_NAME, 'relay')
    monkeypatch.setattr(cli.signal, 'signal', lambda *_: None)
    with pytest.raises(SystemExit) as exc:
        run(world, node, 'run', '--node', node)
    return exc.value.code, FakeApplet.made[0]


def test_run_without_a_job_stays_up_and_beats(world, monkeypatch):
    """Enabled with no job joined, run stays up: it beats every loop pass and never waits longer
    than the SDK's heartbeat_s, instead of exiting 1. A job joined later is picked up and its work started."""
    hooks = {3: lambda: (joined(world), init(world))}
    code, app = run_loop_applet(world, monkeypatch, hooks=hooks, max_rounds=8)
    assert code == 0
    assert len(StubBeat.made[0].ticks) == 8 and max(app.delays) <= app.heartbeat_s
    started = [s for _, s, _ in world['m']['laptop'].executor.started]
    assert started[0] == 'M3' and len(started) == len(set(started))      # work began, nothing twice


def test_ticks_core_loop_heartbeat_once_per_loop_pass(world, monkeypatch):
    """Under the applet, run takes its applet from relay_applet.loop_heartbeat() (one call, no SDK thread)
    and ticks it once at the start of every loop pass; the waits between passes stay within heartbeat_s, so the
    helper's rate limit lets a beat out on each pass."""
    joined(world)
    init(world)
    code, app = run_loop_applet(world, monkeypatch, max_rounds=12)
    assert code == 0 and len(StubBeat.made) == 1 and StubBeat.made[0].applet is app
    assert app.thread_started is False
    ticks = StubBeat.made[0].ticks
    assert len(ticks) == 12
    assert max(b - a for a, b in zip(ticks, ticks[1:])) <= app.heartbeat_s


def test_a_hung_loop_stops_beating(world, monkeypatch):
    """While one pass hangs (a git call that does not return) nothing beats, so the host sees the stale
    heartbeat and marks the applet stalled; beating resumes with the next pass."""
    from fleetmux.relay import flow as flow_mod
    joined(world)
    init(world)
    real_step = flow_mod.Flow.step
    seen = {}

    def hang(self):
        monkeypatch.setattr(flow_mod.Flow, 'step', real_step)  # one hung pass
        app = FakeApplet.made[0]
        seen['before'] = len(app.beats)
        FakeTime.now += 5 * app.stale_after_s              # the pass hangs well past the stale limit
        seen['during'] = len(app.beats)
        return real_step(self)
    hooks = {2: lambda: monkeypatch.setattr(flow_mod.Flow, 'step', hang)}
    code, app = run_loop_applet(world, monkeypatch, hooks=hooks, max_rounds=8)
    assert code == 0 and seen['before'] == seen['during']
    gaps = [b - a for a, b in zip(app.beats, app.beats[1:])]
    assert len(app.beats) == 8 and max(gaps) > app.stale_after_s          # the host sees the hang
    assert sorted(gaps)[-2] <= app.heartbeat_s                             # every other gap is short


def test_sigterm_sets_the_sdk_stopping_flag(world, monkeypatch):
    """Without the SDK's start() its SIGTERM handler is not installed: run installs one that sets `stopping`."""
    handlers = {}
    monkeypatch.setattr(cli.signal, 'signal', lambda sig, fn: handlers.__setitem__(sig, fn))

    class StopApplet(LoopApplet):
        def run(self, step):
            step(self)
            handlers[cli.signal.SIGTERM]()
            assert self.stopping.is_set()
            sys.exit(0)
    FakeApplet.made = []
    use_stub_heartbeat(monkeypatch, StopApplet)
    monkeypatch.setattr(cli, 'time', FakeTime)
    monkeypatch.setenv(cli.APPLET_NAME, 'relay')
    with pytest.raises(SystemExit) as exc:
        run(world, 'laptop', 'run', '--node', 'laptop')
    assert exc.value.code == 0


def unreachable(world, reads=None):
    def down():
        if reads is not None:
            reads.append(FakeTime.now)
        raise OSError('state remote unreadable')
    world['store'].tasks = down


def backoff_run(world, monkeypatch, **over):
    joined(world)
    init(world)
    b = world['m']['laptop']
    p = params(**over)
    b.params = lambda: p
    reads = []
    unreachable(world, reads)
    code, app = run_loop_applet(world, monkeypatch, max_rounds=1000)
    return code, [y - x for x, y in zip(reads, reads[1:])], p, app


def test_unreachable_hub_backs_off_from_the_config_and_exits_75(world, monkeypatch):
    """Failed reads are retried later and later (poll_s, doubling, capped at max(poll_s, beat_s)) while
    the loop keeps beating; once the hub has been unreachable for lost_after_s the process exits 75 (restart, not
    a crash) and does not hand its segments over."""
    code, gaps, p, app = backoff_run(world, monkeypatch)
    cap = max(p['poll_s'], p['beat_s'])
    assert code == 75
    assert gaps[0] == p['poll_s'] and max(gaps) == cap and gaps == sorted(gaps) and len(set(gaps)) > 1
    assert max(b - a for a, b in zip(app.beats, app.beats[1:])) <= app.heartbeat_s
    assert world['store'].nodes()['laptop'].get('leaving_reason') is None


def test_backoff_follows_the_config_values(world, monkeypatch):
    p = params()
    code, gaps, _, _ = backoff_run(world, monkeypatch, poll_s=p['poll_s'] / 3, beat_s=p['beat_s'] * 2,
                                   lost_after_s=p['lost_after_s'] * 2)
    assert code == 75 and gaps[0] == p['poll_s'] / 3 and max(gaps) == p['beat_s'] * 2


def test_a_hub_that_comes_back_resets_the_backoff(world, monkeypatch):
    joined(world)
    init(world)
    p = world['m']['laptop'].params()
    real = world['store'].tasks
    reads = []

    def counted():
        reads.append(FakeTime.now)
        return real()
    world['store'].tasks = counted
    hooks = {1: lambda: unreachable(world, reads), 30: lambda: setattr(world['store'], 'tasks', counted)}
    code, app = run_loop_applet(world, monkeypatch, hooks=hooks, max_rounds=80)
    steps = sorted(set(reads))[:-1]       # one time per step (a claim reads back); the last is leave on exit
    gaps = [y - x for x, y in zip(steps, steps[1:])]
    assert code == 0
    assert max(gaps) > p['poll_s']                                         # backed off while down
    assert gaps[-1] == p['poll_s']                                         # back to polling


def test_a_failed_beat_is_not_a_crash(world, monkeypatch):
    """A tick that raises (host Redis briefly unreachable) is logged and the loop goes on: no exit 1. The host
    sees the stale beat if it lasts."""
    joined(world)
    init(world)
    fails = [True, True]

    def flaky(self):
        self.ticks.append(FakeTime.now)
        if fails:
            fails.pop()
            raise ConnectionError('host redis unreachable')
        return True
    monkeypatch.setattr(StubBeat, 'tick', flaky)
    code, app = run_loop_applet(world, monkeypatch, max_rounds=6)
    assert code == 0 and len(StubBeat.made[0].ticks) == 6


# ---------------------------------------------------------------- status and one-shots read the run's view

def node_rows(out):
    rows = {}
    for line in out.splitlines()[2:]:
        if not line.strip():
            break
        parts = line.split()
        rows[parts[0]] = parts[1:3]
    return rows


def test_status_reads_node_classes_and_seen_from_the_run_view(world, monkeypatch):
    """A one-shot process has no observation history: `status` shows the classes and the SEEN times the running
    relay saved in its view, and this machine's own row shows its own record's state."""
    joined(world)
    init(world)
    code, _ = run_loop_applet(world, monkeypatch, max_rounds=4)
    assert code == 0
    for n in world['judged']:
        world['judged'][n] = 'unknown'                 # what a one-shot judge without history would say
    code, out, err = run(world, 'laptop', 'status', 'demo')
    assert code == 0, err
    rows = node_rows(out)
    assert rows['desktop'][0] == 'online' and rows['server'][0] == 'online'
    assert rows['desktop'][1] != '-'
    assert rows['laptop'][0] == 'leaving'              # its own record: run left with `shutdown`


def test_status_without_a_view_falls_back_to_the_judge(world):
    joined(world)
    init(world)
    code, out, _ = run(world, 'laptop', 'status', 'demo')
    rows = node_rows(out)
    assert code == 0 and rows['desktop'] == ['online', '-']


def test_one_shot_commands_judge_peers_from_a_fresh_view(world, monkeypatch):
    """`take` and the other one-shots use the run's classes while its view is fresh (saved within
    lost_after_s), and the judge alone once it is stale."""
    joined(world)
    init(world)
    run_loop_applet(world, monkeypatch, max_rounds=4)
    for n in world['judged']:
        world['judged'][n] = 'unknown'
    b = world['m']['laptop']
    ctx = cli.Ctx(b, b.state_dir, io.StringIO(), io.StringIO())
    assert ctx.flow('demo', 'laptop').judged['desktop'] == 'online'
    FakeTime.now += b.params()['lost_after_s'] + 1
    assert 'desktop' not in ctx.flow('demo', 'laptop').judged


# ---------------------------------------------------------------- held (78): a fix by a person, not a restart

NO_SELF = ('the inventory has no `source: self` machine: run `fleetmux superpowers refresh --name <name>` once '
           'on this machine')


def test_unavailable_inside_the_run_loop_is_held_with_one_notice(world, monkeypatch):
    """Under the applet host an Unavailable (here: no source:self machine) exits 78 (held), not 1 (a crash
    the host would count), and the supervisor gets one notice saying what to fix. Segments are not handed over."""
    from fleetmux.relay.backend import Unavailable
    joined(world)
    init(world)

    def no_self(job):
        raise Unavailable(NO_SELF)
    world['m']['laptop'].store = no_self
    code, app = run_loop_applet(world, monkeypatch, max_rounds=3)
    assert code == 78
    assert len(app.notices) == 1 and 'source: self' in app.notices[0] and 'superpowers refresh' in app.notices[0]


def test_unavailable_configuration_under_the_applet_is_held_with_one_notice(tmp_path, monkeypatch):
    """A bad relay block fails before the run loop exists (building the backend): still 78 and one notice."""
    from fleetmux.relay.backend import Unavailable

    def bad_config(state_dir):
        raise Unavailable('relay configuration: relay.poll_s must be below relay.lost_after_s')
    use_stub_heartbeat(monkeypatch, LoopApplet)
    FakeApplet.made = []
    monkeypatch.setenv(cli.APPLET_NAME, 'relay')
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(['run', '--node', 'laptop'], backend_factory=bad_config, state_dir=tmp_path, out=out, err=err)
    assert code == 78 and 'relay.poll_s' in err.getvalue()
    notices = [t for a in FakeApplet.made for t in a.notices]
    assert len(notices) == 1 and 'relay.poll_s must be below relay.lost_after_s' in notices[0]


def test_unavailable_by_hand_still_exits_69(tmp_path, monkeypatch):
    from fleetmux.relay.backend import Unavailable
    monkeypatch.delenv(cli.APPLET_NAME, raising=False)

    def bad_config(state_dir):
        raise Unavailable('relay configuration: broken')
    code = cli.main(['run', '--node', 'laptop'], backend_factory=bad_config, state_dir=tmp_path,
                    out=io.StringIO(), err=io.StringIO())
    assert code == 69
