"""E2: the production studio-ops roster as one real process tree on ``--backend fleetmux`` (brief-m4 §5 item 1;
design-1/m4-design.md v0.7 §8.4; e2e-1 plan r3 §5 steps 0-10). The acceptance evidence of brief §5 item 1.

Real processes only: ``tests/m4e/launch.py start`` (``fleet.main`` with the orphan-Redis sweep scoped to this
world, plan F1), the root and every child ``fleet.py daemon`` as their own processes, real ``redis-server``
instances, this worktree's ``fleetmux`` binary (``FLEETMUX_REAL_BIN``) with real PTYs, the native fake
``claude`` / ``codex`` (tests/m4e/fake_cli.py) in them. Typed delivery goes through the real
``claude_input.send`` screen checks, hook delivery through the real Stop-hook commands, Codex through the real
``codex queue`` path. The three daemon deaths are SIGKILLs of the real mayfly child daemon, verified against the
world's ledger first. No product constant is shortened and nothing is monkeypatched in the product processes.

The world lives under ``~/.m4e/fm4-e2e/<id>/`` and is kept (evidence in ``ev/``). Missing prerequisites fail;
only ``FLEETMUX_GATE`` below m4 skips (``@milestone(4)``). Before M4 is implemented this test is red at step 1:
``start`` refuses a roster with ``subfleets:`` on the fleetmux backend (``subfleet.backend_problem``).
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _fleetmux_milestone import milestone  # noqa: E402
from fleetmux import fleet, named_subfleet, runtime_v2  # noqa: E402
from m4e import world as m4world  # noqa: E402
from m4e.evidence import Evidence  # noqa: E402
from m4e.inventory import Ledger, ctl, daemon_of, ps, read_json  # noqa: E402

pytestmark = milestone(4)
HOOK_CHANNEL = True  # every hook runs as the product's real subprocess: no in-process stand-in
HARD_LIMIT_S = 1800  # plan §8: the whole run; a timeout is red, evidence kept, then the verified fallback
START_TIMEOUT_S = 180 + 120 + 120  # profile_ready_timeout + LAUNCH_GRACE + margin (plan F12)
AUTOSTART_LIMIT_S = 180 + 120 + 60
REVIVE_GAP_S = 125  # subfleet.tick judges a daemon gone only LAUNCH_GRACE (120 s) after its launch (plan F13)
STOP_LIMIT_S = 3 * 180 + 60  # STOP_WAIT per child + margin
CLOSER_LIMIT_S = 240
NAMED = ('memx', 'tgog', 'mayfly')
SLOTS = {'memx': 'memx-ops-1', 'tgog': 'tgog-ops-1', 'mayfly': 'mayfly-ops-1'}
CHILD_KEYS = ('claude', 'worker_model', 'stall_timeout', 'stall_window', 'request_timeout', 'max_busy',
              'clear_after_result', 'clear_timeout', 'claude_hook_delivery', 'worker_domains')
# drain_s is not a child-config key (subfleet.NAMED_OVERRIDES has none, on either backend): the parent reads it from its
# own declaration, see assert_declared_drain_s (brief-m4 §7.2 test-contract).
ROOT_PAGE = ''


class Deadline(Exception):
    pass


# ----------------------------------------------------------------------------------------------- helpers
def wait_for(what, predicate, limit, every=1.0):
    deadline = time.monotonic() + limit
    last = None
    while True:
        last = predicate()
        if last:
            return last
        if time.monotonic() >= deadline:
            raise AssertionError('timed out after %.0f s waiting for %s (last: %r)' % (limit, what, last))
        time.sleep(every)


def redis_at(sock):
    import redis
    return redis.Redis(unix_socket_path=str(sock), decode_responses=True, socket_connect_timeout=3, socket_timeout=10)


def pings(sock):
    try:
        return bool(sock) and redis_at(sock).ping()
    except Exception:
        return False


class Tree:
    """Read-only views of the running tree (config, Redis, audit, fleetmux control socket)."""

    def __init__(self, world, cfg_path):
        self.w = world
        self.cfg_path = Path(cfg_path)
        self.cfg = json.loads(self.cfg_path.read_text())
        self.run = Path(self.cfg['run'])
        self.r = redis_at(self.cfg['redis_socket'])

    def hash(self, name):
        return self.r.hgetall(named_subfleet.HASH + name)

    def agent(self, slot, r=None):
        return (r or self.r).hgetall(fleet.key('agent', slot))

    def audit(self, cfg=None, r=None):
        cfg = cfg or self.cfg
        r = r or self.r
        key = runtime_v2.bus_for(cfg, r).audit_key
        return [fields for _, fields in r.xrange(key)]

    def events(self, event, cfg=None, r=None, **match):
        return [e for e in self.audit(cfg, r) if e.get('event') == event
                and all(str(e.get(k)) == str(v) for k, v in match.items())]

    def child(self, name):
        h = self.hash(name)
        cfg = json.loads((Path(h['run_dir']) / 'config.json').read_text())
        return cfg, redis_at(cfg['redis_socket'])

    def ctl_sock(self):
        socks = sorted(self.w.s.glob('fleetmux/*/ctl.sock'))
        assert len(socks) == 1, 'route (a): one fleetmux server for the whole tree, found ' + repr(socks)
        return socks[0]

    def pages(self):
        hello, listing = ctl(self.ctl_sock(), {'cmd': 'hello', 'protocol': 1}, {'cmd': 'page.list'})
        assert hello.get('ok') and hello.get('pages') == 1, 'server has no pages capability: ' + json.dumps(hello)
        assert listing.get('ok'), 'page.list failed: ' + json.dumps(listing)
        return {p['page']: p for p in listing['pages']}

    def sidebar(self):
        hello, got = ctl(self.ctl_sock(), {'cmd': 'hello', 'protocol': 1}, {'cmd': 'kv.get', 'key': 'sidebar'})
        assert got.get('ok'), got
        return json.loads(got.get('value') or '{}')

    def card(self, name):
        cards = [c for c in self.sidebar().get('fleets', []) if c.get('page') == name or c.get('id') == name]
        return cards[0] if cards else None

    def panes(self):
        rows = []
        for path in self.w.s.glob('fleetmux/*/panes.json'):
            data = read_json(path) or {}
            rows += data.get('panes', []) if isinstance(data, dict) else data
        return rows

    def request(self, n, spec, cfg_path=None):
        path = self.w.ev / ('req-%02d-%s.json' % (n, spec['id']))
        path.write_text(json.dumps(spec, ensure_ascii=False))
        done = self.w.fleet('request', str(path), '--config', str(cfg_path or self.cfg_path))
        assert done.returncode == 0, 'fleet request %s failed: %s' % (spec['id'], done.stdout + done.stderr)
        return done.stdout

    def result(self, rid, r=None, limit=300):
        raw = wait_for('fx:result:' + rid, lambda: (r or self.r).get(fleet.RESULT + rid), limit)
        return json.loads(raw)


MAYFLY_WORKERS = ('exit-1', 'reentry-1')
MAYFLY_PROFILE = 'claude-opus'  # mayfly_roster.yaml profile of exit-1 / reentry-1: the target of their first switch


def rid_needle(rid):
    """A request's own id as it reaches the worker. runtime_v2.queue strips the `[Request <id>]` head on purpose
    (runtime_v2.py ~516-519, "transport labels are metadata"), so the E2 puts `m4e_rid` into the payload and matches
    it as fleet.request_message renders the payload (json.dumps(payload, ensure_ascii=False, indent=1))."""
    return '"m4e_rid": ' + json.dumps(rid, ensure_ascii=False)


def fake_cli_logs(world):
    out = []
    for p in sorted(world.ev.glob('fake-cli/claude-*.jsonl')):
        rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
        if rows:
            out.append((p, rows))
    return out


def slot_process(tree, slot):
    """The mayfly worker `slot` on its *current* generation, or None while it is not idle there yet: the child
    agent's generation / profile, and the one live fake-CLI process of that generation (cwd = the slot's
    workspace, argv carries that generation's `--settings <workspace>/.fleet-settings-g<N>.json`)."""
    child, cr = tree.child('mayfly')
    agent = tree.agent(slot, r=cr)
    gen = agent.get('generation')
    if gen in (None, '') or agent.get('state') != 'idle':
        return None
    ws = os.path.realpath(Path(tree.hash('mayfly')['run_dir']) / 'workspace' / slot)
    settings = os.path.join(ws, '.fleet-settings-g%s.json' % gen)
    procs = ps()
    live = [(p, rows) for p, rows in fake_cli_logs(tree.w)
            if os.path.realpath(rows[0].get('cwd') or '/') == ws
            and settings in [os.path.realpath(a) if a.startswith('/') else a for a in rows[0].get('argv') or []]
            and rows[0].get('pid') in procs and not any(r.get('event') == 'claude.exit' for r in rows)]
    if len(live) != 1:
        return None
    path, rows = live[0]
    return {'slot': slot, 'generation': gen, 'label': agent.get('label'),
            'profile': agent.get('current_profile') or agent.get('runtime_profile'), 'pid': rows[0]['pid'],
            'log': str(path), 'pane': agent.get('pane')}


