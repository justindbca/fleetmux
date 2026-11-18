"""relay.liveness + relay.events: judging the other machines on this machine's own monotonic clock (R6, R7),
peer notices sent once (relay.peer)."""
import itertools

import pytest

import relay_kit
from fleetmux.relay import records as R
from fleetmux.relay.clock import FakeClock
from fleetmux.relay.events import Events, ViewStore
from fleetmux.relay.hub import Listing, NodeView, TaskView
from fleetmux.relay.liveness import ABSENT, LEAVING, LOST, ONLINE, Observer
from fleetmux.relay.refs import RefNames

N = RefNames("j")
_seq = itertools.count(1)


def sha():
    return f"{next(_seq):040x}"


class World:
    """What the observer (laptop) lists on the hub."""

    def __init__(self, nodes=("desktop", "server")):
        self.nodes = {}
        self.tasks = {}
        for n in nodes:
            self.set_node(n)

    def set_node(self, n, **kw):
        prev = self.nodes.get(n)
        rec = R.next_node(prev.record, **kw) if prev else R.new_node(job="j", node=n, state="online",
                                                                      machine=relay_kit.machine_info())
        if not prev:
            rec = R.next_node(rec, state=kw.get("state", "online"))
        self.nodes[n] = NodeView(sha(), rec)

    def set_task(self, t, owner):
        rec = R.claim(R.new_task(job="j", id=t, title=t, weight="light", dir=f".relay/j/{t}", base="a" * 40),
                      me=owner)
        self.tasks[t] = TaskView(sha(), rec)

    def listing(self, clock, ok=True):
        if not ok:
            return Listing(False, {}, clock.mono(), "unreachable")
        refs = {N.node(n): v.sha for n, v in self.nodes.items()}
        refs.update({N.task(t): v.sha for t, v in self.tasks.items()})
        return Listing(True, refs, clock.mono())


class Sink:
    def __init__(self):
        self.notices, self.audits = [], []

    def notify(self, text):
        self.notices.append(text)

    def audit(self, event, **detail):
        self.audits.append((event, detail))


@pytest.fixture
def env(tmp_path):
    clock = FakeClock(wall=5000.0, mono=100.0)
    sink = Sink()
    events = Events(sink.notify, sink.audit, ViewStore(tmp_path / "view-j.json", clock), settings=relay_kit.settings())
    obs = Observer(relay_kit.settings(), "laptop", clock, events, job="j")
    return obs, clock, sink, World(), tmp_path


def upd(obs, w, clock, ok=True):
    return obs.update(w.listing(clock, ok), w.tasks, w.nodes)


def test_silence_beyond_lost_after_s_is_lost(env):
    obs, clock, sink, w, _ = env
    assert upd(obs, w, clock) == {"desktop": ONLINE, "server": ONLINE}
    clock.advance(300)
    assert upd(obs, w, clock)["desktop"] == ONLINE                 # exactly lost_after_s: still online
    clock.advance(1)
    assert upd(obs, w, clock) == {"desktop": LOST, "server": LOST}
    assert obs.takeable("desktop") and "lost after 301 s" in obs.judged("desktop")


def test_any_change_of_the_node_ref_resets(env):
    obs, clock, sink, w, _ = env
    upd(obs, w, clock)
    for _ in range(5):
        clock.advance(200)
        w.set_node("desktop")
        classes = upd(obs, w, clock)
    assert classes["desktop"] == ONLINE and classes["server"] == LOST


def test_a_task_record_written_by_the_node_counts_as_a_sign_of_life(env):
    obs, clock, sink, w, _ = env
    upd(obs, w, clock)
    clock.advance(250)
    w.set_task("M0", "server")
    upd(obs, w, clock)
    clock.advance(100)
    assert upd(obs, w, clock) == {"desktop": LOST, "server": ONLINE}


def test_goodbye_is_leaving_at_once_and_takeable_after_offline_grace(tmp_path):
    clock = FakeClock(mono=10.0)
    sink = Sink()
    events = Events(sink.notify, sink.audit, ViewStore(tmp_path / "v.json", clock), settings=relay_kit.settings())
    obs = Observer(relay_kit.settings(offline_grace_s=60), "laptop", clock, events, job="j")
    w = World()
    upd(obs, w, clock)
    w.set_node("desktop", state="leaving", leaving_reason="sleep")
    assert upd(obs, w, clock)["desktop"] == LEAVING
    assert not obs.takeable("desktop") and obs.judged("desktop") == "goodbye: sleep"
    clock.advance(60)
    upd(obs, w, clock)
    assert obs.takeable("desktop") and not obs.takeable("server")


