"""Named sub-fleets (docs/design.md §29): roster ``subfleets:`` entries, external up/down, service children.

A named sub-fleet is declared like a worker (roster ``subfleets:``, hot) and run by the §19 machinery
(``subfleet.spawn_child`` with ``named=<entry>``): the entry's ``supervisor_slot`` worker becomes the
child's supervisor, the child runs its own roster and worker runtime, and a service child has no task.

Parent Redis
  fx:subfleet:<name>      hash: state (down / starting / up / paused / draining), slot, redis_socket,
                          run_dir, up_at, drain_since (awake time), drain_reason, crashloop, usage_at
  fx:subfleet:opq         list of external op ids still open
  fx:subfleet:op:<id>     hash: op, name, reason, status (awaiting / approved), created, approved_at

External ops ``{"id", "op": "subfleet.up" | "subfleet.down", "name", "reason"}`` arrive as service
requests but never go through validate_request / REQQ / request_timeout.

Attached children (``fleetmux start`` of a service fleet while a root service fleet runs, §29 / §58): the op
``subfleet.attach`` declares a service child at run time from the start options (no roster edit, no supervisor
approval). The program gives it an idle worker as its supervisor (or adds one, as for a job), brings it up like
an autostart entry and answers the op with the child's address. ``subfleet.detach`` takes the declaration out
again: the child drains and goes down, an added worker leaves the roster. Attached entries live in
``fx:subfleets:attached`` (JSON {name: record}), so a roster reload never drains them and a resume keeps them. The program gate answers
what it can from state alone (unknown name, autostart down, already up / already down, limits);
everything else asks the parent supervisor, whose service decision JSON carries
``"subfleet": {"id", "decision": "approve" | "reject", "note"}``. Execution is the daemon's (``tick``).
The result is published under ``fx:result:<id>`` like any service request.

Internal API
  declare(fleet, entries)           roster reload: new / removed / re-added entries
  request(fleet, spec) -> id        inside the request event (transaction-safe: Redis + queue only)
  validate_decision(item)           shape of the decision's ``subfleet`` field (parse_service_decision)
  decide(fleet, item)               supervisor approve / reject (transaction-safe)
  tick(fleet)                       parent daemon tick, outside transactions (called by subfleet.tick)
  revive(fleet, d, rec, cfg)        autostart child daemon gone: relaunch unless crashlooping
  collected(fleet, rec, returned)   subfleet.collect finished a named child
"""
import json
import re
import shlex
import time
import uuid
from pathlib import Path

from fleetmux import applet_host, awake, fleet_config, job_chain, roster, subfleet

OPS = ('subfleet.up', 'subfleet.down', 'subfleet.attach', 'subfleet.detach')
HASH = 'fx:subfleet:'
OPQ = 'fx:subfleet:opq'
OP = 'fx:subfleet:op:'
ATTACHED = 'fx:subfleets:attached'  # JSON {name: {entry, id, created, waiting, hot_slot}}: run-time declarations
DETACHED = 'fx:subfleets:detached'  # hash name -> worker added for it, removed from the roster once the child is down
RESERVED_NAMES = ('opq', 'op')  # fx:subfleet:<name> would meet the op queue / op hashes
ATTACH_KEYS = ('id', 'op', 'name', 'reason', 'roster', 'roles', 'workers', 'worker_names', 'entry', 'overrides',
               'supervisor_model')
ATTACH_ENTRY_KEYS = ('claude', 'worker_model', 'worker_cli', 'pi_model', 'pi_thinking')  # start options it takes
ATTACH_HOT_BASE = 'worker'
DOWN_OUTCOMES = ('down', 'stopped')  # collect outcomes that are a planned down, not a failure
SLOT_WAIT = 600  # seconds an approved up waits for the supervisor slot to be idle / READY
CRASH_WINDOW, CRASH_MAX = 3600, 3  # autostart: this many daemon deaths within the window = crashloop
# A returned session's last Stop marks its channel stopping (claude_hook.STOPPING_TTL), then the rewake waits
# claude_hook.GRACE: past this, nothing will take the return handoff (§29).
RETURN_WAKE_S = 180
USAGE_EVERY = 30  # seconds between two context-usage reads of an up autostart child's supervisor
SUP_DOWN = ('dead', 'held')  # child supervisor states in which a drain ends at once (nothing in flight can settle)


def _fleet():
    from fleetmux import fleet
    return fleet


def declared(f):
    """{name: entry}: the roster's ``subfleets:`` entries plus the attached ones (a roster name wins)."""
    out = dict(getattr(f, 'subfleet_decl', None) or {})
    for name, rec in attached(f).items():
        out.setdefault(name, rec['entry'])
    return out


def attached(f):
    raw = f.r.get(ATTACHED)
    return json.loads(raw) if raw else {}


def save_attached(f, recs):
    f.r.set(ATTACHED, json.dumps(recs, ensure_ascii=False, separators=(',', ':')))


def reserved(f, slot, attached_only=False):
    """The declared sub-fleet ``slot`` supervises (roster or attached; ``attached_only``: attached), else ''."""
    entries = {n: r['entry'] for n, r in attached(f).items()} if attached_only else declared(f)
    return next((n for n, e in entries.items() if e.get('supervisor_slot') == slot), '')


def state(f, name):
    return f.r.hgetall(HASH + name)


def waiting_rows(r):
    """{name: {state, blocked | waiting}} of the declared sub-fleets that carry a reason (``status``): a roster entry's
    ``fx:subfleet:<name>`` blocked, an attached one's record ``waiting``. Read-only, no daemon contact; a sub-fleet
    that is not up has no subfleet.json record, so ``subfleet.tree`` cannot show it."""
    out = {}
    for key in r.scan_iter(HASH + '*'):
        name = key[len(HASH):]
        if name == 'opq' or name.startswith('op:') or r.type(key) != 'hash':
            continue
        h = r.hgetall(key)
        if h.get('blocked'):
            out[name] = {'state': h.get('state') or 'down', 'blocked': h['blocked']}
    raw = r.get(ATTACHED)
    for name, rec in (json.loads(raw) if raw else {}).items():
        if rec.get('waiting'):
            out[name] = {'state': 'attached', 'waiting': rec['waiting']}
    return out


def live(f):
    """{name: child run dir} of the named children this fleet runs now (starting / running / stopping)."""
    out = {}
    for d in subfleet.active_children(f.run_dir):
        rec = subfleet.record(d)
        if rec.get('named'):
            out[rec['name']] = d
    return out


def address(f, name):
    h = state(f, name)
    return {'name': name, 'state': h.get('state') or 'down', 'redis_socket': h.get('redis_socket') or '',
            'run_dir': h.get('run_dir') or ''}


