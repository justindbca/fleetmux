"""ui.language = en: what Python pushes to the terminal and the pages has no CJK character (docs/design.md §63).

zh-Hans is the suite default (conftest ``ui_language_zh``), so the existing tests in test_fleetmux_sidebar.py,
test_fleetmux_history.py, test_lifecycle_close.py and test_presentation_v2.py check that zh-Hans is unchanged.
Here: the same outputs in English, and every one of them pure ASCII.
"""
import json
import re

import pytest

from fleetmux import fleetmux_history, fleetmux_sidebar, history, i18n, lifecycle_close, presentation, subfleet
from test_fleetmux_sidebar import RUN, FakeFleet, request_ready, run_dir, stop_request

CJK = re.compile(r'[　-〿぀-ヿ㐀-䶿一-鿿＀-￯]')


@pytest.fixture
def en(monkeypatch):
    monkeypatch.setenv(i18n.ENV, 'en')
    i18n.reset()
    yield
    i18n.reset()


def ascii_only(value):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    assert not CJK.search(text), text
    # The one non-ASCII mark left is the language-neutral cut mark `…` of sanitize_text / _clamp_lines (code matches
    # on `` … [truncated]``, e.g. timeline_line); it is not a word of either language.
    assert all(ord(c) < 128 for c in text.replace('…', '')), [c for c in text if ord(c) >= 128]


TREE = {
    'M0': {'run': 'runs/fleet-child-a', 'title': 'sub A', 'state': 'running', 'slot': 'worker-1', 'workers': 2,
           'subfleets': {'M0.1': {'run': 'runs/fleet-grand-b', 'title': '', 'state': 'stopped', 'outcome': 'ok',
                                  'workers': ['w1', 'w2', 'w3']}}},
    'M1': {'run': 'runs/fleet-child-c', 'state': 'running', 'slot': 'worker-2', 'workers': ['w1']},
}


def test_sidebar_payload_in_english(en, tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: TREE)
    p = fleetmux_sidebar.payload(FakeFleet(run_dir(tmp_path), name='fleetmux-demo'))
    cards = p['fleets']
    assert cards[0]['line1'] == 'running, workers 2, busy 1'
    assert [c['line1'] for c in cards[1:]] == ['running, workers 2', 'reclaimed, outcome ok', 'running, workers 1']
    assert [c['line2'] for c in cards[1:]] == ['delegated by worker-1', 'read-only, log kept', 'delegated by worker-2']
    assert p['close'] == {
        'title': 'Close the whole fleet "fleetmux-demo"?',
        'body': ('Stops the supervisor and its workers (2), and its sub-fleets M0 (workers: 2), M1 (workers: 1). '
                 'Running work is interrupted. The run log is kept; restore it later with the restore button in the '
                 'sidebar.'),
    }
    ascii_only({k: v for k, v in p.items() if k != 'fleets'})
    for card in cards:
        ascii_only({k: v for k, v in card.items() if k != 'line2' or not card['line2'].startswith(('~', '/'))})


def test_sidebar_line1_fits_the_default_card_width(en, tmp_path, monkeypatch):
    """Rust draws a card's text in `SIDEBAR_W - 5` = 29 columns (sidebar.rs card_text_w): the usual cases fit."""
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    for kw in ({}, {'paused': True}, {'stopping': True}):
        line1 = fleetmux_sidebar.payload(FakeFleet(run_dir(tmp_path), **kw))['fleets'][0]['line1']
        assert len(line1) <= 29, line1


def test_sidebar_states_and_no_outcome_in_english(en, tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {'M2': {'run': 'r', 'state': 'stopped'}})
    p = fleetmux_sidebar.payload(FakeFleet(run_dir(tmp_path), workers=('worker-1',), busy=(), paused=True))
    assert p['fleets'][0]['line1'] == 'paused, workers 1, busy 0'
    assert p['fleets'][1]['line1'] == 'reclaimed, outcome -'
    stopping = fleetmux_sidebar.payload(FakeFleet(run_dir(tmp_path), stopping=True))
    assert stopping['fleets'][0]['line1'].startswith('stopping, ')


