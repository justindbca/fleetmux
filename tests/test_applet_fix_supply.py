"""Fix replies of applet leases (no review: straight back to the applet) and the worker supply (docs/applets.md
3.3, docs/applet-interface.md 1.2 and 2.5). Same v2 service fleet as ``test_applet_lease``.
"""
import contextlib
import json

import pytest
import yaml

from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_runtime_v2 import event, fx, receive, settle  # noqa: F401
from test_applet_lease import NAME, TOKEN, ask, decide, declare, events, grant, host, message, rec  # noqa: F401
from fleetmux import applet_lease as al, applet_ops, fleet, roster
from fleetmux.applet import core

FIX = {'dir': '/srv/demo/work', 'base': 'abc123', 'head': None}


def result(f, rid):
    raw = f.r.get(fleet.RESULT + rid)
    return json.loads(raw) if raw else None


def hand_back(f, lid, rid='m.1', reply=None):
    """The leased worker gets a fix message and answers it."""
    assert message(f, rid, lid, fix=FIX) is None
    f.pump()
    settle(f, 'worker-1', receive(f, 'worker-1'), reply or {'status': 'done'})


def supervisor_quiet(f):
    return not f.v2.depth('supervisor')


# ====================================================================== fix replies: no review
def test_fix_reply_goes_straight_back_to_the_applet(host):
    lid = grant(host)
    hand_back(host, lid)
    out = result(host, 'm.1')
    assert out['status'] == 'ok' and out['result'] == {'status': 'done'} and 'review' not in out
    assert not host.r.lrange(applet_ops.REVQ, 0, -1) and supervisor_quiet(host)
    assert not events(host, 'review.started')


def test_an_old_applet_sdk_caller_sends_fix_and_reads_review(host):
    """An applet built against the SDK that had the review step: it hands a fix back with send(..., fix=...),
    reads reply.review the way it did (getattr on verdict / round / capped / reason / findings / id) and handles
    Rejected review_cap. The fix is shape-checked and otherwise ignored, the worker's result comes straight back,
    reply.review is None, and no number of fix messages is refused with review_cap."""
    from test_applet_lease_cli import FakeApplet
    from fleetmux.applet import Lease, Rejected
    from test_runtime_v2 import receive, settle
    lid = grant(host)
    answer = {'summary': 'fixed the failing test', 'status': 'done'}

    def worker(spec):
        if spec.get('to'):
            host.pump()
            settle(host, 'worker-1', receive(host, 'worker-1'), answer)
    lease = Lease(FakeApplet(host, worker), {'id': lid, 'slot': 'worker-1', 'job': 'job-1'})
    fix = {'dir': '/srv/demo/work', 'base': 'abc123', 'head': 'def456', 'files': ['src/app.py']}
    for n in range(5):  # more hand-backs than the removed round cap ever allowed
        try:
            reply = lease.send({'kind': 'rollout.repair.handback', 'round': n}, instruction='Hand back the fix.',
                               required=['summary'], allowed=['summary', 'status'], fix=fix,
                               wait_s=10)  # bound only: a held reply would hang an old host's caller
        except Rejected as e:  # what the old applet handled; it must not happen any more
            pytest.fail('send(fix=...) refused: %s %s' % (e.reason, e))
        assert (reply.status, reply.result) == ('ok', answer)
        rv = reply.review
        assert rv is None
        assert [getattr(rv, k, None) for k in ('verdict', 'round', 'capped', 'reason', 'findings', 'id')] == [None] * 6
    assert not host.r.lrange(applet_ops.REVQ, 0, -1) and supervisor_quiet(host)
    assert not events(host, 'review.started') and not rec(host, lid).get('reviewing')


def test_fix_shape_is_still_checked(host):
    lid = grant(host)
    refused = message(host, 'm.1', lid, fix={'dir': 'relative/path', 'base': 'abc'})
    assert (refused['status'], refused['reason']) == ('rejected', 'invalid')