# ------------------------------------------------------------------ declaration (roster reload)
def declare(f, entries):
    """Roster reload accepted ``entries`` (roster.subfleet_entries). A removed entry that is up drains
    and goes down; added back before that, the drain is cancelled. A changed entry applies at its next up."""
    before = declared(f)
    f.subfleet_decl = {e['name']: e for e in entries}
    redeclared(f, before)


def redeclared(f, before):
    """Apply the change from ``before`` to the declarations now (roster reload, attach, detach)."""
    now = declared(f)
    running = live(f)
    for name in now:
        if name not in before:
            f.log('subfleet.declared', name=name, slot=now[name]['supervisor_slot'], autostart=now[name]['autostart'],
                  mode=now[name]['mode'])
            h = state(f, name)
            if h.get('state') == 'draining' and h.get('drain_reason') == 'removed' and name in running \
                    and subfleet.record(running[name]).get('state') == 'running':
                f.r.hset(HASH + name, mapping={'state': 'up', 'drain_since': '', 'drain_reason': ''})
                f.log('subfleet.declared', name=name, result='removal cancelled')
        elif now[name] != before[name]:
            f.log('subfleet.declared', name=name, result='changed; applies at its next up')
    for name in before:
        if name not in now:
            f.log('subfleet.declared', name=name, result='removed' + ('; draining' if name in running else ''))
            if name in running:
                begin_drain(f, name, 'removed')


# ------------------------------------------------------------------ external ops (inside the request event)
def is_op(spec):
    return isinstance(spec, dict) and spec.get('op') in OPS


class InProgress(Exception):
    """The same id resent while its op is still open (awaiting / approved): not a new op, not refused."""
    def __init__(self, rid, status):
        super().__init__(rid + ' is in progress (' + status + ')')
        self.rid, self.status = rid, status


def request(f, spec):
    """Take one external up / down / attach / detach. ValueError: refused before it got an id (malformed /
    duplicate / an attach the program refuses at entry). InProgress: the same op under the same id is still open
    (a retry, e.g. after the parent resumed)."""
    fleet = _fleet()
    rid = spec.get('id')
    if not isinstance(rid, str) or not re.fullmatch(fleet.REQ_ID_RE, rid):
        raise ValueError('id must match ' + fleet.REQ_ID_RE)
    if not isinstance(spec.get('name'), str) or not spec['name']:
        raise ValueError('name is required')
    if spec.get('reason') is not None and not isinstance(spec['reason'], str):
        raise ValueError('reason must be text')
    allowed = ATTACH_KEYS if spec['op'] == 'subfleet.attach' else ('id', 'op', 'name', 'reason')
    extra = sorted(set(spec) - set(allowed))
    if extra:
        raise ValueError('unknown keys for ' + spec['op'] + ': ' + ', '.join(extra))
    open_op = f.r.hgetall(OP + rid)
    kind = {'attach': 'up', 'detach': 'down'}.get(spec['op'].split('.', 1)[1], spec['op'].split('.', 1)[1])
    if open_op and open_op.get('op') == kind and open_op.get('name') == spec['name']:
        raise InProgress(rid, open_op.get('status') or 'awaiting')
    if (open_op or f.r.hgetall(fleet.REQ + rid) or f.r.get(fleet.RESULT + rid)
            or (f.v2 and f.r.get(f.v2.prefix + ':public:' + _digest(rid)))):
        raise ValueError('duplicate request id: ' + rid)
    if spec['op'] == 'subfleet.attach':
        return attach(f, rid, spec)
    if spec['op'] == 'subfleet.detach':
        return detach(f, rid, spec)
    op, name = spec['op'].split('.', 1)[1], spec['name']
    f.r.hset(OP + rid, mapping={'op': op, 'name': name, 'reason': spec.get('reason') or '', 'status': 'awaiting',
                                'created': time.time(), 'approved_at': 0})
    f.r.rpush(OPQ, rid)
    f.log('subfleet.request_in', id=rid, op=op, name=name, reason=spec.get('reason') or '')
    verdict = gate(f, op, name)
    if verdict is not None:
        finish(f, rid, *verdict)
        return rid
    ask(f, rid, op, name, spec.get('reason') or '')
    return rid


def sidebar_down(f, name, attached=False):
    """fleetmux sidebar (design section 19 "Closing a sub-fleet from the sidebar"): the user confirmed the close box of
    named sub-fleet ``name``. That confirmation is the approval: the down is recorded approved at once (never put to
    the root supervisor) with one audit record, and the tick runs it like any approved down (``op_down``). An attached
    child goes through :func:`detach`. The program gate still applies (an autostart child is refused). Returns '' or
    the refusal reason."""
    rid = 'sidebar-' + uuid.uuid4().hex[:12]
    reason = 'closed from the fleetmux sidebar'
    if attached:
        detach(f, rid, {'id': rid, 'op': 'subfleet.detach', 'name': name, 'reason': reason})
        f.log('subfleet.sidebar_down', id=rid, name=name, how='detach', approved_by='user (fleetmux sidebar)')
        return ''
    verdict = gate(f, 'down', name)
    if verdict is not None:
        status, fields = verdict
        return '' if status == 'ok' else (fields.get('reason') or status)
    now = time.time()
    f.r.hset(OP + rid, mapping={'op': 'down', 'name': name, 'reason': reason, 'status': 'approved', 'created': now,
                                'approved_at': now})
    f.r.rpush(OPQ, rid)
    f.log('subfleet.sidebar_down', id=rid, name=name, how='down', approved_by='user (fleetmux sidebar)')
    return ''


def ask(f, rid, op, name, reason):
    f.queue('supervisor', '[Request ' + rid + '] External request ' + rid + ' wants sub-fleet ' + name + ' '
            + op + ('. Reason: ' + reason if reason else '') + '.\nYou only approve or reject; '
            'the daemon does the rest. Reply with the service decision JSON from AGENTS.md: status RUNNING, '
            'dispatch [], skip [], plus "subfleet": {"id": "' + rid + '", "decision": "approve" or "reject", '
            '"note": "<one sentence>"}.')


def requeue_awaiting(f):
    """Parent ``--resume`` (service): resume cancels every undelivered supervisor message, approvals included.
    Each op still awaiting its approval is asked again, so every accepted op reaches a final result; approved
    ops need nothing (tick runs them)."""
    for rid in f.r.lrange(OPQ, 0, -1):
        op = f.r.hgetall(OP + rid)
        if op.get('status') == 'awaiting':
            ask(f, rid, op['op'], op['name'], op.get('reason') or '')
            f.log('subfleet.request_requeued', id=rid, op=op['op'], name=op['name'])


