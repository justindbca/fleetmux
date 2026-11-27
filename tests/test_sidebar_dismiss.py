"""Dismissing a finished sub-fleet's card from the fleetmux sidebar (docs/design.md section 19, "Dismissing a finished
sub-fleet card").

A finished direct child of the root (its record's ``state`` is ``returned`` or ``failed``; a job or a named sub-fleet,
nothing below it still running) carries a ``close`` object with ``action: "dismiss"``. After ``y`` in its box the server
writes kv ``child_stop`` with ``action: "dismiss"``; the root daemon checks the card again by the same rule, adds it to
``<run>/sidebar_dismissed.json`` and answers ``accepted``. Later pushes leave the card (and what hangs below it) out;
no sub-fleet directory, record, result or workspace is touched. Running cards keep the stop path unchanged.

Real run directories under tmp_path and the real ``subfleet.tree``; no Redis, no daemon: the fake fleet carries what
``payload()`` / ``close_child()`` read, its manager is a kv dict.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fleetmux import fleetmux_sidebar, named_subfleet  # noqa: E402

RUN = 'fleet-20261125-000000-dddd0000'


class FakeMgr:
    def __init__(self, pages=False):
        self.kv = {}
        self.pages = pages

    def kv_get(self, key):
        return self.kv.get(key)

    def _kv_set(self, key, value):
        self.kv[key] = value
        return True

    def server_has_pages(self):
        return self.pages


class FakeFleet:
    """What ``payload()`` and ``close_child()`` read; ``jobs`` are the open job rows, ``logs`` the audit lines."""

    def __init__(self, run, jobs=None, pages=False):
        self.cfg = {'run': str(run), 'workers': ['worker-1'], 'backend': 'fleetmux'}
        self.workers = ['worker-1']
        self.stopping = False
        self.service = True
        self.mgr = FakeMgr(pages)
        self.jobs = dict(jobs or {})
        self.cancelled = []
        self.logs = []

    def agent(self, name):
        return {'state': 'idle'}

    def task(self):
        return {}

    def paused(self):
        return False

    def job_state(self):
        return self.jobs

    def job_cancel(self, jid, by=None):
        self.cancelled.append((jid, by))

    def log(self, event, **fields):
        self.logs.append((event, fields))


def child(parent, key, state, **rec):
    """A sub-fleet run directory ``<parent>/sub/<key>`` with its record (``created`` defaults to 1.0) and a config
    whose ``run`` is that directory (so ``subfleet.tree`` walks below it)."""
    d = Path(parent) / 'sub' / key
    d.mkdir(parents=True)
    fields = dict({'task_id': key, 'state': state, 'created': 1.0, 'title': key}, **rec)
    fields = {k: v for k, v in fields.items() if v is not None}
    (d / 'subfleet.json').write_text(json.dumps(fields))
    (d / 'config.json').write_text(json.dumps({'run': str(d), 'redis_socket': str(d / 'no-redis.sock')}))
    return d


def cards(f):
    return {c['id']: c for c in fleetmux_sidebar.payload(f, history=[])['fleets']}


def confirmed(f, card, at=1, action='dismiss'):
    """What the server writes after `y` in a child card's box, then the root daemon's part of the tick."""
    req = {'v': 1, 'state': 'requested', 'id': card, 'page': '', 'at': at}
    if action is not None:
        req['action'] = action
    f.mgr.kv['child_stop'] = json.dumps(req)
    fleetmux_sidebar._child_stop_request(f)
    return json.loads(f.mgr.kv['child_stop'])


def dismissed_file(run):
    return Path(run) / 'sidebar_dismissed.json'


@pytest.fixture
def run(tmp_path):
    r = tmp_path / 'runs' / RUN
    r.mkdir(parents=True)
    return r


# ------------------------------------------------------------------ which cards carry the dismiss close
def test_finished_direct_children_carry_a_dismiss_close(run, monkeypatch):
    """Whitelist: the record's state is ``returned`` or ``failed`` (completed / cancelled / blocked / failed outcomes),
    a job or a named sub-fleet. Suspended, down, detached and still running / stopping cards have no dismiss."""
    monkeypatch.setattr(named_subfleet, 'ops', lambda f, name, kind, approved=False: [])
    monkeypatch.setattr(named_subfleet, 'attached', lambda f: {})
    child(run, 'M1', 'returned', outcome='completed', job='job-1')
    child(run, 'M2', 'failed', outcome='cancelled', job='job-2')
    child(run, 'M3', 'failed', outcome='blocked', job='job-3')
    child(run, 'M4', 'failed', outcome='failed', job='job-4')
    child(run, 'ops', 'failed', outcome='failed', named=True, name='ops')
    child(run, 'M5', 'suspended', outcome='suspended', job='job-5')
    child(run, 'web', 'down', outcome='down', named=True, name='web')
    child(run, 'docs', 'failed', outcome='down', named=True, name='docs', detached=True)
    child(run, 'M6', 'stopping', outcome='cancelled', job='job-6')
    child(run, 'M7', 'running', job='job-7')
    f = FakeFleet(run, jobs={'job-7': {'state': 'running'}})
    c = cards(f)
    dismiss = {k for k, v in c.items() if v.get('close', {}).get('action') == 'dismiss'}
    assert dismiss == {'M1', 'M2', 'M3', 'M4', 'ops'}, c
    for key in dismiss:
        assert key in c[key]['close']['title'] and c[key]['close']['body'], c[key]
        assert c[key]['state'] == 'reclaimed'
    assert c['M7']['close']['action'] == 'stop', c['M7']
    for key in ('M5', 'web', 'docs', 'M6'):
        assert 'close' not in c[key], c[key]


def test_grandchild_and_split_finished_cards_have_no_dismiss(run):
    """Design section 19 boundary: a finished split / escalate child (neither job nor named) and a finished grandchild
    never carry a close; a forged dismiss request for either is refused and nothing is written."""
    m0 = child(run, 'M0', 'returned', outcome='completed', job='job-0')
    child(m0, 'M0.1', 'returned', outcome='completed', job='job-0')
    child(run, 'M2', 'returned', outcome='completed')  # a split child: no job, not named
    f = FakeFleet(run)
    c = cards(f)
    assert c['M0']['close']['action'] == 'dismiss'
    assert 'close' not in c['M0.1'] and 'close' not in c['M2'], c
    for at, card in enumerate(('M0.1', 'M2'), 1):
        answer = confirmed(f, card, at=at)
        assert answer['state'] == 'refused' and answer['reason'], answer
    assert not dismissed_file(run).exists()
    assert {'M0', 'M0.1', 'M2'} <= set(cards(f))


def test_a_finished_child_with_a_running_descendant_cannot_be_dismissed(run):
    """A child collected as failed after its daemon died leaves its own sub-fleets running: dismissing it would hide a
    running card with it, so it carries no dismiss and a forged request is refused; a card dismissed earlier shows
    again while something below it runs."""
    m0 = child(run, 'M0', 'failed', outcome='cancelled', job='job-0')
    child(m0, 'M0.1', 'running', job='job-0')
    f = FakeFleet(run)
    c = cards(f)
    assert 'close' not in c['M0'] and c['M0.1']['state'] == 'running', c
    answer = confirmed(f, 'M0')
    assert answer['state'] == 'refused' and 'running' in answer['reason'], answer
    assert not dismissed_file(run).exists()
    # dismissed while nothing ran below it, then a descendant came up again: the card is shown again
    dismissed_file(run).write_text(json.dumps({'v': 1, 'cards': {'M0': {'task_id': 'M0', 'created': 1.0}}}))
    assert {'M0', 'M0.1'} <= set(cards(f))


# ------------------------------------------------------------------ y -> the card leaves the sidebar
def test_confirmed_dismiss_hides_the_card(run):
    m3 = child(run, 'M3', 'returned', outcome='completed', job='job-3')
    child(m3, 'M3.1', 'returned', outcome='completed', job='job-3')
    child(run, 'M4', 'failed', outcome='cancelled', job='job-4')
    f = FakeFleet(run)
    assert {'M3', 'M3.1', 'M4'} <= set(cards(f))
    answer = confirmed(f, 'M3')
    assert answer['state'] == 'accepted' and answer['id'] == 'M3' and answer['action'] == 'dismiss', answer
    saved = json.loads(dismissed_file(run).read_text())
    assert saved['v'] == 1 and saved['cards']['M3']['task_id'] == 'M3' and saved['cards']['M3']['created'] == 1.0
    c = cards(f)
    assert 'M3' not in c and 'M3.1' not in c and 'M4' in c, c
    assert ('sidebar.child_dismissed', {'card': 'M3', 'task': 'M3'}) in f.logs, f.logs
    assert f.cancelled == []
    # the same (id, at) seen again is left alone; a second card adds to the set
    f.mgr.kv['child_stop'] = json.dumps({'v': 1, 'state': 'requested', 'id': 'M3', 'page': '', 'at': 1,
                                         'action': 'dismiss'})
    fleetmux_sidebar._child_stop_request(f)
    assert json.loads(f.mgr.kv['child_stop'])['state'] == 'requested'
    assert confirmed(f, 'M4', at=2)['state'] == 'accepted'
    assert set(json.loads(dismissed_file(run).read_text())['cards']) == {'M3', 'M4'}


def test_nothing_is_written_without_the_confirmation(run):
    child(run, 'M3', 'returned', outcome='completed', job='job-3')
    f = FakeFleet(run)
    fleetmux_sidebar._child_stop_request(f)  # no kv child_stop: the box was never confirmed
    for state in ('accepted', 'refused', 'nonsense'):
        f.mgr.kv['child_stop'] = json.dumps({'v': 1, 'state': state, 'id': 'M3', 'at': 1, 'action': 'dismiss'})
        fleetmux_sidebar._child_stop_request(f)
        assert json.loads(f.mgr.kv['child_stop'])['state'] == state
    assert not dismissed_file(run).exists() and 'M3' in cards(f)


def test_dismissed_card_stays_hidden_for_a_new_daemon(run):
    """The set lives in the run directory and every push reads it: a fresh daemon (no memory) on the same run still
    leaves the card out. The same directory brought up again (another ``created``) or running again shows again."""
    m3 = child(run, 'M3', 'returned', outcome='completed', job='job-3')
    assert confirmed(FakeFleet(run), 'M3')['state'] == 'accepted'
    fresh = FakeFleet(run)
    assert 'M3' not in cards(fresh)
    rec = json.loads((m3 / 'subfleet.json').read_text())
    (m3 / 'subfleet.json').write_text(json.dumps(dict(rec, created=2.0)))
    assert 'M3' in cards(fresh)  # a new sub-fleet in the same directory: never hidden by the old dismissal
    (m3 / 'subfleet.json').write_text(json.dumps(dict(rec, state='running')))
    fresh.jobs = {'job-3': {'state': 'running'}}
    assert cards(fresh)['M3']['close']['action'] == 'stop'  # running again: shown, with its stop button


def test_a_record_without_created_matches_only_a_record_without_created(run):
    """Old records may lack ``created``: the dismissal stores ``created: null`` and hides the card while the record
    still has none; once the directory holds a record with a ``created`` (a new sub-fleet), the card is shown."""
    m3 = child(run, 'M3', 'returned', outcome='completed', job='job-3', created=None)
    f = FakeFleet(run)
    assert cards(f)['M3']['close']['action'] == 'dismiss'
    assert confirmed(f, 'M3')['state'] == 'accepted'
    assert json.loads(dismissed_file(run).read_text())['cards']['M3']['created'] is None
    assert 'M3' not in cards(f)
    rec = json.loads((m3 / 'subfleet.json').read_text())
    (m3 / 'subfleet.json').write_text(json.dumps(dict(rec, created=5.0)))
    assert 'M3' in cards(f)


def test_a_corrupt_dismissed_file_hides_nothing(run):
    child(run, 'M3', 'returned', outcome='completed', job='job-3')
    dismissed_file(run).write_text('not json')
    f = FakeFleet(run)
    assert 'M3' in cards(f)
    assert confirmed(f, 'M3')['state'] == 'accepted'  # rewritten as a valid set
    assert 'M3' not in cards(f)


# ------------------------------------------------------------------ records stay byte for byte
def _snapshot(d):
    out = {'dir': (sorted(p.name for p in d.iterdir()), d.stat().st_mtime_ns)}
    for p in sorted(d.rglob('*')):
        st = p.stat()
        out[str(p.relative_to(d))] = (p.read_bytes() if p.is_file() else None, st.st_mtime_ns)
    return out


def test_dismiss_leaves_every_record_byte_and_mtime(run):
    m3 = child(run, 'M3', 'returned', outcome='completed', job='job-3')
    (m3 / 'result.json').write_text(json.dumps({'status': 'completed'}))
    (m3 / 'workspace').mkdir()
    (m3 / 'workspace' / 'out.md').write_text('deliverable')
    child(m3, 'M3.1', 'returned', outcome='completed', job='job-3')
    f = FakeFleet(run)
    cards(f)  # the push that showed the card (tree() opens each daemon.lock once: before the snapshot)
    before, sub_before = _snapshot(m3), _snapshot(run / 'sub')
    root_before = sorted(p.name for p in run.iterdir())
    assert confirmed(f, 'M3')['state'] == 'accepted'
    cards(f)
    fleetmux_sidebar.payload(FakeFleet(run), history=[])
    assert _snapshot(m3) == before
    assert _snapshot(run / 'sub') == sub_before
    assert (m3 / 'result.json').is_file()
    assert sorted(p.name for p in run.iterdir()) == sorted(root_before + ['sidebar_dismissed.json'])


# ------------------------------------------------------------------ running cards keep the stop path
def test_running_cards_keep_the_stop_path(run):
    child(run, 'M7', 'running', job='job-7')
    child(run, 'M3', 'returned', outcome='completed', job='job-3')
    f = FakeFleet(run, jobs={'job-7': {'state': 'running'}})
    c = cards(f)
    assert c['M7']['close']['action'] == 'stop' and 'job-7' in c['M7']['close']['title']
    # a dismiss request for a running card is refused: never turned into a stop
    answer = confirmed(f, 'M7', at=1)
    assert answer['state'] == 'refused' and f.cancelled == [] and not dismissed_file(run).exists(), answer
    # an action the daemon does not know is refused too
    assert confirmed(f, 'M7', at=2, action='explode')['state'] == 'refused' and f.cancelled == []
    # a stop request for a finished card is refused: never turned into a dismiss
    answer = confirmed(f, 'M3', at=3, action='stop')
    assert answer['state'] == 'refused' and 'not running' in answer['reason'], answer
    assert not dismissed_file(run).exists()
    # an old server's request (no action) still stops the running job
    answer = confirmed(f, 'M7', at=4, action=None)
    assert answer['state'] == 'accepted' and f.cancelled == [('job-7', 'fleetmux-sidebar')], answer
    assert 'M7' in cards(f)


# ------------------------------------------------------------------ the 16-card cap and pages
def test_a_dismissed_card_gives_its_place_under_the_cap_to_the_next_older_one(run):
    """Twenty finished job children: the cap shows the newest fifteen; dismissing one lets the next older one in."""
    for i in range(20):
        child(run, 'M%02d' % i, 'returned', outcome='completed', job='job-%d' % i, created=float(i + 1))
    f = FakeFleet(run)
    shown = [c['id'] for c in fleetmux_sidebar.payload(f, history=[])['fleets'][1:]]
    assert shown == ['M%02d' % i for i in range(19, 4, -1)], shown
    assert confirmed(f, 'M12')['state'] == 'accepted'
    shown = [c['id'] for c in fleetmux_sidebar.payload(f, history=[])['fleets'][1:]]
    assert 'M12' not in shown and shown[-1] == 'M04' and len(shown) == 15, shown


def test_an_older_attempt_card_without_a_page_can_be_dismissed(run):
    """Cards of one task share its page: only the newest attempt carries it. The older attempt still has a dismiss;
    once the newest is dismissed, the page moves to the older one (the same task's record page)."""
    child(run, 'M0', 'failed', outcome='failed', job='job-0', attempt=1, created=1.0)
    child(run, 'M0-attempt-2', 'returned', outcome='completed', job='job-0', task_id='M0', attempt=2, created=2.0)
    f = FakeFleet(run, pages=True)
    c = cards(f)
    assert 'page' not in c['M0'] and c['M0-attempt-2']['page'] == 'M0'
    assert c['M0']['close']['action'] == 'dismiss' and c['M0-attempt-2']['close']['action'] == 'dismiss'
    assert confirmed(f, 'M0-attempt-2')['state'] == 'accepted'
    c = cards(f)
    assert 'M0-attempt-2' not in c and c['M0']['page'] == 'M0', c


def test_a_sub_fleet_daemon_never_handles_a_dismiss(run):
    child(run, 'M3', 'returned', outcome='completed', job='job-3')
    f = FakeFleet(run)
    f.cfg['parent_config'] = str(run.parent / 'parent' / 'config.json')
    f.mgr.kv['child_stop'] = json.dumps({'v': 1, 'state': 'requested', 'id': 'M3', 'at': 1, 'action': 'dismiss'})
    fleetmux_sidebar.tick(f, now=100.0)
    assert json.loads(f.mgr.kv['child_stop'])['state'] == 'requested' and not dismissed_file(run).exists()


def test_the_docs_describe_dismissing_a_finished_card():
    root = Path(__file__).resolve().parents[1]
    design = (root / 'docs' / 'design.md').read_text()
    readme = (root / 'README.md').read_text()
    assert 'sidebar_dismissed.json' in design and '"action":"dismiss"' in design.replace(' ', '')
    assert 'sidebar_dismissed.json' in readme
    assert os.path.basename(str(dismissed_file('x'))) == 'sidebar_dismissed.json'


def test_a_closed_root_leaves_no_dismiss_button_behind(run):
    """Closing the root keeps its cards in kv ``sidebar`` (greyed in place): no daemon is left to answer, so every card
    of its tree loses ``close`` (a finished card's dismiss as well as a running card's stop)."""
    from fleetmux import lifecycle_close
    child(run, 'M3', 'returned', outcome='completed', job='job-3')
    child(run, 'M7', 'running', job='job-7')
    f = FakeFleet(run, jobs={'job-7': {'state': 'running'}})
    f.mgr.kv['sidebar'] = json.dumps(fleetmux_sidebar.payload(f, history=[]), ensure_ascii=False)
    before = {c['id']: c for c in json.loads(f.mgr.kv['sidebar'])['fleets']}
    assert before['M3']['close']['action'] == 'dismiss' and before['M7']['close']['action'] == 'stop'
    ids = lifecycle_close.subtree_card_ids(f.cfg)
    assert lifecycle_close.mark_sidebar_closed(f.mgr, RUN, subtree=ids)
    after = {c['id']: c for c in json.loads(f.mgr.kv['sidebar'])['fleets']}
    assert 'close' not in after['M3'] and 'close' not in after['M7'], after
    assert after['M7']['state'] == 'reclaimed' and after[RUN]['state'] == 'closed'