def mayfly_workers(ctx, first_switch):
    """brief-m4 §7.3: both mayfly workers alive and idle on MAYFLY_PROFILE, bound to their current generation.
    `first_switch`: after mayfly's first up, wait for each worker's `roster.profile_switched` and require the idle
    generation to be the switched one. After a relaunch (`--resume`) the child may come up directly on the roster
    profile, so no generation number is assumed there: whatever generation is current is bound."""
    tree = ctx.tree
    child, cr = tree.child('mayfly')
    out = {}
    for slot in MAYFLY_WORKERS:
        want = None
        if first_switch:
            switched = wait_for(slot + ' first roster-profile switch', lambda: tree.events(
                'roster.profile_switched', cfg=child, r=cr, agent=slot), AUTOSTART_LIMIT_S, 1)
            want = str(switched[0].get('generation'))
            assert want not in ('', 'None', '0'), switched
        out[slot] = wait_for(slot + ' idle and alive on its current generation (%s)' % MAYFLY_PROFILE, lambda: (
            lambda p: p if p and p['profile'] == MAYFLY_PROFILE and (want is None or p['generation'] == want)
            else None)(slot_process(tree, slot)), AUTOSTART_LIMIT_S, 1)
    ctx.ledger.refresh()
    ledger = {e['pid'] for e in ctx.ledger.alive()}
    assert all(p['pid'] in ledger for p in out.values()), ('mayfly worker fake CLI not in the ledger', out)
    return out