def test_an_older_roster_review_block_is_accepted_and_ignored(host, tmp_path):
    declare(host, tmp_path, review={'max_rounds': 1, 'timeout_s': 600})
    assert 'review' not in al.specs(host)[NAME]
    lid = grant(host)
    for rid in ('m.1', 'm.2'):  # no round cap any more
        hand_back(host, lid, rid)
        assert result(host, rid)['status'] == 'ok'


def test_a_reply_an_older_host_held_for_review_is_released_once(host):
    """Upgrade with a fix reply still withheld by the removed review step: the next applet tick publishes it as
    the worker gave it, gives the lease its normal expiry back and frees the reviewer slot."""
    lid = grant(host)
    key = al.KEY + NAME + ':review:R-0ld00000'
    host.r.hset(key, mapping={'id': 'R-0ld00000', 'lease': lid, 'message': 'm.9', 'fixer': 'worker-1',
                              'reviewer': 'worker-2', 'state': 'running', 'round': 1,
                              'withheld': json.dumps({'status': 'ok', 'result': {'status': 'done'},
                                                      'worker': 'worker-1'})})
    inflight = json.loads(rec(host, lid).get('inflight') or '[]') + ['m.9']
    host.r.hset(al.lease_key(NAME, lid), mapping={'reviewing': 'R-0ld00000', 'inflight': json.dumps(inflight),
                                                  'boot': 'older-daemon'})  # issued before the upgrade restart
    host.r.hset('fx:agent:worker-2', mapping={'applet_review': 'R-0ld00000', 'applet_review_grant': json.dumps(
        {'dir': '/srv/demo/work', 'lease': lid, 'applet': NAME, 'review': 'R-0ld00000'})})
    rules = fleet.agent_cwd(host.cfg, 'worker-2') / 'AGENTS.md'  # the read-only grant an older host wrote
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text('# Worker\n\n## Applet review grant R-0ld00000 (written by the fleet program)\n'
                     'You may read, not write, inside: /srv/demo/work\n\n## Reply format\nkeep\n')
    host.r.rpush(applet_ops.REVQ, NAME + '|R-0ld00000')
    al.tick(host)
    out = result(host, 'm.9')
    assert out['status'] == 'ok' and out['result'] == {'status': 'done'} and out['lease'] == lid
    assert rec(host, lid)['state'] == 'revoked'  # the restart still ends the lease, after the held reply went out
    assert not host.agent('worker-2').get('applet_review')
    assert not host.agent('worker-2').get('applet_review_grant')
    assert 'Applet review grant' not in rules.read_text() and '## Reply format\nkeep' in rules.read_text()
    assert host.r.hget(key, 'state') == 'done' and not host.r.lrange(applet_ops.REVQ, 0, -1)
    assert not al.taken(host, 'worker-2')
    al.tick(host)  # nothing left: a no-op
    assert result(host, 'm.9') == out


def test_fix_dir_outside_the_lease_is_still_refused(host):
    lid = grant(host)
    refused = message(host, 'm.1', lid, fix={'dir': '/srv/elsewhere', 'base': 'abc123', 'head': None})
    assert (refused['status'], refused['reason']) == ('rejected', 'invalid')


# ====================================================================== worker supply (section 3.3)
@pytest.fixture
def rostered(host, tmp_path):
    path = tmp_path / 'roster.yaml'
    path.write_text('# team roster\nworkers:\n  - {name: worker-1}\n  - {name: worker-2}\napplets:\n'
                    '  - {name: ' + NAME + ', command: ["{python}", app.py], cwd: ' + str(tmp_path)
                    + ', lease: {hot_add_max: 1}}\nmax_busy: 0\n')
    host.cfg['roster'] = str(path)
    declare(host, tmp_path, hot_add_max=1)
    host.roster_path = path
    return host


