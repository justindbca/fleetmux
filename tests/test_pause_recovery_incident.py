"""Incident replay (task brief section 4 item 4; Studio root fleet ops, run fleet-20261114-214946-e413dd4d):

1. sub-fleet memsub: pi worker mem-3 gets a memory-extraction request (pure question, timeout_s 900, one answer);
2. the watchdog presses Escape after 153 s of silence; the pi turn ends aborted (execution stop unconfirmed);
3. before: mem-3 held, memsub paused, its daemon could not recover it (held not_recovered), the root was told
   ([sub-fleet stuck]), the root supervisor replied BLOCKED, the root stayed paused for 5 days and a job queued
   there never started.

Now: only mem-3 is held; after held_requeue_after_s the daemon ends that session (kill and verify), starts its next
generation and routes the request to it once more; the answer is published ok. memsub never pauses and never reports
upwards, the root never pauses, and a job submitted to the root meanwhile starts.

Two real private Redis servers: memsub = test_runtime_v2.fx (pi service fleet), root = test_service_split.split_tree
(service root with jobs). Fake cmux, tmp_path run directories; nothing outside the test's own directories.
The Escape itself is the watchdog's existing path (tests/test_watchdog.py); here its effect, the pi ``settled``
event with aborted true, is posted directly. Red until the runtime_v2.py wiring round (settle / held_watch_tick).
"""
import json
import time

import pytest

from fleetmux import awake, fleet, subfleet
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, session  # noqa: F401  (fixture)
from test_service_split import split_tree  # noqa: F401  (fixture)
from test_service_job import job_rec, jobs, start_job

pytestmark = pytest.mark.usefixtures('job_chains_as_before')  # the root's job uses the slot's own model


def ready(f, slot):
    meta = session(f, slot)
    event(f, dict(meta, kind='bridge_session', request_id='register-' + slot + '-' + str(meta['generation'])))
    f.v2.bus.register_slot(slot, meta['generation'], meta['thread'])
    event(f, dict(meta, kind='startup', text='READY', failed=False, aborted=False))
    event(f, dict(meta, kind='availability', idle=True))


def test_interrupted_pure_request_is_requeued_and_no_fleet_pauses(fx, split_tree):
    memsub, t = fx, split_tree
    root = t.f
    memsub.cfg['parent_config'] = str(root.path)  # memsub reports to this root (held-*.json land in its run)
    record_dir = root.run_dir / 'sub' / 'memsub'
    record_dir.mkdir(parents=True)
    fleet.save(record_dir / 'config.json', dict(memsub.cfg))
    rec = {'state': 'running', 'task_id': 'memsub', 'named': 'memsub', 'launched': str(time.time() - 3600)}
    subfleet.save_record(record_dir, rec)

    def no_pause_anywhere():
        assert not memsub.paused(), 'memsub paused: ' + str(memsub.r.hget(fleet.SERVICE, 'reason'))
        assert not root.paused(), 'root paused: ' + str(root.r.hget(fleet.SERVICE, 'reason'))
        notices = root.run_dir / 'subfleet-notices'
        assert not (notices.is_dir() and list(notices.glob('held-*.json'))), 'memsub reported a held slot upwards'

    def tick_memsub():
        memsub.r.set('fx:daemon:beat', time.time())
        memsub.v2.tick()
        no_pause_anywhere()

    # 1. the request reaches mem-3 (worker-1 here)
    spec = dict(id='mx-1', payload={'message': 'extract the memories of this note'}, to='worker-1', timeout_s=900,
                reply={'required': ['reply'], 'allowed': ['reply']})
    event(memsub, dict(kind='request', request_id='cmd-mx-1', spec=spec))
    memsub.pump()
    env = receive(memsub, 'worker-1')
    old_pane = memsub.agent('worker-1')['pane']
    # 2. Escape -> the pi turn ends aborted, no tool running
    event(memsub, dict(session(memsub, 'worker-1'), kind='settled', delivery=env, candidates=[], failed=True,
                     aborted=True, unfinished=False))
    assert memsub.agent('worker-1')['state'] == 'held'
    no_pause_anywhere()
    assert memsub.r.get(fleet.RESULT + 'mx-1') is None, 'published failed at hold time: the replay is then refused'
    # meanwhile a job is submitted to the root and starts
    jid, _ = start_job(t)
    assert jobs(root)[jid]['state'] in ('starting', 'running'), jobs(root)[jid]
    no_pause_anywhere()
    # 3. held_requeue_after_s later the daemon regenerates mem-3 and routes the request again
    mark = json.loads(memsub.agent('worker-1')['requeue_due'])
    mark['at'] = awake.now() - 60
    memsub.setf('worker-1', requeue_due=json.dumps(mark))
    tick_memsub()
    w = memsub.agent('worker-1')
    assert w['generation'] == '1' and old_pane in memsub.mgr.killed
    assert memsub.r.hget(fleet.REQ + 'mx-1', 'replay') == '2'
    ready(memsub, 'worker-1')
    memsub.pump()
    again = receive(memsub, 'worker-1')
    event(memsub, dict(session(memsub, 'worker-1'), kind='settled', delivery=again, failed=False, aborted=False,
                     unfinished=False, candidates=[{'tool': 'fleet_result', 'value': {'reply': 'three memories'}}]))
    assert json.loads(memsub.r.get(fleet.RESULT + 'mx-1'))['status'] == 'ok'
    # 4. the root's own watch of memsub finds nothing to report, past every threshold
    memsub.setf('worker-1', held_since=awake.now() - 4000)
    for _ in range(3):
        tick_memsub()
    facts = subfleet.child_progress(fleet.load(record_dir / 'config.json'), rec)
    assert subfleet.stuck_reasons(root, record_dir, rec, facts, 600) == []
    root.tick()
    no_pause_anywhere()
    assert job_rec(root, jid)[1] is not None