def mayfly_daemon(tree, ledger):
    """Plan §6.3: the mayfly page's `daemon` pane, its group's `fleet.py daemon <sub cfg>`, holding the lock."""
    sub = tree.run / 'sub' / 'mayfly'
    rows = [p for p in tree.panes() if p.get('type') == 'daemon' and p.get('page') == 'mayfly']
    assert len(rows) == 1, 'mayfly daemon pane (type daemon, page mayfly) in panes.json: ' + json.dumps(rows)
    procs = ledger.refresh()
    proc = daemon_of(procs, sub / 'config.json')
    assert proc and proc['pgid'] == int(rows[0]['pgid']), ('mayfly daemon process not in its pane group: '
                                                           + json.dumps([proc, rows[0]], default=str))
    assert fleet.daemon_running(sub), 'mayfly daemon does not hold ' + str(sub / 'daemon.lock')
    return proc


def kill_daemon(ctx, n, why):
    tree, ledger = ctx.tree, ctx.ledger
    proc = mayfly_daemon(tree, ledger)
    try:
        ctx.kills.append(ledger.signal(proc['pid'], signal.SIGKILL, why))
    except ProcessLookupError:  # exited by itself (e.g. BusError after its Redis went away): still a death
        ctx.kills.append({'pid': proc['pid'], 'why': why, 'exited_before_kill': True})
    def gone():  # one ps snapshot per poll: the pid can vanish between two reads
        now = ps().get(proc['pid'])
        return now is None or now['start'] != proc['start']
    wait_for('killed mayfly daemon %d gone' % proc['pid'], gone, 30)
    ctx.ev.step(70 + n, 'kill-%d' % n, ctx.kills[-1])
    return proc


def last_launch_ms(tree):
    times = [int(e['at']) for e in tree.events('subfleet.autostart', name='mayfly')]
    h = tree.hash('mayfly')
    for k in ('started_at', 'up_at'):
        try:
            times.append(int(float(h.get(k) or 0) * 1000))
        except ValueError:
            pass
    return max(times) if times else 0


def wait_revive_gap(tree):
    gap = REVIVE_GAP_S - (time.time() - last_launch_ms(tree) / 1000)
    if gap > 0:
        time.sleep(gap)


# ----------------------------------------------------------------------------------------------- steps
def step0_preconditions(ctx):
    w = ctx.world
    facts = w.preconditions()
    versions = {}
    for argv in ([str(m4world.FLEETMUX_REAL), '--version'], ['redis-server', '--version'], ['tmux', '-V'],
                 [m4world.PYTHON, '--version'], ['git', '-C', str(m4world.REPO), 'rev-parse', 'HEAD']):
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=w.env(marked=False))
        versions[argv[0] if argv[0] != 'git' else 'repo HEAD'] = (done.stdout or done.stderr).strip()
    ctx.ev.step(0, 'env', {'world': str(w.dir), 'marker': w.marker, 'facts': facts, 'versions': versions,
                           'kit': w.pin, 'patch': w.patch, 'start_argv': w.start_argv(),
                           'env': {k: v for k, v in w.env().items() if k.startswith(('CMUX', 'HOME', 'PATH', 'M4E'))}})


