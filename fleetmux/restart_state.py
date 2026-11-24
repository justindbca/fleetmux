"""Restart state (docs/design.md section 60): snapshots of a fleet's state in its fleet home, and the reconcile ledger.

The daemon writes a snapshot of its fleet (kinds: hot roster items, sub-fleets, applets, jobs, in-flight Redis
communication, errors) on a timer, at the start of an operator stop (before anything is taken down), at the entry
of a resume (before anything is cancelled) and before every new supervisor generation. Snapshots live under
``<fleet home>/restart/<fleet key>/`` (not in the run directory, so a new run reads them), are written atomically,
carry a schema number, and only the newest ``keep_timer`` timer / ``keep_events`` other snapshots are kept.

When a daemon comes up (a new run or a continued one) it compares the newest snapshot of its previous life with the
fleet now, restores the declarations it can restore as they were (attached service sub-fleets; jobs follow
section 58), and opens a ledger of what stopped, got lost or errored. This does not wait for the supervisor. After
every new supervisor generation (any path) the same comparison runs within the run. The supervisor rules each item
(recovered / redo / drop with a reason) through the ``reconcile`` key of its decision JSON, or the operator through
``fleetmux reconcile rule``; the ledger, its Markdown view and the closing report live in the fleet home, and a
summary goes out on the notify channels when a ledger opens and when it closes.

Every daemon entry point here catches its own errors, records ``restart.error`` and returns: a snapshot or a
reconcile never blocks or delays a stop, a resume or a supervisor generation change.
"""
import hashlib
import json
import os
import socket
import time
import uuid
from pathlib import Path

SCHEMA = 1
KNOB_DEFAULTS = {'enabled': True, 'snapshot_every_s': 300, 'keep_timer': 12, 'keep_events': 20,
                 'ruling_remind_s': 3600, 'item_payload_max': 20000, 'inline_items': 30, 'keep_child_dirs': 20}
KINDS = ('hot', 'subfleets', 'applets', 'jobs', 'inflight', 'errors')
VERDICTS = ('recovered', 'redo', 'drop')
CLASSES = ('stopped', 'lost', 'errored')
DONE = ('recovered', 'dropped', 'done')  # item states that need nothing more
STOP_ENDS = ('operator', 'final')  # how a run ended that the next start continues ('final': nothing comes back)
STATE = 'state.json'
AUDIT_CAP = 200  # audit errors one snapshot carries (all not reconciled yet, newest kept)
REQUEST_CAP = 1000  # live requests one snapshot carries
AUDIT_PAGE, AUDIT_PAGES = 5000, 20  # audit entries read per page, pages at most (newest first)


# ------------------------------------------------------------------ knobs, paths, files
def knobs():
    """fleet.yaml#restart_resume, each key validated on its own (a bad value keeps its default)."""
    from fleetmux import fleet_config
    try:
        raw = fleet_config.block('restart_resume') or {}
    except Exception:  # noqa: BLE001 - a broken private file never stops the daemon
        raw = {}
    out = dict(KNOB_DEFAULTS)
    for k, d in KNOB_DEFAULTS.items():
        v = raw.get(k, d) if isinstance(raw, dict) else d
        if type(d) is bool:
            ok = type(v) is bool
        else:
            ok = type(v) in (int, float) and v >= (1 if k != 'snapshot_every_s' else 10)
        out[k] = v if ok else d
    return out


def home(cfg):
    from fleetmux import fleet
    return Path(cfg.get('fleet_home') or fleet.HOME).expanduser()


def fleet_key(cfg, run_dir=None):
    """root | sub-<name> | job-<job id> | split-<root run>-<M n>: one directory per fleet of a fleet home."""
    named = cfg.get('named') or {}
    if named.get('name'):
        return 'sub-' + str(named['name'])
    if cfg.get('job'):
        return 'job-' + str(cfg['job'])
    if cfg.get('parent_config'):
        run = Path(run_dir or cfg.get('run') or '.')
        return 'split-' + run.parent.parent.name + '-' + str(cfg.get('task_id') or run.name)
    return 'root'


def fleet_dir(f):
    return home(f.cfg) / 'restart' / fleet_key(f.cfg, f.run_dir)


def root_dir(cfg):
    return home(cfg) / 'restart' / 'root'


