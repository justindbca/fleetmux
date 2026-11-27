"""Parent heals child (docs/design.md, the layered supervisor rule): a sub-fleet's supervisor that is held, dead,
walled or cannot start -- or the parent's own slot that serves as one -- is reported to the parent fleet's supervisor
with a copy-paste ``heal`` line, and the parent's ``heal`` decision makes the program reopen it (new generation,
model stay / next / head / a card), clear a wall record and lift the brake pause it caused. The root supervisor stays
the daemon's (unchanged).

Interface (map.md section D): decision key ``"heal": [{"subfleet", "reopen"}, {"subfleet", "slot", "reopen"},
{"slot", "reopen"}, {"clear_wall", "in"}]``; notice lines ``[sub-fleet supervisor held]``, ``[sub-fleet supervisor
dead]``, ``[sub-fleet walled]``, ``[slot down]``, ``[sub-fleet not up]``, ``[sub-fleet recovered]`` /
``[slot recovered]``, ``[heal result]``.

F1-F5 run real fleets: ``fleet.py start``, real daemon processes (root and sub-fleets), private Redis per fleet,
the fake terminal server (tests/fake_fleetmux.py) and scripted agents (tests/parent_heals_actor.py) in an isolated
``/tmp/fmxd-*`` world (tests/parent_heals_kit.py). The level-2 test runs in process (tests/test_subfleet.py's tree).

Run only these: PATH=/opt/homebrew/opt/redis/bin:$PATH python -m pytest -q tests/test_parent_heals_child.py
"""
import json
import re
import shutil
import time
from pathlib import Path

import pytest

import parent_heals_kit as kit
from parent_heals_kit import wait

ROOT_SUP = '/agents/supervisor'  # the root supervisor's pane cwd (<root run>/agents/supervisor)
ROOT_TASK = {'goal': 'Write one file and report it', 'hours': 1,
             'artifacts': [{'path': 'worker-1/step.txt', 'contains': ['[scripted]']}]}
JOB = {'goal': 'Write two files and report them',
       'artifacts': [{'path': w + '/step.txt', 'contains': ['[scripted]']} for w in ('worker-1', 'worker-2')]}


def need_programs():
    for name in ('redis-server', 'tmux', 'git'):
        if not shutil.which(name):
            pytest.fail(name + ' is needed (PATH=/opt/homebrew/opt/redis/bin:$PATH)')


@pytest.fixture
def world():
    need_programs()
    w = kit.World()
    try:
        yield w
    finally:
        left = w.close()
        assert not left, 'left running after clean-up: ' + '; '.join(left)


def heal_items(text):
    """The ``"heal": [...]`` list a notice offers to copy, or None."""
    m = kit_heal_line(text)
    return json.loads(m) if m else None


def kit_heal_line(text):
    m = re.search(r'"heal":\s*(\[.*?\])\s*(?:$|\n)', text, re.S)
    return m.group(1) if m else None


def trips(w, cfg=None):
    return w.events(cfg, name='brake.tripped')


def child_of(w, timeout=90):
    kids = wait(lambda: w.children(), timeout)
    assert kids, 'no sub-fleet was started under ' + str(w.cfg['run'])
    return kids[0]