def step1_start(ctx):
    """Red point today: `start` refuses a roster with `subfleets:` on fleetmux (subfleet.backend_problem)."""
    w = ctx.world
    done = subprocess.run(w.start_argv(), cwd=str(m4world.REPO), env=w.env(), capture_output=True, text=True,
                          timeout=START_TIMEOUT_S)
    ctx.ev.step(1, 'start', {'argv': w.start_argv(), 'rc': done.returncode, 'stdout': done.stdout, 'stderr': done.stderr})
    ctx.ledger.refresh()
    assert done.returncode == 0, ('start of the studio-ops tree on --backend fleetmux failed (rc %d); output:\n%s'
                                  % (done.returncode, (done.stdout + done.stderr)[-4000:]))
    cfgs = sorted(w.runs.glob('fleet-*/config.json'))
    assert len(cfgs) == 1, 'one run directory expected: ' + repr(cfgs)
    ctx.tree = Tree(w, cfgs[0])


def step2_main_fleet(ctx):
    tree, w = ctx.tree, ctx.world
    cfg = tree.cfg
    assert fleet.run_backend(cfg) == 'fleetmux' and cfg.get('lifecycle') == 'service', cfg
    assert sorted(e['name'] for e in cfg.get('subfleets') or []) == sorted(NAMED)
    assert (cfg.get('kit_fleet') or {}).get('roster_md5') == m4world.ROSTER_MD5, cfg.get('kit_fleet')
    names = ['supervisor'] + list(SLOTS.values())
    wait_for('supervisor and ops workers READY (idle)',
             lambda: all(tree.agent(n).get('state') in ('idle', 'delegated') for n in names), START_TIMEOUT_S, 2)
    server = [read_json(p) for p in w.s.glob('fleetmux/*/server.json')]
    assert len(server) == 1 and server[0].get('pages') == 1, 'route (a) marker server.json pages == 1: ' + repr(server)
    tmux = subprocess.run(['tmux', '-S', str(tree.run / 'tmux.sock'), 'list-sessions', '-F', '#{session_name}'],
                          capture_output=True, text=True, timeout=10)
    sessions = tmux.stdout.split()
    assert tmux.returncode == 0 and len(sessions) == 3, 'three ops workers in <run>/tmux.sock: ' + tmux.stdout + tmux.stderr
    ctx.ledger.refresh()
    ctx.ev.step(2, 'main-fleet', {'config': str(tree.cfg_path), 'agents': {n: tree.agent(n) for n in names},
                                  'server': server, 'tmux_sessions': sessions, 'pages': tree.pages()})


def roster_entries(tree, w):
    """{name: entry} of the roster's subfleets as an independent process parses them (roster.load_subfleets)."""
    probe = subprocess.run([m4world.PYTHON, '-c', 'import json, sys; from fleetmux import roster; '
                                                  'print(json.dumps(roster.load_subfleets(sys.argv[1])))',
                            str(tree.cfg.get('roster') or w.fleet_dir / 'roster.yaml')],
                           cwd=str(m4world.REPO), env=w.env(marked=False), capture_output=True, text=True, timeout=60)
    return {e['name']: e for e in json.loads(probe.stdout)}


def assert_declared_drain_s(tree, name, entry):
    """The drain limit of `name`'s down / reclaim is the parent's declared drain_s (brief-m4 §7.2).

    The parent daemon's reload_roster persists cfg['subfleets'] (fleet.py ~6094, persist_roster) and hands the same
    list to named_subfleet.declare (fleet.py ~6110); named_subfleet.tick reads the limit from that declaration:
    `float((decl.get(name) or {}).get('drain_s') or roster.DRAIN_S)` (named_subfleet.py ~366). So the declaration in
    the parent's config.json, as the product wrote it, must carry drain_s and equal the roster's (parsed by another
    process); a missing or zero drain_s would silently fall back to roster.DRAIN_S and fail here."""
    decl = {e['name']: e for e in json.loads(tree.cfg_path.read_text()).get('subfleets') or []}
    assert name in decl and 'drain_s' in decl[name], (name, decl.get(name))
    assert decl[name]['drain_s'] == entry['drain_s'], (name, decl[name]['drain_s'], entry['drain_s'])
    assert not isinstance(decl[name]['drain_s'], bool) and float(decl[name]['drain_s']) > 0, decl[name]
    return decl[name]['drain_s']


