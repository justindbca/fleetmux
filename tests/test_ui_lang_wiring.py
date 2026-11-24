"""ui.language wiring (docs/design.md §63): the terminal server gets the language from Python only, `fleetmux setup`
shows it with its source and writes the template, the demo recording forces en, the table ships in the package."""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from fleetmux import discover, fleet, fleet_config, i18n
from fleetmux.backends import fleetmux as fm

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def private(monkeypatch, tmp_path):
    path = tmp_path / 'private' / 'fleet.yaml'
    monkeypatch.setenv('FLEETMUX_CONFIG', str(path))
    fleet_config.pin(None)
    i18n.reset()
    yield path
    i18n.reset()


# --- the server is started with the resolved language ------------------------------------------------------------

@pytest.mark.parametrize('lang', ['en', 'zh-Hans'])
def test_spawn_server_passes_the_resolved_language(monkeypatch, tmp_path, lang):
    monkeypatch.setenv(i18n.ENV, lang)
    i18n.reset()
    monkeypatch.setattr(fm, 'fleetmux_bin', lambda: '/bin/true')
    mgr = fm.FleetmuxSessionManager(session_name='ui-lang', state_dir=tmp_path / 'state')
    mgr.runtime_root.mkdir(parents=True)
    seen = {}
    calls = iter([None, {'ok': True}])
    monkeypatch.setattr(mgr, '_hello', lambda: next(calls, {'ok': True}))

    def run(argv, **kw):
        seen['argv'], seen['env'] = argv, kw.get('env')
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(fm.subprocess, 'run', run)
    assert mgr._spawn_server()
    assert seen['env'][i18n.ENV] == lang
    assert seen['env'].get('HOME') == os.environ.get('HOME')  # the rest of the environment as before
    assert '--language' not in seen['argv']  # an older binary would reject an unknown flag


def test_spawn_server_overrides_an_inherited_value(monkeypatch, tmp_path, private):
    """The server always gets the value Python resolved, never a stale one from the caller's environment."""
    private.parent.mkdir(parents=True)
    private.write_text('ui:\n  language: zh-Hans\n', encoding='utf-8')
    monkeypatch.delenv(i18n.ENV, raising=False)
    i18n.reset()
    assert i18n.server_env() == {i18n.ENV: 'zh-Hans'}


# --- fleetmux setup (§1d) -----------------------------------------------------------------------------------------

def _setup(monkeypatch, capsys, *args):
    monkeypatch.setattr(discover, 'run', lambda: {'clis': {}, 'probed_at': 'now', 'default': None})
    monkeypatch.setattr(discover, 'save', lambda result: None)
    fleet.main(['setup', *args])
    return capsys.readouterr().out


def test_setup_shows_language_and_source_and_writes_the_template(monkeypatch, capsys, private):
    monkeypatch.delenv(i18n.ENV, raising=False)
    monkeypatch.setattr(i18n, '_platform', lambda: 'linux')
    monkeypatch.setenv('LANG', 'zh_CN.UTF-8')
    for name in ('LC_ALL', 'LC_MESSAGES'):
        monkeypatch.delenv(name, raising=False)
    i18n.reset()
    out = _setup(monkeypatch, capsys)
    assert 'ui language: zh-Hans (setting: system; source: default; system: LANG=zh_CN.UTF-8)' in out
    assert 'ui.language template: created in ' + str(private) in out
    assert 'applies at the next fleetmux start' in out
    assert private.read_text(encoding='utf-8') == i18n.TEMPLATE


def test_setup_names_the_private_config_and_appends_once(monkeypatch, capsys, private):
    monkeypatch.delenv(i18n.ENV, raising=False)
    private.parent.mkdir(parents=True)
    private.write_text('ui:\n  language: en\n', encoding='utf-8')
    i18n.reset()
    out = _setup(monkeypatch, capsys)
    assert 'ui language: en (setting: en; source: private config %s)' % private in out
    assert 'ui.language template: present in ' + str(private) in out
    assert private.read_text(encoding='utf-8') == 'ui:\n  language: en\n'


def test_setup_shows_the_env_source(monkeypatch, capsys, private):
    monkeypatch.setenv(i18n.ENV, 'en')
    i18n.reset()
    assert 'source: env ' + i18n.ENV in _setup(monkeypatch, capsys)


def test_setup_json_carries_the_language(monkeypatch, capsys, private):
    monkeypatch.setenv(i18n.ENV, 'en')
    i18n.reset()
    out = json.loads(_setup(monkeypatch, capsys, '--json'))
    assert out['ui_language']['value'] == 'en' and out['ui_language']['source'] == 'env ' + i18n.ENV
    assert out['ui_template'] == {'status': 'created', 'path': str(private)}