def write_json(path, data):
    """Atomic: temp file, fsync, rename, fsync the directory. A reader never sees half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w') as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=1, default=str) + '\n')
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def load_state(d):
    st = read_json(Path(d) / STATE) or {}
    st.setdefault('seq', 0)
    st.setdefault('consumed_seq', 0)
    st.setdefault('ledger_seq', 0)
    st.setdefault('ledger', '')
    st.setdefault('ruled', {})
    st.setdefault('delivered', {})
    return st


def save_state(d, st):
    write_json(Path(d) / STATE, st)


def sha(text):
    return hashlib.sha256((text or '').encode()).hexdigest()


def cap(text, limit):
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + ' ...[cut ' + str(len(text) - limit) + ' chars]'


def _read(path, limit):
    try:
        return cap(Path(path).read_text(), limit) if path and Path(path).is_file() else None
    except OSError:
        return None


def audit(f, event, **fields):
    try:
        f.log(event, **fields)
    except Exception:  # noqa: BLE001 - an audit write never raises out of here
        pass


# ------------------------------------------------------------------ collectors (one per kind)
def collect_hot(f, lim):
    from fleetmux import applet_host, fleet, named_subfleet, roster
    path = f.cfg.get('roster') or ''
    text = _read(path, 10 ** 7) or ''
    rows = []
    if path:
        try:
            rows = [e.get('name') for e in roster.load_entries(path)[0]]
        except Exception:  # noqa: BLE001
            rows = []
    added = {}
    for name in applet_host.names(f.r):
        for slot in f.r.hgetall(applet_host.key(name) + ':added'):
            added[slot] = 'applet:' + name
    for jid, job in f.job_state().items():
        if job.get('hot_slot'):
            added[job['hot_slot']] = 'job:' + jid
    for name, rec in named_subfleet.attached(f).items():
        if rec.get('hot_slot'):
            added[rec['hot_slot']] = 'subfleet:' + name
    values = {name: {k: h.get(k, '') for k in ('hot_add_max', 'hot_add_row')}
              for name, h in applet_host.states(f.r, full=True).items()}
    refused = f.r.hgetall(fleet.ROSTER)
    return {'roster': {'path': path, 'sha256': sha(text), 'rows': rows}, 'added': added, 'hot_values': values,
            'roster_refused': {k: refused.get(k, '') for k in ('rejected', 'accepted')}}


def collect_subfleets(f, lim):
    from fleetmux import fleet, named_subfleet, subfleet
    recs = named_subfleet.attached(f)
    declared = named_subfleet.declared(f)
    live = named_subfleet.live(f)
    sub = f.run_dir / 'sub'
    names = set(declared) | set(recs)
    if sub.is_dir():
        names |= {p.name for p in sub.iterdir() if p.is_dir()}
    out = {}
    for name in sorted(names):
        rec = subfleet.record(sub / name)
        if name not in declared and name not in recs and rec.get('state') not in subfleet.ACTIVE + ('suspended',):
            continue  # an ended job / split child: the jobs kind and the run directory keep it
        form = 'attached' if name in recs else 'roster' if name in declared else 'job' if rec.get('job') else 'split'
        att = recs.get(name) or {}
        entry = att.get('entry') or declared.get(name) or {}
        h = f.r.hgetall(named_subfleet.HASH + name)
        alive = name in live or rec.get('state') in subfleet.ACTIVE
        row = {'form': form, 'run_dir': str(sub / name), 'alive': bool(alive),
               'record': {k: rec.get(k) for k in ('state', 'outcome', 'reason', 'detached', 'attention', 'job')
                          if rec.get(k) is not None},
               'hash': {k: h.get(k) for k in ('state', 'crashloop', 'blocked', 'applets', 'applet_crashloop') if h.get(k)}}
        if form in ('attached', 'roster'):
            row['entry'] = entry
        if form == 'attached':
            row['spec'] = att.get('spec') or {}
            row['attach_id'] = att.get('id') or ''
            row['files'] = {'roster': _read(entry.get('roster'), lim)}
        out[name] = row
    return out


def collect_applets(f, lim):
    from fleetmux import applet_host
    out = {}
    for name, h in applet_host.states(f.r, full=True).items():
        k = applet_host.key(name)
        esc = []
        for ek in f.r.scan_iter(match=k + ':esc:*', count=200):
            try:
                st = f.r.hget(ek, 'status') if f.r.type(ek) == 'hash' else ''
            except Exception:  # noqa: BLE001
                st = ''
            if st and st.startswith('awaiting'):
                esc.append(ek[len(k) + 5:])
        out[name] = {'state': h.get('state') or '', 'state_at': h.get('state_at') or '', 'pid': h.get('pid') or '',
                     'restarts': h.get('restarts') or '0', 'leases': sorted(f.r.smembers(k + ':leases')),
                     'escalations': sorted(esc), 'notices': f.r.llen(k + ':notices')}
    return out


def collect_jobs(f, lim):
    keys = ('state', 'slot', 'task', 'run', 'created', 'tries', 'waiting', 'cancel', 'resume_run', 'hot_slot', 'goal')
    out = {}
    for jid, row in f.job_state().items():
        out[jid] = {k: (cap(row.get(k), 300) if k == 'goal' else row.get(k)) for k in keys if row.get(k) not in (None, '')}
        out[jid]['submit'] = submit_spec(row)  # what a redo submits again (the whole spec, not only the goal)
    return out


def submit_spec(row):
    """The job spec of ``fleetmux task`` rebuilt from a job row (goal, settings, artifacts, budget, lifecycle and
    the sub-fleet fields), so a redo runs with the settings the job was submitted with."""
    from fleetmux import fleet
    spec = {'goal': row.get('goal') or ''}
    settings, spawn = row.get('settings') or {}, row.get('spawn') or {}
    for k in getattr(fleet, 'JOB_SPAWN', ()):
        if spawn.get(k) not in (None, '', [], {}):
            spec[k] = spawn[k]
    if settings.get('hours') is not None:
        spec['hours'] = settings['hours']
    src, chains = settings.get('chain_source') or {}, settings.get('chains') or {}
    # model chains and cards only where the job spec set them (job_chain.effective source 'spec'): a fleet.yaml or
    # discovery layer is taken again from where it is now
    for role in ('supervisor', 'worker'):
        if src.get(role) == 'spec' and chains.get(role):
            spec[role] = chains[role]
    if settings.get('supervisor_profile') and src.get('supervisor_profile', 'spec') == 'spec':
        spec['supervisor_profile'] = settings['supervisor_profile']
    if not spawn.get('roster'):  # a roster names the workers and their cards itself
        if settings.get('workers') is not None:
            spec['workers'] = settings['workers']
        if settings.get('worker_profiles') and src.get('worker_profiles', 'spec') == 'spec':
            spec['worker_profiles'] = settings['worker_profiles']
    for k in ('artifacts', 'budget', 'lifecycle'):
        if row.get(k) is not None:
            spec[k] = row[k]
    return spec


def collect_inflight(f, lim):
    from fleetmux import fleet, named_subfleet
    requests = {}
    queued = list(f.r.lrange(fleet.REQQ, 0, 499))
    live = ('dispatched', 'routing')  # on a worker / being routed again; answered rows have a result

    def take(rid, h):
        if not h or h.get('job') or rid.startswith(getattr(fleet, 'JOB_PREFIX', 'job-')):
            return
        if f.r.exists(fleet.RESULT + rid):
            return
        requests[rid] = {'status': h.get('status', ''), 'worker': h.get('worker', ''),
                         'spec': cap(h.get('spec', ''), lim)}
    for rid in queued:
        try:
            take(rid, f.r.hgetall(fleet.REQ + rid))
        except Exception:  # noqa: BLE001 - not a request hash
            continue
    for k in f.r.scan_iter(match=fleet.REQ + '*', count=500):  # not in the queue: filtered before the cap
        if len(requests) >= REQUEST_CAP:
            break
        rid = k[len(fleet.REQ):]
        if ':' in rid or rid in requests:
            continue
        try:
            if f.r.type(k) != 'hash' or f.r.hget(k, 'status') not in live:
                continue
            take(rid, f.r.hgetall(k))
        except Exception:  # noqa: BLE001
            continue
    escalations = {k: {'status': v.get('status'), 'reason': cap(v.get('reason') or '', 300), 'slot': v.get('slot', '')}
                   for k, v in f.escalation_state().items() if v.get('status') in ('escalated', 'awaiting')}
    ops = {}
    for rid in f.r.lrange(named_subfleet.OPQ, 0, -1):
        op = f.r.hgetall(named_subfleet.OP + rid)
        if op.get('status') == 'awaiting':
            ops[rid] = {'op': op.get('op'), 'name': op.get('name')}
    supervisor = {}
    if f.v2:
        bus = f.v2.bus
        mids = list(bus.pending('supervisor')[:200])
        cur = (bus.slot_state('supervisor') or {}).get('message_id') or ''
        if cur and cur not in mids:
            mids.insert(0, cur)  # offered / received by the supervisor, not committed yet
        # offered to a generation that was retired / cleared before it answered: the bus committed it cancelled
        # (audit stop_resolved), so it is neither pending nor current any more; since the last reconcile
        since = load_state(fleet_dir(f)).get('audit_cursor') or None
        for _, d in audit_scan(f, since, lambda d: d.get('event') == 'stop_resolved' and d.get('slot') == 'supervisor')[0]:
            mid = d.get('message_id') or ''
            if mid and mid not in mids and bus.r.hget(bus.msg_key(mid), 'outcome') != 'completed':
                mids.append(mid)
        for mid in mids:
            m = bus.message(mid) or {}
            body = m.get('body') if isinstance(m.get('body'), dict) else {}
            if str(body.get('text') or '').startswith('[Reconcile'):
                continue  # the ledger's own notices: an open ledger delivers its items again itself
            supervisor[mid] = {'kind': str(m.get('kind') or ''), 'request_id': str(m.get('request_id') or ''),
                               'text': cap(body.get('text') or json.dumps(body, ensure_ascii=False), lim)}
    confirmations = {}
    for k in f.r.scan_iter(match='fx:channel:pending:*', count=200):
        try:
            confirmations[k] = cap(f.r.hgetall(k) if f.r.type(k) == 'hash' else f.r.get(k), 500)
        except Exception:  # noqa: BLE001
            continue
    return {'requests': requests, 'escalations': escalations, 'ops': ops, 'supervisor': supervisor,
            'confirmations': confirmations}


def collect_errors(f, lim, cursor=None):
    from fleetmux import fleet
    held = {}
    for slot in f.names:
        a = f.agent(slot)
        if a.get('state') == 'held' or a.get('regen_state') or a.get('link_lost') == '1':
            held[slot] = {'state': a.get('state', ''), 'since': a.get('held_at') or '',  # wall clock (held_since: awake)
                          'reason': cap(a.get('regen_reason') or a.get('api_error') or a.get('regen_state') or
                                        a.get('hold_pause') or 'held', 300)}
    from fleetmux import join
    svc = f.r.hgetall(fleet.SERVICE)
    stop = any(r in str(svc.get('reason') or '') for r in join.STOP_REASONS)  # the operator's stop is no error
    pause = ({'reason': svc.get('reason', ''), 'kind': svc.get('kind', ''), 'at': svc.get('at', '')}
             if svc.get('paused') == '1' and not stop else {})
    undelivered = read_json(f.run_dir / 'notify-undelivered.json') or {}
    found, last = audit_scan(f, cursor, lambda d: d.get('level') == 'error'
                             and not str(d.get('event', '')).startswith('restart.'))
    audits = [{'id': eid, 'event': data.get('event', ''), 'slot': data.get('slot', ''), 'at': data.get('at', ''),
               'reason': cap(data.get('reason') or data.get('error') or '', 300)} for eid, data in found]
    last = last or cursor or ''
    return {'held': held, 'pause': pause, 'undelivered': {'count': undelivered.get('count', 0),
                                                          'last': cap(undelivered.get('text') or '', 300)}
            if undelivered else {}, 'audit': audits, 'cursor': last}


def audit_scan(f, cursor, want):
    """([(id, fields)] oldest first, newest id): the newest AUDIT_CAP audit entries after ``cursor`` that ``want``
    keeps, read newest first page by page (the audit stream is not trimmed: never only its oldest part)."""
    found, last, top = [], '', '+'
    if not f.v2:
        return found, last
    for _ in range(AUDIT_PAGES):
        try:
            entries = f.r.xrevrange(f.v2.bus.audit_key, max=top, min='(' + cursor if cursor else '-', count=AUDIT_PAGE)
        except Exception:  # noqa: BLE001
            entries = []
        if not entries:
            break
        last = last or entries[0][0]
        found += [(eid, data) for eid, data in entries if want(data)]
        if len(found) >= AUDIT_CAP or len(entries) < AUDIT_PAGE:
            break
        top = '(' + entries[-1][0]
    return list(reversed(found[:AUDIT_CAP])), last


def collector(name):
    """The collector of kind ``name`` (looked up at call time: a new kind is one more collect_<kind> function)."""
    return globals()['collect_' + name]


def collect(f, cursor=None):
    """{kinds, kind_errors}: a collector that fails leaves its kind out and names the error (reported as an item)."""
    lim = knobs()['item_payload_max']
    kinds, errors = {}, {}
    for name in KINDS:
        try:
            kinds[name] = collector(name)(f, lim, cursor) if name == 'errors' else collector(name)(f, lim)
        except Exception as exc:  # noqa: BLE001
            errors[name] = 'collector failed: ' + repr(exc)[:300]
    return kinds, errors


# ------------------------------------------------------------------ snapshots
def snapshots(d):
    """[(seq, reason, path)] of readable-named snapshot files, oldest first."""
    out = []
    for p in Path(d).glob('snap-*.json'):
        parts = p.stem.split('-', 2)
        try:
            out.append((int(parts[1]), parts[2] if len(parts) > 2 else '', p))
        except (IndexError, ValueError):
            continue
    return sorted(out)


def boot_id(f):
    bid = getattr(f, 'restart_boot', None)
    if not bid:
        bid = f.restart_boot = uuid.uuid4().hex[:12]
    return bid


def write(f, reason, **extra):
    """Write one snapshot; never raises (records restart.error). Returns its path or None."""
    try:
        k = knobs()
        if not k['enabled']:
            return None
        d = fleet_dir(f)
        d.mkdir(parents=True, exist_ok=True)
        st = load_state(d)
        # every snapshot carries all audit errors since the last reconcile (not only since the previous snapshot):
        # two timer snapshots between reconciles must not hide the first one's errors
        kinds, kind_errors = collect(f, cursor=st.get('audit_cursor') or None)
        seq = max([st['seq']] + [s for s, _, _ in snapshots(d)]) + 1
        sup = f.agent('supervisor')
        doc = {'schema': SCHEMA, 'seq': seq, 'at': time.time(), 'reason': reason, 'boot': boot_id(f),
               'fleet': {'key': fleet_key(f.cfg, f.run_dir), 'name': f.cfg.get('name') or '',
                         'mode': 'service' if f.service else 'task', 'run': str(f.run_dir),
                         'config': str(getattr(f, 'path', '') or ''), 'parent': f.cfg.get('parent_config') or None,
                         'host': socket.gethostname(),
                         'supervisor': {'generation': int(sup.get('generation') or 0), 'label': sup.get('label', '')}},
               'kinds': kinds, 'kind_errors': kind_errors}
        doc.update(extra)
        path = d / ('snap-%08d-%s.json' % (seq, reason))
        write_json(path, doc)
        st['seq'] = seq
        if reason == 'timer':
            st['last_timer_at'] = time.time()
        save_state(d, st)
        prune(d, k, st)
        return path
    except Exception as exc:  # noqa: BLE001 - never block a stop / resume / generation change
        audit(f, 'restart.error', level='warn', where='snapshot ' + reason, error=repr(exc)[:500])
        return None


def prune(d, k, st):
    """Keep the newest keep_timer timer and keep_events other snapshots, and every one not consumed yet."""
    snaps = snapshots(d)
    timer = [s for s in snaps if s[1] == 'timer']
    other = [s for s in snaps if s[1] != 'timer']
    keep = {s[0] for s in timer[-int(k['keep_timer']):]} | {s[0] for s in other[-int(k['keep_events']):]}
    keep |= {s[0] for s in snaps if s[0] > int(st.get('consumed_seq') or 0) and s[1] != 'timer'}
    keep.add(int(st.get('baseline_seq') or 0))
    for seq, _, path in snaps:
        if seq not in keep:
            try:
                path.unlink()
            except OSError:
                pass
    for tmp in Path(d).glob('*.tmp'):  # a write that died half way
        try:
            if time.time() - tmp.stat().st_mtime > 60:
                tmp.unlink()
        except OSError:
            pass


def read_snapshot(path):
    """(doc, problem): problem '' when usable."""
    try:
        doc = json.loads(Path(path).read_text())
    except OSError as exc:
        return None, 'cannot read: ' + str(exc)
    except ValueError as exc:
        return None, 'not valid JSON: ' + str(exc)[:200]
    if not isinstance(doc, dict) or not isinstance(doc.get('kinds'), dict):
        return None, 'not a snapshot object'
    if doc.get('schema') != SCHEMA:
        return None, 'schema ' + repr(doc.get('schema')) + ' (this release reads ' + str(SCHEMA) + ')'
    return doc, ''


# ------------------------------------------------------------------ hooks called by the daemon
def tick(f):
    """Fleet.tick part: the boot reconcile once per daemon life, then redo actions, delivery, reminders, the timer
    snapshot (frozen while the fleet stops) and the retention of ended child fleets' directories."""
    if not knobs()['enabled']:
        return
    if not getattr(f, 'restart_booted', False):
        f.restart_booted = True
        f.restart_life_at = getattr(f, 'restart_life_at', None) or time.time()
        boot(f)
    if f.stopping or f.explicit_stop:
        return
    try:
        ready_followup(f)
        follow(f)
        deliver(f)
    except Exception as exc:  # noqa: BLE001
        audit(f, 'restart.error', level='warn', where='ledger tick', error=repr(exc)[:500])
    d = fleet_dir(f)
    st = load_state(d)
    if time.time() - float(st.get('last_timer_at') or 0) >= knobs()['snapshot_every_s']:
        if not st.get('last_timer_at'):  # the first tick of a life: the boot snapshots are enough for now
            st['last_timer_at'] = time.time()
            save_state(d, st)
        else:
            write(f, 'timer')
            if not f.cfg.get('parent_config'):
                prune_children(f)


