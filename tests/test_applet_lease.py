"""Applet leases (docs/applets.md sections 3 to 8, docs/applet-interface.md sections 3, 4 and 9).

A v2 service fleet from ``test_runtime_v2.fx`` (pi supervisor + worker-1, worker-2) on the repo's private test
Redis and ``tmp_path``. The applet process is not started: its hash is written as the host would write it.
"""
import hashlib
import json

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import event, fx, receive, settle  # noqa: F401  (a v2 service fleet)
from fleetmux import applet_lease as al, fleet, roster

NAME, TOKEN = 'applet-demo-1', 'secret-token'


@pytest.fixture
def host(fx, tmp_path, monkeypatch):
    clock = {'now': 1000.0}
    monkeypatch.setattr(al, 'clock', lambda: clock['now'])
    declare(fx, tmp_path)
    for slot in fx.workers:  # the rule files a worker reads
        path = fleet.agent_cwd(fx.cfg, slot) / 'AGENTS.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text('# Worker\n\n## 1. Rules\n- own directory only\n')
    fx.clock = clock
    return fx


def declare(f, base, review=None, **lease):
    row = {'name': NAME, 'command': ['{python}', 'app.py'], 'cwd': str(base), 'lease': lease,
           'review': review or {}, 'notify': {'max_per_hour': 2}}
    entries, errors = roster.applet_entries([row], base, list(f.workers))
    assert not errors
    f.applet_specs = entries
    f.r.hset(al.hkey(NAME), mapping={'state': 'up', 'pid': '4242', 'started_at': '900',
                                     'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest()})


def op(f, opname, rid, **fields):
    override = fields.pop('_override', {})
    spec = dict(id=rid, op=opname, applet=NAME, token=TOKEN, v=1, **fields)
    spec.update(override)
    event(f, dict(kind='request', request_id='cmd-' + rid, spec=spec))
    raw = f.r.get(fleet.RESULT + rid)
    return json.loads(raw) if raw else None


def ack(f, rid):
    return json.loads(f.r.xrange(f.v2.prefix + ':reply:cmd-' + rid)[-1][1]['data'])


def ask(f, rid='a.1', **extra):
    fields = dict(job='job-1', purpose='Fix the failing test in src/app.py', ttl_s=600,
                  paths=['/srv/demo/work'])
    fields.update(extra)
    return op(f, 'applet.lease.request', rid, **fields)


def decide(f, *items, dispatch=()):
    if not f.v2.depth('supervisor'):  # the supervisor's own initiative rides on any reply it gives
        f.v2.queue('supervisor', '[Fleet note] nothing to route', request_id='note', kind='followup')
    f.pump()
    env = receive(f, 'supervisor')
    settle(f, 'supervisor', env, {'status': 'RUNNING', 'dispatch': list(dispatch), 'skip': [], 'applet': list(items)})
    return env


def lease_of(f, rid):
    return json.loads(f.r.get(fleet.RESULT + rid))['lease']


def grant(f, rid='a.1', slot='worker-1', **extra):
    ask(f, rid, **extra)
    decide(f, {'id': rid, 'decision': 'grant', 'slot': slot, 'note': 'ok'})
    lid = lease_of(f, rid)['id']
    al.tick(f)
    return lid


def events(f, kind=None):
    out = [json.loads(x['data']) for _, x in f.r.xrange(al.events_key(NAME))]
    return [e for e in out if kind is None or e['type'] == kind]


def rec(f, lid):
    return al.record(f.r, NAME, lid)


def audits(f, name):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == name]


def message(f, rid, lid, to='worker-1', **extra):
    spec = dict(id=rid, to=to, lease=lid, applet=NAME, token=TOKEN, instruction='Fix only what the merge broke.',
                payload={'file': 'src/app.py'}, reply={'required': ['status']})
    spec.update(extra)
    event(f, dict(kind='request', request_id='cmd-' + rid, spec=spec))
    raw = f.r.get(fleet.RESULT + rid)
    return json.loads(raw) if raw else None


# ====================================================================== the program gate (section 3.3)
def test_gate_step_one_token_state_version(host):
    assert op(host, 'applet.lease.request', 'g.1', _override={'token': 'wrong'})['reason'] == 'bad_token'
    host.r.hset(al.hkey(NAME), 'state', 'stalled')
    assert ask(host, 'g.2')['reason'] == 'applet_not_up'
    host.r.hset(al.hkey(NAME), 'state', 'up')
    out = op(host, 'applet.lease.request', 'g.3', _override={'v': 2})
    assert (out['status'], out['by'], out['reason']) == ('rejected', 'program', 'bad_version')
    host.r.hset(al.hkey(NAME), 'paused', '1')
    assert ask(host, 'g.4')['reason'] == 'applet_paused'


def test_gate_step_two_host_paused_and_draining(host):
    host.pause('test')
    assert ask(host, 'g.1')['reason'] == 'host_paused'
    host.resume()
    host.r.xadd(al.events_key(NAME), {'data': json.dumps({'type': 'host.draining', 'at': 1})})
    assert ask(host, 'g.2')['reason'] == 'host_draining'


def test_gate_step_three_max_active(host, tmp_path):
    declare(host, tmp_path, max_active=1)
    assert ask(host, 'g.1') is None  # waiting for the supervisor
    assert ask(host, 'g.2', job='job-2')['reason'] == 'max_active'


def test_gate_step_four_invalid_shape(host):
    assert ask(host, 'g.2', job='bad job!')['reason'] == 'invalid'
    assert ask(host, 'g.3', purpose='')['reason'] == 'invalid'
    assert ask(host, 'g.4', paths=['relative/dir'])['reason'] == 'invalid'



def test_resent_op_answers_in_progress_then_duplicate(host):
    ask(host, 'g.1')
    ask(host, 'g.1')
    assert ack(host, 'g.1')['status'] == 'in_progress'
    assert ask(host, 'g.2', job='bad job!')['reason'] == 'invalid'
    ask(host, 'g.2')
    assert ack(host, 'g.2')['status'] == 'rejected' and ack(host, 'g.2')['reason'].startswith('duplicate')


# ====================================================================== supervisor decisions and re-checks
def test_grant_and_deny(host):
    ask(host, 'a.1')
    env = decide(host, {'id': 'a.1', 'decision': 'deny', 'note': 'not now'})
    assert '[applet lease request] a.1' in env['body']['text']
    out = json.loads(host.r.get(fleet.RESULT + 'a.1'))
    assert (out['status'], out['by'], out['reason'], out['note']) == ('rejected', 'supervisor', 'supervisor', 'not now')
    assert audits(host, 'applet.lease.denied')
    lid = grant(host, 'a.2')
    assert lease_of(host, 'a.2')['slot'] == 'worker-1' and rec(host, lid)['granted_by'].startswith('supervisor')
    assert host.agent('worker-1')['applet_lease'] == lid and host.agent('worker-1')['applet_lease_holder'] == NAME


def bad_reply(f):
    return int(f.r.hget(fleet.SERVICE, 'bad_replies') or 0)


def test_program_rechecks_the_grant(host):
    ask(host, 'a.1', ttl_s=600)
    host.setf('worker-2', state='busy')
    decide(host, {'id': 'a.1', 'decision': 'grant', 'slot': 'worker-2'})
    assert bad_reply(host) == 1 and host.r.get(fleet.RESULT + 'a.1') is None  # not idle: correction path
    host.setf('worker-2', state='idle')
    decide(host, {'id': 'a.1', 'decision': 'grant', 'slot': 'worker-1', 'paths': ['/srv/demo/work', '/etc']})
    assert bad_reply(host) == 2  # paths may only narrow
    decide(host, {'id': 'a.1', 'decision': 'grant', 'slot': 'worker-1', 'ttl_s': 99999, 'paths': []})
    out = lease_of(host, 'a.1')
    assert out['ttl_s'] == 7200 and out['paths'] == []  # clipped to ttl_max_s, narrowed



# ====================================================================== waiting for the decision
def open_lease(f):
    return f.r.lrange(al.open_key(NAME), 0, -1)[0]


@pytest.mark.parametrize('sub', [False, True], ids=['root', 'sub-fleet'])
def test_requested_lease_waits_across_ticks_for_the_decision(host, sub):
    if sub:
        host.cfg['parent_config'] = '/parent/config.json'
    assert ask(host, 'a.1') is None
    lid = open_lease(host)
    for _ in range(2):  # term checks never run before the term starts
        host.clock['now'] += 5
        al.tick(host)
    assert rec(host, lid)['state'] == 'requested' and host.r.get(fleet.RESULT + 'a.1') is None
    assert not audits(host, 'applet.lease.denied')
    decide(host, {'id': 'a.1', 'decision': 'grant', 'slot': 'worker-1', 'note': 'ok'})
    assert lease_of(host, 'a.1')['state'] == 'granted' and rec(host, lid)['state'] == 'granted'
    al.tick(host)
    assert rec(host, lid)['state'] == 'active' and float(rec(host, lid)['expires_at']) == 1010.0 + 600
    assert not audits(host, 'applet.decision_unknown') and not audits(host, 'applet.decision_late')


def test_request_past_decision_wait_is_denied_no_decision_and_a_late_grant_says_so(host):
    ask(host, 'a.1')
    lid = open_lease(host)
    host.clock['now'] += 599
    al.tick(host)
    assert rec(host, lid)['state'] == 'requested'
    host.clock['now'] += 2  # decision_wait_s (600) passed: the one bound on the wait
    al.tick(host)
    assert (rec(host, lid)['state'], rec(host, lid)['end_reason']) == ('denied', 'no_decision')
    out = json.loads(host.r.get(fleet.RESULT + 'a.1'))
    assert (out['status'], out['reason']) == ('timeout', 'no_decision')
    assert [a['reason'] for a in audits(host, 'applet.lease.denied')] == ['no_decision']
    decide(host, {'id': 'a.1', 'decision': 'grant', 'slot': 'worker-1', 'note': 'ok'})
    late = audits(host, 'applet.decision_late')
    assert len(late) == 1 and not audits(host, 'applet.decision_unknown')
    assert (late[0]['lease'], late[0]['state'], late[0]['end_reason'], late[0]['decision']) == \
        (lid, 'denied', 'no_decision', 'grant')
    assert rec(host, lid)['state'] == 'denied' and not host.agent('worker-1').get('applet_lease')


def test_unknown_decision_id_stays_unknown(host):
    decide(host, {'id': 'zz.9', 'decision': 'grant', 'slot': 'worker-1'})
    assert audits(host, 'applet.decision_unknown') and not audits(host, 'applet.decision_late')


@pytest.mark.parametrize('why,by,reason', [
    ('supervisor', 'supervisor', 'supervisor'), ('operator', 'operator', 'supervisor'),
    ('host_down', 'program', 'host_draining'), ('applet_down', 'program', 'applet_not_up'),
    ('applet_detached', 'program', 'applet_detaching')])
def test_ending_a_requested_lease_carries_the_true_reason(host, monkeypatch, why, by, reason):
    ask(host, 'a.1')
    lid = open_lease(host)
    if why == 'supervisor':
        decide(host, {'id': lid, 'decision': 'revoke', 'note': 'plan changed'})
    elif why == 'operator':
        assert al.operator(host, {'action': 'revoke', 'lease': lid, 'note': 'stop'})['status'] == 'ok'
    elif why == 'host_down':
        monkeypatch.setattr(al, 'BOOT', 'another')
        al.tick(host)
    elif why == 'applet_down':
        host.r.hset(al.hkey(NAME), 'state', 'exited')
        al.tick(host)
    else:
        al.revoke_all(host, NAME, 'applet_detached', 'detached')
    assert (rec(host, lid)['state'], rec(host, lid)['end_reason']) == ('denied', why)
    out = json.loads(host.r.get(fleet.RESULT + 'a.1'))
    assert (out['status'], out['by'], out['reason']) == ('rejected', by, reason)
    assert [(a['reason'], a['by']) for a in audits(host, 'applet.lease.denied')] == [(why, by)]
    assert not host.r.exists(al.OP + 'a.1')


def test_ending_a_granted_or_renewing_lease_carries_the_true_reason(host):
    host.setf('worker-1', delivered=1)
    ask(host, 'a.1')
    decide(host, {'id': 'a.1', 'decision': 'grant', 'slot': 'worker-1'})
    lid = lease_of(host, 'a.1')['id']
    assert al.operator(host, {'action': 'revoke', 'lease': lid, 'note': 'stop'})['status'] == 'ok'
    assert (rec(host, lid)['state'], rec(host, lid)['end_reason']) == ('revoked', 'operator')
    assert audits(host, 'applet.lease.revoked')[-1]['end_reason'] == 'operator'
    lid = grant(host, 'a.2', slot='worker-2')
    op(host, 'applet.lease.renew', 'n.1', lease=lid, need_s=900, progress='p')
    host.r.hset(al.hkey(NAME), 'state', 'exited')
    al.tick(host)
    assert (rec(host, lid)['state'], rec(host, lid)['end_reason']) == ('revoked', 'applet_down')
    out = json.loads(host.r.get(fleet.RESULT + 'n.1'))
    assert (out['status'], out['reason'], out['note']) == ('rejected', 'lease_not_active',
                                                           'the lease ended: revoked (applet_down)')


def test_term_end_never_ends_a_requested_lease(host):
    ask(host, 'a.1')
    lid = open_lease(host)
    assert al.end(host, NAME, lid, 'expired', '', 'term over') is False
    assert rec(host, lid)['state'] == 'requested' and host.r.exists(al.OP + 'a.1')


# ====================================================================== granted -> active
def test_granted_to_active_clean_session_rule_section(host):
    host.setf('worker-1', delivered=1)
    ask(host, 'a.1')
    decide(host, {'id': 'a.1', 'decision': 'grant', 'slot': 'worker-1'})
    lid = lease_of(host, 'a.1')['id']
    info = host.agent('worker-1')
    assert info['fresh_pending'] == '1' and info['result_fresh'] == '1'  # a clean session before the term
    al.tick(host)
    assert rec(host, lid)['state'] == 'granted' and not events(host, 'lease.active')
    rules = (fleet.agent_cwd(host.cfg, 'worker-1') / 'AGENTS.md').read_text()
    assert '## Applet lease ' + lid in rules and 'You may also write inside: /srv/demo/work' in rules
    assert '## Applet lease ' + lid in host.role_scope('worker-1', host.runtime_cfg('worker-1'), {})
    host.setf('worker-1', generation=int(info.get('generation') or 0) + 1, fresh_pending=0)
    host.clock['now'] = 1100.0
    al.tick(host)
    assert rec(host, lid)['state'] == 'active' and float(rec(host, lid)['expires_at']) == 1100.0 + 600
    assert events(host, 'lease.active')[0]['slot'] == 'worker-1'
    al.end(host, NAME, lid, 'released', '', 'done')
    assert '## Applet lease' not in (fleet.agent_cwd(host.cfg, 'worker-1') / 'AGENTS.md').read_text()


def test_prepare_timeout_revokes(host):
    host.setf('worker-1', delivered=1)
    ask(host, 'a.1')
    decide(host, {'id': 'a.1', 'decision': 'grant', 'slot': 'worker-1'})
    lid = lease_of(host, 'a.1')['id']
    host.clock['now'] += 301
    al.tick(host)
    assert (rec(host, lid)['state'], rec(host, lid)['end_reason']) == ('revoked', 'prepare_failed')


# ====================================================================== renewal, release, expiry
def test_renewal_rules(host, tmp_path):
    declare(host, tmp_path, max_renewals=1)
    lid = grant(host)
    out = op(host, 'applet.lease.renew', 'n.1', lease=lid, need_s=900, progress='two of three pass')
    assert out is None and rec(host, lid)['state'] == 'renew_requested'
    assert op(host, 'applet.lease.renew', 'n.2', lease=lid, need_s=900, progress='again')['reason'] == 'renew_pending'
    host.clock['now'] = 1200.0
    decide(host, {'id': 'n.1', 'decision': 'grant', 'ttl_s': 600})
    out = json.loads(host.r.get(fleet.RESULT + 'n.1'))
    assert out['lease'] == {'id': lid, 'expires_at': 1800.0, 'renewals': 1}
    assert events(host, 'lease.renewed')[0]['expires_at'] == 1800.0
    out = op(host, 'applet.lease.renew', 'n.3', lease=lid, need_s=900, progress='more')
    assert out['reason'] == 'max_renewals'


def test_renewal_denied_keeps_the_end(host):
    lid = grant(host)
    op(host, 'applet.lease.renew', 'n.1', lease=lid, need_s=900, progress='p')
    decide(host, {'id': 'n.1', 'decision': 'deny', 'note': 'finish now'})
    assert rec(host, lid)['state'] == 'active' and float(rec(host, lid)['expires_at']) == 1600.0
    assert events(host, 'lease.renew_denied')[0]['note'] == 'finish now'


def test_release_message_in_flight_and_force(host):
    lid = grant(host)
    assert message(host, 'm.1', lid) is None
    out = op(host, 'applet.lease.release', 'x.1', lease=lid)
    assert out['reason'] == 'message_in_flight'
    out = op(host, 'applet.lease.release', 'x.2', lease=lid, force=True, outcome={'result': 'fixed'})
    assert out['lease'] == {'id': lid, 'state': 'released'}
    assert json.loads(host.r.get(fleet.RESULT + 'm.1'))['status'] == 'lease_ended'
    assert events(host, 'lease.ended')[-1]['state'] == 'released' and not host.agent('worker-1')['applet_lease']
    assert audits(host, 'applet.lease.released')


def test_expiry_and_expiring_notice(host):
    lid = grant(host)
    host.clock['now'] = 1000 + 600 - 299
    al.tick(host)
    al.tick(host)
    assert len(events(host, 'lease.expiring')) == 1
    host.clock['now'] = 1601
    al.tick(host)
    assert rec(host, lid)['state'] == 'expired' and not host.r.smembers(al.leases_key(NAME))


# ====================================================================== revocations
@pytest.mark.parametrize('why', ['supervisor', 'operator', 'host_down', 'applet_down', 'slot_held', 'slot_retired'])
def test_every_revoke_reason(host, monkeypatch, why):
    lid = grant(host)
    if why == 'supervisor':
        decide(host, {'id': lid, 'decision': 'revoke', 'note': 'plan changed'})
    elif why == 'operator':
        assert al.operator(host, {'action': 'revoke', 'lease': lid, 'note': 'stop'})['status'] == 'ok'
    elif why == 'host_down':
        monkeypatch.setattr(al, 'BOOT', 'another')
        al.tick(host)
    elif why == 'applet_down':
        host.r.hset(al.hkey(NAME), 'state', 'exited')
        al.tick(host)
    elif why == 'slot_held':
        host.setf('worker-1', state='held')
        al.tick(host)
    else:
        al.slot_retired(host, 'worker-1')
    assert (rec(host, lid)['state'], rec(host, lid)['end_reason']) == ('revoked', why)
    assert events(host, 'lease.ended')[-1]['end_reason'] == why


def test_applet_restart_between_ticks_ends_its_leases(host):
    lid = grant(host)
    host.r.hset(al.hkey(NAME), 'started_at', '950')  # a new copy is up already
    al.tick(host)
    assert rec(host, lid)['end_reason'] == 'applet_down'


# ====================================================================== the leased slot and other senders
def test_message_to_leased_slot_needs_the_lease(host):
    lid = grant(host)
    spec = dict(id='o.1', to='worker-1', payload={}, reply={'required': ['x']})
    event(host, dict(kind='request', request_id='cmd-o.1', spec=spec))
    out = json.loads(host.r.get(fleet.RESULT + 'o.1'))
    assert (out['status'], out['reason']) == ('rejected', 'slot_leased')
    assert message(host, 'm.1', lid, to='worker-2')['reason'] == 'wrong_slot'
    assert message(host, 'm.2', 'L-ffffffff')['reason'] == 'lease_not_active'
    assert message(host, 'm.3', lid, token='bad')['reason'] == 'bad_token'


def test_message_is_framed_and_its_result_reported(host):
    lid = grant(host)
    assert message(host, 'm.1', lid) is None
    host.pump()
    env = receive(host, 'worker-1')
    assert env['body']['text'].startswith('[Applet lease ' + lid + ' · ' + NAME + ' · message 1 · term ends ')
    settle(host, 'worker-1', env, {'status': 'done'})
    al.tick(host)
    assert events(host, 'message.result')[0] == dict(events(host, 'message.result')[0], id='m.1', status='ok')
    assert rec(host, lid)['last_status'] == 'ok' and rec(host, lid)['messages'] == '1'


def test_blocked_reply_returns_to_the_applet(host):
    lid = grant(host)
    message(host, 'm.1', lid)
    host.pump()
    env = receive(host, 'worker-1')
    settle(host, 'worker-1', env, {'blocked': 'needs a schema change outside the purpose'})
    out = json.loads(host.r.get(fleet.RESULT + 'm.1'))
    assert out['status'] == 'blocked' and out['lease'] == lid
    assert not [k for k in host.escalation_state() if k.startswith('blocked:')]


def test_supervisor_dispatch_to_leased_slot_is_refused(host):
    grant(host)
    event(host, dict(kind='request', request_id='cmd-q.1', spec={'id': 'q.1', 'payload': {}, 'reply': {'required': ['x']}}))
    host.pump()
    env = receive(host, 'supervisor')
    settle(host, 'supervisor', env, {'status': 'RUNNING', 'dispatch': [
        {'to': 'worker-1', 'request_id': 'q.1', 'instruction': 'Answer this request in full.'}], 'skip': []})
    assert bad_reply(host) == 1 and host.r.hget(fleet.REQ + 'q.1', 'status') == 'routing'


def test_leased_slot_counts_as_busy_and_skips_fresh_after_result(host):
    grant(host)
    host.cfg['max_busy'] = 1
    req = {'id': 'q.1', 'to': 'worker-2', 'payload': {}, 'reply': {'required': ['x']}}
    event(host, dict(kind='request', request_id='cmd-q.1', spec=req))
    host.pump()
    assert host.agent('worker-2')['state'] == 'idle'  # worker-1 is leased: the one busy place is taken
    host.cfg['max_busy'] = 0
    assert al.leased(host, 'worker-1')
    assert al.skip_fresh(host, 'worker-1') and not al.skip_fresh(host, 'worker-2')


def test_rotation_keeps_the_lease(host):
    lid = grant(host)
    gen = int(host.agent('worker-1').get('generation') or 0)
    host.setf('worker-1', generation=gen + 1, label='worker-1.1')
    al.tick(host)
    rotated = events(host, 'worker.rotated')[0]
    assert (rotated['from'], rotated['to']) == ('worker-1', 'worker-1.1') and rec(host, lid)['state'] == 'active'
    assert 'Lease: you hold applet lease ' + lid in al.start_prompt(host, 'worker-1', 'Read the note.')


# ====================================================================== escalation, notify, audit, pause
def test_escalation_ruled_by_the_supervisor_with_choice(host):
    assert op(host, 'applet.escalate', 'e.1', **{'class': 'plan'}, what='Drop branch x', why='fails alone',
              options=['drop it', 'hold the batch']) is None
    decide(host, {'id': 'e.1', 'decision': 'approve', 'choice': 5, 'note': 'x'})
    assert bad_reply(host) == 1
    decide(host, {'id': 'e.1', 'decision': 'approve', 'choice': 0, 'note': 'drop it'})
    out = json.loads(host.r.get(fleet.RESULT + 'e.1'))
    assert (out['decision'], out['choice']) == ('approve', 0) and events(host, 'escalation.ruled')[0]['by'] == 'supervisor'


def test_delete_escalation_waits_for_the_human(host):
    op(host, 'applet.escalate', 'e.1', **{'class': 'delete'}, what='Remove build/', why='stale', paths=['/srv/b'])
    assert host.r.hget(al.esc_key(NAME, 'e.1'), 'status') == 'awaiting_human'
    decide(host, {'id': 'e.1', 'decision': 'approve', 'note': 'go'}) if host.v2.depth('supervisor') else None
    assert host.r.get(fleet.RESULT + 'e.1') is None
    assert al.operator(host, {'action': 'answer', 'id': 'e.1', 'decision': 'reject', 'note': 'keep'})['status'] == 'ok'
    out = json.loads(host.r.get(fleet.RESULT + 'e.1'))
    assert (out['status'], out['by']) == ('rejected', 'human')


def test_human_escalation_takes_the_delete_path(host, monkeypatch):
    """Class ``human``: a non-deletion step only the human owner approves. Same path as ``delete``: recorded
    ``awaiting_human``, a notice card and channel push, no supervisor ruling, answered by the operator command."""
    from fleetmux import tabs
    seen = []
    monkeypatch.setattr(tabs, 'notice', lambda f, text, label: seen.append((text, label)))
    assert op(host, 'applet.escalate', 'h.1', **{'class': 'human'}, what='Deploy batch-0007 to production',
              why='WAIT_USER step of the release', options=['deploy now', 'hold']) is None
    assert host.r.hget(al.esc_key(NAME, 'h.1'), 'status') == 'awaiting_human'
    assert len(seen) == 1 and 'fleetmux applet answer h.1' in seen[0][0] and seen[0][1] == NAME
    assert not host.v2.depth('supervisor')  # never shown to a supervisor for approval
    decide(host, {'id': 'h.1', 'decision': 'approve', 'choice': 0, 'note': 'go'})
    assert bad_reply(host) == 1 and host.r.get(fleet.RESULT + 'h.1') is None
    out = al.operator(host, {'action': 'answer', 'id': 'h.1', 'decision': 'approve', 'choice': 0, 'note': 'ok'})
    assert out['status'] == 'ok'
    res = json.loads(host.r.get(fleet.RESULT + 'h.1'))
    assert (res['status'], res['decision'], res['choice'], res['by']) == ('ok', 'approve', 0, 'human')
    assert events(host, 'escalation.ruled')[-1]['by'] == 'human'


def test_scope_approval_records_the_ruling_and_leaves_the_lease(host):
    """Section 3.7: approving ``scope`` widens nothing; the applet asks for a new lease with the wider scope."""
    lid = grant(host, paths=['/srv/demo/work'])
    op(host, 'applet.escalate', 's.1', **{'class': 'scope'}, what='Also edit /srv/demo/lib', why='import moved',
       job='job-1', paths=['/srv/demo/lib'], options=['widen'])
    decide(host, {'id': 's.1', 'decision': 'approve', 'choice': 0, 'note': 'fine'})
    assert json.loads(host.r.get(fleet.RESULT + 's.1'))['decision'] == 'approve'
    assert json.loads(rec(host, lid)['paths']) == ['/srv/demo/work'] and rec(host, lid)['state'] in al.OPEN


def test_escalation_wait_s_range(host):
    assert op(host, 'applet.escalate', 'w.1', **{'class': 'plan'}, what='x', why='y', wait_s=9)['reason'] == 'invalid'
    op(host, 'applet.escalate', 'w.2', **{'class': 'plan'}, what='x', why='y', wait_s=10 ** 6)
    assert float(host.r.hget(al.OP + 'w.2', 'wait_s')) == 86400


def test_notify_rate_limit_and_audit(host):
    assert op(host, 'applet.notify', 'n.1', text='batch 7 merged')['status'] == 'ok'
    assert op(host, 'applet.notify', 'n.2', text='batch 8 merged')['status'] == 'ok'
    assert op(host, 'applet.notify', 'n.3', text='batch 9 merged')['reason'] == 'rate_limited'
    assert op(host, 'applet.audit', 'u.1', event='batch.bounced', detail={'n': 1})['status'] == 'ok'
    assert audits(host, 'applet.event') and op(host, 'applet.audit', 'u.2', event='Bad Event')['reason'] == 'invalid'


def test_notice_is_kept_in_full_and_the_audit_line_stays_short(host):
    text = 'Deploy batch 7 is ready. To approve, run: fleetmux applet answer x.1 approve --note ok. ' + 'x' * 700
    assert op(host, 'applet.notify', 'n.1', text=text, job='batch-7')['status'] == 'ok'
    assert len(audits(host, 'applet.notified')[0]['text']) == 300
    kept = al.notices(host.r, NAME)
    assert [(n['id'], n['job'], n['text']) for n in kept] == [('n.1', 'batch-7', text)]


def test_pause_and_resume(host):
    decide(host, {'id': NAME, 'decision': 'pause', 'note': 'hold'})
    assert host.r.hget(al.hkey(NAME), 'paused') == '1' and events(host, 'control')[0]['action'] == 'pause'
    assert ask(host, 'p.1')['reason'] == 'applet_paused'
    al.operator(host, {'action': 'resume', 'name': NAME})
    assert host.r.hget(al.hkey(NAME), 'paused') == '0' and audits(host, 'applet.resumed')


def test_reasons_are_the_documented_set():
    assert set(al.REASONS) == {'bad_token', 'bad_version', 'applet_not_up', 'applet_paused', 'applet_detaching',
                               'host_paused', 'host_draining', 'max_active', 'max_renewals', 'renew_pending',
                               'worker_limit', 'invalid', 'lease_not_active', 'wrong_slot',
                               'slot_leased', 'message_in_flight', 'rate_limited', 'duplicate', 'no_decision',
                               'supervisor'}
