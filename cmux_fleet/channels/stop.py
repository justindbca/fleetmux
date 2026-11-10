"""Hard stop ``/stop`` (P8m; brief v2.2 §8.2 E2; contracts §8 「硬停 /stop」 Z1–Z11 and #17, security freeze batch 2).

Actor side (``handle``): publish the stop with one ``PUBLISH_LUA`` (pause in ``fx:service`` + ``stop_epoch`` + the
merged ``fx:channel:stop`` + the ``stop_req:<id>`` dedupe record), answer with a receipt, never through the
supervisor and never waiting for the daemon. If Redis cannot be reached (or the EVAL outcome is unknown) the
request goes into the run's stop flag file ``<run>/state/stop.flag`` for the daemon to import.

Daemon side (``daemon_tick``, run by ``Fleet.security_tick`` through ``run_stages``): import the stop flag
(claim by rename, publish, acknowledge by unlinking the claim), then handle ``fx:channel:stop``: ``interrupt`` /
``kill`` press Escape once per own busy / starting worker slot per stop epoch (``stop_done:<epoch>``), ``kill``
also sets the explicit stop; ``pause`` presses nothing. ``RELEASE_LUA`` deletes the stop record only when its
epoch is still the one handled.

Without a policy (``channels.security.policy(cfg) is None``) every entry point is an identity: ``handle`` answers
``unknown_command``, ``daemon_tick`` returns at once, ``keep_pause_on_resume`` is False before any Redis command.
P8m never resumes the fleet.
"""
import fcntl
import glob as _glob
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from cmux_fleet import channels
from cmux_fleet.channels import envelope, security

LEVELS = ('pause', 'interrupt', 'kill')
RANK = {'pause': 1, 'interrupt': 2, 'kill': 3}
REASON_PREFIX = 'channel stop by '
FLAG_NAME = 'stop.flag'          # <run>/state/stop.flag (Z7)
FLAG_LOCK = 'stop.flag.lock'     # writers' cross-process lock (Z7)
DONE_TTL_S = 604800              # stop_req:<id> / stop_done:<epoch> (Z3 / Z4)
REASON_MAX = 400
TEXT_MAX = 300                   # the user's words inside the reason
SERVICE = 'fx:service'

# Z5: validate everything, then write in a fixed order; Redis does not roll back a script, so the dedupe record is
# written last and a replay of the same id completes any half write. ``#!lua`` without ``allow-oom``: Redis checks
# memory before the script starts.
PUBLISH_LUA = r"""#!lua
local id, level, rank, at, by, reason = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5], ARGV[6]
local RANKS = {pause = '1', interrupt = '2', kill = '3'}
local function epoch_ok(v)
  if not v then return true end
  if v == '0' then return true end
  if #v > 16 or not string.match(v, '^[1-9]%d*$') then return false end
  return tonumber(v) < 9007199254740992
end
local function chars(s)
  local _, n = string.gsub(s, '[^\128-\191]', '')
  return n
end
if #id ~= 32 or not string.match(id, '^[0-9a-f]+$') then return {'invalid', 'id'} end
if RANKS[level] == nil or RANKS[level] ~= rank then return {'invalid', 'level'} end
local n = tonumber(at)
if n == nil or n ~= n or n <= 0 or n == math.huge then return {'invalid', 'at'} end
if chars(by) > 64 then return {'invalid', 'by'} end
if chars(reason) > 400 then return {'invalid', 'reason'} end
local ts = redis.call('TYPE', KEYS[1])['ok']
if ts ~= 'hash' and ts ~= 'none' then return {'invalid', 'type:fx:service'} end
if ts == 'hash' and not epoch_ok(redis.call('HGET', KEYS[1], 'stop_epoch')) then return {'invalid', 'stop_epoch'} end
local tk = redis.call('TYPE', KEYS[2])['ok']
if tk ~= 'hash' and tk ~= 'none' then return {'invalid', 'type:fx:channel:stop'} end
local old_rank, old_level = 0, false
if tk == 'hash' then
  local rk = redis.call('HGET', KEYS[2], 'rank')
  if rk then
    if rk ~= '1' and rk ~= '2' and rk ~= '3' then return {'invalid', 'stop.rank'} end
    old_level = redis.call('HGET', KEYS[2], 'level')
    if not old_level or RANKS[old_level] ~= rk then return {'invalid', 'stop.level'} end
    old_rank = tonumber(rk)
  end
  if not epoch_ok(redis.call('HGET', KEYS[2], 'epoch')) then return {'invalid', 'stop.epoch'} end
end
local tr = redis.call('TYPE', KEYS[3])['ok']
if tr ~= 'hash' and tr ~= 'none' then return {'invalid', 'type:stop_req'} end
if tr == 'hash' then
  local de = redis.call('HGET', KEYS[3], 'epoch')
  if de then
    if not epoch_ok(de) then return {'invalid', 'type:stop_req'} end
    return {'dup', tonumber(de)}
  end
end
local e = redis.call('HINCRBY', KEYS[1], 'stop_epoch', 1)
local es = string.format('%d', e)
local new_at = at
if redis.call('HGET', KEYS[1], 'at') == at then new_at = string.format('%.6f', n + 0.000001) end
redis.call('HSET', KEYS[1], 'paused', '1', 'reason', reason, 'at', new_at)
local keep_level, keep_rank = level, tonumber(rank)
if old_rank > keep_rank then keep_level, keep_rank = old_level, old_rank end
redis.call('HSET', KEYS[2], 'level', keep_level, 'rank', string.format('%d', keep_rank), 'id', id, 'by', by,
           'at', at, 'reason', reason, 'epoch', es)
redis.call('HSET', KEYS[3], 'epoch', es, 'level', level, 'at', at)
redis.call('EXPIRE', KEYS[3], 604800)
return {'ok', e}
"""