def on_stop(f):
    """First statement of the explicit-stop tail (before applets and sub-fleets are taken down)."""
    try:
        if getattr(f, 'suspending', False):
            return write(f, 'suspend', ended='suspend')
        final = bool(getattr(f, 'stop_final', False))
        ended = 'final' if final else 'lifecycle' if getattr(f, 'lifecycle_close', None) else 'operator'
        if not final and run_end(f.run_dir) == 'self' and result_of_this_life(f):
            ended = 'self'  # D7: the run had ended by itself (done / budget / failed) before this stop tail ran
        if final:
            final_cancels(f)
        return write(f, 'stop_final' if final else 'stop', ended=ended, final=final)
    except Exception as exc:  # noqa: BLE001
        audit(f, 'restart.error', level='warn', where='stop', error=repr(exc)[:500])


def final_cancels(f):
    """stop --final: every open job is cancelled (published, never resumed) and every attached sub-fleet gets a
    cancel tombstone, so the next start brings nothing back."""
    from fleetmux import named_subfleet
    for jid, job in list(f.job_state().items()):
        if job.get('cancel'):
            continue
        try:
            f.job_cancel(jid, by='operator (stop --final)')
        except Exception as exc:  # noqa: BLE001
            audit(f, 'restart.error', level='warn', where='final cancel ' + jid, error=repr(exc)[:300])
    if not f.cfg.get('parent_config'):
        for name in named_subfleet.attached(f):
            write_cancel(f.cfg, name, how='stop --final')


def result_of_this_life(f):
    """result.json was written while this daemon ran (not a file an earlier life left behind)."""
    try:
        at = (Path(f.run_dir) / 'result.json').stat().st_mtime
    except OSError:
        return False
    return at >= float(getattr(f, 'restart_life_at', 0) or 0) - 1


def on_resume_entry(f):
    """First statement of RuntimeV2.resume_run: Redis as the previous daemon left it. Then a cancel tombstone
    written while the root was down (a stub stop, stop --final) holds for the continued run too: that attached
    sub-fleet's declaration is taken out (it drains like a detach) instead of coming back. Never raises."""
    write(f, 'resume_entry')
    for step in (apply_cancels, apply_supervisor_continue):
        try:
            step(f)
        except Exception as exc:  # noqa: BLE001
            audit(f, 'restart.error', level='warn', where='resume ' + step.__name__, error=repr(exc)[:500])