# ------------------------------------------------------------------ attached children (``fleetmux start``)
SUPERVISOR_ROLE = ('You supervise the service sub-fleet {name}. It was started with `fleetmux start` while this '
                   'machine already ran a root service fleet, so it runs as a sub-fleet of that root, on its own '
                   'page. Requests for {name} arrive on its own bus; route them to your workers as AGENTS.md says.\n')


def attach_dir(f, name):
    return f.run_dir / 'attached' / name


def name_clash(f, name):
    """Why ``name`` cannot be attached now, else ''."""
    fleet = _fleet()
    if not re.fullmatch(roster.NAME_RE, name) or name == 'supervisor':
        return 'name must match ' + roster.NAME_RE + ' and not be "supervisor"'
    if name in RESERVED_NAMES:
        return 'name ' + name + ' is reserved (' + ', '.join(RESERVED_NAMES) + ')'
    if name in declared(f):
        return 'a sub-fleet named ' + name + ' is already declared in this fleet'
    if name in f.names or name in f.workers or f.r.hgetall(fleet.key('agent', name)):
        return name + ' is a worker name of this fleet'
    if name in live(f) or subfleet.record(f.run_dir / 'sub' / name).get('state') in subfleet.ACTIVE:
        return 'a sub-fleet named ' + name + ' is still running in this fleet'
    if f.r.hget(DETACHED, name) is not None:
        return 'the sub-fleet ' + name + ' is still going down; pick another name or wait'
    return ''


def attach_roster(f, name, spec):
    """(roster path, supervisor role path): the child's roster (a copy of ``roster`` inside the run, or one made from
    ``workers`` / ``worker_names``) and its supervisor role, under <run>/attached/<name>/. ValueError when bad."""
    folder = attach_dir(f, name)
    given = spec.get('roster')
    if given is not None and (spec.get('workers') is not None or spec.get('worker_names') is not None):
        raise ValueError('roster names the sub-fleet workers; do not also give workers or worker_names')
    if given is not None and (not isinstance(given, str) or not Path(given).is_absolute() or not Path(given).is_file()):
        raise ValueError('roster must be the absolute path of a roster YAML file: ' + repr(given))
    names = spec.get('worker_names')
    count = spec.get('workers')
    if names is not None and (not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names)):
        raise ValueError('worker_names must be a non-empty list of names')
    if count is not None and (type(count) is not int or count < 1):
        raise ValueError('workers must be a positive integer')
    if names is not None and count is not None and count != len(names):
        raise ValueError('workers ' + str(count) + ' does not match the ' + str(len(names)) + ' worker_names')
    folder.mkdir(parents=True, exist_ok=True)
    if given is not None:
        path = roster.copy_into_run(given, folder)  # the child follows its own copy; the caller's file is never written
    else:
        names = names or ['worker-' + str(i + 1) for i in range(count or 1)]
        path = folder / roster.RUN_COPY
        path.write_text('workers:\n' + ''.join('  - {name: ' + n + '}\n' for n in names))
    role = folder / 'supervisor.md'
    role.write_text(SUPERVISOR_ROLE.format(name=name))
    return str(path), str(role)


def attach_entry(f, name, spec):
    """The validated entry of an attached child (supervisor_slot '' until attach_tick gives it one)."""
    extra = spec.get('entry') or {}
    if not isinstance(extra, dict) or set(extra) - set(ATTACH_ENTRY_KEYS):
        raise ValueError('entry takes only ' + ', '.join(ATTACH_ENTRY_KEYS))
    roles = spec.get('roles')
    if roles is not None and (not isinstance(roles, str) or not Path(roles).is_absolute() or not Path(roles).is_dir()):
        raise ValueError('roles must be the absolute path of a directory of <agent-name>.md files: ' + repr(roles))
    path, role = attach_roster(f, name, spec)
    pending = '-pending-'  # the validator wants a worker; the program picks the real one at the next tick
    item = dict(extra, name=name, supervisor_slot=pending, supervisor_role=role, roster=path, autostart=True,
                mode='service')
    entries, errors = roster.subfleet_entries([item], attach_dir(f, name), [pending])
    errors = errors or roster.subfleet_suffix_errors(entries)
    if errors:
        raise ValueError('; '.join(e.replace('subfleets[0] (' + name + '): ', '') for e in errors))
    entry = dict(entries[0], supervisor_slot='', attached=True)
    if roles is not None:
        entry['roles'] = str(Path(roles).resolve())
    overrides = subfleet.check_overrides(spec.get('overrides'))  # the start's own settings (join.py)
    if overrides:
        entry['overrides'] = overrides
    model = spec.get('supervisor_model')
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError('supervisor_model must be a Claude --model value')
        entry['supervisor_model'] = model.strip()
    return entry


def slot_model(f, slot):
    """The Claude model ``slot``'s current generation runs (its launch record over the fleet default)."""
    fleet = _fleet()
    return fleet.claude_model(f.runtime_cfg(slot), slot)


def added_launch(f, slot):
    """Fleet.init_worker of a worker added for an attached child that asked for a supervisor model: its first
    generation (and every later one: the launch record is replayed) runs that model."""
    from fleetmux import profile_launch
    rec = next((r for r in attached(f).values() if r.get('hot_slot') == slot), None)
    model = ((rec or {}).get('entry') or {}).get('supervisor_model')
    picked = ((rec or {}).get('supervisor_pick') or {}).get('model')  # §29: the supervisor chain's pick
    if model or picked:  # the shape Fleet.spawn reads (profile_id, role_text, overrides), as a wall switch writes it
        # a chain pick has no card: profile_id '' so model_usage counts the model it runs (as a job's pick, §58)
        launch = {'profile_id': 'supervisor-model' if model else '', 'role_text': '',
                  'overrides': {'worker_cli': 'claude', 'worker_model': model or picked}}
        f.setf(slot, runtime_launch=profile_launch.encode_launch(launch))


def attach_problem(f, entry):
    """Why no worker of this fleet, existing or added, could ever supervise ``entry``, else ''."""
    fleet = _fleet()
    if f.cfg.get('adopted_supervisor_hook_delivery') is not True:
        return ('no worker of this run can become a sub-fleet supervisor (the run predates '
                'adopted_supervisor_hook_delivery); start a new run for attached sub-fleets')
    like = next((w for w in f.active_workers() if fleet.agent_cli(f.runtime_cfg(w), w) == 'claude'), None)
    if like is None:
        return ('no worker of this fleet runs the official Claude Code CLI: only such a worker can become the '
                'supervisor of a sub-fleet')
    workers = len(roster.load_entries(entry['roster'])[0])
    return subfleet.check(f, like, entry['name'], workers, ready=False, named=dict(entry, supervisor_slot=like)) or ''