# ------------------------------------------------------------------ F1
def test_dead_named_supervisor_slot_is_reported_and_reopened_by_parent(world):
    """brief 5.1 (incident 1): the named sub-fleet alpha's supervisor slot (worker-2) died at startup; an external
    subfleet.up arrives. The root supervisor is told at once -- even though that startup failure paused the root
    (ruling G6) -- with a copy-paste heal; its heal reopens the slot, lifts that pause, and alpha comes up. Then
    (ruling G5) alpha's supervisor slot fails while a request waits in alpha: reopened by the parent, the request
    survives in alpha's Redis."""
    w = world
    kit.roster(w.root / 'alpha.yaml', ['worker-1'])
    (w.root / 'alpha-sup.md').write_text('Supervisor of the alpha sub-fleet (scripted test).\n')
    kit.roster(w.root / 'root.yaml', ['worker-1', 'worker-2'], [
        {'name': 'alpha', 'supervisor_slot': 'worker-2', 'roster': 'alpha.yaml', 'supervisor_role': 'alpha-sup.md',
         'autostart': False}])
    w.cue(match='/workspace/worker-2', when='startup', do='refuse_ready', times=2)  # READY ask + the one nudge
    w.start('service', extra=['--roster', str(w.root / 'root.yaml')])
    assert wait(lambda: w.agent('worker-2').get('state') == 'dead', 90), w.agent('worker-2')
    paused = w.service()
    up = w.request({'id': 'up-1', 'op': 'subfleet.up', 'name': 'alpha', 'reason': 'session opens'})
    assert up.returncode == 0, up.stdout + up.stderr

    told = w.wait_text(ROOT_SUP, '[slot down]', 60)
    assert told, ('the root supervisor was never told that worker-2 (supervisor slot of sub-fleet alpha) is dead; '
                  'root fx:service at the time: ' + json.dumps(paused) + '; texts it got: '
                  + json.dumps([t[:160] for t in w.received(ROOT_SUP, '')]))
    assert 'worker-2' in told[0] and 'alpha' in told[0] and 'dead' in told[0], told[0]
    assert heal_items(told[0]) and heal_items(told[0])[0].get('slot') == 'worker-2', told[0]

    # the scripted supervisor copies the heal line: the slot gets a new generation and is READY
    assert wait(lambda: w.agent('worker-2').get('state') == 'idle' and int(w.agent('worker-2').get('generation')
                                                                             or 0) >= 1, 120), w.agent('worker-2')
    assert w.wait_text(ROOT_SUP, '[heal result]', 60), 'no [heal result] for the slot reopen'
    assert 'done' in w.received(ROOT_SUP, '[heal result]')[0]
    if paused.get('paused') == '1':  # G6: the pause the slot's startup failure set is lifted by the heal
        assert wait(lambda: w.service().get('paused') != '1', 60), w.service()
    assert w.wait_text(ROOT_SUP, 'wants sub-fleet alpha up', 60), 'the subfleet.up ask never reached the supervisor'
    assert wait(lambda: _named(w, 'alpha').get('state') == 'up', 180), _named(w, 'alpha')

    # G5: a request waits in alpha; its supervisor (the parent's worker-2) fails; the parent reopens it
    child = Path(_named(w, 'alpha')['run_dir']) / 'config.json'
    w.cue(match='/workspace/worker-2', when='work', needle='quote AAPL', do='stop_failure', times=5)
    sent = w.fleet('request', '--config', str(child), '-', stdin=json.dumps(
        {'id': 'tg-req-1', 'payload': {'ask': 'quote AAPL'}, 'reply': {'required': ['quote']}}))
    assert sent.returncode == 0, sent.stdout + sent.stderr
    note = w.wait_text(ROOT_SUP, '[sub-fleet supervisor', 120)
    assert note and 'alpha' in note[0] and heal_items(note[0]), 'no notice about alpha\'s supervisor'
    # (decisions D9) tg-req-1 failed by the section 24 service StopFailure rule (published, its fx:req deleted);
    # tg-req-2 is sent while alpha's supervisor is held: it waits unanswered across the reopen
    sent = w.fleet('request', '--config', str(child), '-', stdin=json.dumps(
        {'id': 'tg-req-2', 'payload': {'ask': 'quote MSFT'}, 'reply': {'required': ['quote']}}))
    assert sent.returncode == 0, sent.stdout + sent.stderr
    assert w.wait_text(ROOT_SUP, '[sub-fleet recovered] alpha', 240), 'alpha was not reopened'
    from fleetmux import fleet as fleet_mod
    cfg = json.loads(child.read_text())

    def child_read(fn):
        r = fleet_mod.connect(cfg, socket_timeout=5)
        try:
            return fn(r)
        finally:
            r.close()
    assert child_read(lambda r: r.hgetall('fx:req:tg-req-2') or r.get('fx:result:tg-req-2')), (
        'the waiting request tg-req-2 did not survive the reopen of alpha')
    result2 = wait(lambda: child_read(lambda r: r.get('fx:result:tg-req-2')), 180)
    assert result2, ('the reopened alpha supervisor never handled tg-req-2 (still '
                     + json.dumps(child_read(lambda r: r.hgetall('fx:req:tg-req-2'))) + ')')
    assert json.loads(result2).get('status') == 'ok', result2  # answered by the new session, not failed
    assert child_read(lambda r: r.get('fx:result:tg-req-1')), 'the result of tg-req-1 did not survive the reopen'