def apply_supervisor_continue(f):
    """D6: `start` changed the supervisor card / model of this continued run (fleet.continue_supervisor). The
    supervisor's launch record (a wall / fallback / profile card) would override the config for the next generation;
    it is dropped once per change, so resume_run's new generation launches from the changed config."""
    change = f.cfg.get('supervisor_continue') or {}
    if not change.get('id'):
        return
    d = fleet_dir(f)
    st = load_state(d)
    done = list(st.get('supervisor_continue_applied') or [])
    if change['id'] in done:
        return
    old = f.agent('supervisor').get('runtime_launch') or ''
    if old:
        f.setf('supervisor', runtime_launch='', runtime_profile='')
    st['supervisor_continue_applied'] = (done + [change['id']])[-20:]
    save_state(d, st)
    audit(f, 'restart.supervisor_continue', keys=change.get('keys') or [], dropped_launch=old[:300])


def apply_cancels(f):
    from fleetmux import named_subfleet
    if f.cfg.get('parent_config'):
        return
    for name in list(named_subfleet.attached(f)):
        tomb = root_dir(f.cfg) / 'cancels' / ('subfleet-' + name + '.json')
        rec = read_json(tomb) if tomb.exists() else None
        if rec is None:
            continue
        named_subfleet.detach(f, 'detach-' + uuid.uuid4().hex[:12],
                              {'name': name, 'reason': 'cancelled while the root was down (' +
                               str(rec.get('how') or 'operator') + ')'})
        write_json(tomb, rec)  # detach wrote its own tombstone: keep how it was cancelled
        audit(f, 'restart.cancel_applied', name=name, how=rec.get('how') or '')


def before_supervisor(f, source=''):
    """Fleet.spawn of the supervisor: a snapshot before each new generation of this daemon's life (the first
    generation of a life is the boot's, which the boot reconcile covers). Never raises."""
    try:
        if getattr(f, 'restart_ready_gen', None) is None:
            return
        path = write(f, 'supervisor', for_generation=int(f.agent('supervisor').get('generation') or 0),
                     source=source)
        if path:
            f.restart_gen_snapshot = str(path)
    except Exception as exc:  # noqa: BLE001
        audit(f, 'restart.error', level='warn', where='before supervisor', error=repr(exc)[:500])


def on_supervisor_ready(f, generation):
    """READY of a supervisor generation (RuntimeV2.handle): only recorded here; the next tick reconciles a later
    generation of this life against the snapshot taken before it started, then hands the open items to it."""
    try:
        f.restart_ready_pending = int(generation)
    except Exception:  # noqa: BLE001
        pass


def ready_followup(f):
    gen = getattr(f, 'restart_ready_pending', None)
    if gen is None:
        return
    f.restart_ready_pending = None
    first = getattr(f, 'restart_ready_gen', None) is None
    f.restart_ready_gen = gen
    path = getattr(f, 'restart_gen_snapshot', None)
    if not first and path:
        f.restart_gen_snapshot = None
        doc, problem = read_snapshot(path)
        if doc is not None:
            reconcile(f, doc, 'generation')
        else:
            open_ledger(f, [item('errored', 'snapshot', 'generation', 'snapshot before this generation is '
                                 'unreadable: ' + problem)], 'generation')
    deliver(f, force=True)


# ------------------------------------------------------------------ boot reconcile
def previous_run_exists(f, d):
    """Was there a run of this fleet before this daemon life (so a missing snapshot is worth reporting)? Snapshots
    this life already wrote (an attach before the first tick) do not count."""
    me = boot_id(f)
    if any((read_json(path) or {}).get('boot') != me for _, _, path in snapshots(d)):
        return True
    st = read_json(Path(d) / STATE) or {}
    if st.get('consumed_seq') or st.get('ledger_seq'):  # a boot of an earlier life ran here
        return True
    prev = f.cfg.get('previous_run')
    if prev and Path(prev).resolve() != Path(f.run_dir).resolve() and Path(prev).is_dir():
        return True
    return bool(getattr(f, 'resume_requested', False))


def run_end(run):
    """How a run without a stop snapshot ended: 'crash' (no result.json), 'operator', or 'self'."""
    from fleetmux import fleet, join
    res = fleet.run_result(run) if run else {}
    if not res:
        return 'crash'
    reason = str(res.get('reason') or '')
    if res.get('status') in ('stopped', 'suspended') or any(r in reason for r in join.STOP_REASONS):
        return 'operator'
    return 'self'


def choose_baseline(f, d, st):
    """(doc, problems, end): the snapshot to compare with, the unreadable ones skipped (named in problems), and how
    the previous daemon life ended. A stop of that life (operator, final, lifecycle, suspend) is the baseline: it
    was taken before anything was taken down. After a crash there is none; then this life's resume-entry snapshot
    (Redis as the dead daemon left it, before resume_run cancelled anything) or else the newest snapshot of the
    previous life is the baseline."""
    me = boot_id(f)
    problems, prev, entry = [], None, None
    cands = [s for s in snapshots(d) if s[0] > int(st.get('consumed_seq') or 0)]
    for seq, reason, path in reversed(cands):
        got, problem = read_snapshot(path)
        if got is None:
            problems.append(path.name + ': ' + problem)
            continue
        if got.get('boot') == me:
            if reason == 'resume_entry' and entry is None:
                entry = got
            continue
        prev = got
        break
    if prev is not None and prev.get('ended'):
        return prev, problems, prev['ended']
    doc = entry or prev
    if doc is None:
        return None, problems, None
    end = run_end(doc['fleet'].get('run'))
    if end == 'operator' and doc is entry and prev is None:
        end = 'crash'  # a result.json without a stop snapshot of this release: treat as the crash case
    return doc, problems, end


def boot(f):
    """Phase one, right after the daemon is up (no supervisor needed): baseline, restores, ledger, open summary."""
    try:
        d = fleet_dir(f)
        had_before = previous_run_exists(f, d)
        st = load_state(d)
        doc, problems, end = choose_baseline(f, d, st)
        st['consumed_seq'] = max([int(st.get('consumed_seq') or 0)] + [s for s, _, _ in snapshots(d)])
        save_state(d, st)
        if doc is None:
            if had_before and (problems or run_end(f.cfg.get('previous_run')) in ('crash', 'operator')
                               or getattr(f, 'resume_requested', False)):
                why = '; '.join(problems) or 'no snapshot in ' + str(d)
                audit(f, 'restart.snapshot_unreadable', level='error', error=why[:500])
                items = [item('errored', 'snapshot', 'baseline', 'no readable snapshot of the previous run: ' + why)]
                items += not_applied_items(f)
                open_ledger(f, items, 'restart', baseline=None)
            elif not_applied_items(f):
                open_ledger(f, not_applied_items(f), 'restart', baseline=None)
            return
        st = load_state(d)
        st['baseline_seq'] = doc.get('seq')
        save_state(d, st)
        if end not in STOP_ENDS + ('crash',):  # the run ended by itself (done, budget, failed): a quiet new start
            audit(f, 'restart.quiet', previous=doc['fleet'].get('run'), ended=end or '')
            mark_audit(f, audit_end(f))  # the ended task's errors are not carried into later snapshots
            if not_applied_items(f):
                open_ledger(f, not_applied_items(f), 'restart', baseline=doc.get('seq'))
            return
        if end == 'final':
            audit(f, 'restart.final', previous=doc['fleet'].get('run'))
            mark_audit(f, audit_end(f))
            st['final_note'] = 'the previous run ended with stop --final: nothing was brought back'
            save_state(d, st)
            return
        reconcile(f, doc, 'restart', end=end, problems=problems)
    except Exception as exc:  # noqa: BLE001
        audit(f, 'restart.error', level='error', where='boot reconcile', error=repr(exc)[:500])


def not_applied_items(f):
    return [item('errored', 'start', 'option ' + o, 'not applied to the continued run: ' + o)
            for o in (f.cfg.get('restart_not_applied') or [])]


# ------------------------------------------------------------------ reconcile
def item(cls, kind, key, evidence, **extra):
    return dict({'class': cls, 'kind': kind, 'key': key, 'evidence': evidence, 'state': 'open', 'auto': None,
                 'verdict': None, 'reason': '', 'fp': fingerprint(kind, key, evidence if cls == 'errored' else cls)},
                **extra)


def fingerprint(kind, key, what):
    norm = ''.join(ch for ch in str(what) if not ch.isdigit())[:200]
    return hashlib.sha1((str(kind) + '|' + str(key) + '|' + norm).encode()).hexdigest()[:16]


def cancelled_subfleet(f, name, row):
    tomb = root_dir(f.cfg) / 'cancels' / ('subfleet-' + name + '.json')
    if tomb.exists():
        return 'cancel tombstone (' + str((read_json(tomb) or {}).get('how') or 'operator') + ')'
    if (row.get('record') or {}).get('detached'):
        return 'detached by the operator'
    return ''