def attach(f, rid, spec):
    """``subfleet.attach`` (inside the request event): declare a service child from the start options, approved by
    the program. Every check that does not depend on a free worker runs here; the op is answered with the child's
    address once it is up (op_up)."""
    if f.cfg.get('parent_config') or not f.v2:
        raise ValueError('only a root service fleet of transport v2 takes attached sub-fleets')
    problem = subfleet.backend_problem(f.cfg)
    if problem:
        raise ValueError(problem)
    name = spec['name']
    from fleetmux import restart_state
    recs = attached(f)
    if name in recs and recs[name].get('restored') and not str(spec.get('id', '')).startswith('restore-'):
        # section 60: the root restored this attached child after a restart; the same start again gets its address
        restart_state.clear_cancel(f.cfg, name)
        f.log('subfleet.attach', id=recs[name]['id'], name=name, result='already restored')
        return recs[name]['id']
    clash = name_clash(f, name)
    if clash:
        raise ValueError(clash)
    entry = attach_entry(f, name, spec)
    problem = attach_problem(f, entry)
    if not problem and not entry.get('supervisor_model'):  # D28: the chain must have a Claude entry on this machine
        problem = chain_problem(name, _registry(f))
    if problem:
        raise ValueError(problem)
    before = declared(f)
    recs = attached(f)
    now = time.time()
    recs[name] = {'entry': entry, 'id': rid, 'created': now, 'hot_slot': '', 'waiting': '',
                  'spec': {k: v for k, v in spec.items() if k not in ('id', 'op')}}  # section 60: restore options
    save_attached(f, recs)
    if not rid.startswith('restore-'):  # the operator attached it again: an earlier cancel no longer holds
        restart_state.clear_cancel(f.cfg, name)
    restart_state.write(f, 'attach')  # section 60: on disk now, not only at the next timer (never raises)
    redeclared(f, before)
    f.r.hset(OP + rid, mapping={'op': 'up', 'name': name, 'reason': spec.get('reason') or 'attached by start',
                                'status': 'approved', 'created': now, 'approved_at': now})
    f.r.rpush(OPQ, rid)
    f.log('subfleet.attach', id=rid, name=name, roster=entry['roster'])
    return rid


def detach(f, rid, spec):
    """``subfleet.detach``: take an attached child's declaration out; it drains and goes down (op_down answers)."""
    name = spec['name']
    recs = attached(f)
    if name not in recs:
        raise ValueError(name + ' is not an attached sub-fleet of this fleet')
    rec = recs.pop(name)
    save_attached(f, recs)
    from fleetmux import restart_state
    restart_state.write_cancel(f.cfg, name, how='detach')  # section 60: the next start does not bring it back
    f.r.hset(DETACHED, mapping={name: rec.get('hot_slot') or ''})
    # No drain here (inside the request event): the tick drains a running child no longer declared (op_down).
    f.log('subfleet.declared', name=name, result='detached' + ('; draining' if name in live(f) else ''))
    for orid, _ in ops(f, name, 'up'):
        finish(f, orid, 'rejected', {'reason': 'program: ' + name + ' was detached before it came up'})
    now = time.time()
    f.r.hset(OP + rid, mapping={'op': 'down', 'name': name, 'reason': spec.get('reason') or 'detached',
                                'status': 'approved', 'created': now, 'approved_at': now})
    f.r.rpush(OPQ, rid)
    f.log('subfleet.detach', id=rid, name=name)
    restart_state.write(f, 'detach')  # section 60: on disk now, not only at the next timer (never raises)
    return rid


def same_model(have, want):
    """D30: ``have`` runs ``want`` when both are the same string or sit at the same place of discovery.supervisor
    (claude-opus-5-5 and opus count as one), as job_chain.runs_pick compares a pick."""
    if have == want:
        return True
    from fleetmux import discover, fleet_config
    chain = list((fleet_config.block('discovery') or {}).get('supervisor') or [])
    at = discover.first_match(chain, want)
    return at is not None and discover.first_match(chain, have) == at


def free_for(f, slot, entry, pick=None):
    """'' when ``slot`` can supervise ``entry`` now: an idle Claude worker no sub-fleet, job, lease, split or
    request holds (subfleet.check with ready=False gives the limits and card reasons), else the reason.
    ``pick`` (an entry without supervisor_model): the slot must already run the supervisor chain's pick."""
    fleet = _fleet()
    if fleet.agent_cli(f.runtime_cfg(slot), slot) != 'claude':
        return 'not an official Claude Code worker'
    if reserved(f, slot):
        return 'supervises sub-fleet ' + reserved(f, slot)
    if f.agent(slot).get('state') != 'idle':
        return 'not idle'
    if entry.get('supervisor_model') and not same_model(slot_model(f, slot), entry['supervisor_model']):
        return 'runs model ' + slot_model(f, slot) + ', not the asked supervisor model ' + entry['supervisor_model']
    if pick and pick.get('entry') and not job_chain.runs_pick(pick, pick['chain'], 'claude', slot_model(f, slot)):
        return 'runs model ' + slot_model(f, slot) + ', not the supervisor chain pick claude:' + pick['entry']['model']
    busy = f.job_slot_busy(slot) if hasattr(f, 'job_slot_busy') else ''
    if busy:
        return busy
    workers = len(roster.load_entries(entry['roster'])[0])
    return subfleet.check(f, slot, entry['name'], workers, ready=False, named=dict(entry, supervisor_slot=slot)) or ''


