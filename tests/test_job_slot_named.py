"""A job never takes the supervisor slot a roster ``subfleets:`` entry declares (design sections 58, 29, 67).

The slot stays reserved also while its named sub-fleet is down: the job takes another idle worker, or the program
adds one (job_hot_add). A reclaimed reserved slot is not woken for a queued job and does not stop the hot add; a
reclaimed ordinary worker still is. svc fixture of test_subfleet (roster sub-fleet ``ops`` on worker-1, private
Redis, the real daemon step / tick, fake panes).
"""
import time

import pytest

from test_service_job import jobs, submit  # noqa: F401
from test_subfleet import step, svc  # noqa: F401  (fixture)
from fleetmux import discover, lifecycle_hold, named_subfleet

pytestmark = pytest.mark.usefixtures('job_chains_as_before')  # the job tests' model chains: the slot keeps its model

POLICY = {'worker_idle_reclaim_min': 30, 'idle_stop_hours': None}


@pytest.fixture(autouse=True)
def no_probe_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(discover.CACHE_ENV, str(tmp_path / 'no-discovered.yaml'))


def audits(f, event):
    return [x[1] for x in f.r.xrange(f.v2.bus.audit_key) if x[1].get('event') == event]


def test_job_takes_another_idle_worker_not_the_named_supervisor_slot(svc):
    t, f = svc, svc.f
    assert named_subfleet.reserved(f, 'worker-1') == 'ops' and 'ops' not in named_subfleet.live(f)  # down
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-2')['state'] == 'idle'
    jid = submit(f, t=t)['job_id']
    f.tick()
    step(t)
    job = jobs(f)[jid]
    assert job['slot'] == 'worker-2', job
    assert f.agent('worker-1')['state'] == 'idle'


def test_job_hot_adds_when_only_the_named_supervisor_slot_is_idle(svc):
    t, f = svc, svc.f
    f.setf('worker-2', state='busy')
    jid = submit(f, t=t)['job_id']
    f.tick()
    job = jobs(f)[jid]
    assert not job.get('slot') and job['state'] == 'queued', job
    assert job['hot_slot'] == 'worker-3', job
    assert 'supervisor slot of named sub-fleet ops' in job['waiting'], job['waiting']
    assert f.agent('worker-1')['state'] == 'idle'


def test_job_does_not_wake_the_reclaimed_named_supervisor_slot_and_hot_adds(svc):
    t, f = svc, svc.f
    f.setf('worker-2', state='busy')
    lifecycle_hold.service_tick(f, POLICY, time.time() + 31 * 60)
    assert f.agent('worker-1')['state'] == 'reclaimed'  # the only reclaimed worker
    gen = f.agent('worker-1')['generation']
    jid = submit(f, t=t)['job_id']
    f.tick()
    lifecycle_hold.service_tick(f, POLICY, time.time())
    assert f.agent('worker-1')['state'] == 'reclaimed' and f.agent('worker-1')['generation'] == gen
    assert not [a for a in audits(f, 'lifecycle.respawn') if a.get('slot') == 'worker-1']
    assert jobs(f)[jid]['hot_slot'] == 'worker-3', jobs(f)[jid]


def test_job_still_wakes_a_reclaimed_ordinary_worker(svc):
    """Control: the job wake of service_tick is unchanged for a worker no sub-fleet declares."""
    t, f = svc, svc.f
    lifecycle_hold.service_tick(f, POLICY, time.time() + 31 * 60)
    assert f.agent('worker-1')['state'] == 'reclaimed' and f.agent('worker-2')['state'] == 'reclaimed'
    gen = int(f.agent('worker-2')['generation'])
    jid = submit(f, t=t)['job_id']
    f.tick()
    assert not jobs(f)[jid].get('hot_slot')  # a reclaimed ordinary worker can be woken: no worker added
    lifecycle_hold.service_tick(f, POLICY, time.time())
    assert f.agent('worker-2')['state'] == 'starting' and f.agent('worker-2')['generation'] == str(gen + 1)
    assert f.agent('worker-1')['state'] == 'reclaimed'
