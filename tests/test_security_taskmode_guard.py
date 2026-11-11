"""P1-01 (reviewer-1 final review; M197): the PreToolUse gate must not treat a task-mode delivery as outside content.

Task-mode deliveries are bound to a dispatch id / task id / 'notice' with no fx:req record (RuntimeV2.queue;
Fleet.request writes fx:req only in service mode). The first test is reviewer-1/repro/test_zz_probe_taskmode.py as
written (renamed into the suite). The second keeps channel-originated requests guarded, 'unknown' included.
Real subprocess runs of ``fleet.py claude-pretool`` on the private test Redis; helpers from test_security_guard.
"""
import json

from test_security_guard import make_run, bind, pretool, denied, policy, pending_ids  # helpers only
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from fleetmux import channels

EDIT = {'old_string': 'a', 'new_string': 'b'}


def test_probe_task_mode_delivery_without_fx_req_record_is_treated_as_untrusted(private_redis_v2, tmp_path):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    path, cfg = make_run(tmp_path, r, policy(confirm__wait_s=1), mode='task')
    edit = dict(EDIT, file_path=str(tmp_path / 'notes.md'))
    # control 1: startup turn (request '') -> allowed, nothing registered
    bind(r, request='')
    rc, out, _, _ = pretool(path, 'worker-1', 'Edit', edit)
    assert rc == 0 and not denied(out) and not pending_ids(r)
    # control 2: a service-style request with its fx:req record -> clean -> allowed
    bind(r, request='rq-1')
    r.hset('fx:req:rq-1', 'spec', json.dumps({'id': 'rq-1', 'payload': {'message': '请改一下 notes.md'}}))
    rc, out, _, _ = pretool(path, 'worker-1', 'Edit', edit)
    assert rc == 0 and not denied(out) and not pending_ids(r)
    # the task-mode shape: dispatch id 'M0' (also 't-<task>' / 'notice'), no fx:req record
    bind(r, request='M0')
    assert not r.exists('fx:req:M0')
    rc, out, _, _ = pretool(path, 'worker-1', 'Edit', edit)
    print('TASK-MODE DECISION:', out.strip()[:220])
    assert rc == 0 and not denied(out), 'a plain Edit of a task-mode worker needs no confirmation'


def test_task_ids_and_notice_are_clean_but_channel_originated_requests_stay_guarded(private_redis_v2, tmp_path):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    path, cfg = make_run(tmp_path, r, policy(confirm__wait_s=1), mode='task')
    edit = dict(EDIT, file_path=str(tmp_path / 'notes.md'))
    for rid in ('t-1762650000-ab12', 'notice', 'M0.1'):                     # task id / notice / sub-fleet dispatch id
        bind(r, request=rid)
        rc, out, _, _ = pretool(path, 'worker-1', 'Edit', edit)
        assert rc == 0 and not denied(out) and not pending_ids(r), rid
    # a request accepted from a channel (actor.write_origin), no fx:req record, no P8d mark -> 'unknown' -> confirm
    origin = json.dumps({'channel': 'telegram', 'account': 'fleet-bot-1', 'conversation': '42'})
    r.set(channels.ORIGIN + 'ui-ch1', origin, ex=600)
    bind(r, request='ui-ch1')
    rc, out, _, _ = pretool(path, 'worker-1', 'Edit', edit)
    assert rc == 0 and denied(out) and len(pending_ids(r)) == 1
    assert r.hget(channels.PENDING + pending_ids(r)[0], 'kind') == 'action'
    # its '.done' follow-up is the same request (envelope.origin_id)
    bind(r, request='ui-ch1.done')
    rc, out, _, _ = pretool(path, 'worker-1', 'Edit', dict(edit, new_string='c'))
    assert denied(out) and len(pending_ids(r)) == 2
    # a P8d mark alone (no origin record) is channel evidence too; deny mode denies without registering
    r.set(channels.UNTRUSTED + 'ui-ch2', 'forwarded', ex=600)
    bind(r, request='ui-ch2')
    fleet_cfg = dict(cfg, security=policy(untrusted_actions='deny'))
    from fleetmux import fleet
    fleet.save(path, fleet_cfg)
    rc, out, _, _ = pretool(path, 'worker-1', 'Edit', dict(edit, new_string='d'))
    assert denied(out) and len(pending_ids(r)) == 2
    # reads stay exempt for every request kind
    rc, out, _, _ = pretool(path, 'worker-1', 'Read', {'file_path': 'README.md'})
    assert rc == 0 and not denied(out)
