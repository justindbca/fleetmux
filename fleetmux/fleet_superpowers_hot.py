"""The hot blocks of fleet-superpowers (``models``, ``quota``) inside a running fleet's Redis.

At every daemon start and resume, ``load`` copies each hot block from the file (migrating it once from its old
file, fleetmux/fleet_superpowers.py ``migrate``) into the fleet's Redis. While the fleet runs, ``get`` and ``put``
read and write Redis only; ``put`` marks the block dirty. ``flush`` (every daemon tick, and with ``force`` when the
daemon exits) writes the dirty blocks back to the file once ``hot.write_back_seconds`` (same YAML file, default
``DEFAULT_WRITE_BACK``) has passed since the last write-back. ``machines`` and ``workflows`` never enter Redis.

Quota: the treasurer outside this repository keeps writing ``quota.json``. ``ingest_quota`` (every daemon tick,
and once after ``load``) copies it into the hot ``quota`` block when the file changed (size or mtime, or an mtime
younger than ``RECENT_MTIME``; then the content). A missing or unreadable file keeps the last block. ``quota``
is the one reader of the brake: it reads the hot block and never opens ``quota.json``.

Keys (docs/contracts.md):

    fx:superpowers:models   string: the models block as JSON
    fx:superpowers:quota    string: the quota block as JSON
    fx:superpowers:meta     hash: write_back_seconds, loaded_at, flushed_at, dirty:models, dirty:quota ("1" / "0"),
                            quota_source (size:mtime_ns of the last ingested quota.json; "" after load)
"""
import json
import os
import time
from pathlib import Path

from . import fleet_superpowers as fs

HOT_BLOCKS = ('models', 'quota')
PREFIX = 'fx:superpowers:'
META = PREFIX + 'meta'
DEFAULT_WRITE_BACK = 300  # seconds, when the file sets no hot.write_back_seconds
RECENT_MTIME = 2.0  # seconds: quota.json is read on every tick while its mtime is this young


def key(block):
    if block not in HOT_BLOCKS:
        raise fs.InventoryError('not a hot block: %s' % block)
    return PREFIX + block


def load(r, path=None, now=None):
    """Copy the hot blocks into Redis. A block changed in Redis and not yet written back is kept (resume)."""
    now = time.time() if now is None else now
    meta = r.hgetall(META) or {}
    for block in HOT_BLOCKS:
        if meta.get('dirty:' + block) == '1' and r.get(key(block)) is not None:
            continue
        r.set(key(block), json.dumps(fs.migrate(block, path), ensure_ascii=False))
        r.hset(META, mapping={'dirty:' + block: '0'})
    r.hset(META, mapping={'write_back_seconds': str(fs.write_back_seconds(path, DEFAULT_WRITE_BACK)),
                          'loaded_at': str(now), 'flushed_at': meta.get('flushed_at') or str(now),
                          'quota_source': ''})  # the block may have come from the YAML file: ingest again


def get(r, block, path=None):
    """The block from Redis; from the file when it was never loaded."""
    raw = r.get(key(block))
    return fs.read_block(block, path) if raw is None else json.loads(raw)


def put(r, block, value):
    """Replace the block in Redis and mark it for write-back."""
    r.set(key(block), json.dumps(value, ensure_ascii=False))
    r.hset(META, mapping={'dirty:' + block: '1'})


def ingest_quota(r, source, now=None):
    """Copy ``source`` (quota.json) into the hot ``quota`` block when its content differs from the block; True
    when the block was replaced. The file is read when its size or mtime differs from the last ingest, or while
    its mtime is younger than ``RECENT_MTIME`` (a same-size rewrite inside one coarse mtime step). Missing,
    unreadable, not JSON or not a mapping: the block stays as it is."""
    now = time.time() if now is None else now
    try:
        st = os.stat(source)
    except OSError:
        return False
    stamp = '%d:%d' % (st.st_size, st.st_mtime_ns)
    if (r.hget(META, 'quota_source') or '') == stamp and now - st.st_mtime >= RECENT_MTIME:
        return False
    try:
        data = json.loads(Path(source).read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    changed = r.get(key('quota')) is None or json.loads(r.get(key('quota'))) != data
    if changed:
        put(r, 'quota', data)
    r.hset(META, mapping={'quota_source': stamp})
    return changed


def quota(r):
    """The quota readings for the brake: the hot ``quota`` block, ``{}`` when it is missing or unusable."""
    try:
        value = get(r, 'quota')
    except (fs.InventoryError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def flush(r, path=None, now=None, force=False):
    """Write the dirty blocks back when the interval has passed (or ``force``); return the blocks written."""
    now = time.time() if now is None else now
    meta = r.hgetall(META) or {}
    if not meta:
        return []
    interval = float(meta.get('write_back_seconds') or DEFAULT_WRITE_BACK)
    if not force and now - float(meta.get('flushed_at') or 0) < interval:
        return []
    done = []
    for block in HOT_BLOCKS:
        if meta.get('dirty:' + block) == '1':
            raw = r.get(key(block))
            if raw is not None:
                fs.write_block(block, json.loads(raw), path=path)
                done.append(block)
            r.hset(META, mapping={'dirty:' + block: '0'})
    r.hset(META, mapping={'flushed_at': str(now)})
    return done