def test_close_refused_reason_in_english(en, tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    f = request_ready(FakeFleet(run_dir(tmp_path)))
    f.mgr.kv['stop_request'] = stop_request(state='requested')
    fleetmux_sidebar.tick(f, now=0.0)
    assert json.loads(f.mgr.kv['stop_request'])['reason'] == ('This run has no lifecycle. Run start.sh stop in a '
                                                              'terminal.')


def test_history_and_lifecycle_cards_in_english(en):
    assert fleetmux_history._line1({'closed_via': fleetmux_history.SIDEBAR_VIA}) == 'closed, restorable'
    assert fleetmux_history._line1({'closed_by': 'timeout'}) == 'timed out, restorable'
    card = fleetmux_history.cards([{'id': 'r1', 'title': 't', 'closed_at': 0}])[0]
    assert card['line1'] == 'closed, restorable' and card['line2'].startswith('closed ')
    ascii_only(card)

    class Mgr:
        def __init__(self):
            self.kv = {'sidebar': json.dumps({'v': 1, 'current': 'root', 'fleets': [
                {'id': 'root', 'title': 'x', 'state': 'running', 'line1': '', 'line2': ''},
                {'id': 'kid', 'title': 'y', 'state': 'running', 'line1': '', 'line2': ''}]})}

        def kv_get(self, key):
            return self.kv.get(key)

        def _kv_set(self, key, value):
            self.kv[key] = value
    mgr = Mgr()
    assert lifecycle_close.mark_sidebar_closed(mgr, 'root', {'kid'})
    root, kid = json.loads(mgr.kv['sidebar'])['fleets']
    assert root['line1'] == 'closed, restorable'
    assert (kid['line1'], kid['line2']) == ('reclaimed, closed with root', 'read-only, log kept')


def test_sub_fleet_titles_in_english(en):
    assert subfleet.title(['demo', 'worker-1', 'M3']) == 'demo › worker-1 › sub-fleet M3'
    assert subfleet.placeholder_label('worker-1', 'M3', 'demo') == ('worker-1 -> delegated: sub-fleet M3 (see "demo" in '
                                                                    'the sidebar)')


def test_history_page_texts_in_english(en, tmp_path):
    labels = history.labels()
    assert labels == {'jumping': 'Switching to that workspace...', 'parent': '<- back to the parent fleet',
                      'show': 'Show in the sidebar: {title}', 'children': 'Sub-fleets ({n}):'}
    page = history.PAGE
    fleets_js = page[page.index('function fleets()'):page.index('function render()')]
    assert not CJK.search(fleets_js), fleets_js


def test_history_page_texts_in_chinese_unchanged():
    assert history.labels() == {'jumping': '正在切到对应工作区…', 'parent': '← 回到父 fleet',
                                'show': '切到侧栏：{title}', 'children': '子 fleet {n}：'}


RECORDS = [
    {'status': 'ok', 'slot': 'worker-1', 'result': {'reply': 'x' * 500}, 'elapsed_s': 3700},
    {'status': 'ok', 'slot': 'worker-1', 'result': {'a': 'y' * 400, 'b': 1}, 'elapsed_s': 90000},
    {'status': 'ok', 'slot': 'worker-1', 'elapsed_s': 5},
    {'status': 'timeout', 'slot': 'worker-1', 'elapsed_s': 120},
    {'status': 'invalid', 'slot': 'worker-1'},
    {'status': 'failed', 'slot': 'worker-1'},
    {'status': 'stalled', 'slot': 'worker-1', 'idle_s': 4000},
    {'status': 'stalled', 'slot': 'worker-1'},
    {'status': 'blocked', 'slot': 'worker-1'},
    {'status': 'blocked', 'slot': 'worker-1', 'reason': 'r' * 400},
    {'status': 'cancelled', 'slot': 'worker-1'},
    {'status': 'skipped', 'slot': 'worker-1'},
    {'status': 'rejected', 'slot': 'worker-1', 'reason': 'no'},
]


@pytest.mark.parametrize('record', RECORDS, ids=[r['status'] for r in RECORDS])
@pytest.mark.parametrize('stopped', [None, False, True])
def test_result_cards_in_english(en, record, stopped):
    card = presentation.result_card(dict(record), task_title='task', execution_stopped=stopped, now=1700000000000)
    ascii_only(card['render'])


def test_result_card_texts_in_english(en):
    card = presentation.result_card({'status': 'timeout', 'slot': 'worker-1', 'elapsed_s': 120}, task_title='T',
                                    now=1700000000000)
    assert card['render'] == ('Task: T\nworker-1 timed out (about 2 min), no valid result.\nThe original run is not '
                              'confirmed stopped (unconfirmed does not mean stopped).\nDetails in control.')


@pytest.mark.parametrize('kind,text,spec', [
    ('heartbeat', 'x', None), ('result', '', None), ('operator', 'do it', None), ('followup', 'next', None),
    ('result', 'ok', {'required': ['ok']}), ('result', 'ok', {'required': ['plan']}), ('result', 'done', None),
    ('result', '/tmp/out.md', None), ('result', '12 ms', None), ('result', 'please decide', None),
    ('result', 'word ' * 40, None), ('result', 'fine', None)])
def test_routing_cards_in_english(en, kind, text, spec):
    card = presentation.classify_routing(text, kind, reply_spec=spec, at=1700000000000, slot='worker-1')
    ascii_only(card['render'])
    ascii_only(presentation.timeline_line(card))


def test_notice_card_and_masking_in_english(en):
    card = presentation.routing_card('worker-1 waits', kind='notice', at=1700000000000, slot='worker-1')
    assert card['render'].endswith(' notice: worker-1 waits')
    deep = current = {}
    for _ in range(20):
        current['k'] = {}
        current = current['k']
    ascii_only(json.dumps(presentation.mask_secrets(deep)))


@pytest.mark.parametrize('lang', ['en', 'zh-Hans'])
def test_close_body_stays_within_the_terminal_limit(monkeypatch, tmp_path, lang):
    """Rust's parse refuses a `close` string over 400 characters (sidebar.rs) and then draws no sidebar at all: with
    many live sub-fleets the list gives way to their count."""
    monkeypatch.setenv(i18n.ENV, lang)
    i18n.reset()
    tree = {'M11.%d' % i: {'run': 'r%d' % i, 'state': 'running', 'slot': 'worker-1', 'workers': 2} for i in range(30)}
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: tree)
    body = fleetmux_sidebar.payload(FakeFleet(run_dir(tmp_path), name='fleetmux-demo'))['close']['body']
    assert len(body) <= fleetmux_sidebar.CLOSE_MAX, len(body)
    assert ('and its 30 sub-fleets' if lang == 'en' else '以及它的 30 个子 fleet') in body, body