def reconcile(f, S, mode, end='operator', problems=()):  # noqa: ARG001 - end: kept for the audit
    """Compare snapshot ``S`` with the fleet now, restore what restores as it was (restart mode, not --no-restore),
    and add the items to the open ledger (a new one when none is open)."""
    N, kind_errors = collect(f, cursor=(S.get('kinds', {}).get('errors') or {}).get('cursor') or None)
    mark_audit(f, (N.get('errors') or {}).get('cursor'))
    restore = mode == 'restart' and not f.cfg.get('no_restore')
    new_run = S['fleet'].get('run') != str(f.run_dir)
    items, cancelled = [], []
    for p in problems:
        items.append(item('errored', 'snapshot', 'older', 'skipped an unreadable snapshot: ' + p))
    for kind, why in sorted({**(S.get('kind_errors') or {}), **kind_errors}.items()):
        items.append(item('errored', 'snapshot', kind, why))
    sk = S.get('kinds') or {}
    items += sub_items(f, sk.get('subfleets') or {}, N.get('subfleets') or {}, restore, new_run, cancelled)
    items += applet_items(sk.get('applets') or {}, N.get('applets') or {})
    items += job_items(f, sk.get('jobs') or {}, N.get('jobs') or {}, cancelled)
    items += inflight_items(f, sk.get('inflight') or {}, N.get('inflight') or {}, new_run)
    items += hot_items(f, sk.get('hot') or {}, N.get('hot') or {}, restore)
    items += error_items(f, sk.get('errors') or {}, N.get('errors') or {})
    if mode == 'restart':
        items += not_applied_items(f)
    items = suppress(f, items)
    if items or cancelled:
        open_ledger(f, items, mode, baseline=S.get('seq'), cancelled=cancelled)
    else:
        audit(f, 'restart.reconciled', mode=mode, items=0)


def mark_audit(f, cursor):
    """The audit errors up to ``cursor`` went into a reconcile (or belong to a run that ended by itself): later
    snapshots start after it."""
    if not cursor:
        return
    d = fleet_dir(f)
    st = load_state(d)
    st['audit_cursor'] = cursor
    save_state(d, st)


def audit_end(f):
    try:
        last = f.r.xrevrange(f.v2.bus.audit_key, count=1) if f.v2 else []
    except Exception:  # noqa: BLE001
        return ''
    return last[0][0] if last else ''


def sub_items(f, S, N, restore, new_run, cancelled):
    from fleetmux import fleet
    out = []
    for name, s in sorted(S.items()):
        if s.get('form') == 'job':
            continue  # the jobs kind covers it
        n = N.get(name) or {}
        if s.get('form') == 'roster' and n.get('form') not in ('roster', 'attached'):
            # D14: the operator removed its roster row: a cancel, not a loss (no item, no redo)
            cancelled.append({'kind': 'subfleet', 'key': name, 'why': 'removed from the roster'})
            continue
        why = cancelled_subfleet(f, name, s)
        if why:
            cancelled.append({'kind': 'subfleet', 'key': name, 'why': why})
            continue
        h, r = s.get('hash') or {}, s.get('record') or {}
        if h.get('crashloop') == '1' or r.get('attention') or h.get('blocked'):
            out.append(item('errored', 'subfleet', name, 'had an error: ' + json.dumps({**h, **r},
                                                                                       ensure_ascii=False)[:300]))
        attached_lost = s.get('form') == 'attached' and n.get('form') != 'attached'
        if not s.get('alive') and not attached_lost:
            continue  # it was not running (a declaration only); the declaration is still here
        if n.get('alive'):
            continue
        if s.get('form') == 'attached' and n.get('form') != 'attached':
            it = item('stopped', 'subfleet', name, 'attached service sub-fleet (applets: '
                      + (s.get('hash') or {}).get('applets', 'none') + ') is not declared in this run')
            if restore and new_run:
                it['auto'] = restore_attached(f, name, s)
            elif not restore:
                it['auto'] = {'action': 're-attach', 'state': 'skipped', 'note': 'start --no-restore'}
            out.append(it)
        elif n:
            out.append(item('stopped', 'subfleet', name, 'declared (' + str(n.get('form')) + ') but not running '
                            'now; its autostart brings it up when it can', auto={'action': 'autostart',
                                                                                  'state': 'pending'}))
        else:
            out.append(item('lost', 'subfleet', name, s.get('form', '') + ' sub-fleet that ran before is not '
                            'declared in this run'))
    return out


def applet_items(S, N):
    out = []
    for name, s in sorted(S.items()):
        n = N.get(name)
        if s.get('state') in ('crashloop', 'config_error', 'exited'):
            out.append(item('errored', 'applet', name, 'was ' + s['state']))
        elif s.get('state') in ('up', 'starting', 'stalled') and n is not None and n.get('state') not in ('up',):
            out.append(item('stopped', 'applet', name, 'was ' + s['state'] + ', now ' + str(n.get('state') or
                            'declared') + '; its host starts it again', auto={'action': 'host restart',
                                                                               'state': 'pending'}))
        for lease in s.get('leases') or []:
            if lease not in ((n or {}).get('leases') or []):
                out.append(item('stopped', 'lease', name + ':' + lease, 'open lease ended with the host '
                                '(the applet asks again when it needs a worker)'))
        for esc in s.get('escalations') or []:
            if esc not in ((n or {}).get('escalations') or []):
                out.append(item('lost', 'applet_escalation', name + ':' + esc, 'escalation awaiting a ruling '
                                'is gone with the previous Redis'))
    return out


def job_items(f, S, N, cancelled):
    from fleetmux import fleet
    out = []
    for jid, s in sorted(S.items()):
        n = N.get(jid)
        if s.get('cancel') or (n or {}).get('cancel'):
            cancelled.append({'kind': 'job', 'key': jid, 'why': 'cancelled by ' + str(
                ((n or s).get('cancel') or {}).get('by') or 'operator')})
            continue
        if n is not None:
            if s.get('state') == 'running' and n.get('state') != 'running':
                out.append(item('stopped', 'job', jid, 'was running, now ' + str(n.get('state')) +
                                '; section 58 resumes it', auto={'action': 'job resume', 'state': 'done'}))
            continue
        res = job_result(f, jid)
        if res is None:
            out.append(item('lost', 'job', jid, 'open job (' + str(s.get('state')) + ') is gone without a result',
                            spec_goal=s.get('goal', ''), submit=s.get('submit') or None))
            continue
        outcome = (res.get('result') or {}).get('outcome') if isinstance(res.get('result'), dict) else ''
        if outcome == 'cancelled':
            cancelled.append({'kind': 'job', 'key': jid, 'why': 'cancelled'})
        elif outcome == 'not_carried' or (res.get('status') == 'failed' and s.get('state') in ('running', 'starting')):
            out.append(item('errored', 'job', jid, 'published failed after the restart: ' + str(
                res.get('error') or outcome)[:300] + '; redo submits it again as a new job', spec_goal=s.get('goal', ''),
                submit=s.get('submit') or None))
    return out


def job_result(f, jid):
    from fleetmux import fleet
    raw = f.r.get(fleet.RESULT + jid)
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            return {'status': 'unknown'}
    for run in {f.run_dir}:
        got = read_json(Path(run) / getattr(fleet, 'JOB_RESULTS', 'job-results') / (jid + '.json'))
        if got:
            return got
    return None


def inflight_items(f, S, N, new_run):
    from fleetmux import fleet
    out = []
    for rid, s in sorted((S.get('requests') or {}).items()):
        if rid in (N.get('requests') or {}) or f.r.exists(fleet.RESULT + rid):
            continue
        if not new_run and f.r.exists(fleet.REQ + rid):
            continue  # the same Redis still holds it without a result: this run still handles it (never twice)
        out.append(item('lost', 'request', rid, 'request without a result (' + str(s.get('status')) +
                        ') is not in this run; redo routes it again once (replay_of)', payload=s.get('spec', '')))
    for k, s in sorted((S.get('escalations') or {}).items()):
        if k not in (N.get('escalations') or {}):
            out.append(item('lost', 'escalation', k, 'escalation awaiting a ruling is gone: ' + str(s.get('reason'))))
    for rid, s in sorted((S.get('ops') or {}).items()):
        if rid not in (N.get('ops') or {}) and not f.r.exists(fleet.RESULT + rid):
            out.append(item('lost', 'subfleet_op', rid, str(s.get('op')) + ' ' + str(s.get('name')) +
                            ' awaited a ruling and is gone'))
    for k, s in sorted((S.get('confirmations') or {}).items()):
        if k not in (N.get('confirmations') or {}):
            out.append(item('lost', 'confirmation', k, 'a confirmation that waited for the user is gone (never '
                            'pressed by the program): ' + str(s)[:300]))
    if f.v2:
        for mid, s in sorted((S.get('supervisor') or {}).items()):
            status, outcome = f.r.hmget(f.v2.bus.msg_key(mid), 'status', 'outcome')
            if status == 'committed' and outcome == 'completed':
                continue  # the supervisor's turn on it completed (cancelled by a retire / clear, failed or
                #           expired: not taken, listed)
            if status in ('queued', 'offered', 'received') and mid in (f.v2.bus.pending('supervisor') or []) + [
                    (f.v2.bus.slot_state('supervisor') or {}).get('message_id')]:
                continue  # still on its way in this run's bus
            out.append(item('lost', 'supervisor_message', mid, 'a message for the supervisor was not delivered ('
                            + (status or 'gone') + (' ' + outcome if outcome else '') + '); its text is in this item',
                            payload=s.get('text', '')))
    return out