def _named(w, name):
    from fleetmux import fleet
    r = fleet.connect(w.cfg, socket_timeout=5)
    try:
        return r.hgetall('fx:subfleet:' + name)
    finally:
        r.close()


# ------------------------------------------------------------------ F2
def test_job_worker_wall_parent_clears_wall_and_reopens_child_resumes(world):
    """brief 5.2 (incident 2): a job sub-fleet's worker-1 hits an account wall; the child brakes. The root
    supervisor gets [sub-fleet walled] with a heal that clears the wall record in the child and reopens that worker
    at the chain head; the child's brake pause is lifted by the program, the job goes on from where it stood (the
    block worker-2 already finished is not dispatched again) and completes."""
    w = world
    w.cue(match='/sub/', when='work', needle='.2]', do='wall')  # the child's worker-1 on block M<n>.2
    w.start('service')
    job = w.task(JOB)
    assert job.get('status') == 'accepted', job
    child = child_of(w)
    first = wait(lambda: trips(w, child), 120)
    assert first, 'the child never braked on its worker-1 wall'

    told = w.wait_text(ROOT_SUP, '[sub-fleet walled]', 60)
    assert told, ('the root supervisor was not told that job sub-fleet ' + child['task_id'] + ' braked on a wall '
                  '(child brake: ' + str(first[0].get('reason', ''))[:200] + '); texts it got: '
                  + json.dumps([t[:160] for t in w.received(ROOT_SUP, '')]))
    items = heal_items(told[0])
    assert items and any('clear_wall' in x for x in items) and any(x.get('slot') for x in items), told[0]

    assert w.wait_text(ROOT_SUP, '[heal result]', 120), 'no [heal result]'
    assert wait(lambda: w.service(child).get('paused') != '1', 120), w.service(child)
    root_braked = trips(w)  # section 67: the notices quote no CLI wall line, so the root never brakes on them
    assert not root_braked and w.service().get('paused') != '1', (
        'the root braked itself (' + str(len(root_braked)) + ' trip(s), agent '
        + str(root_braked[0].get('agent') if root_braked else '-') + ')')
    done = wait(lambda: w.received(ROOT_SUP, '[Sub-fleet ' + child['task_id'] + ' completed]')
                or w.received(ROOT_SUP, 'job ' + str(job.get('job_id')) + ' ok'), 240)
    assert done, 'the job did not complete after the heal'
    again = [t for t in w.received('/sub/', '[' + child['task_id'] + '.1]')]
    assert len(again) == 1, 'block ' + child['task_id'] + '.1 (done before the wall) was dispatched again'


# ------------------------------------------------------------------ F3
def test_adopted_subfleet_supervisor_wall_parent_reopens_it(world):
    """brief 5.3 (incident 3): the adopted supervisor of a job sub-fleet (a root worker's pane) hits an account
    wall; the child stops it (no automatic switch, 4.4) and the root supervisor gets [sub-fleet walled] with a heal
    that reopens that sub-fleet's supervisor; the parent's slot gets a new generation, the child re-adopts it, its
    pause is lifted and the job completes."""
    w = world
    w.cue(match='/workspace/worker-', needle='Goal: Sub-fleet', when='work', do='wall')  # the adopted supervisor
    w.start('service')
    job = w.task(JOB)
    assert job.get('status') == 'accepted', job
    child = child_of(w)
    first = wait(lambda: trips(w, child), 120)
    assert first and 'supervisor' in str(first[0].get('reason', '')), 'the child did not brake on its supervisor wall'
    slot = json.loads(Path(child['config']).read_text())['adopt']['slot']
    gen = int(w.agent(slot).get('generation') or 0)

    told = w.wait_text(ROOT_SUP, '[sub-fleet walled]', 60)
    assert told, ('the root supervisor was not told that the supervisor of sub-fleet ' + child['task_id']
                  + ' braked on a wall (child brake: ' + str(first[0].get('reason', ''))[:200] + ')')
    items = heal_items(told[0])
    assert items and items[-1].get('subfleet') == child['task_id'] and items[-1].get('reopen'), told[0]

    assert wait(lambda: int(w.agent(slot).get('generation') or 0) > gen, 120), w.agent(slot)
    assert wait(lambda: w.service(child).get('paused') != '1', 120), w.service(child)
    done = wait(lambda: w.received(ROOT_SUP, '[Sub-fleet ' + child['task_id'] + ' completed]')
                or w.received(ROOT_SUP, 'job ' + str(job.get('job_id')) + ' ok'), 240)
    assert done, 'the job did not complete after its supervisor was reopened'


