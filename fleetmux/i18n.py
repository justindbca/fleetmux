"""Interface language: ``fleet.yaml#ui.language`` and the string lookup (docs/design.md §63).

Every interface text is a key plus its English default, written in the code: ``t('key', 'English {x}', x=...)``.
The Chinese texts live in one table, ``fleetmux/locales/zh-Hans.json``, shared with the Rust terminal (it embeds the
same file at build time). A key missing from the table falls back to the English default.

The language is resolved once per process, in this order:

1. ``$FLEETMUX_UI_LANGUAGE`` -- set by this module for the terminal server it starts, and by the demo recording and
   the tests to force a language. Not meant for daily use: pin a language in the private file instead.
2. ``ui.language`` of the private file (``$FLEETMUX_CONFIG``, else ``~/.fleetmux/fleet.yaml``) -- the block replaces
   the shipped one as a whole, like every private block.
3. ``ui.language`` of the shipped ``fleetmux/config/fleet.yaml``: ``system``.

``system`` reads the system language once: on macOS the first entry of ``AppleLanguages``, elsewhere the first
non-empty of ``LC_ALL`` / ``LC_MESSAGES`` / ``LANG``. Simplified Chinese gives ``zh-Hans``; anything else, or
anything that cannot be read, gives ``en``. An invalid value is reported once on stderr and gives ``en``.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ENV = 'FLEETMUX_UI_LANGUAGE'
LANGS = ('en', 'zh-Hans')
SETTINGS = ('system',) + LANGS
LOCALES = Path(__file__).resolve().parent / 'locales'
TEMPLATE = ('# Interface language (docs/design.md section 63). Uncomment to set it; restart fleetmux to apply.\n'
            '# ui:\n'
            '#   language: system  # system | en | zh-Hans\n')

_lock = threading.Lock()
_info = None
_pinned = None  # the run's language (config.json `ui_language`, set by `start`): every process of the run uses it
_TABLES = {}  # lang -> {key: text}, loaded on first use
_reported = set()
_TEMPLATE_RE = re.compile(r'^#\s*ui:\s*$', re.M)


def pin(value):
    """Use ``value`` (``en`` / ``zh-Hans``, the run's config.json ``ui_language``) from now on in this process, or
    None to resolve from the configuration again. The daemon and the agents run in terminal panes, which do not
    inherit FLEETMUX_UI_LANGUAGE (and a demo's panes have their own HOME), so a run's processes read the language
    its `start` resolved, not whatever their environment says. Anything else is ignored."""
    global _pinned
    with _lock:
        _pinned = value if value in LANGS else None
    reset()


def reset():
    """Forget the resolved language (tests; a long-lived process never changes language mid-way)."""
    global _info
    with _lock:
        _info = None


def _platform():
    return sys.platform


def _apple_languages():
    """The raw output of ``defaults read -g AppleLanguages``; None when it cannot be read."""
    try:
        done = subprocess.run(['defaults', 'read', '-g', 'AppleLanguages'], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _report(message):
    if message not in _reported:
        _reported.add(message)
        print('fleetmux: ' + message, file=sys.stderr)


def _lang_of(tag):
    """A language tag (``zh-Hans-CN``, ``zh_CN.UTF-8``, ``en-CA``) -> a supported language."""
    parts = tag.split('.', 1)[0].split('@', 1)[0].strip().lower().replace('_', '-').split('-')
    return 'zh-Hans' if parts[0] == 'zh' and (len(parts) == 1 or parts[1] in ('hans', 'cn', 'sg')) else 'en'


def resolve_system():
    """``system`` -> (language, detail): what the machine says, and how it was read."""
    if _platform() == 'darwin':
        raw = _apple_languages()
        if raw is None:
            return 'en', 'AppleLanguages unreadable'
        items = [s.strip().strip(',').strip().strip('"') for s in raw.strip().strip('()').splitlines()]
        items = [s for s in items if s]
        if not items:
            return 'en', 'AppleLanguages empty'
        return _lang_of(items[0]), 'AppleLanguages ' + items[0]
    if _platform().startswith(('linux', 'freebsd', 'openbsd', 'netbsd')):
        for name in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
            value = os.environ.get(name)
            if value:
                return _lang_of(value), name + '=' + value
        return 'en', 'no LC_ALL / LC_MESSAGES / LANG'
    return 'en', 'platform ' + _platform()


def _setting():
    """(setting, source, where) before ``system`` is resolved; setting None = invalid (already reported)."""
    from fleetmux import fleet_config
    value = os.environ.get(ENV)
    if value is not None:
        if value in LANGS:  # the override names a language, never `system`
            return value, 'env ' + ENV, ENV
        _report('%s=%r is not one of %s; using en' % (ENV, value, ', '.join(LANGS)))
        return None, 'invalid (%s=%r) -> en' % (ENV, value), ENV
    if _pinned is not None:
        return _pinned, 'run config', 'config.json'
    try:
        merged = fleet_config.load()
    except (ValueError, OSError) as exc:
        _report('cannot read ui.language (%s); using en' % str(exc).splitlines()[0])
        return None, 'invalid (unreadable config) -> en', 'config'
    user = fleet_config.user_path()
    from_user = False
    if user is not None:
        try:
            from_user = isinstance(fleet_config._read(user), dict) and 'ui' in fleet_config._read(user)
        except (ValueError, OSError):
            from_user = False
    where = str(user) + '#ui.language' if from_user else str(fleet_config.SHIPPED) + '#ui.language'
    block = merged.get('ui')
    if not isinstance(block, dict):
        _report('%s: ui must be a mapping with language: system | en | zh-Hans, got %r; using en'
                % (where.rsplit('#', 1)[0], block))
        return None, 'invalid (ui %r) -> en' % (block,), where
    value = block.get('language', 'system')
    if not isinstance(value, str) or value not in SETTINGS:
        _report('%s %r is not one of %s; using en' % (where, value, ', '.join(SETTINGS)))
        return None, 'invalid (%r) -> en' % (value,), where
    return value, ('private config ' + str(user)) if from_user else 'default', where


def language_info():
    """{'value', 'setting', 'source', 'detail'}: the language in effect and where it came from (cached)."""
    global _info
    with _lock:
        if _info is not None:
            return dict(_info)
        setting, source, where = _setting()
        if setting is None:
            info = {'value': 'en', 'setting': None, 'source': source, 'detail': where}
        elif setting == 'system':
            value, detail = resolve_system()
            info = {'value': value, 'setting': 'system', 'source': source, 'detail': 'system: ' + detail}
        else:
            info = {'value': setting, 'setting': setting, 'source': source, 'detail': where}
        _info = info
        return dict(info)


def language():
    """``en`` or ``zh-Hans``."""
    return language_info()['value']


def server_env():
    """What the terminal server is started with: it reads the language from here and nowhere else."""
    return {ENV: language()}


def table(lang):
    """The translations of ``lang`` ({} for English, or when the file cannot be read: everything stays English)."""
    if lang == 'en':
        return {}
    with _lock:
        if lang not in _TABLES:
            try:
                data = json.loads((LOCALES / (lang + '.json')).read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    raise ValueError('not an object')
            except (OSError, ValueError) as exc:
                _report('cannot read %s: %s; texts stay English' % (LOCALES / (lang + '.json'), exc))
                data = {}
            _TABLES[lang] = data
        return _TABLES[lang]


_FIELD = re.compile(r'\{(\w+)\}')


def _fill(text, kw):
    """One pass: a filled value is never scanned again, a name not given stays as it is."""
    return _FIELD.sub(lambda m: str(kw[m.group(1)]) if m.group(1) in kw else m.group(0), text) if kw else text


def tr(lang, key, default, /, **kw):
    """``default`` (English) translated into ``lang`` when the table has ``key``; ``{name}`` filled from ``kw``."""
    text = table(lang).get(key, default)
    return _fill(text if isinstance(text, str) else default, kw)


def t(key, default, /, **kw):
    """``tr`` in the language in effect."""
    return tr(language(), key, default, **kw)


def render_setup(info, status, where):
    """The `fleetmux setup` lines (§1d): the language in effect, its source, the template, when it applies."""
    line = 'ui language: %s (setting: %s; source: %s' % (info['value'], info['setting'] or 'invalid', info['source'])
    if info['setting'] == 'system':
        line += '; ' + info['detail']
    lines = [line + ')']
    if where is not None:
        lines.append('ui.language template: %s in %s' % (status, where))
    else:
        lines.append('ui.language template: ' + status)
    lines.append('  set ui: {language: system | en | zh-Hans} there; applies at the next fleetmux start '
                 '(or terminal server restart)')
    return '\n'.join(lines)


def ensure_template(path=None):
    """§1a: make sure the private file says how to set ``ui.language``, without touching what the user wrote.

    Returns (status, path): ``created`` (no private file: a new 0600 file holding only the commented template),
    ``appended`` (the template added after the original bytes), ``present`` (a ``ui`` block or the template is
    already there) or ``skipped: <why>`` (unreadable / invalid file, or this run has no private file)."""
    from fleetmux import fleet_config
    import yaml
    target = path if path is not None else fleet_config.user_path()
    if target is None:
        return 'skipped: this run has no private config', None
    target = Path(target).expanduser()
    real = target.resolve() if target.is_symlink() else target
    if not real.exists():
        real.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(real), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(TEMPLATE)
        return 'created', target
    try:
        raw = real.read_bytes()
        text = raw.decode('utf-8')
        data = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return 'skipped: ' + str(exc).splitlines()[0], target
    if (isinstance(data, dict) and 'ui' in data) or _TEMPLATE_RE.search(text):
        return 'present', target
    if data is not None and not isinstance(data, dict):
        return 'skipped: not a mapping of blocks', target
    out = raw + (b'' if not raw or raw.endswith(b'\n') else b'\n') + TEMPLATE.encode('utf-8')
    mode = os.stat(real).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(prefix=real.name + '.', dir=str(real.parent))
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(out)
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return 'appended', target
