"""The hot blocks of fleet-superpowers: ``models`` and ``quota`` (fleetmux/fleet_superpowers_hot.py).

One-time migration from discovered.yaml / quota.json, start-time load into the fleet's Redis, reads and writes
through Redis while the fleet runs, write-back at the interval set in the same YAML file.
"""
import json

import pytest
import yaml

import test_fleet
from test_fleet import fx  # noqa: F401 (the fleet fixture)
from fleetmux import brake, discover, fleet, fleet_superpowers as fs, fleet_superpowers_hot as hot

QUOTA = {'at': 1, 'pools': {'pool-a': {'at': 1, 'cash_used_usd': 3.5}}}
MODELS = {'version': 1, 'probed_at': '2030-01-01T10:00:00+00:00', 'clis': {'claude': {'installed': True}}}


@pytest.fixture
def old(tmp_path, monkeypatch):
    """The old locations: quota.json under the data root, discovered.yaml under the fleet home."""
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))
    monkeypatch.setattr(fs, 'OLD_QUOTA', None)  # the default old location under the temporary HOME
    root = tmp_path / 'user' / '.fleetmux'
    root.mkdir(parents=True)
    (root / 'quota.json').write_text(json.dumps(QUOTA))
    monkeypatch.setenv(discover.CACHE_ENV, str(tmp_path / 'fleet-home' / 'discovered.yaml'))
    discover.save(MODELS)  # the old cache, written while the variable names it
    monkeypatch.delenv(discover.CACHE_ENV)
    monkeypatch.setenv('FLEETMUX_HOME', str(tmp_path / 'fleet-home'))
    return {'quota': root / 'quota.json', 'models': tmp_path / 'fleet-home' / 'discovered.yaml'}


# -- one-time migration ----------------------------------------------------------------------------------

def test_old_locations(old):
    assert fs.old_location('quota') == old['quota']
    assert fs.old_location('models') == old['models']


def test_migrate_reads_the_old_file_once_and_writes_the_new_one(old):
    assert not fs.file_path().exists()
    assert fs.migrate('quota') == QUOTA
    assert fs.read_block('quota') == QUOTA
    old['quota'].write_text(json.dumps({'at': 2}))  # never read again: the new file is the source
    assert fs.migrate('quota') == QUOTA
    assert old['quota'].exists()  # the old file is left where it is
    assert fs.migrate('models') == MODELS


def test_migrate_with_nothing_anywhere(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))
    assert fs.migrate('quota') == {}
    assert not fs.file_path().exists()


def test_discover_reads_and_writes_the_models_block(old):
    assert discover.load() == MODELS  # migrated on first read
    assert fs.read_block('models') == MODELS
    discover.save(dict(MODELS, probed_at='later'))
    assert fs.read_block('models')['probed_at'] == 'later'
    assert yaml.safe_load(old['models'].read_text())['probed_at'] == MODELS['probed_at']  # old file untouched
    assert discover.load()['probed_at'] == 'later'


def test_discover_keeps_an_explicit_cache_file(tmp_path, monkeypatch):
    monkeypatch.setenv(discover.CACHE_ENV, str(tmp_path / 'd.yaml'))
    discover.save(MODELS)
    assert (tmp_path / 'd.yaml').exists() and discover.load() == MODELS
    assert not fs.file_path().exists()


# -- hot store -------------------------------------------------------------------------------------------

def test_load_puts_the_hot_blocks_into_redis(old):
    r = test_fleet.FakeRedis()
    fs.write_back_seconds()  # no file yet: the default
    hot.load(r, now=100.0)
    assert json.loads(r.get('fx:superpowers:quota')) == QUOTA
    assert json.loads(r.get('fx:superpowers:models')) == MODELS
    meta = r.hgetall('fx:superpowers:meta')
    assert meta['write_back_seconds'] == str(hot.DEFAULT_WRITE_BACK) and meta['loaded_at'] == '100.0'


def test_reads_and_writes_go_through_redis_and_flush_at_the_interval(old):
    fs.migrate('quota')
    text = fs.file_path().read_text()
    fs.file_path().write_text(text + 'hot:\n  write_back_seconds: 60\n')
    r = test_fleet.FakeRedis()
    hot.load(r, now=1000.0)
    assert r.hgetall('fx:superpowers:meta')['write_back_seconds'] == '60'
    newer = {'at': 5, 'pools': {}}
    hot.put(r, 'quota', newer)
    assert hot.get(r, 'quota') == newer  # served from Redis
    assert fs.read_block('quota') == QUOTA  # the file is not written yet
    assert hot.flush(r, now=1030.0) == []  # inside the interval
    assert fs.read_block('quota') == QUOTA
    assert hot.flush(r, now=1061.0) == ['quota']
    assert fs.read_block('quota') == newer
    assert hot.flush(r, now=2000.0) == []  # nothing dirty: no write
    assert 'hot:\n  write_back_seconds: 60\n' in fs.file_path().read_text()  # the setting line stays


def test_force_flush_and_resume_keep_unwritten_values(old):
    r = test_fleet.FakeRedis()
    hot.load(r, now=0.0)
    hot.put(r, 'models', {'version': 9})
    hot.load(r, now=1.0)  # a resume: the unwritten value is kept, not reloaded from the file
    assert hot.get(r, 'models') == {'version': 9}
    assert hot.flush(r, now=2.0, force=True) == ['models']
    assert fs.read_block('models') == {'version': 9}


def test_get_without_a_loaded_key_reads_the_file(old):
    fs.migrate('quota')
    assert hot.get(test_fleet.FakeRedis(), 'quota') == QUOTA


