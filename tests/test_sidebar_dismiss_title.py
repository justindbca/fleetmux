"""The dismiss box names its sub-fleet even after the server clips the title (design section 19).

The server draws the box title on one row of 56 columns (``MODAL_MAX_W`` 60 minus the borders and the padding,
``rust/crates/app/src/render/sidebar.rs``) and ends a longer title with an ellipsis. The card titles the root daemon
gives its sub-fleets are long (``<fleet> › worker-N › \u5b50 fleet <id>``), so the card id has to come first in the
title, in both interface languages.
"""
import unicodedata

import pytest

from fleetmux import fleetmux_sidebar, i18n

INNER = 56  # the box title's columns on the real server

CARDS = [
    ('M21', 'remote-dev › worker-12 › \u5b50 fleet M21'),
    ('M26', 'remote-dev › worker-15 › \u5b50 fleet sidebar-dismiss-finished · M26'),
    ('sidebar-dismiss-finished', 'remote-dev › worker-15 › \u5b50 fleet sidebar-dismiss-finished · M26'),
]


def _width(text):
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)


def _clip(text, cols=INNER):
    """What the server shows of a box title: up to ``cols`` columns, a longer one cut to ``cols - 1`` plus ``…``."""
    if _width(text) <= cols:
        return text
    out = ''
    for c in text:
        if _width(out + c) > cols - 1:
            break
        out += c
    return out + '…'


@pytest.fixture(params=['en', 'zh-Hans'])
def lang(request, monkeypatch):
    monkeypatch.setenv(i18n.ENV, request.param)
    i18n.reset()
    yield request.param
    i18n.reset()


@pytest.mark.parametrize('card_id,title', CARDS)
def test_the_clipped_dismiss_title_still_names_the_card(lang, card_id, title):
    box = fleetmux_sidebar._close_texts('dismiss', card_id, {'title': title})
    shown = _clip(box['title'])
    assert card_id in shown, (lang, box['title'], shown)
    assert box['action'] == 'dismiss'


def test_the_dismiss_title_follows_the_interface_language(lang):
    box = fleetmux_sidebar._close_texts('dismiss', 'M21', {'title': CARDS[0][1]})
    if lang == 'en':
        assert box['title'].isascii() is False  # the card title itself is Chinese; the frame around it is English
        assert 'sidebar' in box['title'] and 'sidebar' in box['body'], box
        assert '\u4fa7\u680f' not in box['title'] and '\u4fa7\u680f' not in box['body'], box
    else:
        assert '\u4fa7\u680f' in box['title'] and '\u4fa7\u680f' in box['body'], box


def test_the_dismiss_texts_stay_within_the_close_limit(lang):
    box = fleetmux_sidebar._close_texts('dismiss', 'M21', {'title': 'x' * 1000})
    assert len(box['title']) <= fleetmux_sidebar.CLOSE_MAX, len(box['title'])
    assert len(box['body']) <= fleetmux_sidebar.CLOSE_MAX, len(box['body'])
    assert 'M21' in _clip(box['title']), box['title'][:80]