def step3_autostart(ctx):
    tree, w = ctx.tree, ctx.world
    entries = roster_entries(tree, w)
    out = {}
    for name in ('memx', 'mayfly'):
        h = wait_for(name + ' autostart up', lambda: (lambda h: h if h.get('state') == 'up' else None)(tree.hash(name)),
                     AUTOSTART_LIMIT_S, 2)
        assert h['slot'] == SLOTS[name] and Path(h['run_dir']) == tree.run / 'sub' / name, h
        assert h.get('up_at') and not h.get('crashloop') and pings(h['redis_socket']), h
        child, cr = tree.child(name)
        assert child.get('parent_config') and child.get('fleetmux_page') == name, child  # design §0 item 2
        assert child.get('fleetmux_root_session') == 'cmux-fleet', child
        for k in CHILD_KEYS:
            if k in entries[name]:
                assert child.get(k) == entries[name][k], (name, k, child.get(k), entries[name][k])
        drain_s = assert_declared_drain_s(tree, name, entries[name])
        slot_cwd = fleet.agent_cwd(tree.cfg, SLOTS[name])
        role = (slot_cwd / 'AGENTS.md').read_text()
        assert '## 4. Your role (supervisor)' in role and (tree.run / 'sub' / name / 'handover').is_dir(), name
        assert tree.agent(SLOTS[name]).get('state') == 'delegated'
        order = [e['event'] for e in tree.audit() if e.get('name') == name and e.get('event', '').startswith('subfleet.')]
        assert order.index('subfleet.declared') < order.index('subfleet.autostart') < order.index('subfleet.up'), order
        assert tree.events('subfleet.adopted', cfg=child, r=cr), name + ': child audit has no subfleet.adopted'
        out[name] = {'hash': h, 'child_config': {k: child.get(k) for k in CHILD_KEYS + ('fleetmux_page',)}, 'order': order,
                     'declared_drain_s': drain_s}
    memx, mr = tree.child('memx')
    tabs = tree.events('tab.started', cfg=memx, r=mr, tab='evermem-shell')
    assert tabs, 'memx: no tab.started for evermem-shell'
    stub = [json.loads(line) for line in w.stub_log.read_text().splitlines() if '"start"' in line]
    assert stub and stub[-1]['home'] == str(w.h) and stub[-1]['cwd'] == str(w.h / m4world.TAB_CWD), stub
    ctx.ledger.refresh()
    assert stub[-1]['pid'] in {e['pid'] for e in ctx.ledger.alive()}, 'tab stub not in the ledger'
    pages = tree.pages()
    assert {ROOT_PAGE, 'memx', 'mayfly'} <= set(pages) and pages[ROOT_PAGE]['state'] == 'live', pages
    for name in ('memx', 'mayfly'):
        assert pages[name]['state'] == 'live', pages
        assert any(p.get('page') == name and p.get('type') == 'daemon' for p in tree.panes()), name + ' daemon pane'
        card = tree.card(name)
        assert card and card['state'] == 'running' and card.get('line2') == '由 ' + SLOTS[name] + ' 委派', card
    ctx.mayfly_g = mayfly_workers(ctx, first_switch=True)  # §7.3: both alive and idle after the first switch
    status = w.fleet('status', '--config', str(tree.cfg_path))
    ctx.ev.step(3, 'autostart', dict(out, tab=tabs[-1], stub=stub[-1], pages=pages, sidebar=tree.sidebar(),
                                     status=status.stdout[-20000:], mayfly_workers=ctx.mayfly_g))


def step4_tgog_up(ctx):
    tree = ctx.tree
    tree.request(4, {'id': 'e2e-up-1', 'op': 'subfleet.up', 'name': 'tgog', 'reason': 'e2e'})
    res = tree.result('e2e-up-1', limit=AUTOSTART_LIMIT_S + 600)
    assert res.get('status') == 'ok', res
    h = tree.hash('tgog')
    assert h.get('state') == 'up' and pings(h.get('redis_socket')), h
    again = tree.request(4, {'id': 'e2e-up-1', 'op': 'subfleet.up', 'name': 'tgog', 'reason': 'e2e again'})
    tree.request(4, {'id': 'e2e-down-memx', 'op': 'subfleet.down', 'name': 'memx', 'reason': 'e2e'})
    refused = tree.result('e2e-down-memx', limit=60)
    assert refused.get('status') == 'rejected' and 'autostart' in json.dumps(refused), refused
    pages = tree.pages()
    assert 'tgog' in pages and pages['tgog']['state'] == 'live', pages
    ctx.ev.step(4, 'tgog-up', {'result': res, 'hash': h, 'resend': again, 'memx_down': refused, 'pages': pages})