def hot_items(f, S, N, restore):
    from fleetmux import applet_ops
    out = []
    for slot, why in sorted((S.get('added') or {}).items()):
        if slot not in (N.get('added') or {}):
            out.append(item('stopped', 'hot_worker', slot, 'worker added for ' + why + ' is not in this run '
                            '(not added again: its lease / job ended with the restart)'))
    for name, s in sorted((S.get('hot_values') or {}).items()):
        n = (N.get('hot_values') or {}).get(name)
        if not n or not s.get('hot_add_max') or s.get('hot_add_max') == n.get('hot_add_max'):
            continue
        it = item('stopped', 'hot_value', name + ':hot_add_max', 'was ' + s['hot_add_max'] + ', now ' +
                  str(n.get('hot_add_max')))
        if restore and s.get('hot_add_row') == n.get('hot_add_row'):
            try:
                applet_ops.set_hot_add_max(f, name, int(s['hot_add_max']))
                it['auto'] = {'action': 'hot_add_max ' + s['hot_add_max'], 'state': 'done'}
            except Exception as exc:  # noqa: BLE001
                it['auto'] = {'action': 'hot_add_max', 'state': 'failed', 'note': repr(exc)[:200]}
                it['class'] = 'errored'
        out.append(it)
    rej = (N.get('roster_refused') or {}).get('rejected') or (S.get('roster_refused') or {}).get('rejected')
    if rej:
        out.append(item('errored', 'roster', 'rejected', 'the roster file was rejected: ' + str(rej)[:300]))
    return out


def error_items(f, S, N):
    out, seen = [], set()
    for src in (S, N):
        for slot, h in sorted((src.get('held') or {}).items()):
            it = item('errored', 'slot', slot, str(h.get('state')) + ': ' + str(h.get('reason')), since=h.get('since'))
            if it['fp'] not in seen:
                seen.add(it['fp'])
                out.append(it)
        if src.get('pause'):
            it = item('errored', 'pause', 'fleet', 'paused: ' + str(src['pause'].get('reason')))
            if it['fp'] not in seen:
                seen.add(it['fp'])
                out.append(it)
        if (src.get('undelivered') or {}).get('count'):
            it = item('errored', 'notify', 'undelivered', str(src['undelivered']['count']) + ' notice(s) for a human '
                      'left on no channel; last: ' + str(src['undelivered'].get('last')))
            if it['fp'] not in seen:
                seen.add(it['fp'])
                out.append(it)
        for a in src.get('audit') or []:
            sub = a.get('slot') or ''.join(ch for ch in str(a.get('reason') or '') if not ch.isdigit())[:80]
            it = item('errored', 'audit', str(a.get('event', '')) + (':' + sub if sub else ''),
                      (a.get('slot') + ': ' if a.get('slot') else '') + str(a.get('reason') or a.get('event')),
                      since=a.get('at'))
            if it['fp'] not in seen:
                seen.add(it['fp'])
                out.append(it)
    return out


def suppress(f, items):
    """Drop an error a closed ledger already ruled when it has not happened again since that ruling. (An item
    still open in the open ledger is merged into it by open_ledger, never doubled.)"""
    st = load_state(fleet_dir(f))
    ruled = st.get('ruled') or {}
    out = []
    for it in items:
        at = ruled.get(it['fp'])
        if at is not None and it['class'] == 'errored':
            since = it.get('since')
            try:
                since = float(since) / (1000.0 if since and float(since) > 1e11 else 1.0) if since else None
            except (TypeError, ValueError):
                since = None
            if since is None or since <= float(at):
                continue
        out.append(it)
    return out


# ------------------------------------------------------------------ restore actions (check first, then do)
def restore_attached(f, name, s):
    """Declare an attached service sub-fleet again with its attach options. The source roster / roles named at attach
    time are used when they still exist; the snapshot's copy of the roster is only the fallback. The existing
    autostart tick is still the only launcher (never twice: name_clash / live / an old daemon holding its lock)."""
    from fleetmux import fleet, named_subfleet
    if s.get('form') != 'attached':  # D14: a roster / job / split row is never declared again as an attach
        return {'action': 're-attach', 'state': 'failed', 'note': name + ' was a ' + str(s.get('form') or '?')
                + ' sub-fleet, not an attached one: not re-attached'}
    old = Path(s.get('run_dir') or '')
    if old and (old / 'daemon.lock').is_file() and fleet.daemon_running(old):
        return {'action': 're-attach', 'state': 'failed', 'note': 'the previous run\'s sub-fleet ' + name +
                ' still runs (' + str(old) + '); not started a second time'}
    spec = {k: v for k, v in (s.get('spec') or {}).items() if k not in ('id', 'op')}
    entry = s.get('entry') or {}
    source = 'source file'
    given = spec.get('roster')
    if not spec:  # attached before snapshots kept the options: rebuild them from the entry
        for k in getattr(named_subfleet, 'ATTACH_ENTRY_KEYS', ()):
            if k in entry:
                spec.setdefault('entry', {})[k] = entry[k]
        for k in ('roles', 'overrides', 'supervisor_model'):
            if entry.get(k):
                spec[k] = entry[k]
        given = None
    if 'workers' in spec or 'worker_names' in spec:
        source = 'attach options (workers)'
    elif not given or not Path(given).is_file():
        copy = entry.get('roster')
        if copy and Path(copy).is_file():
            spec['roster'], source = copy, 'previous run copy'
        elif (s.get('files') or {}).get('roster'):
            path = fleet_dir(f) / 'restore' / (name + '-roster.yaml')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(s['files']['roster'])
            spec['roster'], source = str(path), 'snapshot mirror'
        else:
            return {'action': 're-attach', 'state': 'failed', 'note': 'no roster left for ' + name}
        spec.pop('worker_names', None)
        spec.pop('workers', None)
    if spec.get('roles') and not Path(spec['roles']).is_dir():
        spec.pop('roles')
        source += '; roles dir gone (default roles)'
    rid = 'restore-' + uuid.uuid4().hex[:12]
    spec.update(id=rid, op='subfleet.attach', name=name,
                reason='restored after a restart of the root (section 60)')
    try:
        named_subfleet.request(f, spec)
    except Exception as exc:  # noqa: BLE001 - ValueError: refused at entry
        return {'action': 're-attach', 'state': 'failed', 'note': str(exc)[:300], 'source': source}
    recs = named_subfleet.attached(f)
    if name in recs:
        recs[name]['restored'] = rid
        named_subfleet.save_attached(f, recs)
    follow_stubs(f, name, rid)
    audit(f, 'restart.restored', name=name, id=rid, source=source)
    return {'action': 're-attach', 'state': 'started', 'op': rid, 'source': source}


def follow_stubs(f, name, rid):
    """Service join stubs of this fleet home that name ``name`` point at the new root run from now on."""
    from fleetmux import fleet, join
    h = home(f.cfg)
    paths = list((h / 'joins').glob('*.json')) if (h / 'joins').is_dir() else []
    for path in paths:
        stub = (read_json(path) or {}).get(join.KEY)
        if not isinstance(stub, dict) or stub.get('kind') != 'service' or stub.get('child') != name:
            continue
        if stub.get('root_home') and Path(stub['root_home']).expanduser().resolve() != h.resolve():
            continue
        stub.update(root_config=str(f.path), attach_id=rid,
                    child_config=str(f.run_dir / 'sub' / name / 'config.json'))
        try:
            fleet.save(path, {join.KEY: stub})
        except OSError:
            continue