def attach_tick(f):
    """Parent daemon tick (outside transactions): give each attached child without one a supervisor slot (an idle
    worker, else a worker added to the roster, as for a job); drop the worker added for a detached child once it
    is down."""
    from fleetmux import applet_ops
    fleet = _fleet()
    recs = attached(f)
    dirty = False
    for name, rec in recs.items():
        entry = rec['entry']
        slot, hot = entry.get('supervisor_slot') or '', rec.get('hot_slot') or ''
        if slot and slot == hot:
            if slot in f.workers and f.agent(slot).get('state') != 'dead':
                continue
            refused = f.r.hget(fleet.ROSTER, 'rejected') or ''
            if slot not in f.workers and not (refused and slot not in getattr(f, 'desired', [slot])):
                continue  # the roster reload has not started it yet
            drop_rows(f, slot)
            entry['supervisor_slot'], rec['hot_slot'] = '', ''
            rec['waiting'] = 'added worker ' + slot + ' did not start' + (': ' + refused[:300] if refused else '')
            f.log('subfleet.attach_wait', name=name, reason=rec['waiting'])
            dirty = True
            continue
        if slot and (slot in f.active_workers() or name in live(f)):
            continue
        pick = None
        if not entry.get('supervisor_model'):  # Q2 (a): a worker already on the chain's pick, else one added on it
            registry = _registry(f)
            pick = chain_pick(f, '', registry)
            if pick['entry'] is None:
                waiting = chain_problem(name, registry) or CHAIN_WAIT + 'no usable entry now: ' + pick['reason']
                if rec.get('waiting') != waiting:
                    rec['waiting'] = waiting
                    f.log('subfleet.attach_wait', name=name, reason=waiting[:500])
                    dirty = True
                continue
        slot, why = '', []
        for w in sorted(f.active_workers(), key=lambda w: f.agent(w).get('state') != 'idle'):
            reason = free_for(f, w, entry, pick)
            if not reason:
                slot = w
                break
            why.append(f.label(w) + ': ' + reason)
        if not slot:
            paths = applet_ops.roster_paths(f)
            if not paths:
                waiting = 'no free worker (' + '; '.join(why) + ') and no roster file to add one'
                if rec.get('waiting') != waiting:
                    rec['waiting'] = waiting
                    f.log('subfleet.attach_wait', name=name, reason=waiting[:500])
                    dirty = True
                continue
            slot = hot = applet_ops.free_name(f, ATTACH_HOT_BASE)
            try:
                for path in paths:
                    applet_ops.write_worker_row(path, slot, None)
            except (OSError, ValueError) as exc:
                drop_rows(f, slot)
                rec['waiting'] = 'no worker added: ' + str(exc)[:300]
                f.log('subfleet.attach_wait', name=name, reason=rec['waiting'])
                dirty = True
                continue
            f.r.hset(applet_ops.USED, mapping={slot: 'subfleet ' + name})  # never given out again in this run
            rec['hot_slot'] = slot
            if pick:  # added_launch starts it on the pick
                rec['supervisor_pick'] = {'model': pick['entry']['model'],
                                          'trace': job_chain.trace(pick, pick['source'])}
            f.log('subfleet.worker_added', name=name, slot=slot)
        entry['supervisor_slot'], rec['waiting'] = slot, ''
        if slot in f.workers:  # an added worker gets it when it is created (Fleet.init_worker): no agent hash before
            tmux_mark(f, slot)
        for orid, _ in ops(f, name, 'up', approved=True):
            f.r.hset(OP + orid, mapping={'approved_at': time.time()})  # SLOT_WAIT counts from the slot
        f.log('subfleet.attach_slot', name=name, slot=slot, added=bool(hot))
        dirty = True
    if dirty:
        save_attached(f, recs)
    gone = f.r.hgetall(DETACHED)
    running = live(f)
    for name, hot in gone.items():
        if name not in running:
            if hot:
                drop_rows(f, hot)
            mark_detached(f, name)  # detached while already down: no collect ran for it
            f.r.hdel(DETACHED, name)


def mark_detached(f, name):
    """The last run of attached child ``name`` was taken out for good: its record says detached (status) and its
    record page with the history viewer goes (subfleet.collect does the same for a child detached while up)."""
    d = f.run_dir / 'sub' / name
    rec = subfleet.record(d)
    if not rec.get('named') or rec.get('detached') or rec.get('state') in subfleet.ACTIVE:
        return
    rec['detached'] = True
    subfleet.save_record(d, rec)
    try:
        cfg = _fleet().load(d / 'config.json')
    except (OSError, ValueError):
        cfg = {}
    subfleet.drop_child_page(f, rec, cfg)
    f.log('subfleet.detached', name=name, run=str(d))


def tmux_mark(f, slot):
    """A slot that supervises a sub-fleet runs its generations in the fleet's tmux: shown on both pages (§44)."""
    if getattr(f.mgr, 'tmux_view_info', None) is not None:
        f.setf(slot, tmux_view=1)


def drop_rows(f, slot):
    """Take the worker row added for an attached child out of the roster: the reload retires the slot (§16)."""
    from fleetmux import applet_ops
    for path in applet_ops.roster_paths(f):
        try:
            applet_ops.remove_worker_row(path, slot)
        except (OSError, ValueError) as exc:
            f.log('subfleet.worker_remove_failed', slot=slot, path=str(path), error=str(exc)[:200])
    f.log('subfleet.worker_removed', slot=slot)


def _digest(rid):
    from fleetmux import runtime_v2
    return runtime_v2.digest(rid)


def gate(f, op, name):
    """Program verdict (status, fields) without the supervisor, or None (ask the supervisor)."""
    entry = declared(f).get(name)
    if entry is None:
        return 'rejected', {'reason': 'program: no sub-fleet named ' + name + ' is declared'}
    if op == 'down' and entry.get('attached'):
        return 'rejected', {'reason': 'program: ' + name + ' is attached; take it down with subfleet.detach'}
    if op == 'down' and entry['autostart']:
        return 'rejected', {'reason': 'program: ' + name + ' is autostart; it cannot be taken down from outside'}
    running = name in live(f)
    h = state(f, name)
    if op == 'up' and running and h.get('state') in ('up', 'paused'):
        return 'ok', address(f, name)
    if op == 'down' and not running:
        return 'ok', {'name': name, 'state': 'down'}
    if op == 'up' and not running:
        reason = limits_problem(f, entry)
        if reason:
            return 'rejected', {'reason': 'program: ' + reason}
    return None


def limits_problem(f, entry):
    try:
        workers = len(roster.load_entries(entry['roster'])[0])
    except (OSError, ValueError) as exc:
        return 'sub-fleet roster unreadable: ' + str(exc)
    return subfleet.check(f, entry['supervisor_slot'], entry['name'], workers, ready=False, named=entry)


def finish(f, rid, status, fields):
    op = f.r.hgetall(OP + rid)
    f.publish(rid, status, **fields)
    f.r.delete(OP + rid)
    f.r.lrem(OPQ, 0, rid)
    if status == 'rejected':
        f.log('subfleet.rejected', id=rid, name=op.get('name', ''), reason=fields.get('reason', ''))


def validate_decision(item):
    if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
            or item.get('decision') not in ('approve', 'reject') or not isinstance(item.get('note', ''), str)):
        raise ValueError('subfleet must be {"id", "decision": "approve" | "reject", "note"}')