def step5_mayfly_exit(ctx, rid='e2e-exit-1', n=5, expect=None):
    """A service request to mayfly exit-1. `expect` (a mayfly_workers() entry): the request must be answered by that
    bound generation / process, and exit-1 must still be idle on it afterwards (brief-m4 §7.3)."""
    tree = ctx.tree
    run_dir = Path(tree.hash('mayfly')['run_dir'])  # the review bridge's lookup (_subfleet_socket): fx:subfleet:mayfly
    child, cr = tree.child('mayfly')
    tree.request(n, {'id': rid, 'to': 'exit-1', 'payload': {'note': 'm4e end-to-end', 'm4e_rid': rid},
                     'reply': {'required': ['reply']}}, cfg_path=run_dir / 'config.json')
    res = tree.result(rid, r=cr, limit=600)
    assert res.get('status') == 'ok' and (res.get('result') or {}).get('reply') == 'E2E-OK ' + rid, res
    seen = [(str(p), t) for p, rows in fake_cli_logs(ctx.world) for t in rows
            if t.get('event') == 'turn.start' and rid_needle(rid) in t.get('text', '')]
    assert seen, rid + ' never reached a fake CLI turn (payload ' + rid_needle(rid) + ')'
    after = None
    if expect is not None:
        assert {p for p, _ in seen} == {expect['log']}, (rid + ' was not handled by the bound exit-1 generation',
                                                         expect, [p for p, _ in seen])
        after = wait_for('exit-1 idle again on the bound generation', lambda: (
            lambda p: p if p and (p['generation'], p['pid'], p['profile']) == (
                expect['generation'], expect['pid'], MAYFLY_PROFILE) else None)(slot_process(tree, 'exit-1')), 120, 1)
    ctx.ev.step(n, 'mayfly-' + rid, {'result': res, 'turns': [t for _, t in seen], 'logs': sorted({p for p, _ in seen}),
                                     'expect': expect, 'after': after})


def step5b_memx(ctx):
    tree = ctx.tree
    child, cr = tree.child('memx')
    tree.request(51, {'id': 'e2e-mem-1', 'to': 'mem-1', 'payload': {'note': 'm4e', 'm4e_rid': 'e2e-mem-1'},
                      'reply': {'required': ['reply']}}, cfg_path=Path(tree.hash('memx')['run_dir']) / 'config.json')
    res = tree.result('e2e-mem-1', r=cr, limit=600)
    assert res.get('status') == 'ok', res
    assert (res.get('result') or {}).get('reply') == 'E2E-OK e2e-mem-1', res
    assert any(t.get('event') == 'turn.start' and rid_needle('e2e-mem-1') in t.get('text', '')
               for _, rows in fake_cli_logs(ctx.world) for t in rows), 'e2e-mem-1 never reached a fake CLI turn'

    for event in ('clear.pasted', 'slot_rebound', 'clear.ready'):
        wait_for('memx ' + event, lambda: tree.events(event, cfg=child, r=cr), 120, 2)
    ctx.ev.step(51, 'memx-clear', {'result': res, 'audit': [e for e in tree.audit(child, cr)
                                                             if e.get('event', '').startswith(('clear.', 'slot_'))]})


def step6_tgog_down(ctx):
    """§29 return of the same session (plan F8: expected red until the fleetmux tmux liveness is fixed)."""
    tree = ctx.tree
    gen = int(tree.agent('tgog-ops-1').get('generation') or 0)
    drain_s = assert_declared_drain_s(tree, 'tgog', roster_entries(tree, ctx.world)['tgog'])  # the limit this down uses
    tree.request(6, {'id': 'e2e-down-1', 'op': 'subfleet.down', 'name': 'tgog', 'reason': 'e2e'})
    res = tree.result('e2e-down-1', limit=900)
    assert res.get('status') == 'ok' and 'down' in json.dumps(res), res
    h = tree.hash('tgog')
    assert h.get('state') == 'down' and h.get('redis_socket') == '', h
    assert tree.events('subfleet.slot_respawned', task='tgog') == [], 'tgog took the session-lost path (plan F8)'
    wait_for('tgog-ops-1 next generation READY', lambda: int(tree.agent('tgog-ops-1').get('generation') or 0) > gen
             and tree.agent('tgog-ops-1').get('state') == 'idle', 600, 2)
    # the note comes through the returned session's rewake; finish_rotation writes it before the generation moves
    notes = sorted((tree.run / 'handoffs').glob('tgog-ops-1-g*'))
    assert notes, 'no handoff note handoffs/tgog-ops-1-g* (§29 plan A)'
    role = (fleet.agent_cwd(tree.cfg, 'tgog-ops-1') / 'AGENTS.md').read_text()
    assert '## 4. Your role (supervisor)' not in role
    card = tree.card('tgog')
    assert card and card['state'] == 'reclaimed' and card.get('line1') == '已收回 · 结局 down' \
        and card.get('line2') == '只读，可看记录', card
    pages = tree.pages()
    ctx.ev.step(6, 'tgog-down', {'result': res, 'hash': h, 'notes': [str(p) for p in notes], 'card': card, 'pages': pages,
                                 'declared_drain_s': drain_s})


