"""The decompose parameters in Redis (docs/design.md §62), as fleetmux/fleet_superpowers_hot.py does it: loaded at start
(a value changed and not yet written back survives a resume), read and written in Redis, written back to the private
fleet.yaml's decompose block every write_back_seconds and on exit, under the private file's lock that the relay's
write-back takes too. Only a root fleet writes back (a service root too); a sub-fleet never does."""
import threading
import time

import pytest
import yaml

from fleetmux import decompose, fleet_config
from fleetmux.relay import params as relay_params
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_v2_codex_claude import mixed  # noqa: F401


@pytest.fixture
def private(tmp_path, monkeypatch):
    path = tmp_path / 'fleet.yaml'
    path.write_text('# the operator\'s own notes\naccounts:\n  a: {kind: x}   # keep me\n'
                    'decompose:\n  enabled: true\n  split: {k: 2}\nrelay:\n  hot: {beat_s: 60}\n')
    monkeypatch.setenv(fleet_config.USER_ENV, str(path))
    return path


def test_load_put_and_a_resume_keep_the_unwritten_value(private_redis_v2, private):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    decompose.params_load(r, now=100)
    assert decompose.params_get(r)['split']['k'] == 2 and r.hget(decompose.META_KEY, 'dirty') == '0'
    assert r.hget(decompose.META_KEY, 'write_back_seconds') == '300'
    decompose.params_put(r, dict(decompose.params_get(r), split={'k': 3, 'margin': None, 'floor': 2}))
    assert r.hget(decompose.META_KEY, 'dirty') == '1'
    decompose.params_load(r, now=200)  # resume: the changed value is not replaced by the file's
    assert decompose.params_get(r)['split']['k'] == 3
    with pytest.raises(ValueError, match='split.k must be a number'):
        decompose.params_put(r, dict(decompose.params_get(r), split={'k': 0}))


def test_flush_waits_for_the_interval_and_replaces_only_the_decompose_block(private_redis_v2, private):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    decompose.params_load(r, now=1000)
    decompose.params_put(r, dict(decompose.params_get(r), returns_alert=4))
    assert decompose.params_flush(r, now=1100) is False and 'returns_alert' not in private.read_text()
    assert decompose.params_flush(r, now=1400) is True
    text = private.read_text()
    assert "# the operator's own notes" in text and 'a: {kind: x}   # keep me' in text
    data = yaml.safe_load(text)
    assert data['decompose']['returns_alert'] == 4 and data['relay'] == {'hot': {'beat_s': 60}}
    assert r.hget(decompose.META_KEY, 'dirty') == '0'
    decompose.params_put(r, dict(decompose.params_get(r), returns_alert=5))
    assert decompose.params_flush(r, now=1401, force=True) is True
    assert yaml.safe_load(private.read_text())['decompose']['returns_alert'] == 5


def test_the_shipped_file_is_never_written(monkeypatch):
    with pytest.raises(ValueError, match='never the shipped one'):
        fleet_config.write_user_block('decompose', {'enabled': True}, path=fleet_config.SHIPPED)


def test_a_missing_private_file_is_created_owner_only(tmp_path, monkeypatch):
    path = tmp_path / 'new' / 'fleet.yaml'
    monkeypatch.setenv(fleet_config.USER_ENV, str(path))
    fleet_config.write_user_block('decompose', {'enabled': False})
    assert yaml.safe_load(path.read_text()) == {'decompose': {'enabled': False}}
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('writer', ['decompose', 'relay'])
def test_both_writers_wait_for_the_private_file_lock(private, writer):
    done = []

    def write():
        if writer == 'decompose':
            fleet_config.write_user_block('decompose', {'enabled': False})
        else:
            block = relay_params.file_block()
            store = relay_params.DictHot(dict(block['hot'], beat_s=61))
            relay_params.write_back(store, block)
        done.append(True)
    with fleet_config.user_file_lock(private):
        thread = threading.Thread(target=write)
        thread.start()
        time.sleep(.4)
        assert not done  # held off by the lock
        before = private.read_text()
    thread.join(5)
    assert done and private.read_text() != before
    data = yaml.safe_load(private.read_text())
    assert data['accounts'] == {'a': {'kind': 'x'}}  # each writer keeps the other's blocks


def test_a_root_flushes_a_sub_fleet_never(mixed, private):
    f = mixed
    f.decompose_load()
    decompose.params_put(f.r, dict(decompose.params_get(f.r), overrun_ratio=6))
    f.cfg['parent_config'] = '/nonexistent/parent.json'
    f.decompose_flush(force=True)
    assert 'overrun_ratio' not in private.read_text()
    f.cfg.pop('parent_config')
    f.service = True  # a service root loads and writes back as well
    f.decompose_flush(force=True)
    assert yaml.safe_load(private.read_text())['decompose']['overrun_ratio'] == 6


def test_the_tree_reads_its_parameters_from_redis(mixed, private):
    f = mixed
    f.decompose_load()
    decompose.params_put(f.r, dict(decompose.params_get(f.r), split={'k': 7, 'margin': 1, 'floor': 3}))
    assert f.tree_params()['split'] == {'k': 7, 'margin': 1, 'floor': 3}
