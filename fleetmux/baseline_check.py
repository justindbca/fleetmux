"""The daemon's switch-baseline check (fleetmux/baseline.py holds the block and its precedence rule).

Every daemon (the root and every sub-fleet, at any depth) checks itself on its own tick, every
``baseline.check_every_s`` seconds: it reads this machine's baseline for its own shape and compares the switches it
runs with now (the live advisor and max_busy, the run config for the rest).

* A hot switch with an existing writer (advisor: ``Fleet.set_advisor`` with the roster write) is set back at once
  and reported as restored.
* Every other deviation is reported once (per fleet, switch, running value and baseline value), and once more as
  back to normal when it is gone. A switch under a live exception is not checked; past its ``until`` it is.
* A restart switch that ``start --resume`` applies gets an answer key ``baseline:<switch>`` at that fleet. Only
  the user approves it, with ``fleetmux answer --config <that fleet's config> baseline:<switch> approve`` from a
  terminal outside the fleet's run directories (no supervisor decision can rule on it: it is never an escalation).
  On approve the daemon stops by itself the way SIGTERM stops it (run, Redis and agents stay; no result.json) and
  ``fleetmux answer`` then runs ``start --resume <run>``, which writes the baseline into the run config.
  A switch that needs a new run (worker_kit, worker_domains, kit_fleet) and max_busy (no roster writer) are only
  reported, with what to do.
* A broken block and a machine without baseline entry are reported once by the root; nothing is checked then.
  An inventory file without any ``baseline`` block: a root service fleet tells the user once per run (not its
  supervisor); every other fleet, and a machine without the inventory file, only note it in the audit log.

Reports: the root -> its supervisor + the user (blocked_scope.push); a sub-fleet -> a ``baseline-*.json`` notice
in its parent's ``subfleet-notices`` (Fleet.relay_notices): the nearest parent's supervisor is told (information
only) and the notice goes on up to the root, which tells the user (only the user can approve).

Redis keys (per fleet): ``fx:baseline:cache`` (hash: stamp, raw, error, machine: the block as last read; read again
when the file's stamp changes; never written back to the file), ``fx:baseline:state`` (JSON: what was reported),
``fx:baseline:next`` (wall time of the next check). There is no switch that turns the check off.
"""
import datetime
import hashlib
import json
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from . import baseline
from . import fleet_superpowers as fs

CACHE = 'fx:baseline:cache'
STATE = 'fx:baseline:state'
NEXT = 'fx:baseline:next'
KEY = 'baseline:'  # the answer key prefix (fleetmux answer)
NOTICE = 'baseline-'  # <parent run>/subfleet-notices/baseline-<id>.json
EVENT = 'baseline'  # the push's event name
INFO = ('This is information only: there is nothing for you to rule on and no decision key for it (a restart is the '
        'user\'s own approval).')


# -- the block, through the Redis read cache --------------------------------------------------------------------

def cached(r, path=None):
    """(Loaded, machine) of this machine's inventory: from Redis while the file's stamp is unchanged, else read from
    the file and put in Redis. Never written back."""
    path = Path(path) if path else fs.file_path()
    stamp = json.dumps(baseline._stamp(path))
    row = r.hgetall(CACHE) or {}
    if row.get('stamp') == stamp:
        machine = row.get('machine') or None
        if row.get('error'):
            return baseline.Loaded(None, row['error']), machine
        if not row.get('raw'):
            return baseline.Loaded(None, None), machine
        try:
            return baseline.Loaded(baseline.Block(json.loads(row['raw'])), None), machine
        except (ValueError, baseline.BaselineError):
            pass  # read the file again below
    loaded = baseline.load(path)
    machine = baseline.machine_name(path)
    raw = json.dumps(loaded.block.raw, default=str) if loaded.block is not None else ''
    r.hset(CACHE, mapping={'stamp': stamp, 'raw': raw, 'error': loaded.error or '', 'machine': machine or ''})
    return loaded, machine


# -- state ------------------------------------------------------------------------------------------------------

def _state(f):
    try:
        value = json.loads(f.r.get(STATE) or '{}')
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _save(f, state):
    f.r.set(STATE, json.dumps(state, ensure_ascii=False, sort_keys=True))


def waiting(f):
    """{answer key: state row} of the restart switches that wait for the user's answer at this fleet."""
    return {row['answer']: row for row in _state(f).values()
            if isinstance(row, dict) and row.get('answer') and not row.get('declined') and not row.get('approved')}


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts, default=str, sort_keys=True).encode()).hexdigest()[:16]