def step7_self_heal(ctx):
    tree = ctx.tree
    wait_revive_gap(tree)
    kill_daemon(ctx, 1, 'kill #1: mayfly child daemon (design §8.4 constraint 5)')
    event = wait_for('relaunch #1', lambda: tree.events('subfleet.autostart', name='mayfly', crashes=1), 300, 2)
    assert event[-1].get('reason') == 'daemon died; relaunched', event
    wait_for('new mayfly daemon with --resume', lambda: (daemon_of(ps(), tree.run / 'sub/mayfly/config.json') or {})
             .get('command', '').endswith('--resume') and fleet.daemon_running(tree.run / 'sub/mayfly'), 300, 2)
    wait_for('mayfly up again', lambda: tree.hash('mayfly').get('state') == 'up', 300, 2)
    ctx.mayfly_g = mayfly_workers(ctx, first_switch=False)  # whatever generation the relaunch is on now
    step5_mayfly_exit(ctx, rid='e2e-exit-2', n=7, expect=ctx.mayfly_g['exit-1'])


def step8_crashloop(ctx):
    tree = ctx.tree
    wait_revive_gap(tree)
    h = tree.hash('mayfly')
    sock = Path(h['redis_socket'])
    owner = read_json(sock.parent / 'owner.json') or {}
    assert Path(owner.get('run', '')) == tree.run / 'sub' / 'mayfly', owner
    pid_before = (sock.parent / 'redis.pid').read_text().strip()
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    # SHUTDOWN exactly once: the default client retries it after the expected drop, onto the Redis the product
    # relaunches on this same path (M53). The ConnectionError of that drop is shutdown()'s success.
    once = redis.Redis(unix_socket_path=str(sock), decode_responses=True, socket_connect_timeout=3, socket_timeout=10,
                       retry=Retry(NoBackoff(), 0))
    try:
        once.shutdown()  # this test's own child Redis, owner verified above
    except Exception:
        pass
    ctx.kills.append({'redis_shutdown': str(sock), 'pid': pid_before, 'at': time.time()})
    try:
        kill_daemon(ctx, 2, 'kill #2: mayfly child daemon after its Redis shutdown')
    except AssertionError:
        # the daemon may have exited on its own once Redis went away: that is this death (plan §5 timing note)
        assert not fleet.daemon_running(tree.run / 'sub' / 'mayfly')
    wait_for('relaunch #2', lambda: tree.events('subfleet.autostart', name='mayfly', crashes=2), 300, 2)
    wait_for('child Redis back on the same socket', lambda: pings(sock), 300, 2)
    pid_after = (sock.parent / 'redis.pid').read_text().strip()
    assert pid_after != pid_before and tree.hash('mayfly').get('redis_socket') == str(sock), (pid_before, pid_after)
    wait_revive_gap(tree)
    gen = int(tree.agent('mayfly-ops-1').get('generation') or 0)
    kill_daemon(ctx, 3, 'kill #3: third death in the 3600 s window')
    wait_for('subfleet.crashloop', lambda: tree.events('subfleet.crashloop', name='mayfly'), 300, 2)
    wait_for('hash crashloop=1', lambda: tree.hash('mayfly').get('crashloop') == '1', 120, 2)
    wait_for('mayfly-ops-1 back as a worker, READY', lambda: int(tree.agent('mayfly-ops-1').get('generation') or 0) > gen
             and tree.agent('mayfly-ops-1').get('state') == 'idle', 600, 2)
    time.sleep(30)
    assert daemon_of(ps(), tree.run / 'sub/mayfly/config.json') is None, 'crashloop must not relaunch'
    result = read_json(tree.run / 'sub' / 'mayfly' / 'subfleet.json') or {}
    ctx.ev.step(8, 'crashloop', {'kills': ctx.kills, 'redis_pid': [pid_before, pid_after], 'hash': tree.hash('mayfly'),
                                 'subfleet.json': result, 'card': tree.card('mayfly')})
    assert result.get('outcome') == 'crashloop', result


def step8b_recover(ctx):
    tree = ctx.tree
    tree.request(81, {'id': 'e2e-up-2', 'op': 'subfleet.up', 'name': 'mayfly', 'reason': 'e2e after crashloop'})
    res = tree.result('e2e-up-2', limit=AUTOSTART_LIMIT_S + 600)
    assert res.get('status') == 'ok', res
    h = tree.hash('mayfly')
    assert h.get('state') == 'up' and not h.get('crashloop'), h
    card = tree.card('mayfly')
    assert card and card['state'] == 'running', card
    ctx.ev.step(81, 'mayfly-recover', {'result': res, 'hash': h, 'card': card})