def test_goodbye_with_zero_grace_is_takeable_at_once(env):
    obs, clock, sink, w, _ = env
    upd(obs, w, clock)
    w.set_node("server", state="leaving", leaving_reason="operator")
    upd(obs, w, clock)
    assert obs.takeable("server")


def test_blind_observer_judges_nobody_and_restarts_timers(env):
    obs, clock, sink, w, _ = env
    upd(obs, w, clock)
    clock.advance(200)
    assert upd(obs, w, clock, ok=False) == {"desktop": ONLINE, "server": ONLINE}
    clock.advance(1000)                                             # the observer itself was offline
    assert upd(obs, w, clock, ok=False) == {"desktop": ONLINE, "server": ONLINE}
    assert not obs.takeable("desktop")
    assert upd(obs, w, clock) == {"desktop": ONLINE, "server": ONLINE}   # first good read restarts every timer
    clock.advance(301)
    assert upd(obs, w, clock)["desktop"] == LOST


def test_wall_clock_and_record_contents_are_never_used(env):
    obs, clock, sink, w, _ = env
    upd(obs, w, clock)
    clock.jump_wall(10 ** 6)                                        # a sleep: only the wall clock moves
    assert upd(obs, w, clock)["desktop"] == ONLINE
    clock.advance(301)
    w.nodes["desktop"] = NodeView(w.nodes["desktop"].sha, dict(w.nodes["desktop"].record, beat=999))
    assert upd(obs, w, clock)["desktop"] == LOST                     # same sha: no change, whatever it says


def test_never_seen_node_is_absent_and_me_is_not_judged(env):
    obs, clock, sink, w, _ = env
    w.set_node("laptop")
    classes = upd(obs, w, clock)
    assert "laptop" not in classes and obs.classes().get("other", ABSENT) == ABSENT
    assert not obs.takeable("laptop") and not obs.takeable("other")


def test_peer_notices_once_each_lost_leaving_back(env):
    obs, clock, sink, w, _ = env
    upd(obs, w, clock)                                              # baseline: nothing sent
    assert sink.notices == []
    clock.advance(301)
    upd(obs, w, clock)
    upd(obs, w, clock)
    lost = [t for t in sink.notices if "desktop" in t and "lost" in t]
    assert len(lost) == 1
    w.set_node("desktop")
    upd(obs, w, clock)
    upd(obs, w, clock)
    assert len([t for t in sink.notices if "desktop" in t and "back" in t]) == 1
    w.set_node("desktop", state="leaving", leaving_reason="shutdown")
    upd(obs, w, clock)
    assert len([t for t in sink.notices if "desktop" in t and "goodbye" in t]) == 1
    assert all(e == "relay.peer" for e, _ in sink.audits if e.startswith("relay.peer"))


def test_notices_not_repeated_after_a_restart(env):
    obs, clock, sink, w, tmp = env
    upd(obs, w, clock)
    clock.advance(301)
    upd(obs, w, clock)
    n = len(sink.notices)
    store = ViewStore(tmp / "view-j.json", clock)
    s = relay_kit.settings()
    obs2 = Observer(s, "laptop", clock, Events(sink.notify, sink.audit, store, settings=s), job="j")
    upd(obs2, w, clock)
    clock.advance(301)
    upd(obs2, w, clock)
    assert len(sink.notices) == n                                    # still lost: no second notice
    assert store.nodes["desktop"]["record"]["node"] == "desktop"     # last record kept for status / takeover


def test_notice_and_refusal_reason_lengths_come_from_the_settings(tmp_path):
    from fleetmux.relay import params
    block = relay_kit.block()
    block["notify_max"], block["reason_max"] = 12, 7
    clock = FakeClock(wall=5000.0, mono=100.0)
    sink = Sink()
    ev = Events(sink.notify, sink.audit, ViewStore(tmp_path / "v.json", clock), settings=params.load(block))
    ev.baseline()
    assert ev.emit("relay.peer", "k1", notify="x" * 50) and sink.notices == ["x" * 12]

    def refuse(text):
        raise RuntimeError("refused because of a long reason")
    ev2 = Events(refuse, sink.audit, ViewStore(tmp_path / "w.json", clock), settings=params.load(block))
    ev2.baseline()
    assert ev2.emit("relay.peer", "k2", notify="hello")
    assert sink.audits[-1] == ("relay.notify_refused", {"for_event": "relay.peer", "reason": "refused"})