def write_cancel(cfg, name, how='detach', by='operator'):
    """A cancel tombstone for an attached sub-fleet: the next start does not bring it back."""
    try:
        write_json(root_dir(cfg) / 'cancels' / ('subfleet-' + name + '.json'), {'name': name, 'how': how, 'by': by,
                                                                               'at': time.time()})
    except OSError:
        pass


def clear_cancel(cfg, name):
    """The operator attached ``name`` again: its tombstone (also one from stop --final) no longer holds."""
    try:
        (root_dir(cfg) / 'cancels' / ('subfleet-' + name + '.json')).unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ ledger
def ledger_path(f, lid):
    return fleet_dir(f) / 'ledgers' / (lid + '.json')


def open_ledger(f, items, mode, baseline=None, cancelled=()):
    """Add ``items`` to the open ledger, or open a new one. A new ledger with items sends the opening summary."""
    d = fleet_dir(f)
    st = load_state(d)
    led = read_json(ledger_path(f, st['ledger'])) if st.get('ledger') else None
    created = False
    if not led or led.get('closed'):
        st['ledger_seq'] = int(st.get('ledger_seq') or 0) + 1
        lid = 'L' + str(st['ledger_seq'])
        led = {'id': lid, 'opened': time.time(), 'run': str(f.run_dir), 'mode': mode, 'baseline': baseline,
               'items': [], 'cancelled': [], 'delivered_generation': -1, 'delivered_count': 0, 'closed': None}
        st['ledger'] = lid
        created = True
    n = len(led['items'])
    open_by_key = {(i['kind'], i['key']): i for i in led['items'] if i['state'] not in DONE}
    for it in items:
        same = open_by_key.get((it['kind'], it['key']))
        if same is not None:  # still open from before (an earlier restart or generation): updated, never doubled
            same.update(evidence=it['evidence'], at=time.time(), **({'auto': it['auto']} if it.get('auto') else {}))
            continue
        n += 1
        led['items'].append(dict(it, id='R' + str(n), at=time.time()))
    led['cancelled'] = list(led.get('cancelled') or []) + list(cancelled)
    led['changed'] = time.time()
    save_ledger(f, led)
    save_state(d, st)
    audit(f, 'restart.ledger', ledger=led['id'], mode=mode, items=len(items), created=created)
    if created and items:
        notify(f, 'reconcile_opened', opening_text(f, led))
    if not any(i['state'] not in DONE for i in led['items']):
        close(f, led)
    return led


def save_ledger(f, led):
    write_json(ledger_path(f, led['id']), led)
    try:
        ledger_path(f, led['id']).with_suffix('.md').write_text(markdown(f, led))
    except OSError:
        pass


def label(f):
    return str(f.cfg.get('name') or fleet_key(f.cfg, f.run_dir))


def counts(led):
    c = {'auto': 0, 'open': 0}
    for i in led['items']:
        c[i['class']] = c.get(i['class'], 0) + 1
        if (i.get('auto') or {}).get('state') in ('done', 'started', 'up'):
            c['auto'] += 1
        if i['state'] not in DONE:
            c['open'] += 1
    return c


def opening_text(f, led):
    c = counts(led)
    return (label(f) + ' reconcile ' + led['id'] + ' opened after ' + ('a restart' if led['mode'] == 'restart' else
            'a supervisor change') + ': ' + str(len(led['items'])) + ' item(s) (stopped ' + str(c.get('stopped', 0)) +
            ', lost ' + str(c.get('lost', 0)) + ', errored ' + str(c.get('errored', 0)) + '); restored automatically '
            + str(c['auto']) + ', waiting for the supervisor ' + str(c['open']) + '.')


def closing_text(f, led):
    rec = [i for i in led['items'] if i.get('verdict') == 'recovered']
    redo = [i for i in led['items'] if i.get('verdict') == 'redo']
    drop = [i for i in led['items'] if i.get('verdict') == 'drop']
    text = (label(f) + ' reconcile ' + led['id'] + ' closed: recovered ' + str(len(rec)) + ', redone ' + str(len(redo))
            + ', dropped ' + str(len(drop)))
    if drop:
        text += ' (' + '; '.join(i['id'] + ' ' + i['kind'] + ' ' + i['key'] + ': ' + i.get('reason', '')
                                 for i in drop)[:600] + ')'
    if led.get('cancelled'):
        text += '; cancelled by the operator, not brought back: ' + str(len(led['cancelled']))
    return text + '.'


def markdown(f, led):
    lines = ['# Reconcile ' + led['id'] + ' (' + label(f) + ')', '',
             '- run: ' + led['run'], '- mode: ' + led['mode'], '- baseline snapshot: ' + str(led.get('baseline')),
             '- opened: ' + time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(led['opened'])),
             '- closed: ' + (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(led['closed'])) if led.get('closed')
                             else 'open'), '',
             '| item | class | kind | key | evidence | auto | state | verdict | reason |', '|---|---|---|---|---|---|---|---|---|']
    for i in led['items']:
        auto = i.get('auto') or {}
        lines.append('| ' + ' | '.join(str(x).replace('|', '/').replace('\n', ' ')[:300] for x in (
            i['id'], i['class'], i['kind'], i['key'], i['evidence'],
            (auto.get('action', '') + ' ' + auto.get('state', '') + (' (' + auto['source'] + ')' if auto.get('source')
                                                                       else '') + (' ' + auto['note'] if auto.get('note') else '')).strip(),
            i['state'], i.get('verdict') or '', i.get('reason') or '')) + ' |')
    if led.get('cancelled'):
        lines += ['', '## Cancelled by the operator (not brought back)', '']
        lines += ['- ' + c['kind'] + ' ' + c['key'] + ': ' + c['why'] for c in led['cancelled']]
    return '\n'.join(lines) + '\n'


def notify(f, event, text):
    try:
        from fleetmux import lifecycle_close
        lifecycle_close.notify(f, event, text)
    except Exception as exc:  # noqa: BLE001
        audit(f, 'restart.error', level='warn', where='notify', error=repr(exc)[:300])


def close(f, led):
    led['closed'] = time.time()
    save_ledger(f, led)
    d = fleet_dir(f)
    st = load_state(d)
    for i in led['items']:
        if i['class'] == 'errored' and i.get('verdict'):
            st['ruled'][i['fp']] = i.get('ruled_at') or time.time()
    if st.get('ledger') == led['id']:
        st['ledger'] = ''
    save_state(d, st)
    report = d / 'reports' / ('reconcile-' + led['id'])
    write_json(report.with_suffix('.json'), led)
    report.with_suffix('.md').write_text(markdown(f, led))
    audit(f, 'restart.closed', ledger=led['id'], items=len(led['items']))
    if led['items']:
        notify(f, 'reconcile_closed', closing_text(f, led))


def current(f):
    st = load_state(fleet_dir(f))
    return read_json(ledger_path(f, st['ledger'])) if st.get('ledger') else None


def follow(f):
    """Auto actions that run on later ticks: a restored sub-fleet's attach op answered (up / refused)."""
    from fleetmux import fleet
    led = current(f)
    if not led:
        return
    changed = False
    for i in led['items']:
        auto = i.get('auto') or {}
        if auto.get('state') == 'started' and auto.get('op'):
            raw = f.r.get(fleet.RESULT + auto['op'])
            if raw:
                res = json.loads(raw)
                if res.get('status') == 'ok':
                    auto['state'] = 'up'
                else:
                    auto.update(state='failed', note=str(res.get('reason') or res.get('error') or res)[:300])
                    i['class'] = 'errored'
                if i['state'] == 'redo_running':
                    i['state'] = 'done' if auto['state'] == 'up' else 'open'
                changed = True
    if changed:
        led['changed'] = time.time()
        save_ledger(f, led)
        if not any(i['state'] not in DONE for i in led['items']):
            close(f, led)


def deliver(f, force=False):
    """The open items to the current supervisor generation, once it is READY, through the daemon's own queue (never
    its input box). Again when items were added, and every ruling_remind_s while items stay open."""
    led = current(f)
    if not led:
        return
    open_items = [i for i in led['items'] if i['state'] not in DONE]
    if not open_items:
        return
    sup = f.agent('supervisor')
    if sup.get('runtime_ready') != '1' or getattr(f, 'restart_ready_gen', None) is None:
        return
    gen = int(sup.get('generation') or 0)
    k = knobs()
    due = (gen != led.get('delivered_generation') or len(led['items']) != led.get('delivered_count')
           or time.time() - float(led.get('delivered_at') or 0) >= k['ruling_remind_s'])
    if not due:
        return
    f.queue('supervisor', notice(f, led, open_items, k), kind='followup' if f.service else None)
    led.update(delivered_generation=gen, delivered_count=len(led['items']), delivered_at=time.time())
    save_ledger(f, led)
    audit(f, 'restart.delivered', ledger=led['id'], generation=gen, items=len(open_items))