def test_bad_write_back_setting_is_refused(tmp_path):
    path = tmp_path / 'f.yaml'
    path.write_text('hot:\n  write_back_seconds: 0\n')
    assert fs.write_back_seconds(path) == hot.DEFAULT_WRITE_BACK
    with pytest.raises(fs.InventoryError, match='write_back_seconds'):
        fs.write_block('quota', {}, path=path)


# -- the fleet daemon ------------------------------------------------------------------------------------

def test_fleet_start_loads_and_tick_writes_back(old, fx, monkeypatch):
    import os
    os.utime(old['quota'], (1, 1))  # an old quota.json: read once, then left alone until it changes
    f = fx
    monkeypatch.setattr(f, 'start_all', lambda: None)
    f.stopping = True
    f.run()  # start and resume: load the hot blocks
    assert json.loads(f.r.get('fx:superpowers:quota')) == QUOTA
    f.r.hset('fx:superpowers:meta', mapping={'write_back_seconds': '0.001', 'flushed_at': '0'})
    hot.put(f.r, 'quota', {'at': 7, 'pools': {}})
    f.tick()
    assert fs.read_block('quota') == {'at': 7, 'pools': {}}


def test_daemon_exit_writes_back():
    src = open(fleet.__file__).read()
    assert 'fleet_superpowers_hot.flush(fleet.r, force=True)' in src  # before the Redis shutdown
    assert src.index('fleet_superpowers_hot.flush(fleet.r, force=True)') < src.index('fleet.r.shutdown()')


# -- quota.json into the hot quota block -------------------------------------------------------------------

def quota_fleet(fx, tmp_path, monkeypatch):
    f = fx
    f.cfg['quota_file'] = str(tmp_path / 'quota.json')
    return f, tmp_path / 'quota.json'


def test_a_quota_file_change_shows_up_in_the_brake_reading(fx, tmp_path, monkeypatch):
    f, path = quota_fleet(fx, tmp_path, monkeypatch)
    path.write_text(json.dumps(QUOTA))
    f.tick()
    assert f.quota_reading() == QUOTA
    newer = {'at': 2, 'pools': {'pool-a': {'at': 2, 'cash_used_usd': 9.0}}}
    path.write_text(json.dumps(newer))
    f.tick()
    assert f.quota_reading() == newer
    assert hot.get(f.r, 'quota') == newer


def test_a_missing_or_bad_quota_file_keeps_the_last_block(fx, tmp_path, monkeypatch):
    f, path = quota_fleet(fx, tmp_path, monkeypatch)
    path.write_text(json.dumps(QUOTA))
    f.tick()
    path.write_text('{not json')
    f.tick()
    assert f.quota_reading() == QUOTA
    path.unlink()
    f.tick()
    assert f.quota_reading() == QUOTA


def test_brake_readers_read_the_hot_block_not_the_file(fx, tmp_path, monkeypatch):
    f, path = quota_fleet(fx, tmp_path, monkeypatch)
    hot.put(f.r, 'quota', QUOTA)  # no file at all: the reading comes from Redis
    assert f.quota_reading() == QUOTA
    assert not hasattr(brake, 'read_quota')  # the file reader is gone: only the ingest opens quota.json
    from fleetmux import runtime_v2
    for module in (fleet, runtime_v2):
        src = open(module.__file__).read()
        assert 'read_quota' not in src and src.count("cfg.get('quota_file')") == (module is fleet)


def test_the_quota_reader_works_inside_a_v2_transaction_overlay():
    """brake_tick, wall_switched and pause_owner_resume read quota while ``f.r`` is the v2 Effects overlay."""
    from fleetmux import runtime_v2
    class Typed(test_fleet.FakeRedis):
        def type(self, k):
            return 'string' if k in self.kv else 'hash' if k in self.h else 'none'
    r = Typed()
    hot.put(r, 'quota', QUOTA)
    overlay = runtime_v2.Effects(r)
    assert hot.quota(overlay) == QUOTA and overlay.commands == []  # a read only: nothing joins the plan


def test_a_same_size_rewrite_inside_one_mtime_granule_is_seen(tmp_path):
    """A coarse mtime (whole seconds on some file systems) and an equal size: the content still decides."""
    import os
    r = test_fleet.FakeRedis()
    path = tmp_path / 'q.json'
    path.write_text(json.dumps({'at': 1, 'pools': {}}))
    os.utime(path, ns=(10**18, 10**18))
    hot.ingest_quota(r, path, now=10**9)
    path.write_text(json.dumps({'at': 2, 'pools': {}}))  # same size
    os.utime(path, ns=(10**18, 10**18))  # same mtime
    assert hot.ingest_quota(r, path, now=10**9 + 1) is True
    assert hot.quota(r) == {'at': 2, 'pools': {}}
    assert hot.ingest_quota(r, path, now=10**9 + 60) is False  # old mtime, same stamp: not read again


def test_load_forgets_the_ingest_stamp_so_a_resume_ingests_again(old, tmp_path):
    import os
    r = test_fleet.FakeRedis()
    path = tmp_path / 'q.json'
    path.write_text(json.dumps({'at': 9, 'pools': {}}))
    os.utime(path, ns=(10**18, 10**18))
    assert hot.ingest_quota(r, path, now=10**10) is True  # the block changed
    assert hot.ingest_quota(r, path, now=10**10) is False  # unchanged file: nothing to do
    hot.flush(r, force=True)
    fs.write_block('quota', QUOTA)  # an older copy in the YAML file
    r.hset('fx:superpowers:meta', mapping={'dirty:quota': '0'})
    hot.load(r)  # a resume reloads the clean block from the YAML file
    assert hot.quota(r) == QUOTA
    assert hot.ingest_quota(r, path, now=10**10) is True  # the newer quota.json wins again
    assert hot.quota(r) == {'at': 9, 'pools': {}}