# -- values and words -------------------------------------------------------------------------------------------

def running(f):
    """{switch: value} this daemon runs with now (normalised as in the block)."""
    out = baseline._current_from_cfg(f.cfg)
    out['advisor'] = bool(f.advisor_enabled())
    out['max_busy'] = int(f.cfg.get('max_busy') or 0)
    return out


def show(value):
    if value is None:
        return 'off'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, list):
        return ','.join(value) if value else '[]'
    return str(value)


def _flag(key, value):
    if key == 'worker_kit':
        return '--worker-kit ' + (shlex.quote(str(value)) if isinstance(value, str) else '<kit directory>')
    if key == 'worker_domains':
        return '--worker-domains ' + (','.join(value) if value else '""')
    if key == 'kit_fleet':
        return '--kit-fleet ' + str(value) if value else '(no --kit-fleet)'
    if key == 'max_busy':
        return '--max-busy ' + str(value)
    return '--' + key.replace('_', '-') + ' ' + show(value)


def command_base(cfg):
    """The command prefix of this run's own fleetmux (its pinned locations in front)."""
    from . import fleet
    return fleet.command_base(cfg)


def restart_argv(cfg):
    """`start --resume <run>`: the existing restart of a run whose daemon is gone."""
    return command_base(cfg) + ['start', '--allow-development-v2', '--resume', str(Path(cfg['run']))]


def answer_command(f, key):
    return shlex.join(command_base(f.cfg) + ['answer', '--config', str(f.path), key, 'approve'])


def _who(f, shape, machine):
    return ('fleet ' + str(baseline.fleet_name(f.cfg) or f.run_dir.name) + ' (' + shape + ' on '
            + str(machine) + ', ' + str(f.path) + ')')


# -- the check --------------------------------------------------------------------------------------------------

def tick(f, now=None):
    """Fleet.tick part: one check every ``check_every_s`` seconds (the first one at once), and at once when the
    inventory file's stamp changed since the last check. None while this daemon stops."""
    if getattr(f, 'r', None) is None:  # no bus yet
        return
    if getattr(f, 'stopping', False):  # stopping (e.g. an accepted approve): the next daemon checks, not this one
        return
    now = time.time() if now is None else now
    try:
        due = float(f.r.get(NEXT) or 0)
    except ValueError:
        due = 0
    if now < due and f.r.hget(CACHE, 'stamp') == json.dumps(baseline._stamp(fs.file_path())):
        return  # not due and the file is unchanged (a changed file, e.g. a fixed block, is checked at once)
    every = baseline.DEFAULT_CHECK_EVERY_S
    try:
        every = check(f, now) or every
    finally:
        f.r.set(NEXT, str(now + every))