def decide(f, item):
    rid = item['id']
    op = f.r.hgetall(OP + rid)
    if op.get('status') != 'awaiting':
        f.log('subfleet.decision_unknown', id=rid)
        return
    if item['decision'] == 'reject':
        finish(f, rid, 'rejected', {'reason': 'supervisor: ' + (item.get('note') or 'rejected')})
        return
    f.r.hset(OP + rid, mapping={'status': 'approved', 'approved_at': time.time()})
    f.log('subfleet.approved', id=rid, op=op['op'], name=op['name'], note=item.get('note', ''))


# ------------------------------------------------------------------ parent tick (outside transactions)
def child_peek(cfg):
    """What the parent needs from a child's Redis, or None when it is unreachable."""
    fleet = _fleet()
    from fleetmux import runtime_v2
    if not subfleet.redis_up(cfg):
        return None
    try:
        r = fleet.connect(cfg)
    except Exception:
        return None
    try:
        bus = runtime_v2.bus_for(cfg, r)
        names = ['supervisor'] + list(cfg.get('workers') or [])
        sup = r.hgetall(fleet.key('agent', 'supervisor'))
        flying = [n for n in names if r.hget(bus.lease_key(n), 'current_delivery')]
        queued = r.llen(fleet.REQQ) + sum(r.llen(bus.pending_key(n)) for n in names)
        return {'ready': sup.get('runtime_ready') == '1' and sup.get('state') in ('idle', 'busy'),
                'paused': r.hget(fleet.SERVICE, 'paused') == '1', 'quiet': not flying and not queued,
                'sup_idle': sup.get('state') == 'idle' and 'supervisor' not in flying,
                # its supervisor can finish nothing: in-flight work never settles, a drain would only wait
                'sup_down': sup.get('state') if sup.get('state') in SUP_DOWN else '',
                'applets': applet_host.states(r)}  # mirrored into fx:subfleet:<name> by applet_host.mirror
    except Exception:
        return None
    finally:
        r.close(); r.connection_pool.disconnect()


def begin_drain(f, name, why):
    """Stop taking work (the parent hash says draining) and go down once nothing is in flight."""
    if state(f, name).get('state') == 'draining':
        return
    f.r.hset(HASH + name, mapping={'state': 'draining', 'drain_since': awake.now(), 'drain_reason': why})
    f.log('subfleet.rotate' if why == 'rotate' else 'subfleet.draining', name=name, reason=why)
    applet_host.announce_drain(f, name, why)  # host.draining to the child's applets (applet-interface.md 2.1)


def stop_child(f, d, rec, cfg, reason):
    rec.update(state='stopping', outcome='down', reason=reason,
               requests=subfleet.child_requests(cfg))  # for the slot's return note: its Redis goes with the child
    subfleet.save_record(d, rec)
    subfleet.post_stop(cfg)
    f.log('subfleet.stopping', task=rec['task_id'], outcome='down', reason=reason)


def over_threshold(f, slot):
    """(used, window) when ``slot``'s session is past its handoff ratio, else None (design §22)."""
    used, window = f.usage(slot)
    ratio = float(f.agent(slot).get('handoff_ratio') or fleet_config.handoff_ratio(f.cfg))
    return (used, window) if window and used >= ratio * window else None


def rotation_gate(f, slot):
    """§29 N1: never delegate a session over its context threshold. True: delegate now; False: a
    rotation was started (the next generation's READY lets the delegation through)."""
    over = over_threshold(f, slot)
    if not over:
        return True
    info = f.agent(slot)
    gen = info.get('generation') or '0'
    if int(info.get('delivered') or 0) == 0 and info.get('delegated_gen') != gen:
        f.log('rotate.impossible', agent=slot, used=over[0], window=over[1],
              reason='fresh session already over ratio; delegating anyway')
        return True
    f.start_rotation(slot, *over)
    return False


CHAIN_WAIT = 'supervisor chain: '  # a wait reason from the chain (walls, quota, a rotation): carried into op answers


def _registry(f):
    from fleetmux.agent_registry import AgentRegistry
    return AgentRegistry(f.cfg.get('agent_registry') or None)


def chain_pick(f, slot, registry):
    """job_chain.subfleet_supervisor with this run's walls and each model's quota for ``slot`` (Fleet.card_gate;
    '' = a worker not added yet)."""
    def refused(c, m):
        card = f.job_model_card(c, m, registry)
        gate = f.card_gate(slot, card, registry) if card else None
        return gate[1] if gate else None
    return job_chain.subfleet_supervisor(f.walled(), registry=registry, refused=refused)


def chain_problem(name, registry):
    """Why no Claude entry of discovery.supervisor can ever supervise sub-fleet ``name`` on this machine (walls and
    quota left out), else ''. Such an up is refused (D28); a pick blocked only by walls or quota waits."""
    static = job_chain.subfleet_supervisor((), registry=registry)
    if static['entry'] is None:
        return 'no usable supervisor for sub-fleet ' + name + ' on fleet.yaml#discovery.supervisor: ' + static['reason']
    return ''


def explicit_source(f, slot, entry):
    """'entry' (its supervisor_model), 'card' (a card on the slot's root roster row) or '' (the chain decides)."""
    if entry.get('supervisor_model'):
        return 'entry'
    return 'card' if (f.cfg.get('worker_profiles') or {}).get(slot) else ''


