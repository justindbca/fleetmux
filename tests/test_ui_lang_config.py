"""ui.language (docs/design.md §63): the setting, its resolution, the template and the string lookup.

No test reads the real machine: the private file is a temp path ($FLEETMUX_CONFIG), the macOS language list and the
platform are stubbed, the locale variables are set per test.
"""
import os
import stat

import pytest

from fleetmux import fleet_config, i18n


@pytest.fixture
def ui(monkeypatch, tmp_path):
    """A clean resolver: no override, a private file path that does not exist yet, Linux with LANG=C."""
    private = tmp_path / 'private' / 'fleet.yaml'
    monkeypatch.delenv(i18n.ENV, raising=False)
    monkeypatch.setenv('FLEETMUX_CONFIG', str(private))
    for name in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('LANG', 'C')
    monkeypatch.setattr(i18n, '_platform', lambda: 'linux')
    monkeypatch.setattr(i18n, '_apple_languages', lambda: (_ for _ in ()).throw(AssertionError('not on macOS')))
    fleet_config.pin(None)
    i18n.reset()
    yield private
    i18n.reset()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


# --- the setting ---------------------------------------------------------------------------------------------

def test_shipped_default_is_system_and_ui_is_a_private_block():
    assert fleet_config.block('ui', fleet_config.SHIPPED) == {'language': 'system'}
    assert 'ui' in fleet_config.BLOCKS and 'ui' in fleet_config.USER_BLOCKS


def test_default_system_on_linux_c_is_en(ui):
    info = i18n.language_info()
    assert info['value'] == 'en' and info['setting'] == 'system' and info['source'] == 'default'
    assert 'LANG=C' in info['detail']


def test_private_file_overrides_the_shipped_block(ui):
    write(ui, 'ui:\n  language: zh-Hans\n')
    info = i18n.language_info()
    assert info['value'] == 'zh-Hans' and info['setting'] == 'zh-Hans'
    assert info['source'] == 'private config ' + str(ui)


def test_private_en_beats_a_chinese_system(ui, monkeypatch):
    monkeypatch.setenv('LANG', 'zh_CN.UTF-8')
    write(ui, 'ui:\n  language: en\n')
    assert i18n.language() == 'en'


def test_env_override_beats_the_private_file(ui, monkeypatch):
    write(ui, 'ui:\n  language: zh-Hans\n')
    monkeypatch.setenv(i18n.ENV, 'en')
    info = i18n.language_info()
    assert info['value'] == 'en' and info['source'] == 'env ' + i18n.ENV


@pytest.mark.parametrize('value', ['fr', 'zh', 'EN', '', 7, ['en']])
def test_invalid_private_value_reports_and_falls_back_to_en(ui, value, capsys, monkeypatch):
    import yaml
    monkeypatch.setenv('LANG', 'zh_CN.UTF-8')  # the fallback is en, not the system language
    write(ui, yaml.safe_dump({'ui': {'language': value}}))
    info = i18n.language_info()
    assert info['value'] == 'en' and info['source'].startswith('invalid')
    err = capsys.readouterr().err
    assert 'ui.language' in err and 'system, en, zh-Hans' in err
    i18n.language_info()  # cached: reported once
    assert capsys.readouterr().err == ''


def test_invalid_env_value_reports_and_falls_back_to_en(ui, monkeypatch, capsys):
    monkeypatch.setenv(i18n.ENV, 'de')
    assert i18n.language() == 'en'
    assert i18n.ENV in capsys.readouterr().err


def test_ui_block_not_a_mapping_is_invalid(ui, capsys):
    write(ui, 'ui: zh-Hans\n')
    assert i18n.language_info()['value'] == 'en'
    assert 'ui' in capsys.readouterr().err


def test_broken_private_file_does_not_crash(ui, capsys):
    write(ui, 'ui: [\n')
    assert i18n.language() == 'en'
    assert capsys.readouterr().err


# --- system ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('first,want', [
    ('zh-Hans-CN', 'zh-Hans'), ('zh-Hans-CA', 'zh-Hans'), ('zh-Hans', 'zh-Hans'), ('zh-CN', 'zh-Hans'),
    ('en-CA', 'en'), ('en', 'en'), ('zh-Hant-TW', 'en'), ('zh-TW', 'en'), ('ja-JP', 'en'), ('fr-FR', 'en')])
