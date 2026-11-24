"""The shared zh-Hans string table (docs/design.md §63, brief §1b) against the code that uses it.

Every key the Python package or the Rust terminal uses is in ``fleetmux/locales/zh-Hans.json``, the table has no key
nobody uses, a key's English default is the same wherever it is written, English defaults are ASCII, and both
languages name the same ``{placeholders}``.
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TABLE = REPO / 'fleetmux' / 'locales' / 'zh-Hans.json'
KEY = r'([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)'
PY_STR = r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*\""""
# t('key', 'en' 'en'...), _both('key', ...), ('key', 'en') tuples, tr(<lang>, 'key', ...)
PY_USE = re.compile(r"(?:\(|\btr\(\s*[^,()]+,)\s*'" + KEY + r"'\s*,\s*((?:(?:" + PY_STR + r")\s*)+)")
RS_STR = r'"(?:[^"\\]|\\.)*"'
RS_USE = re.compile(r'\btr(?:_fmt)?\(\s*[\w.]+\s*,\s*"' + KEY + r'"\s*,\s*(' + RS_STR + r'|[\w:]+)')
RS_TUPLE = re.compile(r'\(\s*"' + KEY + r'"\s*,\s*(' + RS_STR + r')\s*\)')
RS_CONST = re.compile(r'const (\w+): &str =\s*(' + RS_STR + r');', re.S)
FIELD = re.compile(r'\{(\w+)\}')


def _py_text(literals):
    import ast
    return ''.join(ast.literal_eval(x) for x in re.findall(PY_STR, literals))


def _rs_text(literal):
    body = literal[1:-1]
    body = re.sub(r'\\\n\s*', '', body)  # a `\` line continuation
    return body.replace('\\"', '"').replace('\\\\', '\\')


def python_uses():
    uses = {}
    for path in sorted((REPO / 'fleetmux').rglob('*.py')):
        text = path.read_text(encoding='utf-8')
        if 'fleetmux.i18n import' not in text:
            continue
        for m in PY_USE.finditer(text):
            uses.setdefault(m.group(1), []).append((path.name, _py_text(m.group(2))))
    return uses


def rust_uses():
    src = REPO / 'rust' / 'crates' / 'app' / 'src'
    files = {p: p.read_text(encoding='utf-8').split('#[cfg(test)]', 1)[0] for p in sorted(src.rglob('*.rs'))}
    consts = {m.group(1): _rs_text(m.group(2)) for text in files.values() for m in RS_CONST.finditer(text)}
    uses = {}
    for path, text in files.items():
        for m in RS_USE.finditer(text):
            arg = m.group(2)
            en = _rs_text(arg) if arg.startswith('"') else consts.get(arg.rsplit('::', 1)[-1])
            assert en is not None, (path.name, m.group(0))
            uses.setdefault(m.group(1), []).append((path.name, en))
        for m in RS_TUPLE.finditer(text):
            uses.setdefault(m.group(1), []).append((path.name, _rs_text(m.group(2))))
    return uses


def all_uses():
    uses = python_uses()
    for key, found in rust_uses().items():
        uses.setdefault(key, []).extend(found)
    return uses


def table():
    return json.loads(TABLE.read_text(encoding='utf-8'))


def test_the_scan_finds_both_sides():
    py, rs = python_uses(), rust_uses()
    assert {'sidebar.line1', 'close.title', 'card.stop.unknown', 'history.parent'} <= set(py), sorted(py)
    assert {'bottom.sidebar.hide', 'modal.fleet.body', 'notice.no_page', 'close.title'} <= set(rs), sorted(rs)


def test_every_used_key_is_in_the_chinese_table():
    missing = sorted(set(all_uses()) - set(table()))
    assert not missing, missing


def test_the_table_has_no_unused_key():
    unused = sorted(set(table()) - set(all_uses()))
    assert not unused, unused


def test_one_english_default_per_key():
    differing = {k: v for k, v in all_uses().items() if len({en for _, en in v}) > 1}
    assert not differing, differing


def test_english_defaults_are_ascii():
    bad = {k: en for k, v in all_uses().items() for _, en in v if not en.isascii()}
    assert not bad, bad


def test_both_languages_name_the_same_placeholders():
    zh = table()
    bad = {}
    for key, found in all_uses().items():
        en = found[0][1]
        if set(FIELD.findall(en)) != set(FIELD.findall(zh.get(key, en))):
            bad[key] = (en, zh.get(key))
    assert not bad, bad


def test_the_table_is_a_flat_sorted_object_of_strings():
    raw = TABLE.read_text(encoding='utf-8')
    data = json.loads(raw)
    assert all(isinstance(v, str) and v for v in data.values())
    assert list(data) == sorted(data)