def check(f, now=None):
    """Compare this fleet with the baseline once; returns the block's check interval (None without a block)."""
    now = time.time() if now is None else now
    loaded, machine = cached(f.r)
    root = not f.cfg.get('parent_config')
    state = _state(f)
    if loaded.error:
        if root:
            _once(f, state, 'broken', loaded.error, '[baseline] the baseline block is broken, nothing is checked '
                  'until it is fixed: ' + loaded.error + '. The other blocks of ' + str(fs.file_path())
                  + ' load as before.')
        _save(f, state)
        return None
    state.pop('broken', None)
    block = loaded.block
    if block is None:
        if 'none' not in state:  # once per run (the state lives in the run's Redis)
            state['none'] = {'at': now}
            path = fs.file_path()
            f.log('baseline.none', path=str(path))
            if root and baseline.shape_of(f.cfg) == 'root_service' and path.exists():
                from . import blocked_scope  # the user only: only the user can define a baseline
                blocked_scope.push(f, '[baseline] no baseline is defined: ' + str(path) + ' has no baseline: block, '
                                   'so no fleet on this machine is checked. Add one (baseline.machines.' + str(
                                       machine or '<machine>') + '.<shape>: switches) to have start and the daemon '
                                   'use it.', event=EVENT)
        _save(f, state)
        return None
    state.pop('none', None)
    every = block.check_every_s
    if machine is None or machine not in block.machines:
        if root:
            _once(f, state, 'nobaseline', machine, '[baseline] no baseline for this machine (' + str(machine or
                  'no machines entry with source: self') + ') in ' + str(fs.file_path()) + ': nothing is checked. '
                  'Add baseline.machines.' + str(machine or '<machine>') + ' to define one.')
        _save(f, state)
        return every
    state.pop('nobaseline', None)
    shape = baseline.shape_of(f.cfg)
    if shape is None:
        _save(f, state)
        return every
    table = block.table(machine, shape)
    fleet_name = baseline.fleet_name(f.cfg)
    when = datetime.datetime.fromtimestamp(now)
    have = running(f)
    who = _who(f, shape, machine)
    for key, sw in baseline.REGISTRY.items():
        slot = 'switch:' + key
        if key not in table or block.exception(machine, shape, fleet_name, key, when):
            state.pop(slot, None)  # not in the baseline, or a live exception: not checked
            continue
        base, value = table[key], have.get(key)
        if baseline.same(key, value, base):
            if slot in state:
                state.pop(slot)
                _report(f, '[baseline] ' + who + ': ' + key + ' is back to normal (' + show(value)
                        + ', the baseline value).')
            continue
        if sw.restore == 'auto':
            why = _restore(f, key, base)
            if why is None:
                state.pop(slot, None)
                _report(f, '[baseline] ' + who + ': ' + key + ' was running ' + show(value) + ', baseline '
                        + show(base) + ': restored (set to ' + show(base) + ' through its existing writer).')
                continue
        else:
            why = None
        digest = _digest(str(f.run_dir), key, value, base)
        prev = state.get(slot) or {}
        if prev.get('digest') == digest and not prev.get('approved'):
            continue  # reported already (an approved restart that did not bring the baseline is reported again)
        row = {'digest': digest, 'value': value, 'baseline': base, 'at': now}
        head = ('[baseline] ' + who + ': ' + key + ' differs from this machine\'s baseline: running ' + show(value)
                + ', baseline ' + show(base) + ' (no exception).')
        if prev.get('approved'):
            head += ' A restart the user approved did not bring the baseline value.'
        if why:
            text = head + ' It could not be restored: ' + why + '. Set it yourself.'
        elif sw.resume_apply:
            row['answer'] = KEY + key
            text = (head + ' It takes effect on a restart; the program does not restart by itself. To restart this '
                    'fleet with the baseline, the user approves with fleetmux answer from their own terminal (outside '
                    'the fleet\'s run directories): `' + answer_command(f, KEY + key) + '`. That stops this fleet\'s daemon (the run and its '
                    'Redis stay, no result.json) and runs `' + shlex.join(restart_argv(f.cfg)) + '`, which writes the '
                    'baseline into the run config, keeps the live worker sessions (respawning the gone ones) and starts a new '
                    'generation of the supervisor (a sub-fleet keeps an adopted supervisor whose session still runs). To keep the running value instead, add an exception with reason '
                    'and until to the baseline block of ' + str(fs.file_path()) + '.')
        elif key == 'max_busy':
            text = (head + ' There is no program writer for it: set `max_busy: ' + show(base) + '` in this run\'s '
                    'roster (' + str(f.cfg.get('roster') or 'roster.yaml') + ', reloaded live), or start a new run '
                    '(start takes the baseline by itself; ' + _flag(key, base) + ').')
        else:
            text = (head + ' It needs a new run (start --resume cannot change it): stop this fleet (`'
                    + shlex.join(command_base(f.cfg) + ['stop', '--config', str(f.path)]) + '`) and start a new '
                    'run with your usual start command; start takes the baseline by itself (' + _flag(key, base)
                    + ' if you pass flags). Nothing is done automatically.')
        state[slot] = row
        _report(f, text)
    _save(f, state)
    return every


def _once(f, state, slot, what, text):
    digest = _digest(slot, what)
    if (state.get(slot) or {}).get('digest') == digest:
        return
    state[slot] = {'digest': digest, 'at': time.time()}
    _report(f, text)


def _restore(f, key, base):
    """Set hot switch ``key`` back through its existing writer; None when done, else why not."""
    if key != 'advisor':
        return 'no writer for ' + key
    why = f.set_advisor(bool(base), 'baseline', write=True)
    if why is None and f.advisor_enabled() != bool(base):
        why = 'the advisor did not change'
    f.log('baseline.restored' if why is None else 'baseline.restore_failed', switch=key, value=bool(base),
          error=why or '')
    return why


# -- reports ----------------------------------------------------------------------------------------------------

def _report(f, text):
    f.log('baseline.report', text=text[:500])
    parent = f.cfg.get('parent_config')
    if parent:
        _notice_up(f, parent, {'kind': 'baseline', 'origin_run': str(f.run_dir), 'task': f.cfg.get('task_id'),
                               'config': str(f.path), 'text': text})
        return
    _tell_root(f, text, supervisor=True)