def test_system_macos_first_apple_language(ui, monkeypatch, first, want):
    monkeypatch.setattr(i18n, '_platform', lambda: 'darwin')
    monkeypatch.setattr(i18n, '_apple_languages', lambda: '(\n    "%s",\n    "en-CA"\n)\n' % first)
    monkeypatch.setenv('LANG', 'zh_CN.UTF-8')  # macOS does not look at LANG
    info = i18n.language_info()
    assert info['value'] == want, info
    assert first in info['detail']


def test_system_macos_unquoted_and_unreadable(ui, monkeypatch):
    monkeypatch.setattr(i18n, '_platform', lambda: 'darwin')
    monkeypatch.setattr(i18n, '_apple_languages', lambda: '(\n    en,\n    "zh-Hans-CN"\n)\n')
    assert i18n.language() == 'en'
    i18n.reset()
    monkeypatch.setattr(i18n, '_apple_languages', lambda: None)
    assert i18n.language_info()['value'] == 'en'
    i18n.reset()
    monkeypatch.setattr(i18n, '_apple_languages', lambda: '(\n)\n')
    assert i18n.language() == 'en'


@pytest.mark.parametrize('env,want', [
    ({'LANG': 'zh_CN.UTF-8'}, 'zh-Hans'),
    ({'LANG': 'zh_SG.UTF-8'}, 'zh-Hans'),
    ({'LANG': 'zh_TW.UTF-8'}, 'en'),
    ({'LANG': 'en_US.UTF-8'}, 'en'),
    ({'LANG': 'C.UTF-8'}, 'en'),
    ({'LANG': 'POSIX'}, 'en'),
    ({'LC_MESSAGES': 'zh_CN.UTF-8', 'LANG': 'en_US.UTF-8'}, 'zh-Hans'),
    ({'LC_ALL': 'en_US.UTF-8', 'LC_MESSAGES': 'zh_CN.UTF-8'}, 'en'),
    ({'LC_ALL': '', 'LC_MESSAGES': '', 'LANG': 'zh_CN.GB18030@stroke'}, 'zh-Hans'),
    ({}, 'en'),
])
def test_system_linux_locale_variables(ui, monkeypatch, env, want):
    monkeypatch.delenv('LANG', raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert i18n.language() == want


def test_unknown_platform_is_en(ui, monkeypatch):
    monkeypatch.setattr(i18n, '_platform', lambda: 'win32')
    monkeypatch.setenv('LANG', 'zh_CN.UTF-8')
    assert i18n.language() == 'en'


# --- lookup ---------------------------------------------------------------------------------------------------

def test_t_follows_the_language_and_falls_back_to_english(ui, monkeypatch):
    monkeypatch.setattr(i18n, '_TABLES', {'zh-Hans': {'x.hello': '你好 {name}'}})
    monkeypatch.setenv(i18n.ENV, 'en')
    i18n.reset()
    assert i18n.t('x.hello', 'hello {name}', name='a') == 'hello a'
    monkeypatch.setenv(i18n.ENV, 'zh-Hans')
    i18n.reset()
    assert i18n.t('x.hello', 'hello {name}', name='a') == '你好 a'
    assert i18n.t('x.missing', 'only {name} here', name='b') == 'only b here'  # no translation: English
    assert i18n.t('x.hello', 'hello {name}') == '你好 {name}'  # names not given stay as they are


def test_tr_takes_an_explicit_language():
    assert i18n.tr('en', 'bottom.sidebar.hide', 'hide sidebar') == 'hide sidebar'
    assert i18n.tr('zh-Hans', 'bottom.sidebar.hide', 'hide sidebar') == '隐藏侧栏'


def test_server_env_carries_the_resolved_language(ui, monkeypatch):
    monkeypatch.setenv('LANG', 'zh_CN.UTF-8')
    assert i18n.server_env() == {i18n.ENV: 'zh-Hans'}


# --- the template (§1a) -----------------------------------------------------------------------------------------

def test_template_is_commented_out_and_says_the_values():
    assert '# ui:\n#   language: system  # system | en | zh-Hans' in i18n.TEMPLATE
    import yaml
    assert yaml.safe_load(i18n.TEMPLATE) is None  # all comments: changes nothing until uncommented


def test_template_created_with_a_new_private_file(ui):
    status, path = i18n.ensure_template()
    assert status == 'created' and path == ui
    assert ui.read_text(encoding='utf-8') == i18n.TEMPLATE
    assert stat.S_IMODE(os.stat(ui).st_mode) == 0o600
    assert i18n.language_info()['source'] == 'default'  # an all-comment file changes nothing
    assert fleet_config.load().get('ui') == {'language': 'system'}


def test_template_appended_once_without_touching_existing_text(ui):
    original = '# my notes\ngui:\n  keep_open: false   # mine\n'
    write(ui, original)
    os.chmod(ui, 0o640)
    assert i18n.ensure_template()[0] == 'appended'
    text = ui.read_text(encoding='utf-8')
    assert text.startswith(original) and text[len(original):] == i18n.TEMPLATE
    assert stat.S_IMODE(os.stat(ui).st_mode) == 0o640
    assert i18n.ensure_template()[0] == 'present'
    assert ui.read_text(encoding='utf-8') == text


def test_template_adds_a_missing_final_newline(ui):
    write(ui, 'gui:\n  keep_open: false')
    assert i18n.ensure_template()[0] == 'appended'
    assert ui.read_text(encoding='utf-8') == 'gui:\n  keep_open: false\n' + i18n.TEMPLATE


@pytest.mark.parametrize('text', ['ui:\n  language: en\n', 'gui: {}\n# ui:\n#   language: zh-Hans\n'])
def test_template_not_added_when_ui_is_there(ui, text):
    write(ui, text)
    assert i18n.ensure_template()[0] == 'present'
    assert ui.read_text(encoding='utf-8') == text


def test_template_skips_a_broken_file_and_a_pinned_none(ui):
    write(ui, 'gui: [\n')
    status, _ = i18n.ensure_template()
    assert status.startswith('skipped') and ui.read_text(encoding='utf-8') == 'gui: [\n'
    fleet_config.pin(fleet_config.NONE)
    try:
        assert i18n.ensure_template()[0].startswith('skipped')
    finally:
        fleet_config.pin(None)


def test_template_follows_a_symlink(ui, tmp_path):
    real = tmp_path / 'real.yaml'
    write(real, 'gui: {}\n')
    ui.parent.mkdir(parents=True, exist_ok=True)
    ui.symlink_to(real)
    assert i18n.ensure_template()[0] == 'appended'
    assert ui.is_symlink() and real.read_text(encoding='utf-8') == 'gui: {}\n' + i18n.TEMPLATE


def test_comment_only_private_file_is_an_empty_config(ui):
    """D14 ①: a private file holding only the template is read as no blocks: no error, every block as shipped."""
    shipped = fleet_config.load()  # no private file yet
    i18n.ensure_template()
    fleet_config._cache.clear()
    assert fleet_config.load() == shipped
    for name in fleet_config.BLOCKS:
        assert fleet_config.block(name) == shipped.get(name)


def test_relay_write_back_keeps_the_ui_block(ui):
    """D15: the relay's write-back rewrites the private file; a `ui` block the user wrote survives with its value
    (the commented template does not: the next `fleetmux setup` adds it again)."""
    from fleetmux.relay import params
    write(ui, 'ui:\n  language: zh-Hans\n' + i18n.TEMPLATE)
    block = params.file_block()
    hot = params.DictHot(block['hot'])
    hot.set('poll_s', block['hot']['poll_s'] + 5)
    assert params.write_back(hot, block)
    import yaml
    assert yaml.safe_load(ui.read_text(encoding='utf-8'))['ui'] == {'language': 'zh-Hans'}
    i18n.reset()
    assert i18n.language_info()['value'] == 'zh-Hans'


def test_placeholder_names_never_clash_with_the_parameters():
    assert i18n.tr('en', 'card.field', '{key}: {value}', key='k', value='v') == 'k: v'
    assert i18n.tr('zh-Hans', 'card.field', '{key}: {value}', key='k', value='v') == 'k：v'
    assert i18n.t('x.y', '{lang} {default}', lang='a', default='b') == 'a b'
