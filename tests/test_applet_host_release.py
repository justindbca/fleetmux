"""An applet of another release is not adopted on resume (docs/applets.md section 2.3, docs/design.md section 56).

A daemon that resumes a run on a new release must not take over an applet process of the old release: ``adoptable``
says why, the runner keeps ``not_adopted`` and ``step`` restarts it as one crash on the daemon's own release. An
applet of the same release is adopted as before; a release that cannot be told is adopted and logged once.

Stubbed: the owner record and the heartbeat are files and dicts under ``tmp_path``, the process checks are
monkeypatched. Nothing is spawned or signalled.
"""
import json
import time
from types import SimpleNamespace

import pytest

from fleetmux import applet_host, releases

PID = 4321


@pytest.fixture
def world(tmp_path, monkeypatch):
    run = tmp_path / 'run'
    run.mkdir()
    cfg = {'run': str(run), 'name': 'demo'}
    spec = {'name': 'relay', 'stale_after_s': 60, 'command': ['{python}', 'x.py'], 'cwd': str(tmp_path),
            'durable_state': False}  # owner record inside the run, never under a fleet home
    monkeypatch.setattr(applet_host, '_leads_session', lambda pid: pid == PID)
    monkeypatch.setattr(releases, 'own', lambda: 'new1234')
    monkeypatch.setattr(releases, 'of_pid', lambda pid: None, raising=False)
    now = time.time()
    h = {'state': 'up', 'started_at': repr(now - 100), 'hb_at': repr(time.time())}
    return SimpleNamespace(cfg=cfg, spec=spec, h=h, now=now)


def owner(w, **extra):
    path = applet_host.owner_path(w.cfg, w.spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict({'host': 'demo', 'run': w.cfg['run'], 'pid': PID}, **extra)))


def test_4_an_applet_of_another_release_is_not_adopted(world):
    owner(world, release='old5678')
    why = applet_host.adoptable(world.cfg, world.spec, world.h, world.now)
    assert why and 'release old5678' in why and 'new1234' in why


def test_4_an_applet_of_the_same_release_is_adopted(world):
    owner(world, release='new1234')
    assert applet_host.adoptable(world.cfg, world.spec, world.h, world.now) is None


def test_4_an_owner_record_without_a_release_falls_back_to_the_process(world, monkeypatch):
    owner(world)
    monkeypatch.setattr(releases, 'of_pid', lambda pid: 'old5678' if pid == PID else None)
    assert 'release old5678' in applet_host.adoptable(world.cfg, world.spec, world.h, world.now)


def test_4_an_unknown_release_is_adopted_and_logged(world):
    owner(world)
    notes = []
    assert applet_host.adoptable(world.cfg, world.spec, world.h, world.now, note=notes.append) is None
    assert len(notes) == 1 and 'unknown' in notes[0]


class FakeRedis:
    def __init__(self, h):
        self.h = dict(h)

    def hgetall(self, key):
        return dict(self.h)

    def hset(self, key, mapping=None, **kw):
        self.h.update(mapping or {})

    def hdel(self, key, *fields):
        for k in fields:
            self.h.pop(k, None)


def fake_fleet(world):
    logs = []
    return SimpleNamespace(cfg=world.cfg, r=FakeRedis(world.h), log=lambda e, **k: logs.append((e, k)),
                           applet_seen=set()), logs


def test_4_resume_restarts_an_old_release_applet_as_one_crash(world):
    owner(world, release='old5678')
    f, logs = fake_fleet(world)
    run = applet_host._adopt(f, world.spec, world.now)
    assert run.proc is None and 'release old5678' in run.not_adopted  # step() restarts it: "not adopted (...)"
    assert not [e for e, _ in logs if e == 'applet.adopted']


def test_4_resume_adopts_a_same_release_applet_and_logs_an_unknown_one(world):
    owner(world, release='new1234')
    f, logs = fake_fleet(world)
    run = applet_host._adopt(f, world.spec, world.now)
    assert run.proc is not None and run.proc.pid == PID and run.not_adopted is None
    owner(world)
    f, logs = fake_fleet(world)
    run = applet_host._adopt(f, world.spec, world.now)
    assert run.proc is not None and run.proc.pid == PID
    assert [k['reason'] for e, k in logs if e == 'applet.release_unknown']


def test_4_start_records_the_release_in_the_owner_record(world, monkeypatch):
    f, _ = fake_fleet(world)
    f.applet_spawn = lambda cfg, spec, token: SimpleNamespace(pid=PID, poll=lambda: None)
    monkeypatch.setattr(applet_host, 'clear_stale', lambda *a, **k: None)
    run = applet_host.Runner(dict(world.spec, interface=1))
    assert applet_host.start(f, run, world.now, {'crash_window_s': 600, 'stale_copy_wait_s': 1}, reason=None)
    rec = json.loads(applet_host.owner_path(world.cfg, world.spec).read_text())
    assert rec['release'] == 'new1234' and rec['pid'] == PID
