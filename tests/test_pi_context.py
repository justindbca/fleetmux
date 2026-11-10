import json

import pytest

from cmux_fleet import fleet_config, pi_adapter

SOURCE = {'providers': {
    'workbuddy-ds': {'baseUrl': 'http://127.0.0.1:3425/v1', 'models': [
        {'id': 'workbuddy-ai/deepseek-v4.1-flash', 'contextWindow': 200000},
        {'id': 'no-window-model'}]},
    'flashnext': {'models': [{'id': 'Qwen-Local', 'contextWindow': 100000}]}}}


def write_source(tmp_path, data=SOURCE):
    src = tmp_path / 'agent'
    src.mkdir()
    (src / 'auth.json').write_text('{}')
    (src / 'models.json').write_text(json.dumps(data))
    return src


def models(home):
    return json.loads((home / 'models.json').read_text())


def window(data, provider, model_id):
    return next(m for m in data['providers'][provider]['models'] if m['id'] == model_id).get('contextWindow')


def test_default_fills_only_models_without_a_window(tmp_path):
    home = pi_adapter.prepare_home(tmp_path / 'home', write_source(tmp_path), {'default_window': 400000})
    data = models(home)
    assert not (home / 'models.json').is_symlink()
    assert window(data, 'workbuddy-ds', 'no-window-model') == 400000
    assert window(data, 'workbuddy-ds', 'workbuddy-ai/deepseek-v4.1-flash') == 200000  # own value kept
    assert window(data, 'flashnext', 'Qwen-Local') == 100000                          # memory-capped local model kept
    assert (home / 'auth.json').is_symlink()


def test_per_model_overrides_own_value_and_builtin_goes_to_model_overrides(tmp_path):
    ctx = {'default_window': 400000, 'per_model': {
        'workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash': 400000, 'openai-codex/gpt-6-astra': 350000}}
    data = models(pi_adapter.prepare_home(tmp_path / 'home', write_source(tmp_path), ctx))
    assert window(data, 'workbuddy-ds', 'workbuddy-ai/deepseek-v4.1-flash') == 400000
    assert data['providers']['openai-codex'] == {'modelOverrides': {'gpt-6-astra': {'contextWindow': 350000}}}


def test_no_block_keeps_the_symlink(tmp_path):
    src = write_source(tmp_path)
    for ctx in (None, {}, {'default_window': None, 'per_model': {}}):
        home = pi_adapter.prepare_home(tmp_path / ('home' + str(id(ctx))), src, ctx)
        assert (home / 'models.json').is_symlink()


def test_source_file_is_not_modified(tmp_path):
    src = write_source(tmp_path)
    before = (src / 'models.json').read_text()
    pi_adapter.prepare_home(tmp_path / 'home', src, {'default_window': 400000})
    assert (src / 'models.json').read_text() == before


@pytest.mark.parametrize('ctx', [{'default_window': 0}, {'default_window': True}, {'default_window': '400k'},
                                 {'per_model': {'no-slash': 400000}}, {'per_model': {'p/m': -1}}])
def test_bad_values_are_refused(tmp_path, ctx):
    with pytest.raises(ValueError):
        pi_adapter.prepare_home(tmp_path / 'home', write_source(tmp_path), ctx)


def test_shipped_default_is_400k_and_private_file_may_override(tmp_path, monkeypatch):
    monkeypatch.setenv(fleet_config.USER_ENV, str(tmp_path / 'none.yaml'))
    fleet_config._cache.clear()
    assert fleet_config.block('pi_context') == {'default_window': 400000, 'per_model': {}}
    private = tmp_path / 'fleet.yaml'
    private.write_text('pi_context:\n  default_window: 300000\n  per_model: {}\n')
    monkeypatch.setenv(fleet_config.USER_ENV, str(private))
    fleet_config._cache.clear()
    assert fleet_config.block('pi_context')['default_window'] == 300000
    fleet_config._cache.clear()
