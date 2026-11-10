"""The history page follows the terminal colours (``<run>/terminal_theme.json``).

``history.write_page`` reads the theme file the daemon captured from the control
tab (``cmux_fleet.terminal_theme``) and, when it is valid, splices one extra
``<style id="terminal-theme">`` block right after the page's own ``<style>`` so
its ``:root`` wins over the built-in ``@media (prefers-color-scheme)`` rule.
The original CSS is not touched; with no (or an invalid) theme file the page is
exactly what it was before.  Pure filesystem tests, no pty, no browser.
"""

import inspect
import json

from cmux_fleet import history


def _page(run, slots=None):
    return history.write_page(run, slots or [('worker-1', 'worker-1.1', 'worker-1')]).read_text()


def _theme(run, data):
    (run / 'terminal_theme.json').write_text(
        data if isinstance(data, str) else json.dumps(data))


def test_page_follows_terminal_theme(tmp_path):
    _theme(tmp_path, {'bg': '#1e1e1e', 'fg': '#e8e6e1', 'source': 'osc', 'at': 1761700000.0})
    page = _page(tmp_path)

    block = '<style id="terminal-theme">'
    assert page.count(block) == 1 and page.count('</style>') == 2
    # The placeholder must be gone (the page's own CSS never contains '<!--').
    assert '<!--' not in page and '<!--THEME-->' not in page

    # Placed after the page's own <style> (so it wins on order), still in <head>.
    theme_at = page.index(block)
    assert theme_at > page.index('@media (prefers-color-scheme:dark)')
    assert theme_at > page.index('</style>')
    assert theme_at < page.index('</head>')

    # The original CSS is untouched: base :root and the media query are both still there.
    assert ':root{--bg:#fbfbfa;--fg:#1d1d1b;' in page
    assert '@media (prefers-color-scheme:dark){:root{--bg:#171716;' in page

    # Real terminal colours, and the derived variables, inside the injected block.
    css = page[theme_at + len(block):page.index('</style>', theme_at)]
    assert css.startswith(':root{')
    assert 'color-scheme:dark' in css
    assert '--bg:#1e1e1e' in css
    assert '--fg:#e8e6e1' in css
    for var in ('--muted', '--line', '--panel', '--accent', '--mark', '--cur'):
        assert var + ':#' in css, var
    # panel/line/muted are mixes of bg towards fg: ordered bg < panel < line < muted.
    red = {var: int(css.split(var + ':', 1)[1][1:3], 16)
           for var in ('--bg', '--panel', '--line', '--muted')}
    assert red['--bg'] < red['--panel'] < red['--line'] < red['--muted'] < 0xff, red


def test_page_light_terminal_theme_sets_light_color_scheme(tmp_path):
    _theme(tmp_path, {'bg': '#fbfbfa', 'fg': None})
    page = _page(tmp_path)
    css = page.split('<style id="terminal-theme">', 1)[1].split('</style>', 1)[0]
    assert 'color-scheme:light' in css
    assert '--bg:#fbfbfa' in css
    assert '--fg:#1d1d1b' in css  # scheme default when fg is null


def test_page_without_theme_file_is_unchanged(tmp_path):
    page = _page(tmp_path)
    assert 'terminal-theme' not in page
    assert '<!--' not in page and '<!--THEME-->' not in page
    assert page.count('<style>') == 1 and page.count('</style>') == 1
    # Same bytes as the template with only the placeholder removed and the blob filled in.
    blob = page.split('const DATA=', 1)[1].split(';\n', 1)[0]
    assert page == history.PAGE.replace('<!--THEME-->', '').replace('/*DATA*/null', blob)


def test_page_falls_back_on_bad_json(tmp_path):
    _theme(tmp_path, '{"bg": "#1e1e1e", ')  # truncated JSON
    page = _page(tmp_path)
    assert 'terminal-theme' not in page and '<!--THEME-->' not in page


def test_page_falls_back_on_invalid_colour(tmp_path):
    for bad in ({'bg': 'red'}, {'bg': '#1E1E1E'}, {'bg': '#1e1e1'}, {'bg': None},
                {'bg': '#1e1e1e', 'fg': 'nope'}, {'fg': '#e8e6e1'}, ['#1e1e1e'],
                {'bg': '#1e1e1e\n'}):
        _theme(tmp_path, bad)
        page = _page(tmp_path)
        assert 'terminal-theme' not in page, bad
        assert '<!--THEME-->' not in page, bad


def test_write_page_signature_is_unchanged():
    params = list(inspect.signature(history.write_page).parameters)
    assert params == ['run_dir', 'slots', 'links']
