"""A named sub-fleet whose supervisor slot was taken back by the idle reclaim still comes up (design sections 29, 67).

With ``worker_idle_reclaim_min`` on, the slot a roster ``subfleets:`` entry names can be reclaimed while its
sub-fleet is down. An approved ``subfleet.up`` request or the autostart then wakes that slot once (its next
generation, the roster card unchanged, audit ``subfleet.slot_wake``) and the sub-fleet comes up after the new
generation's READY; the wait counts in the existing SLOT_WAIT and its reason is in the timeout answer and in the
autostart ``blocked`` reason. svc fixture of test_subfleet: private Redis, the real daemon step / tick and hook
entry, fake panes.
"""
import json
import time

import pytest

from test_subfleet import (adopt_child, ask, hook, named_chain_as_before, result, supervisor_answers,  # noqa: F401
                           svc, write_roster)
from fleetmux import lifecycle_hold, named_subfleet, subfleet

pytestmark = pytest.mark.usefixtures('named_chain_as_before')

POLICY = {'worker_idle_reclaim_min': 30, 'idle_stop_hours': None}


def reclaim(f, slot='worker-1'):
    """The idle reclaim of lifecycle_hold.service_tick, 31 minutes after the last activity."""
    lifecycle_hold.service_tick(f, POLICY, time.time() + 31 * 60)
    assert f.agent(slot)['state'] == 'reclaimed' and not f.agent(slot).get('pane')


def audits(f, event):
    return [x[1] for x in f.r.xrange(f.v2.bus.audit_key) if x[1].get('event') == event]


def card(f, slot):
    """The slot's roster card as the daemon launches it (profile, cli, model, role)."""
    cfg = f.runtime_cfg(slot)
    return cfg.get('worker_cli'), cfg.get('worker_model'), f.agent(slot).get('runtime_launch') or ''


def ready(t, slot='worker-1'):
    assert hook(t, t.f, slot, 'READY', prompt='startup')['status'] == 'ready'
    assert t.f.agent(slot)['state'] == 'idle'


def test_request_up_wakes_the_reclaimed_supervisor_slot_once_then_comes_up(svc):
    t, f = svc, svc.f
    before = card(f, 'worker-1')
    reclaim(f)
    gen = int(f.agent('worker-1')['generation'])
    rid, answer = ask(t, 'up')
    assert answer['status'] == 'accepted' and result(f, rid) is None, answer
    supervisor_answers(t, rid)
    subfleet.tick(f)  # approved: the reclaimed slot is woken, nothing is spawned yet
    info = f.agent('worker-1')
    assert info['state'] == 'starting' and info['generation'] == str(gen + 1)
    assert card(f, 'worker-1') == before
    [wake] = audits(f, 'subfleet.slot_wake')
    assert wake['slot'] == 'worker-1' and wake['name'] == 'ops'
    subfleet.tick(f)  # still starting: woken once, the up waits
    assert f.agent('worker-1')['generation'] == str(gen + 1) and len(audits(f, 'subfleet.slot_wake')) == 1
    assert named_subfleet.state(f, 'ops').get('state') != 'starting' and result(f, rid) is None
    ready(t)
    assert f.last_activity(f.agent('worker-1')) >= time.time() - 60  # the woken generation counts as fresh
    lifecycle_hold.service_tick(f, POLICY, time.time())  # fresh READY: not reclaimed again
    subfleet.tick(f)
    assert named_subfleet.state(f, 'ops')['state'] == 'starting'
    adopt_child(t)
    subfleet.tick(f)
    assert named_subfleet.state(f, 'ops')['state'] == 'up'
    assert result(f, rid)['status'] == 'ok'


def test_request_up_timeout_names_the_wake(svc):
    t, f = svc, svc.f
    reclaim(f)
    rid, _ = ask(t, 'up')
    supervisor_answers(t, rid)
    subfleet.tick(f)  # woken; its next generation never gets READY
    assert f.agent('worker-1')['state'] == 'starting'
    f.r.hset(named_subfleet.OP + rid, mapping={'approved_at': 0})  # SLOT_WAIT is over
    subfleet.tick(f)
    got = result(f, rid)
    assert got['status'] == 'timeout', got
    assert got['reason'].startswith('slot busy') and 'reclaimed' in got['reason'], got


def test_request_up_on_a_paused_run_does_not_wake(svc):
    t, f = svc, svc.f
    reclaim(f)
    rid, _ = ask(t, 'up')
    supervisor_answers(t, rid)
    f.pause('test')
    assert f.paused()
    subfleet.tick(f)
    assert f.agent('worker-1')['state'] == 'reclaimed' and not audits(f, 'subfleet.slot_wake')


def test_autostart_wakes_the_reclaimed_supervisor_slot_once_then_comes_up(svc):
    t, f = svc, svc.f
    reclaim(f)
    gen = int(f.agent('worker-1')['generation'])
    write_roster(t, autostart=True)
    f.roster_tick()
    subfleet.tick(f)  # autostart due: the reclaimed slot is woken
    info = f.agent('worker-1')
    assert info['state'] == 'starting' and info['generation'] == str(gen + 1)
    assert len(audits(f, 'subfleet.slot_wake')) == 1
    blocked = named_subfleet.state(f, 'ops').get('blocked') or ''
    assert 'reclaimed' in blocked, blocked
    waits = [a['reason'] for a in audits(f, 'subfleet.autostart_wait')]
    assert waits and 'reclaimed' in waits[-1], waits
    subfleet.tick(f)  # still starting: no second wake, the blocked reason is not logged again
    assert len(audits(f, 'subfleet.slot_wake')) == 1 and len(audits(f, 'subfleet.autostart_wait')) == len(waits)
    ready(t)
    subfleet.tick(f)
    [auto] = audits(f, 'subfleet.autostart')
    assert auto['slot'] == 'worker-1' and named_subfleet.state(f, 'ops')['state'] == 'starting'
    adopt_child(t)
    subfleet.tick(f)
    assert named_subfleet.state(f, 'ops')['state'] == 'up'
    assert json.loads(json.dumps(named_subfleet.state(f, 'ops'))).get('slot') == 'worker-1'