def supervisor_choice(f, slot, entry):
    """§29 supervisor model (user 2026-11-22: every sub-fleet walks the supervisor chain in order). Returns
    ('ok', trace) when ``slot`` may supervise as it runs now, ('wait', why) while walls or quota block every entry
    or while the slot rotates to the pick, ('refused', why) when no Claude entry of the chain can ever supervise
    here (D28: never the default model).

    Explicit: the entry's ``supervisor_model`` (an attached child) or a card on the slot's root roster row: kept, no
    chain. Else job_chain.subfleet_supervisor over discovery.supervisor; a slot that already runs the pick is adopted
    in place, any other rotates to it first (the §44 split_launch path: finish_rotation / RuntimeV2.respawn put it
    into the next generation). The slot keeps that model after the child goes down."""
    fleet = _fleet()
    cfg = f.runtime_cfg(slot)
    cli, model = fleet.agent_cli(cfg, slot), fleet.claude_model(cfg, slot)
    source = explicit_source(f, slot, entry)
    if source:
        card = (f.cfg.get('worker_profiles') or {}).get(slot) if source == 'card' else None
        return 'ok', {'cli': cli, 'model': model, 'source': source, 'profile': card, 'skipped': [], 'switched': []}
    from fleetmux.agent_registry import AgentRegistryError
    try:
        registry = _registry(f)
    except (AgentRegistryError, OSError, ValueError) as exc:
        return 'refused', 'cannot read agent cards: ' + str(exc)
    pick = chain_pick(f, slot, registry)
    trace = job_chain.trace(pick, pick['source'])
    if pick['entry'] is None:
        problem = chain_problem(entry['name'], registry)
        return ('refused', problem) if problem else ('wait', CHAIN_WAIT + 'no usable entry now: ' + pick['reason'])
    if job_chain.runs_pick(pick, pick['chain'], cli, model):
        return 'ok', trace
    to = pick['entry']['model']
    launch, why = f.split_supervisor_launch(slot, pick['profile'], None if pick['profile'] else to)
    if why:
        return 'wait', CHAIN_WAIT + why
    from fleetmux import profile_launch  # §44 stores the encoded launch: finish_rotation copies it into runtime_launch
    f.setf(slot, split_launch=json.dumps({'launch': profile_launch.encode_launch(launch)}))
    f.r.hset(HASH + entry['name'], mapping={'model_chain': json.dumps({'supervisor': trace}, ensure_ascii=False)})
    f.log('subfleet.supervisor_rotate', name=entry['name'], slot=slot, frm=cli + ':' + model, to='claude:' + to,
          skipped=len(trace['skipped']))
    f.start_rotation(slot, *f.usage(slot))
    return 'wait', CHAIN_WAIT + slot + ' is rotating to claude:' + to + ' first'


def try_up(f, entry, goal=''):
    """('started', rec) | ('wait', why) | ('refused', why). Spawns only when the slot is idle / READY."""
    name, slot = entry['name'], entry['supervisor_slot']
    if not slot or slot not in f.workers:  # an attached child: attach_tick has not given it a started slot yet
        return 'wait', 'no supervisor slot yet' + (' (' + slot + ' is being added)' if slot else '')
    try:
        workers = len(roster.load_entries(entry['roster'])[0])
    except (OSError, ValueError) as exc:
        return 'refused', 'sub-fleet roster unreadable: ' + str(exc)
    reason = subfleet.check(f, slot, name, workers, ready=False, named=entry)
    if reason:
        return 'refused', reason
    busy = subfleet.check(f, slot, name, workers, ready=True, named=entry)
    if busy:
        return 'wait', busy
    status, trace = supervisor_choice(f, slot, entry)
    if status != 'ok':
        return status, trace
    if not rotation_gate(f, slot):
        return 'wait', slot + ' is rotating to a fresh session first'
    if subfleet.tmux_slot(f.cfg, slot) and subfleet.parent_view(f, slot, f.agent(slot).get('pane')) is None:
        # Declared while the slot already ran as a plain cmux pane (hot roster): this delegation takes the
        # §19 move-surface; the slot's next generation starts in the fleet's tmux (dual display from then on).
        f.log('subfleet.view_fallback', name=name, slot=slot,
              reason='slot not yet in fleet tmux; dual display from next generation')
    try:
        rec = subfleet.spawn_child(f, slot, name, goal or ('Named sub-fleet ' + name), workers,
                                   named=dict(entry, model_chain={'supervisor': trace}))
    except subfleet.Refused as exc:
        return 'refused', str(exc)
    cfg = _fleet().load(Path(rec['run']) / 'config.json')
    f.setf(slot, delegated_gen=f.agent(slot).get('generation') or '0')
    f.r.hset(HASH + name, mapping={'state': 'starting', 'slot': slot, 'run_dir': rec['run'],
                                   'redis_socket': cfg['redis_socket'], 'started_at': time.time(), 'up_at': '',
                                   'drain_since': '', 'drain_reason': '', 'crashloop': '', 'blocked': '',
                                   'model_chain': json.dumps({'supervisor': trace}, ensure_ascii=False)})
    f.log('subfleet.supervisor_pick', name=name, slot=slot, model=trace['cli'] + ':' + trace['model'],
          source=trace['source'], skipped=len(trace['skipped']))
    return 'started', rec


def ready_limit(f):
    return float(f.cfg['profile_ready_timeout']) + subfleet.LAUNCH_GRACE


def return_tick(f, now):
    """A returned slot's handoff (subfleet.collect) nobody took within RETURN_WAKE_S: no rewake fallback was
    left to wake the idle session, so the daemon's skeleton note goes to the next generation now (§29)."""
    for slot in f.workers:
        info = f.agent(slot)
        if not (info.get('subfleet_return') and info.get('state') == 'handoff' and f.v2) or f.v2.handoff_taken(slot):
            continue
        waited = now - float(json.loads(info['subfleet_return']).get('at') or now)
        if waited >= RETURN_WAKE_S:
            f.log('subfleet.return_untaken', slot=slot, waited=round(waited))
            f.finish_rotation(slot, timed_out=True)


