"""fix-m4: the fake server's ``kill_group`` and a transient EPERM (py-1 ``m4-fake-exit-fix.md``).

On macOS ``killpg`` answers EPERM for a process group whose members have all exited but are not reaped yet --
a tmux client that just detached (``backends/fleetmux.py`` ``detach_view``: ``detach-client``, then
``pane.kill``). The fake used to turn that into an internal error. Now EPERM from the signal is tolerated, but
success is still only ``gone()``: a group that stays alive (or stays EPERM) ends as a timeout, within the same
bound as before. Deterministic: ``os.killpg`` and ``gone`` are stand-ins, no process is signalled.
"""
import importlib.util
import signal
import time
from pathlib import Path

import pytest

FAKE = Path(__file__).resolve().parent / 'fake_fleetmux.py'


@pytest.fixture(scope='module')
def fake():
    spec = importlib.util.spec_from_file_location('fake_fleetmux_under_test', FAKE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # main() only runs under __main__
    return mod


def eperm_killpg(calls):
    def killpg(pgid, sig):
        calls.append((pgid, sig))
        raise PermissionError(1, 'Operation not permitted')
    return killpg


def test_transient_eperm_then_reaped_is_success(fake, monkeypatch):
    calls, logs, probes = [], [], []
    monkeypatch.setattr(fake.os, 'killpg', eperm_killpg(calls))

    def gone():  # alive at the first look, reaped a few probes after the (EPERM) SIGHUP
        probes.append(time.monotonic())
        return len(probes) > 3
    t0 = time.monotonic()
    assert fake.kill_group(4242, gone, 5000, lambda level, msg: logs.append((level, msg))) is True
    assert time.monotonic() - t0 < 1.0  # it does not wait for the SIGTERM step at 2000 ms
    assert calls == [(4242, signal.SIGHUP)]
    assert [lv for lv, _ in logs] == ['WARN'] and 'permission denied' in logs[0][1]


def test_persistent_eperm_with_a_live_group_is_still_a_timeout(fake, monkeypatch):
    calls, logs = [], []
    monkeypatch.setattr(fake.os, 'killpg', eperm_killpg(calls))
    t0 = time.monotonic()
    assert fake.kill_group(4242, lambda: False, 0, lambda level, msg: logs.append((level, msg))) is False
    took = time.monotonic() - t0
    # timeout_ms 0: only the SIGHUP step, then the reap grace (500 ms), never longer
    assert fake.KILL_REAP_GRACE_MS / 1000 - 0.05 <= took < fake.KILL_REAP_GRACE_MS / 1000 + 1.0
    assert calls == [(4242, signal.SIGHUP)]


def test_persistent_eperm_runs_the_whole_schedule_and_fails(fake, monkeypatch):
    calls = []
    monkeypatch.setattr(fake.os, 'killpg', eperm_killpg(calls))
    monkeypatch.setattr(fake, 'KILL_SCHEDULE', ((0, signal.SIGHUP), (20, signal.SIGTERM), (40, signal.SIGKILL)))
    monkeypatch.setattr(fake, 'KILL_REAP_GRACE_MS', 50)
    t0 = time.monotonic()
    assert fake.kill_group(4242, lambda: False, 40, lambda *_: None) is False
    assert time.monotonic() - t0 < 1.0
    assert [sig for _, sig in calls] == [signal.SIGHUP, signal.SIGTERM, signal.SIGKILL]


def test_kill_pane_with_a_live_group_under_eperm_raises_kill_timeout(fake, monkeypatch):
    """The caller (``pane.kill``) keeps its kill_timeout contract: the pane is not dropped."""
    monkeypatch.setattr(fake.os, 'killpg', eperm_killpg([]))
    dropped = []

    class P:
        id, pgid, killing = 'p-1', 4242, False

        def group_gone(self):
            return False

        def fg_job(self):
            return None  # no foreground job of its own (PROTOCOL.md §4.13)
    s = type('S', (), {'log': staticmethod(lambda *_: None), 'drop': lambda self, p: dropped.append(p.id)})()
    p = P()
    with pytest.raises(fake.Fail) as err:
        fake.kill_pane(s, p, 0)
    assert err.value.code == 'kill_timeout' and dropped == [] and p.killing is False