# Z6: only this script deletes fx:channel:stop, and only while its epoch is the one handled.
RELEASE_LUA = r"""#!lua
if redis.call('TYPE', KEYS[1])['ok'] == 'hash' and redis.call('HGET', KEYS[1], 'epoch') == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
"""


class SecurityTickError(Exception):
    """run_stages: one or more stages raised an ordinary exception. ``errors`` = [(stage_name, repr(exc)), ...]."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__('security stages failed: ' + '; '.join(n + '=' + e for n, e in self.errors))


class _RealFS:
    """The file operations of the stop flag (Z7): read / write_atomic / rename / unlink / glob / lock. Only paths
    under ``<run>/state/`` (0700) are used; files are 0600. Tests replace ``FS`` with an in-memory fake."""

    @staticmethod
    def _dir(path):
        d = Path(path).parent
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return d

    def read(self, path):
        try:
            with open(path, 'rb') as fh:
                return fh.read().decode('utf-8', errors='surrogateescape')
        except FileNotFoundError:
            return None

    def write_atomic(self, path, data, mode=0o600):
        d = self._dir(path)
        tmp = d / (Path(path).name + '.tmp-' + uuid.uuid4().hex)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(fd, 'wb') as fh:
                fh.write(data.encode('utf-8', errors='surrogateescape'))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def rename(self, src, dst):
        os.rename(src, dst)

    def unlink(self, path):
        os.unlink(path)

    def glob(self, pattern):
        return sorted(_glob.glob(str(pattern)))

    @contextmanager
    def lock(self, path):
        self._dir(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


FS = _RealFS()


# ====================================================================== shared
def pause_service(r, reason, *, now=None):
    """Writes fx:service paused/reason/at (HSET {paused: 1, reason, at}, the same fields as Fleet.pause) and no
    audit row; never resumes, never touches stop_epoch, Redis errors propagate. Shared by P8h / P8n (Z9; sec-2
    request #3)."""
    r.hset(SERVICE, mapping={'paused': 1, 'reason': reason, 'at': time.time() if now is None else now})


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


def publish(r, req):
    """One EVAL of PUBLISH_LUA (Z5) -> ('ok', epoch) | ('dup', epoch) | ('invalid', what)."""
    level = str(req.get('level'))
    at = req.get('at')
    argv = [str(req.get('id')), level, str(RANK.get(level, 0)), repr(float(at)) if isinstance(at, (int, float))
            else str(at), str(req.get('by') or ''), str(req.get('reason') or '')]
    out = r.eval(PUBLISH_LUA, 3, SERVICE, channels.STOP, channels.STOP_REQ + argv[0], *argv)
    status, value = _text(out[0]), out[1]
    return (status, _text(value)) if status == 'invalid' else (status, int(value))


def _release(r, epoch):
    return r.eval(RELEASE_LUA, 1, channels.STOP, str(epoch))


def _level(cfg):
    pol = security.policy(cfg) or {}
    level = (pol.get('stop') or {}).get('level') or 'interrupt'
    return level if level in RANK else 'interrupt'


def _config_path(cfg):
    return str(Path(cfg['run']) / 'config.json')


def _flag(cfg):
    return Path(cfg['run']) / 'state' / FLAG_NAME


def _resume_text(cfg, level):
    if level == 'kill':
        return '本 run 已显式结束，不能用 start --resume 恢复；要继续请重新 start。'
    return '恢复派工请运行 cmux-fleet resume --config ' + _config_path(cfg) + '。'


def _call(audit, **fields):
    if audit is None:
        return
    try:
        audit('security.stop', **fields)
    except Exception:  # the stop itself already happened (or is in the flag): an audit failure changes nothing
        pass


# ====================================================================== actor side
def _write_flag(cfg, req, fs):
    """Z7 writer: under the writers' lock, read the flag (bad JSON -> renamed .bad-<ts>), append, atomic replace."""
    flag = _flag(cfg)
    with fs.lock(str(flag.parent / FLAG_LOCK)):
        raw = fs.read(str(flag))
        requests = []
        if raw:
            try:
                requests = list(json.loads(raw)['requests'])
            except (ValueError, TypeError, KeyError):
                fs.rename(str(flag), str(flag) + '.bad-' + '%.6f' % time.time())
                requests = []
        requests.append(req)
        fs.write_atomic(str(flag), json.dumps({'requests': requests}, ensure_ascii=False), mode=0o600)


def handle(r, cfg, env, cmd, *, audit=None, now=time.time):
    """The actor's /stop (Z9): returns the receipt text; handle_inbound sends it with its own reply (Z8)."""
    if security.policy(cfg) is None:
        return envelope.receipt('unknown_command', command='stop')
    level = _level(cfg)
    by = '%s:%s:%s' % (env.get('channel'), env.get('account'), str(env.get('sender') or '')[:4])
    words = ' '.join(str((cmd or {}).get('reason') or '').split())[:TEXT_MAX]
    reason = (REASON_PREFIX + by + (': ' + words if words else ''))[:REASON_MAX]
    req = {'id': uuid.uuid4().hex, 'level': level, 'by': by, 'at': float(now()), 'reason': reason}
    pause_cmd = '（cmux-fleet pause --config ' + _config_path(cfg) + '）'
    sent, res = False, None
    try:
        r.ping()
        sent = True  # from here on the EVAL may have been applied
        res = publish(r, req)
    except Exception:
        res = None
    if res is not None:
        status, value = res
        if status in ('ok', 'dup'):
            _call(audit, stage='actor', id=req['id'], level=level, by=by, epoch=str(value), source='redis')
            return envelope.receipt('stopped', level=level, resume=_resume_text(cfg, level))
        _call(audit, stage='actor', id=req['id'], level=level, by=by, error='invalid:' + str(value))
        return envelope.receipt('invalid_command', reason='停止没有生效：fleet 状态异常（' + str(value) + '），请在电脑处理'
                                + pause_cmd)
    try:
        _write_flag(cfg, req, FS)
    except Exception:
        _call(audit, stage='actor', id=req['id'], level=level, by=by, error='flag_write')
        return envelope.receipt('invalid_command', reason='无法确认停止是否生效，停止标记未写成，请在电脑处理' + pause_cmd)
    _call(audit, stage='actor', id=req['id'], level=level, by=by, source='flag',
          error='eval_uncertain' if sent else 'redis_down')
    if not sent:  # ping failed: certainly nothing was written
        return envelope.receipt('stop_failed', reason='Redis 不通')
    return envelope.receipt('invalid_command', reason='无法确认停止是否生效（Redis 连接中断）；已写停止标记，daemon 下一拍处理')


# ====================================================================== daemon side
def _audit_f(f):
    return lambda **fields: f.log('security.stop', **fields)


def _consume(f, fs, claim, flag, level):
    """Publish every request of one claimed flag file; unlink it only when all were published (ok / dup)."""
    raw = fs.read(claim)
    if raw is None:
        return
    log = _audit_f(f)
    try:
        requests = json.loads(raw)['requests']
        if not isinstance(requests, list) or not all(isinstance(q, dict) for q in requests):
            raise ValueError('requests')
    except (ValueError, TypeError, KeyError):
        req = {'id': hashlib.sha256(raw.encode('utf-8', errors='surrogateescape')).hexdigest()[:32], 'level': level,
               'by': 'flag', 'at': time.time(), 'reason': REASON_PREFIX + 'flag: unreadable stop flag'}
        status, value = publish(f.r, req)
        log(stage='flag', source='flag', id=req['id'], level=level, epoch=str(value), error='unreadable')
        fs.rename(claim, str(flag) + '.bad-' + '%.6f' % time.time())
        return
    bad = False
    for q in requests:
        status, value = publish(f.r, q)
        if status == 'invalid':
            bad = True
            log(stage='flag', source='flag', id=str(q.get('id'))[:32], error='invalid:' + str(value))
        elif status == 'ok':
            log(stage='flag', source='flag', id=str(q.get('id')), level=str(q.get('level')), by=str(q.get('by')),
                epoch=str(value))
    if bad:
        fs.rename(claim, str(flag) + '.bad-' + '%.6f' % time.time())
        return
    try:
        fs.unlink(claim)
    except OSError:  # acknowledged next tick: the claim is replayed, every id is a dup
        log(stage='flag', source='flag', error='unlink')


def _import_flag(f, fs):
    flag = _flag(f.cfg)
    level = _level(f.cfg)
    for claim in fs.glob(str(flag) + '.claim-*'):  # left by a crash: replayed first
        _consume(f, fs, claim, flag, level)
    claim = str(flag) + '.claim-' + uuid.uuid4().hex
    try:
        fs.rename(str(flag), claim)
    except FileNotFoundError:
        return
    _consume(f, fs, claim, flag, level)


def _targets(f):
    """(pressable [(slot, pane)], skipped [{slot, why}]) among this fleet's own worker slots."""
    from cmux_fleet import subfleet
    owns = getattr(f.mgr, 'owns', None)
    pressable, skipped = [], []
    for slot in list(getattr(f, 'workers', []) or []):
        info = f.agent(slot) or {}
        if info.get('state') not in ('busy', 'starting'):
            continue
        pane = info.get('pane')
        if not pane:
            skipped.append({'slot': slot, 'why': 'no pane'})
        elif owns is not None and not owns(pane):
            skipped.append({'slot': slot, 'why': "pane is not in this fleet's registry"})
        elif subfleet.adopted(f.cfg, slot, info.get('generation')):
            skipped.append({'slot': slot, 'why': 'adopted'})
        else:
            pressable.append((slot, pane))
    return pressable, skipped


def daemon_tick(f, *, fs=None):
    """The daemon side (Z4 / Z6 / Z7): import the run's stop flag, handle fx:channel:stop, release it."""
    if security.policy(getattr(f, 'cfg', None)) is None:
        return None
    fs = fs if fs is not None else FS
    _import_flag(f, fs)
    if f.r.type(channels.STOP) not in ('hash', b'hash'):
        return None
    rec = {_text(k): _text(v) for k, v in f.r.hgetall(channels.STOP).items()}
    epoch, level = rec.get('epoch'), rec.get('level')
    if not epoch or level not in RANK:
        return None
    done_key = channels.STOP_DONE + epoch
    replayed = bool(f.r.hget(done_key, 'done'))
    skipped = []
    if not replayed:
        if level in ('interrupt', 'kill'):
            pressable, skipped = _targets(f)
            for slot, pane in pressable:
                if not f.r.hsetnx(done_key, slot, 'attempted:%.6f' % time.time()):
                    continue  # tried before for this epoch: at most one Escape per slot
                f.r.expire(done_key, DONE_TTL_S)
                try:
                    ok = bool(f.mgr.send_key(pane, 'Escape'))
                except Exception:
                    ok = False
                f.r.hset(done_key, slot, ('sent:' if ok else 'send_failed:') + '%.6f' % time.time())
        f.r.hsetnx(done_key, 'done', '%.6f' % time.time())
        f.r.expire(done_key, DONE_TTL_S)
    if level == 'kill':
        f.explicit_stop = True
        f.stopping = True
    if not f.r.hget(done_key, 'audited'):
        done = {_text(k): _text(v) for k, v in f.r.hgetall(done_key).items()}
        slots = {k: v for k, v in done.items() if k not in ('done', 'audited')}
        fields = dict(stage='daemon', id=rec.get('id', ''), epoch=epoch, level=level, by=rec.get('by', ''),
                      sent=sorted(s for s, v in slots.items() if v.startswith('sent:')),
                      send_failed=sorted(s for s, v in slots.items() if v.startswith('send_failed:')),
                      unknown=sorted(s for s, v in slots.items() if v.startswith('attempted:')),
                      skipped=skipped, source='redis')
        if replayed:
            fields['replayed'] = '1'
        f.log('security.stop', **fields)
        f.r.hset(done_key, 'audited', '%.6f' % time.time())
    _release(f.r, epoch)
    return epoch


def run_stages(f, stages):
    """Fleet.security_tick: each (name, fn) stage isolated; one SecurityTickError at the end (Z9). BusError and
    BaseException (a process-level stop) are never caught."""
    from cmux_fleet.message_bus import BusError
    errors = []
    for name, fn in stages:
        try:
            fn(f)
        except BusError:
            raise
        except Exception as exc:
            errors.append((name, repr(exc)))
    if errors:
        raise SecurityTickError(errors)


def keep_pause_on_resume(f):
    """#17: True (and audit security.stop stage=resume kept=1) when start --resume must keep a /stop pause."""
    if security.policy(getattr(f, 'cfg', None)) is None:
        return False
    kind = _text(f.r.type(channels.STOP))
    rec = {_text(k): _text(v) for k, v in f.r.hgetall(channels.STOP).items()} if kind == 'hash' else {}
    reason = _text(f.r.hget(SERVICE, 'reason')) or ''
    if not (kind != 'none' or reason.startswith(REASON_PREFIX)):
        return False
    f.log('security.stop', stage='resume', kept='1', level=rec.get('level') or _level(f.cfg),
          epoch=_text(f.r.hget(SERVICE, 'stop_epoch')) or '0')
    return True