# ------------------------------------------------------------------ F4
def test_stale_wall_text_after_resume_does_not_trip_brake(world):
    """brief 5.4 (incident 4): worker-1 sits on a wall line; the brake trips once. After the operator's resume the
    same old line still on the screen is not a new wall (4.3). A wall line printed after newer output is new, even
    with the same text (ruling G7)."""
    w = world
    w.cue(match='/workspace/worker-1', when='work', do='wall')
    w.start('task')
    w.task(ROOT_TASK)
    assert wait(lambda: trips(w), 90), 'worker-1 wall did not trip the brake'
    assert len(trips(w)) == 1
    done = w.resume()
    assert done.returncode == 0, done.stdout + done.stderr
    time.sleep(15)
    stale = trips(w)
    assert len(stale) == 1, ('the brake tripped again on the old wall line after the resume (' + str(len(stale))
                             + ' trips): ' + json.dumps([str(x.get('reason', ''))[:160] for x in stale]))
    w.signal(match='/workspace/worker-1', do='rewall')  # newer output, then the same wall text again
    assert wait(lambda: len(trips(w)) == 2, 60), 'a wall line printed after newer output did not trip the brake'


# ------------------------------------------------------------------ F5
def test_root_supervisor_still_regenerated_by_daemon(world):
    """brief 5.5 / 4.4: the root supervisor stays the daemon's. Its turn ends on an API error with max-attempts 1:
    the daemon regenerates it (supervisor_regen, next generation), the message is delivered again and the task
    completes. Pinned: passes before and after the change."""
    w = world
    w.cue(match=ROOT_SUP, when='work', needle='[tree] rev', do='stop_failure')
    w.start('task', extra=['--max-attempts', '1'])
    w.task(ROOT_TASK)
    assert wait(lambda: w.events(name='supervisor_regen.started'), 180), 'the daemon did not regenerate the root supervisor'
    assert wait(lambda: int(w.agent('supervisor').get('generation') or 0) >= 1, 60)
    result = Path(w.cfg['run']) / 'result.json'
    assert wait(lambda: result.exists() and json.loads(result.read_text()).get('status') == 'completed', 180), (
        result.read_text() if result.exists() else 'no result.json')
    assert not w.received('', '[heal'), 'a heal notice reached a root supervisor about itself'


# ------------------------------------------------------------------ level 2 (in process)
def test_level2_parent_supervisor_gets_notice_and_its_heal_runs(tmp_path, monkeypatch):
    """Supervisor ruling (brief 2, any level): M0 is a sub-fleet whose own worker-1 supervises M0.1. M0.1's
    supervisor is held and its daemon cannot recover it: M0's supervisor (a level-2 parent, itself an adopted
    sub-fleet supervisor) gets [sub-fleet supervisor held] with a heal line, and the heal it sends is executed by
    M0's daemon, which answers [heal result]."""
    from test_subfleet import child_daemon, hook, no_launch, spawn_m0, start_tree, step, stop_child_like_daemon
    from fleetmux import fleet, subfleet
    gen = start_tree(tmp_path, monkeypatch, limits={'max_depth': 3})
    t = next(gen)
    try:
        rec, _ = spawn_m0(t)
        cf = child_daemon(t, rec)
        for w in cf.workers:
            hook(t, cf, w, 'READY', prompt='startup')
        step(t)
        rec2 = subfleet.spawn_child(cf, 'worker-1', 'M0.1', 'Check one source', 1, launch=no_launch)
        gf = child_daemon(t, rec2)
        for w in gf.workers:
            hook(t, gf, w, 'READY', prompt='startup')
        step(t)
        gf.v2.held_escalate('supervisor', 'API error retries used up (2 of max-attempts 2): overloaded',
                            {'reason': 'stop_failure_exhausted'})
        cf.tick()
        step(t)
        texts = _queued(cf, 'supervisor')
        notice = [x for x in texts if '[sub-fleet supervisor held]' in x and 'M0.1' in x]
        assert notice, ('the level-2 parent (M0) supervisor got no [sub-fleet supervisor held] notice about M0.1; '
                        'it got: ' + json.dumps([x[:200] for x in texts]))
        items = heal_items(notice[0])
        assert items and items[0].get('subfleet') == 'M0.1', notice[0]
        decision = {'status': 'RUNNING', 'completion': 10, 'reason': 'reopen M0.1', 'dispatch': [], 'heal': items}
        cf.on_supervisor(json.dumps(decision))  # M0's supervisor answers the notice (its daemon's handler)
        for _ in range(8):  # M0's daemon runs the heal; the in-process M0.1 daemon is driven like the real one
            cf.tick()
            step(t)
            if getattr(gf, 'explicit_stop', False) and not getattr(gf, 'stopped', False):
                stop_child_like_daemon(gf)  # M0.1 suspended in place (its daemon exits, its Redis AOF stays)
            if cf.agent('worker-1').get('state') == 'starting':
                hook(t, cf, 'worker-1', 'READY', prompt='startup')  # the slot's new session says READY
        result = [x for x in _queued(cf, 'supervisor') if '[heal result] M0.1' in x]
        assert result and 'done' in result[0], json.dumps([x[:200] for x in _queued(cf, 'supervisor')])
        assert fleet.load(Path(rec2['run']) / 'config.json')['adopt']['generation'] > rec2['generation']
    finally:
        for _ in gen:
            pass