# --- the demo recording forces en (§1.5) ---------------------------------------------------------------------------

def test_record_demo_forces_english():
    script = (REPO / 'scripts' / 'record_demo.sh').read_text(encoding='utf-8')
    server = script[script.index('env -u FLEETMUX_HOME'):script.index('--record "$W/rec"')]
    assert 'FLEETMUX_UI_LANGUAGE=en' in server, server
    spawn_env = re.search(r"^env = \{.*\}$", script, re.M).group(0)
    assert "'FLEETMUX_UI_LANGUAGE': 'en'" in spawn_env, spawn_env


# --- packaging (D10) -------------------------------------------------------------------------------------------------

def test_package_data_and_docker_carry_the_table():
    pyproject = (REPO / 'pyproject.toml').read_text(encoding='utf-8')
    data = pyproject[pyproject.index('[tool.setuptools.package-data]'):]
    assert '"locales/*.json"' in data.split('[tool.pytest', 1)[0]
    docker = (REPO / 'docker' / 'Dockerfile').read_text(encoding='utf-8')
    copy_rust = docker.index('COPY rust/ rust/')
    copy_locales = docker.index('COPY fleetmux/locales/ fleetmux/locales/')
    build = docker.index('cargo build')
    assert copy_rust < build and copy_locales < build


# --- fleetmux demo follows ui.language (§1.5) -------------------------------------------------------------------------

DEMO_PATHS = {'redis-server': '/x/a/redis-server', 'tmux': '/x/a/tmux', 'git': '/x/b/git', 'fleetmux': '/x/c/fleetmux'}


@pytest.mark.parametrize('lang', ['en', 'zh-Hans'])
def test_demo_passes_the_callers_language_into_its_private_world(monkeypatch, private, lang):
    """The demo isolates HOME and drops every FLEETMUX_* variable, so its fleet could not see the caller's private
    config: the language is resolved in the caller's process and handed down as FLEETMUX_UI_LANGUAGE."""
    from fleetmux import demo
    monkeypatch.delenv(i18n.ENV, raising=False)
    private.parent.mkdir(parents=True)
    private.write_text('ui:\n  language: %s\n' % lang, encoding='utf-8')
    i18n.reset()
    w = demo.World(DEMO_PATHS)
    try:
        env = w.env()
        assert env[i18n.ENV] == lang
        assert 'FLEETMUX_CONFIG' not in env  # nothing else of the caller's config goes in
    finally:
        demo.cleanup(w)


def test_demo_keeps_a_forced_language(monkeypatch, private):
    from fleetmux import demo
    private.parent.mkdir(parents=True)
    private.write_text('ui:\n  language: zh-Hans\n', encoding='utf-8')
    monkeypatch.setenv(i18n.ENV, 'en')  # record_demo.sh
    i18n.reset()
    w = demo.World(DEMO_PATHS)
    try:
        assert w.env()[i18n.ENV] == 'en'
    finally:
        demo.cleanup(w)


# --- every process of a run speaks the language its start resolved ----------------------------------------------------

def test_a_run_pins_its_language_for_its_daemon_and_panes(monkeypatch, private):
    """The daemon and the agents run in terminal panes, which do not inherit FLEETMUX_UI_LANGUAGE, and a demo's panes
    have their own HOME: `start` records the language in config.json and pin_paths applies it in every process."""
    private.parent.mkdir(parents=True)
    private.write_text('ui:\n  language: zh-Hans\n', encoding='utf-8')
    monkeypatch.delenv(i18n.ENV, raising=False)
    try:
        fleet.pin_paths({'ui_language': 'en'})
        assert i18n.language_info() == {'value': 'en', 'setting': 'en', 'source': 'run config', 'detail': 'config.json'}
        monkeypatch.setenv(i18n.ENV, 'zh-Hans')  # an explicit override still wins (tests, recordings)
        i18n.reset()
        assert i18n.language() == 'zh-Hans'
        monkeypatch.delenv(i18n.ENV)
        i18n.pin(None)
        assert i18n.language() == 'zh-Hans'  # unpinned: the private config again
        i18n.pin('fr')  # never a value outside the table
        assert i18n.language() == 'zh-Hans'
    finally:
        i18n.pin(None)


def test_start_records_the_language_in_the_run_config():
    import inspect
    src = inspect.getsource(fleet.start_new_run)  # start() hands a new run to start_new_run()
    assert "'ui_language': i18n.language()" in src.replace('\n', ' ') or "'ui_language': ui_language" in src
    assert 'i18n.pin(None)' in inspect.getsource(fleet.resume)  # a resume follows the current setting
