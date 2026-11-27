"""Release drift on a real root fleet object (docs/design.md section 56): ``release_drift.tick`` against the v2 service
fleet of tests/test_runtime_v2.py (the repo's private test Redis), with a stub ``Ops`` for everything outside the
fleet: the installed release is a directory under ``tmp_path``, nothing is spawned, signalled or sent.
"""
import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import fx  # noqa: F401  (a v2 service fleet)
from test_release_drift import KNOBS, T0, LATER, make_release
from fleetmux import fleet_config, release_drift


class SmokeOps(release_drift.Ops):
    def __init__(self, tmp):
        self.t, self.new, self.old = T0, make_release(tmp, 'new'), make_release(tmp, 'old')
        self.spawned, self.notices = [], []

    def now(self):
        return self.t

    def knobs(self):
        return dict(KNOBS), None

    def installed_dir(self):
        return self.new

    def own(self):
        return 'old'

    def own_dir(self):
        return self.old

    def cmdline(self, pid):
        return None

    def alive(self, pid):
        return False

    def switcher_alive(self, pid):
        return pid in self.spawned

    def daemon_running(self, run):
        return False

    def ps(self):
        return []

    def redis_get(self, sock, key):
        return ''

    def sub_busy(self, cfg):
        return []

    def run(self, argv, env=None, timeout=None):
        return 0, 'usage'

    def spawn(self, argv, env, log_path):
        self.spawned.append(4242)
        return 4242

    def notify_user(self, f, text):
        self.notices.append(text)


@pytest.fixture
def root(fx, tmp_path):  # noqa: F811
    fleet_config.pin(fleet_config.NONE)
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'start.sh').write_text('#!/bin/sh\n')
    fx.cfg['fleet_home'] = str(home)
    yield fx
    fleet_config.pin(None)


def test_a_real_root_fleet_announces_and_starts_one_switch(root, tmp_path):
    f, ops = root, SmokeOps(tmp_path)
    depth = f.inbox_depth('supervisor')
    release_drift.tick(f, ops=ops)
    ops.t = LATER
    release_drift.tick(f, ops=ops)
    ops.t = LATER + 5
    release_drift.tick(f, ops=ops)
    assert ops.spawned == [4242] and len(ops.notices) == 1
    assert f.inbox_depth('supervisor') == depth + 1  # the root supervisor got the notice as a queued message
    assert 'from release old to release new' in ops.notices[0]


def test_a_real_root_fleet_waits_for_a_worker_in_a_turn(root, tmp_path):
    f, ops = root, SmokeOps(tmp_path)
    f.setf('worker-1', state='busy')
    release_drift.tick(f, ops=ops)
    ops.t = LATER
    release_drift.tick(f, ops=ops)
    assert not ops.spawned and f.release_drift_gate['wait_until'] == LATER + KNOBS['idle_wait_s']