def test_supply_order_idle_then_asleep_then_added(rostered, monkeypatch):
    f = rostered
    woken = []
    monkeypatch.setattr(f.v2, 'respawn', lambda slots, *a: woken.extend(slots))
    entry = al.specs(f)[NAME]
    assert al.supply(f, entry)[0] == 'idle'
    f.setf('worker-1', state='busy')
    f.setf('worker-2', state='reclaimed')
    assert al.supply(f, entry) == ('asleep', ['worker-2'])
    ask(f, 'a.1')
    decide(f, {'id': 'a.1', 'decision': 'grant', 'slot': 'worker-2'})
    al.tick(f)
    assert woken == ['worker-2']
    f.setf('worker-2', state='busy')
    ask(f, 'a.2', job='job-2')
    env = decide(f, {'id': 'a.2', 'decision': 'grant'})
    assert 'a grant adds worker worker-3' in env['body']['text']
    lease = json.loads(f.r.get(fleet.RESULT + 'a.2'))['lease']
    assert lease['slot'] == 'worker-3' and lease['added'] is True
    assert [w['name'] for w in yaml.safe_load(f.roster_path.read_text())['workers']] == ['worker-1', 'worker-2',
                                                                                          'worker-3']
    assert f.roster_path.read_text().startswith('# team roster\n')  # the rest of the file is kept
    assert audits_of(f, 'applet.worker_added')


def audits_of(f, name):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == name]


def test_worker_limit_and_one_rate_limited_line(rostered):
    f = rostered
    f.r.hset(al.hkey(NAME), mapping={'hot_add_max': 0})
    for w in ('worker-1', 'worker-2'):
        f.setf(w, state='busy')
    out = ask(f, 'a.1')
    assert (out['reason'], out['retry_after_s']) == ('worker_limit', 60)
    assert ask(f, 'a.2')['reason'] == 'worker_limit'
    f.pump()
    lines = [receive(f, 'supervisor')]
    assert 'worker limit reached' in lines[0]['body']['text'] and not f.v2.depth('supervisor')


def test_sleeping_added_slot_counts_toward_the_ceiling(rostered):
    f = rostered
    f.r.hset(al.KEY + NAME + ':added', mapping={'worker-3': 0})
    f.workers.append('worker-3')
    f.setf('worker-3', state='reclaimed')
    entry = al.specs(f)[NAME]
    assert 'hot_add_max 1 reached' in applet_ops.add_problem(f, entry)
    assert al.supply(f, entry) == ('idle', ['worker-1', 'worker-2'])
    for w in ('worker-1', 'worker-2'):
        f.setf(w, state='busy')
    assert al.supply(f, entry) == ('asleep', ['worker-3'])  # woken before any new row


def test_hot_add_max_changes_online_and_is_written_back(rostered):
    f = rostered
    out = al.operator(f, {'action': 'set', 'name': NAME, 'key': 'hot_add_max', 'value': 3})
    assert out['hot_add_max'] == 3 and f.r.hget(al.hkey(NAME), 'hot_add_max') == '3'
    [entry] = roster.load_applets(f.roster_path)
    assert entry['lease']['hot_add_max'] == 3 and f.roster_path.read_text().startswith('# team roster\n')
    al.tick(f)
    assert applet_ops.hot_add_max(f, al.specs(f)[NAME]) == 3  # the old row value does not undo it


def test_both_yaml_writers_take_the_lock(rostered, tmp_path, monkeypatch, private_redis_v2):
    taken = []

    @contextlib.contextmanager
    def lock(path):
        taken.append(str(path))
        yield
    monkeypatch.setattr(applet_ops, 'file_lock', lock)
    monkeypatch.setattr(core, 'file_lock', lock)
    applet_ops.write_worker_row(rostered.roster_path, 'worker-9', None)
    params = tmp_path / 'app.yaml'
    params.write_text('applet:\n  hot: [poll_s]\n  poll_s: 5\n')
    p = core.Params(private_redis_v2, NAME, str(params), 'applet')
    p.set('poll_s', 9)
    p.flush()
    assert taken == [str(rostered.roster_path), str(params)]


def test_file_lock_is_exclusive(tmp_path):
    import fcntl
    import os
    from fleetmux.applet.lease import file_lock
    target = tmp_path / 'project.yaml'
    with file_lock(target):
        fd = os.open(str(target) + '.lock', os.O_RDWR)
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(fd)