def _queued(f, slot):
    """Texts of every message the v2 bus took for ``slot`` (queued, delivered or still pending)."""
    out = []
    for _, row in f.r.xrange(f.v2.bus.audit_key):
        if row.get('event') == 'queued' and row.get('slot') == slot and row.get('message_id'):
            try:
                out.append(f.v2.bus.message(row['message_id'])['body']['text'])
            except Exception:  # noqa: BLE001  (expired message)
                continue
    return out


# ------------------------------------------------------------------ delivered wall text (design section 67)
def mid_row_wall(row, lead=20):
    """A wall pattern of the wall table matching ``row`` after at least ``lead`` characters of the row's text."""
    from fleetmux import brake
    table = brake.wall_table()
    for p in table['account'] + table['model_pool']:
        m = p.search(row)
        if m and len(row[:m.start()].strip()) >= lead:
            return True
    return False


def test_root_delivery_quoting_a_wall_line_does_not_brake_the_root(world):
    """A delivery to the root supervisor quotes a raw wall line in the middle of a row; the root's scripted turn
    stays in flight across two watch intervals, so its watchdog reads that screen at least twice (it reads a slot's
    screen only while a delivery is in flight). The root does not brake: delivered text is not the CLI's own line."""
    import parent_heals_actor
    w = world
    hold = 14
    w.cue(match=ROOT_SUP, when='work', needle='quoted_pane_text_follows', do='hold', seconds=hold)
    w.start('service')
    note = '\n[quoted_pane_text_follows]' + parent_heals_actor.WALL_LINE + ' -- for the record only.'
    sent = w.request({'id': 'q-wall', 'instruction': note, 'payload': {'ask': 'nothing to route'},
                      'reply': {'required': ['note']}})
    assert sent.returncode == 0, sent.stdout[-500:] + sent.stderr[-500:]
    assert w.wait_text(ROOT_SUP, 'quoted_pane_text_follows', 120), 'the request never reached the root supervisor'
    shown = wait(lambda: w.turns(ROOT_SUP, 'held_screen'), 60)
    assert shown, 'the root actor did not log the screen it held'
    assert any(mid_row_wall(r) for r in shown[0]['rows']), 'the quoted wall line is not drawn mid-row on the root pane'
    began = [x for x in w.turns(ROOT_SUP) if 'quoted_pane_text_follows' in str(x.get('text', ''))][0]['at']
    ended = wait(lambda: [x for x in w.turns(ROOT_SUP, 'turn.reply') if x['at'] > began], hold + 60)
    assert ended, 'the held root turn never ended'
    assert ended[0]['at'] - began >= 10, 'the root turn was not in flight across two watch intervals'
    time.sleep(2)
    braked = trips(w)
    assert not braked, ('the root braked on a delivered wall line (' + str(len(braked)) + ' trip(s), agent '
                        + str(braked[0].get('agent')) + ')')
    assert w.service().get('paused') != '1', 'the root service is paused'