def _notice_up(f, parent_config, note):
    from . import fleet
    folder = Path(fleet.load(parent_config)['run']) / 'subfleet-notices'
    folder.mkdir(parents=True, exist_ok=True)
    fleet.save(folder / (NOTICE + uuid.uuid4().hex[:12] + '.json'), note)


def _tell_root(f, text, supervisor):
    from . import blocked_scope
    if supervisor and f.running():
        f.queue('supervisor', text + '\n' + INFO)
    blocked_scope.push(f, text, event=EVENT)


def relay(f, path):
    """Fleet.relay_notices: a sub-fleet's baseline notice. The nearest parent tells its supervisor (information
    only); the notice goes on up to the root, which tells the user."""
    from . import fleet
    try:
        note = fleet.load(path)
    except (OSError, ValueError):
        note = None
    path.unlink(missing_ok=True)
    if not note:
        return
    text = '[baseline] sub-fleet ' + str(note.get('task') or '') + ': ' + str(note.get('text') or '')
    if not note.get('relay_only') and f.running():
        f.queue('supervisor', text + '\n' + INFO)
    f.log('baseline.relayed', task=note.get('task'), origin=note.get('origin_run'))
    if f.cfg.get('parent_config'):
        _notice_up(f, f.cfg['parent_config'], dict(note, relay_only=True))
    else:
        _tell_root(f, text, supervisor=False)


# -- the user's answer ------------------------------------------------------------------------------------------

def user_answer(f, ev):
    """The daemon side of ``fleetmux answer baseline:<switch> approve|reject`` (blocked_scope.user_answer)."""
    key, decision = str(ev.get('key') or ''), str(ev.get('decision') or '')
    if decision not in ('approve', 'reject'):
        return {'status': 'rejected', 'reason': 'answer approve or reject'}
    rows = waiting(f)
    if key not in rows:
        return {'status': 'rejected', 'reason': 'nothing waits under ' + key[:80] + ' in this fleet (' + str(f.run_dir)
                + '); waiting: ' + (', '.join(sorted(rows)) or 'none')}
    state = _state(f)
    slot = 'switch:' + key[len(KEY):]
    f.log('baseline.user_answered', level='warn', key=key, decision=decision, by='user', pid=str(ev.get('pid') or ''))
    if decision == 'reject':
        state[slot]['declined'] = True  # the same deviation is not asked again; a new value is
        _save(f, state)
        return {'status': 'accepted', 'key': key, 'decision': decision}
    for row in state.values():  # one restart answers every switch of this fleet that waits for it
        if isinstance(row, dict) and row.get('answer') and not row.get('declined'):
            row['approved'] = time.time()
    _save(f, state)
    if f.cfg.get('parent_config'):  # the parent does not count this daemon as gone while it restarts
        from . import subfleet
        rec = subfleet.record(f.run_dir)
        if rec:
            rec['launched'] = time.time()
            subfleet.save_record(f.run_dir, rec)
    f.stopping = True  # as SIGTERM: the loop ends, no result.json, Redis and agents stay for start --resume
    return {'status': 'accepted', 'key': key, 'decision': decision,
            'restart': {'run': str(f.run_dir), 'config': str(f.path), 'argv': restart_argv(f.cfg)}}


def restart_after_approval(cfg, ans, runner=None, wait_s=60, out=print):
    """`fleetmux answer` after an accepted approve: wait until the fleet's daemon has let go of its run, then run
    `start --resume <run>`. Returns its exit code (3: the daemon did not stop in time; nothing was run). The wait
    stays well inside subfleet.LAUNCH_GRACE, which keeps a parent from collecting a restarting sub-fleet."""
    from . import fleet
    todo = (ans or {}).get('restart')
    if not todo:
        return 0
    deadline = time.monotonic() + wait_s
    while fleet.daemon_running(todo['run']) and time.monotonic() < deadline:
        time.sleep(.5)
    argv = list(todo['argv'])
    if fleet.daemon_running(todo['run']):
        out('the daemon of ' + todo['run'] + ' has not stopped yet; run this yourself once it has: ' + shlex.join(argv))
        return 3
    out('restarting with the baseline: ' + shlex.join(argv))
    code = (runner or subprocess.call)(argv)
    if code:
        out('start --resume failed (exit ' + str(code) + '): the daemon of ' + todo['run'] + ' is down; run it again: '
            + shlex.join(argv))
    return code


# -- the release switch leaves the baseline alone (`fleetmux superpowers baseline check`) ------------------------

def block_hash(text):
    """sha256 of the ``baseline`` block as written (its lines in the file; '' without one)."""
    return hashlib.sha256(baseline.block_text(text).encode('utf-8')).hexdigest()