def tick(f):
    fleet = _fleet()
    return_tick(f, time.time())
    if f.r.exists(ATTACHED) or f.r.exists(DETACHED):
        attach_tick(f)
    decl = declared(f)
    if not decl and not f.r.llen(OPQ) and not live(f):
        return
    now = time.time()
    running = live(f)
    for name, d in running.items():
        rec = subfleet.record(d)
        if rec.get('state') != 'running':
            continue
        try:
            cfg = fleet.load(Path(d) / 'config.json')
        except (OSError, ValueError):
            continue  # subfleet.tick marks it failed
        h = state(f, name)
        peek = child_peek(cfg)
        applet_host.mirror(f, name, peek)  # parent hash: applets, applet_crashloop
        if name not in decl and h.get('state') != 'draining':
            begin_drain(f, name, 'removed')
            h = state(f, name)
        if h.get('state') == 'draining':
            limit = float((decl.get(name) or {}).get('drain_s') or roster.DRAIN_S)
            quiet = bool(peek and peek['quiet'])
            down = '' if quiet or peek is None else peek.get('sup_down') or ''
            if quiet or peek is None or down or awake.since(h.get('drain_since') or awake.now()) >= limit:
                why = h.get('drain_reason') or 'external'
                if down:  # the down op is answered ok when the child is collected, with this reason
                    why += '; drain ended at once: the sub-fleet supervisor is ' + down
                    f.log('subfleet.drain_supervisor_down', name=name, supervisor=down)
                elif not quiet and peek is not None:
                    f.log('subfleet.drain_timeout', name=name, drain_s=limit)
                    for rid, op in ops(f, name, 'down'):
                        finish(f, rid, 'timeout', {'reason': 'drain exceeded'})
                stop_child(f, d, rec, cfg, 'down (' + why + ')')
            continue
        if peek is None:
            continue
        if h.get('state') == 'starting' and peek['ready']:
            since = time.strftime('%H:%M', time.localtime(now))
            f.r.hset(HASH + name, mapping={'state': 'paused' if peek['paused'] else 'up', 'up_at': now})
            f.log('subfleet.up', name=name, slot=rec['slot'], redis_socket=cfg['redis_socket'], run_dir=str(d),
                  line=name + ': up via ' + f.label(rec['slot']) + ' since ' + since)
        elif h.get('state') in ('up', 'paused'):
            want = 'paused' if peek['paused'] else 'up'
            if want != h['state']:
                f.r.hset(HASH + name, mapping={'state': want})
                f.log('subfleet.paused' if want == 'paused' else 'subfleet.up', name=name,
                      line=name + ': ' + want)
            entry = decl.get(name)
            if (entry and entry['autostart'] and want == 'up' and peek['sup_idle']
                    and now - float(h.get('usage_at') or 0) >= USAGE_EVERY):
                f.r.hset(HASH + name, mapping={'usage_at': now})
                if over_threshold(f, rec['slot']):
                    begin_drain(f, name, 'rotate')  # down -> rotation gate -> delegated again (autostart)
    for rid in f.r.lrange(OPQ, 0, -1):
        op = f.r.hgetall(OP + rid)
        if not op:
            f.r.lrem(OPQ, 0, rid)
        elif op.get('status') == 'approved':
            (op_up if op['op'] == 'up' else op_down)(f, rid, op, now)
    for name, entry in decl.items():
        if (entry['autostart'] and name not in live(f) and state(f, name).get('crashloop') != '1'
                and not ops(f, name, 'up', approved=True)):
            result = try_up(f, entry)
            if result[0] == 'started':
                f.log('subfleet.autostart', name=name, slot=entry['supervisor_slot'], run=result[1]['run'])
            elif result[1] != state(f, name).get('blocked'):  # each new reason once
                f.r.hset(HASH + name, mapping={'blocked': result[1]})
                f.log('subfleet.autostart_wait', name=name, reason=result[1])


def ops(f, name, kind, approved=False):
    out = []
    for rid in f.r.lrange(OPQ, 0, -1):
        op = f.r.hgetall(OP + rid)
        if op.get('name') == name and op.get('op') == kind and (not approved or op.get('status') == 'approved'):
            out.append((rid, op))
    return out


def op_up(f, rid, op, now):
    name = op['name']
    entry = declared(f).get(name)
    if entry is None:
        return finish(f, rid, 'rejected', {'reason': 'program: ' + name + ' is no longer declared'})
    if name in live(f):
        h = state(f, name)
        if h.get('state') in ('up', 'paused'):
            finish(f, rid, 'ok', address(f, name))
        elif h.get('state') == 'starting' and now - float(h.get('started_at') or now) >= ready_limit(f):
            finish(f, rid, 'timeout', {'reason': 'child not READY'})
        return  # starting / draining: wait for READY or for the down to finish
    result = try_up(f, entry, op.get('reason') or '')
    if result[0] == 'wait' and result[1].startswith(CHAIN_WAIT) and result[1] != state(f, name).get('blocked'):
        f.r.hset(HASH + name, mapping={'blocked': result[1]})  # D28: a chain wait is kept with the sub-fleet
        f.log('subfleet.up_wait', id=rid, name=name, reason=result[1][:500])
    if result[0] == 'refused':
        finish(f, rid, 'rejected', {'reason': 'program: ' + result[1]})
    elif result[0] == 'wait' and now - float(op.get('approved_at') or now) >= SLOT_WAIT:
        # Q4: a wait the supervisor chain caused says why (walls, quota, a rotation that did not finish)
        finish(f, rid, 'timeout', {'reason': 'slot busy' + (': ' + result[1] if result[1].startswith(CHAIN_WAIT)
                                                            else '')})


def op_down(f, rid, op, now):
    name = op['name']
    if name not in live(f):
        return finish(f, rid, 'ok', {'name': name, 'state': 'down'})
    begin_drain(f, name, 'external')  # finished by collected() once the child is back


# ------------------------------------------------------------------ crash / collect
def revive(f, d, rec, cfg):
    """An autostart child's daemon is gone (§29 2.1): restart its Redis + ``daemon --resume`` unless it
    died CRASH_MAX times within CRASH_WINDOW (then it is collected like before and waits for an operator)."""
    fleet = _fleet()
    name = rec['name']
    entry = declared(f).get(name)
    if not entry or not entry['autostart'] or state(f, name).get('state') == 'draining':
        return False
    now = time.time()
    crashes = [t for t in rec.get('crashes') or [] if now - t < CRASH_WINDOW] + [now]
    rec['crashes'] = crashes
    if len(crashes) >= CRASH_MAX:
        rec['outcome'] = 'crashloop'
        subfleet.save_record(d, rec)
        f.r.hset(HASH + name, mapping={'crashloop': 1})
        f.log('subfleet.crashloop', name=name, crashes=len(crashes), window_s=CRASH_WINDOW)
        return False
    try:
        if not subfleet.redis_up(cfg):
            fleet.start_redis(cfg, tries=300)  # reloads its AOF from the same directory
        command = [cfg['python'], str(Path(fleet.__file__).resolve()), 'daemon', str(Path(d) / 'config.json'),
                   '--resume']
        result = fleet.manager_for(cfg).spawn_window('daemon', shlex.join(command), metadata={'type': 'daemon'},
                                                     start_directory=str(d))
        if not getattr(result, 'success', True):
            raise RuntimeError(str(getattr(result, 'error', 'spawn failed')))
    except (Exception, SystemExit) as exc:
        f.log('subfleet.revive_failed', name=name, reason=str(exc))
        subfleet.save_record(d, rec)
        return False
    rec['launched'] = now
    subfleet.save_record(d, rec)
    f.log('subfleet.autostart', name=name, reason='daemon died; relaunched', crashes=len(crashes))
    return True


def collected(f, rec, returned):
    """subfleet.collect took a named child back: the parent hash says down, open downs answer ok."""
    name = rec['name']
    f.r.hset(HASH + name, mapping={'state': 'down', 'redis_socket': '', 'up_at': '', 'drain_since': '',
                                   'down_at': time.time(), 'outcome': rec.get('outcome') or ''})
    applet_host.mirror(f, name, {'applets': {}})  # a down host runs no applet
    for rid, op in ops(f, name, 'down', approved=True):
        finish(f, rid, 'ok', {'name': name, 'state': 'down', 'reason': rec.get('reason') or ''})
    f.log('subfleet.down', name=name, outcome=rec.get('outcome'), returned=returned, line=name + ': down')