def test_no_parent_notice_quotes_a_cli_wall_line(tmp_path, monkeypatch):
    """Notices to a parent supervisor name the wall (slot, scope, cli:model, reset time) and never quote the CLI's
    line: every builder gets a brake reason made by Fleet.on_wall from the raw line, or a legacy text quoting it."""
    import parent_heals_actor
    from test_brake import make
    from fleetmux import brake, heal, named_subfleet, notify_route, subfleet
    raw = parent_heals_actor.WALL_LINE
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='opus')
    if not hasattr(f.r, 'hkeys'):  # the in-memory Redis of test_fleet has no HKEYS (heal.not_up reads one)
        monkeypatch.setattr(type(f.r), 'hkeys', lambda self, key: list(self.hgetall(key)), raising=False)
    sent = []
    monkeypatch.setattr(f, 'queue', lambda slot, text, *a, **kw: sent.append(('queue', text)))
    monkeypatch.setattr(notify_route, 'unmanaged_send', lambda _f, kind, text: sent.append((kind, text)))
    reason = f.on_wall('worker-1', 'thinking\n' + raw + '\n', 'watch')['reason']
    phrase = re.compile(r'hit a account wall \(claude:[\w.\-]+, resets 3pm \(Europe/Paris\)\)')
    assert phrase.search(reason) and reason.startswith('worker-1 '), 'the brake reason does not name the wall'
    legacy = 'worker-1 hit a account wall ("' + raw + '"); wait for the reset'
    combos = [(s, sl) for s in ('walled', 'held', 'dead', 'switch_failed') for sl in ('supervisor', 'worker-1')]
    for i, (state, slot) in enumerate(combos):
        heal.describe(f, 'M%d' % i, '/run/sub/M%d' % i, slot, slot, state, reason, walls=['claude:opus'])
    heal.describe(f, 'M20', '/run/sub/M20', 'supervisor', 'supervisor', 'held',
                  'API error (rate_limit_error ' + raw + '); its request failed')
    heal.describe(f, 'M21', '/run/sub/M21', 'worker-1', 'worker-1', 'walled', legacy, walls=['claude:opus'])
    held = tmp_path / 'held-x.json'
    held.write_text(json.dumps({'task': 'M22', 'origin_run': '/run/sub/M22', 'slot': 'worker-1', 'label': 'worker-1',
                                'text': 'API error retries used up (2 of max-attempts 2): rate_limit_error ' + raw}))
    f.held_notice(held)
    d = tmp_path / 'sub' / 'M23'
    d.mkdir(parents=True)
    rec = {'task_id': 'M23', 'launched': str(time.time() - 4000)}
    facts = {'beat': time.time(), 'paused': True, 'paused_at': time.time() - 2000, 'paused_reason': 'brake: ' + legacy,
             'supervisor_held': False, 'task': 'running', 'progress_at': time.time(), 'progress_event': 'x',
             'held': [], 'pending_events': 0}
    subfleet.report_stuck(f, d, rec, facts, subfleet.stuck_reasons(f, d, rec, facts, 600))
    heal.result(f, 'M24 / worker-1', True, 'worker-1.1 starts on claude:opus')
    heal.not_up(f, 'tg', 'r-1', named_subfleet.CHAIN_WAIT + 'no usable entry now: every entry is walled')
    heal.tell(f, '[Sub-fleet M25 failed] worker-1 ran sub-fleet M25 (attempt 1): brake: ' + legacy + '. worker-1 is '
              'a worker again.')
    assert len(sent) >= len(combos) + 7, 'not every builder sent its notice (' + str(len(sent)) + ')'
    for n, (kind, text) in enumerate([('reason', reason)] + sent):
        assert brake.classify_wall(text) is None and brake.screen_wall(text) is None, (
            'notice #' + str(n) + ' (' + kind + ') still quotes a CLI wall line')
    walled = [t for _, t in sent if t.startswith(('[sub-fleet walled] sub-fleet M0 ', '[sub-fleet walled] sub-fleet M1 '))]
    assert len(walled) == 2 and all(phrase.search(t) for t in walled), 'a walled notice does not name the wall'