def _open_deviations(cfg):
    """The deviations a running fleet's daemon has reported and not seen back to normal (from its Redis)."""
    from . import fleet
    r = fleet.connect(cfg)
    try:
        state = json.loads(r.get(STATE) or '{}')
    finally:
        r.close()
        r.connection_pool.disconnect()
    out = []
    for slot, row in sorted(state.items() if isinstance(state, dict) else []):
        if slot.startswith('switch:') and isinstance(row, dict):
            out.append({'switch': slot[len('switch:'):], 'value': row.get('value'), 'baseline': row.get('baseline')})
        elif slot in ('broken', 'nobaseline'):
            out.append({'switch': '(' + slot + ')', 'value': None, 'baseline': None})
    return out


def upgrade_report(path=None):
    """Read-only: {path, hash, fleets: [{name, config, workflows, deviations}]} for this machine: the baseline
    block's hash, and per running fleet (the ``machines.<self>.fleets`` rows whose daemon holds its run lock) the
    workflows loaded into it and the deviations its daemon reports. Called before and after a release switch (and
    by an upgrade command); it writes nothing."""
    from . import fleet, superpowers_peer
    path = Path(path) if path else fs.file_path()
    try:
        text = path.read_text()
    except OSError:
        text = ''
    report = {'path': str(path), 'hash': block_hash(text), 'fleets': []}
    try:
        me = fs.self_name(path)
        rows = (fs.machine(me, path) or {}).get('fleets') or [] if me else []
    except (fs.InventoryError, OSError):
        rows = []
    for row in rows:
        if not isinstance(row, dict) or not row.get('config'):
            continue
        config = Path(str(row['config'])).expanduser()
        try:
            cfg = fleet.load(config)
            if not cfg.get('run') or not fleet.daemon_running(cfg['run']):
                continue
        except (OSError, ValueError):
            continue
        name = str(row.get('name') or cfg.get('name') or '') + ('/' + str(row['sub']) if row.get('sub') else '')
        item = {'name': name, 'config': str(config), 'workflows': sorted((superpowers_peer.run_loads(cfg['run'])
                                                                          or {}).get('loaded') or [])}
        try:
            item['deviations'] = _open_deviations(cfg)
        except Exception as exc:  # noqa: BLE001 - a Redis that cannot be read is said, never a crash
            item['deviations'], item['error'] = [], 'cannot read its Redis: ' + str(exc)[:200]
        report['fleets'].append(item)
    return report


def upgrade_problems(before_hash, before=None, after=None):
    """Why a release switch is not clean: the block's hash moved, a fleet has a deviation it did not have before,
    or a workflow loaded before is no longer loaded. [] when clean."""
    after = after if after is not None else upgrade_report()
    out = []
    if before_hash is not None and after['hash'] != before_hash:
        out.append('baseline block hash changed: ' + str(before_hash) + ' -> ' + after['hash'] + ' (' + after['path']
                   + ')')
    if before is None:
        return out
    old = {f['config']: f for f in before.get('fleets') or []}
    for item in after.get('fleets') or []:
        prev = old.get(item['config'])
        if prev is None:
            continue  # a fleet that was not running before is no evidence of the switch
        seen = {(d['switch'], json.dumps(d.get('value')), json.dumps(d.get('baseline'))) for d in prev['deviations']}
        for d in item['deviations']:
            if (d['switch'], json.dumps(d.get('value')), json.dumps(d.get('baseline'))) not in seen:
                out.append('new deviation in ' + item['name'] + ': ' + d['switch'] + ' running ' + show(d.get('value'))
                           + ', baseline ' + show(d.get('baseline')))
        for wf in sorted(set(prev['workflows']) - set(item['workflows'])):
            out.append('workflow ' + wf + ' is no longer loaded in ' + item['name'])
    return out


def upgrade_text(report, problems):
    lines = ['baseline: ' + report['path'] + ' hash ' + report['hash']]
    for item in report['fleets']:
        lines.append('fleet ' + item['name'] + ' (' + item['config'] + '): workflows '
                     + (', '.join(item['workflows']) or 'none') + '; open deviations '
                     + (', '.join(d['switch'] + ' running ' + show(d.get('value')) + ' baseline '
                                  + show(d.get('baseline')) for d in item['deviations']) or 'none')
                     + ('; ' + item['error'] if item.get('error') else ''))
    lines += ['FAIL ' + p for p in problems]
    lines.append('ok' if not problems else 'baseline check failed')
    return '\n'.join(lines)
