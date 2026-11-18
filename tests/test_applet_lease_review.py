"""Fix reviews run by the fleet and the worker supply of applet leases (docs/applets.md 3.3 and 6,
docs/applet-interface.md 1.2, 1.3, 2.5 and 3.9). Same v2 service fleet as ``test_applet_lease``.
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


def review_of(f, rid='m.1'):
    lid = f.agent('worker-1')['applet_lease'] or rec_lease(f)
    fixes = json.loads(rec(f, lid).get('fixes') or '{}')
    return fixes[rid]['review']


def rec_lease(f):
    return sorted(f.r.scan_iter(al.lease_key(NAME, 'L-*')))[0].rsplit(':', 1)[1]


def reviewer_answers(f, verdict='PASS', findings=()):
    """Tick until the review request is out, then the reviewer slot answers it."""
    al.tick(f)  # pick
    al.tick(f)  # send (posted on the bus)
    assert f.v2.step(10)
    f.pump()
    env = receive(f, 'worker-2')
    assert env['body']['text'].startswith('[Fix review R-')
    settle(f, 'worker-2', env, {'verdict': verdict, 'findings': list(findings)})
    al.tick(f)
    return env


def supervisor_quiet(f):
    return not f.v2.depth('supervisor')


# ====================================================================== fix review (section 6)
def test_fix_reply_is_reviewed_by_another_slot_without_the_supervisor(host):
    lid = grant(host)
    hand_back(host, lid)
    assert result(host, 'm.1') is None  # withheld
    started = events(host, 'review.started')[0]
    assert started['id'] == 'm.1' and started['round'] == 1 and supervisor_quiet(host)
    reviewer_answers(host)
    out = result(host, 'm.1')
    assert out['status'] == 'ok' and out['result'] == {'status': 'done'} and out['lease'] == lid
    assert out['review']['verdict'] == 'pass' and out['review']['round'] == 1 and out['review']['capped'] is False
    review = host.r.hgetall(applet_ops.review_key(NAME, out['review']['id']))
    assert review['fixer'] == 'worker-1' and review['reviewer'] == 'worker-2' and supervisor_quiet(host)
    assert not host.agent('worker-2').get('applet_review')
    assert json.loads(rec(host, lid)['last_review'])['verdict'] == 'pass'


def test_fail_counts_rounds_and_caps(host, tmp_path):
    declare(host, tmp_path, review={'max_rounds': 1})
    lid = grant(host)
    hand_back(host, lid)
    reviewer_answers(host, 'PASS', [{'level': 'P1', 'file': 'a.py', 'line': 3, 'what': 'x', 'repro': 'pytest'}])
    out = result(host, 'm.1')['review']  # a PASS that lists a P1 is a FAIL
    assert (out['verdict'], out['round'], out['capped']) == ('fail', 1, True) and len(out['findings']) == 1
    refused = message(host, 'm.2', lid, fix=FIX)
    assert (refused['status'], refused['reason']) == ('rejected', 'review_cap')


def test_review_timeout_gives_none_and_the_lease_outlives_it(host):
    lid = grant(host)  # active at 1000, ends 1600
    host.setf('worker-2', state='busy')  # no reviewer, and no worker can be added (no roster file)
    hand_back(host, lid)
    host.clock['now'] = 1700.0
    al.tick(host)
    assert rec(host, lid)['state'] == 'active' and result(host, 'm.1') is None  # no expiry during the review
    host.clock['now'] = 1000.0 + 1801
    al.tick(host)
    out = result(host, 'm.1')['review']
    assert (out['verdict'], out['reason'], out['capped']) == ('none', 'no_reviewer', False)
    assert float(rec(host, lid)['expires_at']) >= host.clock['now'] + 300
    al.tick(host)
    assert rec(host, lid)['state'] == 'active'


def test_reviewer_is_never_a_slot_of_the_same_job(host):
    lid = grant(host)
    host.r.hset(al.lease_key(NAME, 'L-0ldjob00'), mapping={'id': 'L-0ldjob00', 'job': 'job-1', 'slot': 'worker-2',
                                                          'state': 'released'})
    hand_back(host, lid)
    al.tick(host)
    review = host.r.hgetall(applet_ops.review_key(NAME, review_of(host)))
    assert review['state'] == 'waiting' and review['reviewer'] == ''  # worker-2 worked on job-1


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