def notice(f, led, open_items, k):
    c = counts(led)
    head = ('[Reconcile ' + led['id'] + '] After ' + ('a restart of this fleet' if led['mode'] == 'restart' else
            'a new supervisor generation') + ', ' + str(len(open_items)) + ' item(s) need your ruling (stopped '
            + str(c.get('stopped', 0)) + ', lost ' + str(c.get('lost', 0)) + ', errored ' + str(c.get('errored', 0))
            + '; restored automatically ' + str(c['auto']) + ', confirm them).')
    rows = []
    for i in open_items[:int(k['inline_items'])]:
        auto = i.get('auto') or {}
        rows.append(i['id'] + ' [' + i['class'] + '][' + i['kind'] + ' ' + i['key'] + '] ' + cap(i['evidence'], 300)
                    + (' (auto: ' + auto.get('action', '') + ' ' + auto.get('state', '') + ')' if auto else ''))
    more = len(open_items) - len(rows)
    tail = ''
    if more > 0:
        tail = ('\n' + str(more) + ' more item(s): read ' + str(ledger_path(f, led['id']).with_suffix('.md')) +
                ' or run `fleetmux reconcile show --config ' + str(getattr(f, 'path', '')) + '`.')
    return (head + '\n' + '\n'.join(rows) + tail + '\nThis is not a request. Rule each item with the key "reconcile" '
            'in your decision JSON: "reconcile": [{"item": "R1", "verdict": "recovered" | "redo" | "drop", "reason": '
            '"<one sentence; required for drop>"}], with the rest of your decision as usual (status RUNNING, dispatch '
            '[]). "redo" runs the program\'s action where it has one (re-attach a sub-fleet, submit a job again); '
            'otherwise do it with the usual commands and then rule "recovered".')


def validate_decision(value):
    """Decision JSON ``reconcile``: a list of {item, verdict, reason}; drop needs a reason."""
    if not isinstance(value, list):
        raise ValueError('reconcile must be a list of {"item", "verdict", "reason"}')
    for r in value:
        if not isinstance(r, dict) or not isinstance(r.get('item'), str) or r.get('verdict') not in VERDICTS:
            raise ValueError('every reconcile entry needs item (R<n>) and verdict recovered / redo / drop')
        if 'reason' in r and not isinstance(r['reason'], str):
            raise ValueError('reconcile reason must be text')
        if r['verdict'] == 'drop' and not (r.get('reason') or '').strip():
            raise ValueError('reconcile drop of ' + r['item'] + ' needs a reason')
    return value


def decide(f, rulings, by='supervisor', ledger=None):
    """Apply rulings to the open ledger (or ``ledger``). Returns {applied, refused}. Refusals go back to the
    supervisor as one note (by supervisor) or in the answer (by operator)."""
    applied, refused = [], []
    led = read_json(ledger_path(f, ledger)) if ledger else current(f)
    if not led or led.get('closed'):
        refused = [str(r.get('item')) + ': no open reconcile ledger' + (' ' + ledger if ledger else '') for r in rulings]
    else:
        byid = {i['id']: i for i in led['items']}
        for r in rulings:
            it = byid.get(r['item'])
            if it is None:
                refused.append(r['item'] + ': no such item in ' + led['id'])
                continue
            if it['state'] in DONE:
                refused.append(r['item'] + ': already ' + it['state'])
                continue
            it.update(verdict=r['verdict'], reason=(r.get('reason') or '').strip(), ruled_by=by, ruled_at=time.time())
            if r['verdict'] == 'recovered':
                it['state'] = 'recovered'
            elif r['verdict'] == 'drop':
                it['state'] = 'dropped'
                drop_action(f, it)
            else:
                redo_action(f, it)
            applied.append(r['item'])
        led['changed'] = time.time()
        save_ledger(f, led)
        audit(f, 'restart.ruled', ledger=led['id'], applied=applied, refused=refused, by=by)
        if not any(i['state'] not in DONE for i in led['items']):
            close(f, led)
    if refused and by == 'supervisor':
        f.queue('supervisor', '[Reconcile] Not applied: ' + '; '.join(refused) + '. Reply with the usual decision '
                'JSON (status RUNNING, dispatch []).', kind='followup' if f.service else None)
    return {'applied': applied, 'refused': refused}


def drop_action(f, it):
    """Dropping a sub-fleet the program restored takes it down again and records the cancel."""
    if it['kind'] == 'subfleet' and (it.get('auto') or {}).get('op'):
        from fleetmux import named_subfleet
        try:
            named_subfleet.request(f, {'id': 'detach-' + uuid.uuid4().hex[:12], 'op': 'subfleet.detach',
                                       'name': it['key'], 'reason': 'reconcile drop: ' + it.get('reason', '')})
        except Exception as exc:  # noqa: BLE001
            it['note'] = 'detach failed: ' + str(exc)[:200]
        write_cancel(f.cfg, it['key'], how='reconcile drop')


def redo_action(f, it):
    """The program's redo for an item kind that has one; otherwise the supervisor does it (redo_pending)."""
    from fleetmux import fleet
    try:
        if it['kind'] == 'subfleet':
            snap = baseline_row(f, it)
            if snap is None or snap.get('form') != 'attached':  # D14: only an attached row is re-attached
                it['state'] = 'redo_pending'
                return
            auto = restore_attached(f, it['key'], snap)
            it['auto'] = auto
            it['state'] = 'redo_running' if auto.get('state') == 'started' else 'open'
            return
        if it['kind'] == 'job' and it.get('spec_goal'):
            if it.get('replayed'):
                it['state'] = 'open'
                it['note'] = 'already submitted again as ' + it['replayed']
                return
            jid = f.job_submit(dict(it.get('submit') or {'goal': it['spec_goal']})) if hasattr(f, 'job_submit') else None
            if jid:
                it.update(replayed=jid, state='done')
                return
        if it['kind'] == 'request' and it.get('payload'):
            spec = json.loads(it['payload']) if it['payload'].startswith('{') else None
            if isinstance(spec, dict) and not it.get('replayed'):
                spec = dict(spec, id='replay-' + uuid.uuid4().hex[:10])  # replayed once: the item records it
                f.request(spec)
                it.update(replayed=spec['id'], state='done')
                return
    except Exception as exc:  # noqa: BLE001
        it.update(state='open', note='redo failed: ' + str(exc)[:300])
        return
    it['state'] = 'redo_pending'


def baseline_row(f, it):
    led = current(f)
    seq = (led or {}).get('baseline')
    if not seq:
        return None
    for s, _, path in snapshots(fleet_dir(f)):
        if s == seq:
            doc, _ = read_snapshot(path)
            return ((doc or {}).get('kinds', {}).get('subfleets') or {}).get(it['key'])
    return None


# ------------------------------------------------------------------ retention of child fleets' directories
def prune_children(f):
    """restart/job-* and split-* directories: once the child fleet no longer runs and has no open ledger, only the
    newest keep_child_dirs stay."""
    from fleetmux import fleet
    base = home(f.cfg) / 'restart'
    if not base.is_dir():
        return
    keep = int(knobs()['keep_child_dirs'])
    done = []
    for d in base.iterdir():
        if not d.is_dir() or not d.name.startswith(('job-', 'split-')):
            continue
        st = load_state(d)
        if st.get('ledger'):
            continue
        runs = {(read_json(p) or {}).get('fleet', {}).get('run') for p in list(d.glob('snap-*.json'))[-1:]}
        if any(r and Path(r).is_dir() and (Path(r) / 'daemon.lock').is_file() and fleet.daemon_running(Path(r))
               for r in runs):
            continue
        done.append(d)
    done.sort(key=lambda p: p.stat().st_mtime)
    import shutil
    for d in done[:-keep] if len(done) > keep else []:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ CLI helpers
def show(cfg):
    """`fleetmux reconcile show`: the open ledger of this fleet (or the newest closed one) as Markdown."""
    from types import SimpleNamespace
    d = home(cfg) / 'restart' / fleet_key(cfg, cfg.get('run'))
    st = load_state(d)
    lid = st.get('ledger') or ('L' + str(st['ledger_seq']) if st.get('ledger_seq') else '')
    if not lid:
        return 'no reconcile ledger for this fleet (' + str(d) + ')'
    path = d / 'ledgers' / (lid + '.md')
    try:
        return path.read_text()
    except OSError:
        return 'ledger ' + lid + ' has no Markdown view at ' + str(path)