def stop_tree(ctx, cfg_path):
    """The product's whole-tree stop and the detached lifecycle closer (plan §5 step 9, F5)."""
    w = ctx.world
    done = w.fleet('stop', '--config', str(cfg_path), timeout=STOP_LIMIT_S)
    wait_for('root daemon exit', lambda: daemon_of(ps(), cfg_path) is None, STOP_LIMIT_S, 2)
    closer = lambda: [p for p in ps().values() if 'lifecycle-close' in p['command'] and str(cfg_path) in p['command']]
    wait_for('lifecycle-close exit', lambda: not closer(), CLOSER_LIMIT_S, 2)
    return done


def step9_stop(ctx):
    tree = ctx.tree
    audit_before = len(tree.audit())
    done = stop_tree(ctx, tree.cfg_path)
    ctx.stopped = True
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tree.run / 'lifecycle-close.log').exists(), 'no lifecycle-close.log (plan F5)'
    notes = tree.run / 'notifications.jsonl'
    lines = notes.read_text().splitlines() if notes.exists() else []
    # nothing configured, nothing leaves the machine: lifecycle_close.notify records exactly these two statuses
    records = [(json.loads(line).get('channel'), json.loads(line).get('status')) for line in lines]
    assert records == [('tg', 'no_channel'), ('parent', 'no_initiator')], lines
    ctx.ev.step(9, 'stop', {'stdout': done.stdout, 'stderr': done.stderr, 'audit_index': audit_before,
                            'notifications': lines})


def step10_residue(ctx):
    left = ctx.ledger.wait_clean(60)
    ctx.ev.step(10, 'residue', left)
    assert left['clean'], 'residue after a normal stop: ' + json.dumps(left, default=str)[:3000]
    for rt in ctx.world.s.glob('fleetmux/*'):
        kept = sorted(p.name for p in rt.iterdir())
        assert set(kept) <= {'server.log', 'layout.json', 'start.lock'}, (str(rt), kept)


# ----------------------------------------------------------------------------------------------- finalizer
def finalize(ctx, before, failed):
    """Every exit path: stop what is still running through the product, then the read-only residue proof;
    the verified fallback only if something is left (and then the test is red). Returns problems found."""
    problems = []
    w, ev, ledger = ctx.world, ctx.ev, ctx.ledger
    try:
        if not ctx.stopped:
            for cfg_path in sorted(w.runs.glob('fleet-*/config.json')):
                if daemon_of(ledger.refresh(), cfg_path):
                    try:
                        stop_tree(ctx, cfg_path)
                    except Exception as exc:
                        problems.append('finalizer stop of %s: %s' % (cfg_path, exc))
        left = ledger.wait_clean(60)
        ev.step(98, 'residue-final', dict(left, ledger=ledger.dump()))
        if not left['clean']:
            problems.append('residue after stop: ' + json.dumps(left, default=str)[:3000])
            ev.step(97, 'fallback', ledger.fallback())
    finally:
        if before is not None:
            ev.step(96, 'lifecycle-changes', ev.lifecycle_changes(before, ev.files()))
        if failed or problems:
            ev.tails()
        ev.manifest({'failed': repr(failed)[:2000] if failed else None, 'finalizer_problems': problems})
    return problems


def test_studio_ops_tree_runs_end_to_end_on_fleetmux():
    w = m4world.World()
    print('m4e E2 world: ' + str(w.dir))
    ctx = SimpleNamespace(world=w, ev=Evidence(w), ledger=Ledger(w), tree=None, kills=[], stopped=False)
    before, failed = None, None

    def on_alarm(_sig, _frame):
        raise Deadline('E2 exceeded its %d s hard limit' % HARD_LIMIT_S)

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(HARD_LIMIT_S)
    try:
        w.build()
        before = ctx.ev.files()
        step0_preconditions(ctx)
        step1_start(ctx)
        step2_main_fleet(ctx)
        step3_autostart(ctx)
        step4_tgog_up(ctx)
        step5_mayfly_exit(ctx, expect=ctx.mayfly_g['exit-1'])
        step5b_memx(ctx)
        step6_tgog_down(ctx)
        step7_self_heal(ctx)
        step8_crashloop(ctx)
        step8b_recover(ctx)
        step9_stop(ctx)
        step10_residue(ctx)
    except BaseException as exc:
        failed = exc
        raise
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        problems = finalize(ctx, before, failed)
        if problems and failed is not None:
            failed.add_note('m4e finalizer: ' + '\n'.join(problems))
        elif problems:
            pytest.fail('m4e finalizer: ' + '\n'.join(problems))
