"""relay.beats + relay.nodestatus: one node record per machine on the hub, refreshed every beat (R7), carrying
the measured machine, always-on and toolchains (R17, R20), and the machine's tasks and slots (R18)."""
import os
import shutil
import types

import pytest

import relay_kit
from relay_kit import isolated_git, remote_refs  # noqa: F401
from fleetmux.relay.beats import Beats
from fleetmux.relay.nodestatus import measure_machine


@pytest.fixture
def three(tmp_path):
    return relay_kit.machines(tmp_path)


def beats(m):
    return Beats(m.hub, node=m.name, fleet={"name": "main", "release": "r1", "machine": m.name}, clock=m.clock,
                 settings=m.settings)


def test_every_machine_has_one_node_ref_and_the_beat_grows(three):
    hub, base, ms = three
    bs = {n: beats(m) for n, m in ms.items()}
    for n, b in bs.items():
        assert b.due()
        assert b.push(state="online", machine=relay_kit.machine_info(always_on=n != "laptop")).accepted
    assert not bs["laptop"].due()
    ms["laptop"].clock.advance(60)
    assert bs["laptop"].due()
    assert bs["laptop"].push(state="online", machine=relay_kit.machine_info(always_on=False),
                             tasks=[{"id": "M3", "state": "working", "phase": "impl", "epoch": 1}],
                             slots=[{"name": "dev-1", "busy": True, "title": "protocol core"}], idle=False).accepted
    node_refs = sorted(r for r in remote_refs(hub) if "/nodes/" in r)
    assert node_refs == ["refs/relay/j/nodes/desktop", "refs/relay/j/nodes/laptop", "refs/relay/j/nodes/server"]
    seen = ms["server"].hub.nodes(ms["server"].hub.list())
    lap = seen["laptop"].record
    assert lap["beat"] == 1 and lap["state"] == "online" and lap["idle"] is False
    assert lap["tasks"][0]["id"] == "M3" and lap["slots"][0]["name"] == "dev-1"
    assert lap["machine"]["always_on"] is False and seen["desktop"].record["machine"]["always_on"] is True
    assert lap["fleet"] == {"name": "main", "release": "r1", "machine": "laptop"}


def test_a_restarted_relay_continues_the_beat_count(three):
    hub, base, ms = three
    m = ms["desktop"]
    b = beats(m)
    for _ in range(3):
        b.push(state="online", machine=relay_kit.machine_info())
    b2 = beats(m)
    b2.adopt(m.hub.nodes(m.hub.list()))
    b2.push(state="rejoining", machine=relay_kit.machine_info())
    assert m.hub.nodes(m.hub.list())["desktop"].record["beat"] == 3


def test_leaving_record_carries_the_reason(three):
    hub, base, ms = three
    b = beats(ms["laptop"])
    b.push(state="online", machine=relay_kit.machine_info())
    assert b.push(state="leaving", leaving_reason="sleep", machine=relay_kit.machine_info()).accepted
    rec = ms["server"].hub.nodes(ms["server"].hub.list())["laptop"].record
    assert rec["state"] == "leaving" and rec["leaving_reason"] == "sleep"


def test_unreachable_hub_push_is_not_accepted_and_beat_not_lost(tmp_path):
    hub, base, ms = relay_kit.machines(tmp_path, names=("laptop",))
    m = ms["laptop"]
    b = beats(m)
    b.push(state="online", machine=relay_kit.machine_info())
    m.hub.state_remote = str(tmp_path / "gone.git")
    assert not b.push(state="online", machine=relay_kit.machine_info()).accepted
    assert b.last["beat"] == 0                                      # the refused record is not adopted


def test_measure_machine_in_gib_with_passed_toolchains(tmp_path):
    """mem_gb and disk_free_gb are GiB (2^30 bytes), the same unit as the superpowers inventory (ruling Q39)."""
    calls = []

    def run(argv, **kw):
        calls.append(kw.get("timeout"))
        return types.SimpleNamespace(returncode=0, stdout="4\n")
    m = measure_machine(tmp_path, toolchains={"rust": "1.83.0"}, always_on=False, probe_timeout_s=3, run=run)
    assert calls == [3]                                            # the probe limit comes from the caller (Q47)
    assert set(m) == {"cores", "perf_cores", "mem_gb", "disk_free_gb", "always_on", "toolchains"}
    assert m["perf_cores"] == 4 and m["cores"] >= 1 and m["always_on"] is False
    assert m["toolchains"] == {"rust": "1.83.0"}
    assert 0 < m["disk_free_gb"] < 10 ** 6 and 0 < m["mem_gb"] < 10 ** 5
    gib = 2 ** 30
    assert m["mem_gb"] == round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / gib, 1)
    assert abs(m["disk_free_gb"] - shutil.disk_usage(tmp_path).free / gib) < 0.2
