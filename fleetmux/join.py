"""`start` on a machine that already runs a root fleet: attach to it instead of starting a second one.

One machine, one root fleet, one fleetmux server. A `start` that finds a live root fleet of this user starts
nothing: it writes a *join stub* and exits 0. The work then goes in with ``fleetmux task --config <stub> -``,
which submits it to the root as a job (``fleet.py task --config <root config> -``); the root runs each job in
its own task-mode sub-fleet on its own server, listed in its sidebar in the order the jobs started.
``start --no-join`` keeps the old behaviour (a fleet of its own; in the same fleet home the "tabs already
exist" refusal).

A live root (``live_root``): its daemon holds the run's lock, its Redis answers and records no explicit stop. A root
that was just stopped keeps its lock while it winds down; it is no target. ``start`` waits (at most ``STOP_WAIT``
seconds) for a stopping root in its own fleet home to let go of its lock, since its tabs would refuse the start, then
starts its own fleet. A root in this start's own fleet home with the start's own ``--name`` is the fleet being started itself (a restart
with the same arguments): never a target either. When the root stops between discovery and a service attach,
``start`` starts its own fleet too.

Where the stub goes (the same-home rule): ``<fleet home>/latest.json`` while that file holds nothing to lose
(absent, a finished run, or a stub with no job yet). Otherwise -- it names a live run (the root itself lives in
this home, or another live run does), a run that has not finished, or a stub with jobs --
``<fleet home>/joins/<name>.json`` (``<name>-2.json``, ... while that name holds a stub with jobs), so no
record is overwritten. The attach line prints the exact ``--config`` path to use.

A service-mode start attaches as a service sub-fleet of the root service fleet: the root declares it at run
time (op ``subfleet.attach``), runs it on one of its workers (or an added one) and keeps it up until it is
stopped; its stub (``kind: service``) forwards ``request`` / ``results`` / ``result`` / ``status`` to the child,
runs ``logs`` / ``operator`` / ``pause`` / ``resume`` / ``applet`` on the child's own config.json, and ``stop``
downs only the child. A task stub takes ``task`` / ``status`` / ``stop`` / ``result``; every other
command refuses a stub.

A task root takes jobs too, while its own task runs (design §19 "Task root"): a task start attaches to it as to a
service root, and the stub's ``task`` goes in with ``--job`` (never the root's own task; refused when the root runs
no task). The root's own COMPLETED is held until its last job ends. A service sub-fleet needs a service root: with
only a task root live, a service ``start`` runs a fleet of its own, or, when that task root runs in this very fleet
home, refuses (nothing started).

The start options travel with the attach (``start_settings``): those that describe the started fleet go to the
sub-fleet's config (``overrides``; the root's kit, domains and supervisor split are never inherited), those that cannot
apply to a sub-fleet refuse the start (nothing started), and this machine's set-up keeps the root's own values.

The root is reached only through its command line (``task`` / ``status`` / ``stop`` / ``result``) and judged
only by the JSON it prints: a job is accepted when the reply has ``status == "accepted"`` and a ``job_id``.
"""
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from fleetmux import paths
from fleetmux import lifecycle as lifecycle_modes

KEY = 'join'
# Stub options a task spec may lack: each goes into the job spec unless the spec sets it (paths are absolute, made
# so when the stub is written). The job's own sub-fleet takes the name, roster, roles and the start's own fleet
# settings (``overrides``, start_settings) (design §58); a dev_task job's `deliver` travels in the task spec itself,
# as for a root dev_task run.
FORWARD = ('lifecycle', 'budget', 'name', 'roster', 'roles', 'overrides')
CLI_TIMEOUT = 90  # s: the root's `task` waits up to 30 s for its daemon
# Commands a service stub runs in this process on its child's own config.json (fleet.main / applet_cli.main)
CHILD_COMMANDS = ('logs', 'operator', 'pause', 'resume')
SCAN_ENV = 'FLEETMUX_JOIN_ROOT'  # the directory searched for live root fleets (default ~/.fleetmux)
# What a root writes to its Redis when it takes an explicit stop (runtime_v2 `stop`: the service pause reason; a task
# root's task reason; the v1 task reason). From then on it only winds down: no attach target.
STOP_REASONS = ('explicit fleet stop', 'stopped by operator')
PROBE_TIMEOUT = 2  # s: one root's Redis answer during discovery
STOP_WAIT, STOP_POLL = 120, 0.5  # s: how long `start` waits for a stopping fleet in its own home to let go of its lock


# Every `start` option, by what an attach does with it (a test checks that each option of the start parser is in
# exactly one group). Only options the person typed count: a value equal to the parser default is not "given".
START_TAKEN = ('workers', 'worker_names', 'roster', 'kit_fleet', 'roles', 'lifecycle', 'mode', 'name', 'budget')
START_LOCAL = ('attach', 'gui', 'no_view', 'no_join', 'resume', 'no_restore', 'fleet_home', 'backend', 'extend_hours',
               'transport_version', 'allow_development_v2')  # about this command, not the started fleet
START_MACHINE = ('codex', 'pi', 'redis_server', 'redis_runtime_dir', 'cmux_bin', 'cmux_state_dir', 'codex_home',
                 'pi_agent_dir', 'host', 'security', 'no_security', 'prompt_table', 'chrome_script',
                 'chrome_min_interval', 'runs_dir', 'allow_unsandboxed_pi')  # this machine's set-up: the root's own value is used (a note says so)
# describe the started fleet: forwarded to the sub-fleet's config (subfleet.OVERRIDES); the service entry keys
# (claude / worker_model / worker_cli / pi_*) go through the roster sub-fleet validator instead
START_FORWARD = ('supervisor_split', 'request_timeout', 'max_busy', 'max_attempts', 'worker_idle_reclaim_min',
                 'stall_timeout', 'stall_window', 'stall_cpu_pct', 'stall_grace', 'stall_escapes', 'handoff_ratio',
                 'handoff_timeout', 'window', 'claude_window', 'clear_timeout', 'lease_reclaim', 'worker_model',
                 'claude', 'worker_kit', 'worker_domains', 'brake_notify', 'allow_profile_live_switch', 'worker_cli',
                 'pi_model', 'pi_thinking', 'supervisor_model', 'supervisor_cli')
# Never inherited from the root unless this start gave them: a sub-fleet runs no kit / domains / supervisor split
# of the root's. Sent with every attach as their start defaults.
START_RESET = {'worker_kit': None, 'worker_domains': [], 'supervisor_split': False}
SUB_SUPERVISOR = ('the supervisor of a sub-fleet is a worker of the root fleet that runs the official Claude Code '
                  'CLI (a root worker taken over, or one the root adds)')
START_REFUSED = {
    'profile': 'Codex profiles: a sub-fleet runs no Codex agent (' + SUB_SUPERVISOR + ')',
    'worker_profile': 'a sub-fleet runs no Codex worker; its workers run claude or pi',
    'supervisor_profile': SUB_SUPERVISOR + '; pick its model with --supervisor-model',
    'supervisor_pi_model': SUB_SUPERVISOR + '; pick its model with --supervisor-model',
    'supervisor_pi_thinking': SUB_SUPERVISOR,
    'compact_limit': 'a sub-fleet runs no Codex agent',
    'opencode': 'a sub-fleet runs claude or pi workers only', 'opencode_model': 'a sub-fleet runs claude or pi workers only',
    'opencode_window': 'a sub-fleet runs claude or pi workers only', 'dsh': 'a sub-fleet runs claude or pi workers only',
    'dsh_home': 'a sub-fleet runs claude or pi workers only', 'dsh_model': 'a sub-fleet runs claude or pi workers only',
    'advisor': 'the advisor of a sub-fleet is switched in its own roster (advisor: on)',
    'night_roster': 'a sub-fleet has no night roster of its own',
    'channel_user_resume': 'channels belong to the root fleet',
    'initiator': 'a sub-fleet is closed by its root fleet; only the root notifies an initiator',
    'project_dir': 'a sub-fleet runs under the root\'s run directory (<root run>/sub/<name>), not in a project',
    'clear_after_result': 'retired; use dispatch "fresh"', 'claude_hook_delivery': 'retired',
}


class JoinError(Exception):
    """The root cannot take or report the job; the message is for the person."""


def _fleet():
    from fleetmux import fleet
    return fleet


# ------------------------------------------------------------------ discovery
def scan_root():
    """The directory whose fleet homes are searched: $FLEETMUX_JOIN_ROOT, else ``~/.fleetmux``."""
    import os
    value = os.environ.get(SCAN_ENV)
    return Path(value).expanduser() if value else paths.root()


def search_homes(current):
    """The default fleet home, every ``fleets/<name>/`` under it, and ``current`` (this start's home)."""
    root = scan_root()
    found = [root]
    fleets = root / 'fleets'
    if fleets.is_dir():
        found += sorted(p for p in fleets.iterdir() if p.is_dir())
    found.append(Path(current))
    out, seen = [], set()
    for home in found:
        key = _resolved(home)
        if key not in seen:
            seen.add(key)
            out.append(home)
    return out


def _resolved(path):
    return str(Path(path).expanduser().resolve())


def is_stub(data):
    return isinstance(data, dict) and isinstance(data.get(KEY), dict)


def live_run(home):
    """``(latest.json data, run dir)`` when ``home``'s latest.json names a run whose daemon is alive, else None."""
    fleet = _fleet()
    try:
        latest = fleet.load(Path(home) / 'latest.json')
    except (OSError, ValueError):
        return None
    if not isinstance(latest, dict) or is_stub(latest) or not latest.get('run'):
        return None
    run = Path(latest['run'])
    if not (run / 'daemon.lock').is_file():  # daemon_running would create it
        return None
    try:
        alive = fleet.daemon_running(run)
    except OSError:
        return None
    return (latest, run) if alive else None


def _stopping(r):
    """True when the root's Redis records an explicit stop (STOP_REASONS)."""
    fleet = _fleet()
    service = str(r.hget(fleet.SERVICE, 'reason') or '')
    status, reason = r.hmget(fleet.TASK, 'status', 'reason')
    return (service.startswith(tuple('cancelled: ' + s for s in STOP_REASONS))
            or (status == 'cancelled' and str(reason or '').startswith(STOP_REASONS)))


def root_answers(config):
    """True while the root's private Redis answers and the root has not taken an explicit stop. A stopped root
    keeps its daemon lock while it winds down (retire, Redis shutdown, tmux): the lock alone is no live root."""
    fleet = _fleet()
    try:
        r = fleet.connect(fleet.load(config), socket_timeout=PROBE_TIMEOUT)
    except Exception:  # unreadable config, a socket path this release refuses, no answer: not a root to attach to
        return False
    try:
        return not _stopping(r)
    except Exception:
        return False
    finally:
        r.close()
        r.connection_pool.disconnect()


def _root_run(home):
    """``(latest.json data, run dir, config path)`` of the root-fleet run with a live daemon ``home`` names, else None
    (a sub-fleet or a run on another backend is no root)."""
    found = live_run(home)
    if found is None:
        return None
    latest, run = found
    if latest.get('parent_config') or _fleet().run_backend(latest) != 'fleetmux':
        return None
    return latest, run, str(latest.get('config') or run / 'config.json')


def live_root(home):
    """The live root fleet ``home`` names, as a dict, or None: a sub-fleet or a run on another backend is no root,
    nor is a run whose Redis does not answer or that is stopping (root_answers)."""
    found = _root_run(home)
    if found is None:
        return None
    latest, run, config = found
    if not root_answers(config):
        return None
    return {'config': config, 'home': _resolved(home), 'mode': latest.get('mode') or 'task', 'run': str(run),
            'name': latest.get('name')}


def _pause():
    time.sleep(STOP_POLL)


def wait_stopping(home):
    """A root fleet in this start's own fleet home that keeps its lock but no longer answers (it is stopping) still has
    its tabs up, and the start that follows would be refused. Wait for it to let go, at most STOP_WAIT s."""
    found = _root_run(home)
    if found is None or root_answers(found[2]):
        return
    sys.stderr.write('start: the fleet ' + str(found[1]) + ' in this fleet home is stopping; waiting up to '
                     + str(STOP_WAIT) + ' s for it to finish\n')
    deadline = time.monotonic() + STOP_WAIT
    while live_run(home) is not None:
        if time.monotonic() >= deadline:
            sys.stderr.write('start: it is still stopping after ' + str(STOP_WAIT) + ' s\n')
            return
        _pause()


def find_root(current, homes=None, name=None):
    """The root fleet to attach to, or None. A service root first, then one in ``current`` (this start's fleet
    home), then the earliest started (run directories are named fleet-<date>-<time>-<token>). A root in
    ``current`` named ``name`` is the fleet being started itself (a restart with the same arguments): never a target."""
    here = _resolved(current)
    roots = [r for r in (live_root(h) for h in (search_homes(current) if homes is None else homes))
             if r and not (name and r['home'] == here and r.get('name') == name)]
    if not roots:
        return None
    return min(roots, key=lambda r: (r['mode'] != 'service', r['home'] != here, Path(r['run']).name, r['config']))


# ------------------------------------------------------------------ start -> attach
def _holds(data):
    """True when the stub ``data`` holds something: jobs, or an attached service sub-fleet."""
    return is_stub(data) and bool(data[KEY].get('jobs') or data[KEY].get('child'))


def _stub_jobs(path):
    try:
        data = _fleet().load(path)
    except (OSError, ValueError):
        return False
    return _holds(data)


def _occupied(home):
    """True when ``home``'s latest.json holds something a stub must not replace."""
    latest = Path(home) / 'latest.json'
    try:
        data = _fleet().load(latest)
    except (OSError, ValueError):
        return False
    if is_stub(data):
        return _holds(data)
    run = Path(data['run']) if isinstance(data, dict) and data.get('run') else None
    if run is None or not run.is_dir():
        return False
    if _fleet().open_job_rows(run):  # section 58 "Restart": stopped with open jobs, continued by the next start
        return True
    return live_run(home) is not None or not (run / 'result.json').exists()


def own_stub(home, name, root_home):
    """section 60: an existing service stub of this home for child ``name`` of the root in ``root_home`` (a start
    run again after the root restored the child), else None."""
    fleet = _fleet()
    home = Path(home)
    for path in [home / 'latest.json'] + sorted((home / 'joins').glob('*.json') if (home / 'joins').is_dir() else []):
        try:
            stub = fleet.load(path).get(KEY)
        except (OSError, ValueError, AttributeError):
            continue
        if (isinstance(stub, dict) and stub.get('kind') == 'service' and stub.get('child') == name
                and str(stub.get('root_home')) == str(root_home)):
            return path
    return None


def stub_path(home, name):
    """``<home>/latest.json``, or ``<home>/joins/<name>[-n].json`` when latest.json is occupied (module doc)."""
    home = Path(home)
    if not _occupied(home):
        return home / 'latest.json'
    slug = ''.join(c if c.isalnum() or c in '._-' else '-' for c in name).strip('.-') or 'join'
    path, n = home / 'joins' / (slug + '.json'), 2
    while _stub_jobs(path):
        path, n = home / 'joins' / (slug + '-' + str(n) + '.json'), n + 1
    return path


def stub_options(args):
    """The start options a job needs, paths made absolute. SystemExit on a missing roster / roles / bad budget."""
    fleet = _fleet()
    roster = getattr(args, 'roster', None)
    if getattr(args, 'kit_fleet', None):
        roster = str(fleet.kit_fleet_roster(args.worker_kit, args.kit_fleet))
    if roster:
        roster = Path(roster).expanduser().resolve()
        if not roster.is_file():
            raise SystemExit('--roster ' + str(roster) + ': no such file')
    roles = getattr(args, 'roles', None)
    if roles:
        roles = Path(roles).expanduser().resolve()
        if not roles.is_dir():
            raise SystemExit('--roles must be a directory of <agent-name>.md files')
    budget = None
    if getattr(args, 'budget', None):
        try:
            budget = json.loads(args.budget)
        except ValueError as exc:
            raise SystemExit('--budget rejected: ' + str(exc)) from exc
    name = (getattr(args, 'name', None) or '').strip() or None
    return {'roster': roster and str(roster), 'roles': roles and str(roles), 'lifecycle': getattr(args, 'lifecycle', None),
            'mode': getattr(args, 'mode', None), 'name': name, 'budget': budget}


def start_defaults():
    """{dest: default} of every option of the `start` parser."""
    import argparse
    parser = _fleet().cli()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices['start']
    return {a.dest: a.default for a in sub._actions if a.dest != 'help'}


def given(args):
    """{dest: value} of the start options this command typed (a value other than the parser default; fleet.main
    fills --runs-dir / --codex-home under the fleet home before start runs, which is not typing them)."""
    fleet = _fleet()
    filled = {'runs_dir': str(fleet.HOME / 'runs'), 'codex_home': str(fleet.HOME / 'codex-home')}
    return {k: getattr(args, k) for k, d in start_defaults().items()
            if hasattr(args, k) and getattr(args, k) not in (d, None, filled.get(k))}


def start_settings(args, service):
    """(overrides, supervisor_model, machine-level option names) for the sub-fleet this attaching start becomes.
    SystemExit naming every option that cannot apply to a sub-fleet (nothing is started then)."""
    typed = given(args)
    refused = {k: START_REFUSED[k] for k in typed if k in START_REFUSED}
    cli = typed.get('worker_cli')
    if cli and cli not in ('claude', 'pi'):
        refused['worker_cli'] = 'a sub-fleet runs claude or pi workers only, not ' + cli
    elif not service and (cli == 'pi' or typed.get('pi_model') or typed.get('pi_thinking')):
        refused['worker_cli'] = 'a job sub-fleet runs claude workers; start a service fleet for pi workers'
    elif not cli and (typed.get('pi_model') or typed.get('pi_thinking')):
        refused['pi_model'] = '--pi-model / --pi-thinking need --worker-cli pi'
    if typed.get('supervisor_cli') not in (None, 'claude'):
        refused['supervisor_cli'] = SUB_SUPERVISOR
    if typed.get('supervisor_model') and not service:
        refused['supervisor_model'] = (SUB_SUPERVISOR + '; for a job put "supervisor_profile": "<agent card>" in the '
                                       'task spec')
    if typed.get('supervisor_split') and not service:
        refused['supervisor_split'] = 'only a service fleet splits requests into sub-fleets'
    if refused:
        raise SystemExit('start: a fleet that attaches to the running root becomes its sub-fleet, and these options '
                         'cannot apply to a sub-fleet: ' + '; '.join('--' + k.replace('_', '-') + ': ' + v
                                                                     for k, v in sorted(refused.items()))
                         + '. Nothing was started. Drop them, or pass --no-join to start a fleet of its own')
    overrides = dict(START_RESET)
    for key in START_FORWARD:
        if key not in typed or key in ('supervisor_model', 'supervisor_cli', 'worker_cli', 'pi_model', 'pi_thinking'):
            continue
        if service and key in ('worker_model', 'claude'):
            continue  # the service entry takes these (attach_spec)
        value = typed[key]
        if key == 'claude' and '/' in value:
            value = str(Path(value).expanduser().resolve())
        elif key == 'worker_kit':
            value = str(Path(value).expanduser().resolve())
            if not Path(value).is_dir():
                raise SystemExit('--worker-kit ' + value + ': no such directory')
        elif key == 'worker_domains':
            value = _fleet().parse_worker_domains(value, typed.get('worker_kit'))
        elif key == 'brake_notify':
            import shlex
            value = shlex.split(value)
        elif key == 'worker_idle_reclaim_min' and value == 'off':
            value = None
        overrides[key] = value
    machine = sorted(k for k in typed if k in START_MACHINE)
    return overrides, typed.get('supervisor_model'), machine


def machine_note(machine):
    if machine:
        sys.stderr.write('start: attaching to the running root: ' + ', '.join('--' + k.replace('_', '-')
                                                                         for k in machine)
                         + ' describe this machine\'s set-up; the root fleet\'s own values are used\n')


def attach(args):
    """`start` hook, before anything is provisioned: True when a live root was found and a stub written."""
    fleet = _fleet()
    backend = getattr(args, 'backend', None)
    if backend and backend != 'fleetmux':  # an explicit other backend cannot share the root's server
        return False
    home = fleet.LATEST.parent
    root = find_root(home, name=(getattr(args, 'name', None) or '').strip() or None)
    if root is None:
        wait_stopping(home)
        return False
    try:
        mode = lifecycle_modes.resolve_start(getattr(args, 'lifecycle', None), getattr(args, 'mode', None))[1]
    except ValueError:
        mode = getattr(args, 'mode', None)
    if mode == 'service':
        if root['mode'] != 'service':
            return task_root(root, home)
        return attach_service(args, root, home)
    overrides, _, machine = start_settings(args, service=False)
    options = dict(stub_options(args), overrides=overrides)
    machine_note(machine)
    label = options['name'] or 'join-' + time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4]
    path = stub_path(home, label)
    fleet.save(path, {KEY: dict(options, root_config=root['config'], root_home=root['home'],
                                root_mode=root['mode'], label=label, created=time.time(), jobs=[])})
    where = '' if path == fleet.LATEST else ' --config ' + str(path)
    print('attached to the running ' + root['mode'] + ' fleet ' + root['config'] + ' (no new fleet started); '
          'submit the work with: fleetmux task' + where + ' -')
    return True


def task_root(root, home):
    """A service start while the only live root runs in task mode. A task root takes jobs (task sub-fleets), not
    service sub-fleets: this start runs a service fleet of its own (False), unless the task fleet runs in this very
    fleet home, which holds one running fleet (SystemExit, nothing started)."""
    if root['home'] == _resolved(home):
        raise SystemExit('start: the task fleet ' + root['config'] + ' runs in this fleet home; a task fleet takes '
                         'jobs, not service sub-fleets, and a fleet home runs one fleet. Nothing was started. Stop it '
                         '(`fleetmux stop --config ' + root['config'] + '`; a finished task fleet keeps running until '
                         'stopped), start this fleet in another fleet home (FLEETMUX_HOME) or with --no-join there, '
                         'or run the root fleet as a service fleet (--lifecycle service) so later starts attach to it')
    sys.stderr.write('start: the running fleet ' + root['config'] + ' is a task fleet; it takes jobs, not service '
                     'sub-fleets, so this service fleet starts on its own\n')
    return False


def child_name(text):
    """A sub-fleet name (roster NAME_RE) from ``--name``: lower case, other characters as '-'; 'svc-<hex>' without."""
    import re
    from fleetmux import roster
    slug = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    if slug and not slug[0].isalpha():
        slug = 'svc-' + slug
    slug = slug[:31].rstrip('-')
    return slug if slug and re.fullmatch(roster.NAME_RE, slug) else 'svc-' + uuid.uuid4().hex[:6]


def attach_spec(args, options, name, overrides=None, supervisor_model=None):
    """The ``subfleet.attach`` op for the root: the child's name, roster or workers, roles, worker settings, the
    start's other fleet settings (``overrides``) and the supervisor model it asked for."""
    spec = {'id': 'attach-' + uuid.uuid4().hex[:12], 'op': 'subfleet.attach', 'name': name,
            'reason': 'fleetmux start ' + (options['name'] or name)}
    if overrides:
        spec['overrides'] = overrides
    if supervisor_model:
        spec['supervisor_model'] = supervisor_model
    if options['roster']:
        spec['roster'] = options['roster']
    elif getattr(args, 'worker_names', None):
        spec['worker_names'] = [w.strip() for w in args.worker_names.split(',') if w.strip()]
    elif getattr(args, 'workers', None):
        spec['workers'] = args.workers
    if options['roles']:
        spec['roles'] = options['roles']
    entry = {}
    if getattr(args, 'worker_model', None):
        entry['worker_model'] = args.worker_model
    if getattr(args, 'claude', None):
        entry['claude'] = str(Path(args.claude).expanduser().resolve()) if '/' in args.claude else args.claude
    if getattr(args, 'worker_cli', None) == 'pi':
        entry['worker_cli'] = 'pi'
        for key in ('pi_model', 'pi_thinking'):
            if getattr(args, key, None):
                entry[key] = getattr(args, key)
    if entry:
        spec['entry'] = entry
    return spec


def _gone(root):
    """True when ``root`` stopped between discovery and the attach: ``start`` then starts its own fleet."""
    now = live_root(root['home'])
    if now is not None and now['config'] == root['config']:
        return False
    sys.stderr.write('start: the service fleet ' + root['config'] + ' stopped before it took this fleet; starting '
                     'a fleet of its own\n')
    return True


def attach_service(args, root, home):
    """A service start with a live root service fleet: the root declares it as a service sub-fleet (op
    ``subfleet.attach``), runs it on one of its workers and keeps it up until it is stopped. The stub forwards
    ``request`` / ``results`` / ``result`` / ``status`` / ``stop`` to that child. A refusal starts nothing."""
    fleet = _fleet()
    overrides, supervisor_model, machine = start_settings(args, service=True)
    options = stub_options(args)
    name = child_name(options['name'])
    spec = attach_spec(args, options, name, overrides, supervisor_model)
    machine_note(machine)
    try:
        _root({'root_config': root['config']})
        code, out, err = fleet_cli(['request', '--config', root['config'], '-'], stdin=json.dumps(spec, ensure_ascii=False))
    except JoinError as exc:
        if _gone(root):
            wait_stopping(home)
            return False
        raise SystemExit('start: ' + str(exc) + '. Nothing was started') from exc
    answer = parse_json(out)
    if answer.get('status') != 'accepted' or not answer.get('id'):
        if _gone(root):
            wait_stopping(home)
            return False
        reason = str(answer.get('reason') or err.strip() or out.strip() or 'exit code ' + str(code))
        if 'unknown keys for subfleet.attach' in reason:
            reason += ' (the root runs an older fleetmux release; restart it on this release)'
        raise SystemExit('start: the running service fleet ' + root['config'] + ' did not take ' + name + ' as a '
                         'service sub-fleet: ' + reason + '. Nothing was started. Pass --no-join to start this '
                         'service fleet on its own')
    path = own_stub(home, name, root['home']) or stub_path(home, name)  # section 60: the same start again, one stub
    child = child_config_path({'root_config': root['config'], 'child': name})
    fleet.save(path, {KEY: dict(options, kind='service', child=name, attach_id=answer['id'], child_config=str(child),
                                root_config=root['config'], root_home=root['home'], root_mode=root['mode'],
                                label=name, created=time.time(), jobs=[])})
    where = '' if path == fleet.LATEST else ' --config ' + str(path)
    print('attached to the running service fleet ' + root['config'] + ' as its service sub-fleet ' + name
          + ' (no new fleet started; it comes up on a worker of that fleet and runs until stopped); send requests '
          'with: fleetmux request' + where + ' -; its address: fleetmux result' + where + '; its own config '
          '(exists once it is up; for commands the stub does not forward): ' + str(child))
    return True


# ------------------------------------------------------------------ the root's command line (the adapter)
def fleet_cli(argv, stdin=None, timeout=CLI_TIMEOUT):
    """Run ``fleet.py <argv>`` in a child process: (exit code, stdout, stderr)."""
    command = [sys.executable, str(Path(_fleet().__file__).resolve())] + list(argv)
    try:
        done = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise JoinError('cannot run ' + ' '.join(argv[:1]) + ' on the root: ' + str(exc)) from exc
    return done.returncode, done.stdout, done.stderr


def parse_json(text):
    """The whole output as JSON, else its last line that is a JSON object, else {}."""
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _root(stub):
    fleet = _fleet()
    config = stub.get('root_config')
    try:
        cfg = fleet.load(config)
    except (OSError, ValueError, TypeError) as exc:
        raise JoinError('the root fleet config ' + str(config) + ' is unreadable: ' + str(exc)) from exc
    run = Path(cfg.get('run') or Path(config).parent)
    if not (run / 'daemon.lock').is_file() or not fleet.daemon_running(run):
        raise JoinError('the root fleet ' + str(config) + ' is no longer running; run `fleetmux start` again')
    return cfg


def _root_live(stub):
    """True while the daemon of the stub's root run holds its lock."""
    fleet = _fleet()
    try:
        cfg = fleet.load(stub.get('root_config'))
    except (OSError, ValueError, TypeError):
        return False
    run = Path(cfg.get('run') or Path(stub['root_config']).parent)
    return (run / 'daemon.lock').is_file() and fleet.daemon_running(run)


def follow_home(path, data):
    """section 58 "Restart": the stub's root run is down but another root runs in the same fleet home (``root_home``): the
    stub follows it (root_config rewritten, the old one kept in ``previous_roots``), so task / status / stop / result
    keep finding their jobs. Returns the (possibly updated) stub."""
    stub = data[KEY]
    home = stub.get('root_home')
    if not home or _root_live(stub):
        return stub
    root = live_root(home)
    if root is None or _resolved(root['config']) == _resolved(stub.get('root_config') or '/'):
        return stub
    sys.stderr.write('the root fleet of ' + str(home) + ' now runs ' + root['config'] + '; ' + str(path)
                     + ' follows it\n')
    stub['previous_roots'] = list(stub.get('previous_roots') or []) + [stub.get('root_config')]
    stub.update(root_config=root['config'], root_mode=root['mode'])
    _fleet().save(path, data)
    return stub


def mirrored_result(stub, job):
    """section 58: the result a root kept on disk for ``job`` (<run>/job-results/<job id>.json), in the stub's root run or a
    root run it followed before; None when there is none."""
    fleet = _fleet()
    for config in [stub.get('root_config')] + list(reversed(stub.get('previous_roots') or [])):
        try:
            cfg = fleet.load(config)
            run = Path(cfg.get('run') or Path(config).parent)
            return fleet.load(run / fleet.JOB_RESULTS / (job + '.json'))
        except (OSError, ValueError, TypeError):
            continue
    return None


def submit(root_config, spec):
    """Submit ``spec`` to the root as a job; return its job id. JoinError when the root does not accept it."""
    _root({'root_config': root_config})
    # --job: a task root takes it as a job beside its own task, never as its own task (§19 "Task root")
    code, out, err = fleet_cli(['task', '--config', str(root_config), '--job', '-'],
                               stdin=json.dumps(spec, ensure_ascii=False))
    answer = parse_json(out)
    if answer.get('status') == 'accepted' and answer.get('job_id'):
        return answer['job_id']
    if answer.get('status') == 'pending':
        raise JoinError('the root did not answer in time; the job may or may not be queued. It was not submitted '
                        'again: check `fleetmux status --config ' + str(root_config) + '`')
    reason = str(answer.get('reason') or err.strip() or out.strip() or 'exit code ' + str(code))
    if 'submit requests, not tasks' in reason:
        reason += ' (this fleetmux version cannot run jobs on a service fleet)'
    elif 'a job spec takes only' in reason and 'overrides' in reason.split('(not', 1)[-1]:
        reason += ' (the root runs an older fleetmux release; restart it on this release)'
    raise JoinError('the root refused the job: ' + reason)


def merged_spec(stub, spec):
    """The task spec, with the stub's start options for the keys it does not set (the spec wins)."""
    out = dict(spec)
    for name in FORWARD:
        if stub.get(name) is not None and name not in out:
            out[name] = stub[name]
    return out


def job_view(stub, job):
    """(row of ``job`` in the root's status, its sub-fleet tree entry or None); row None when no longer open."""
    code, out, err = fleet_cli(['status', '--config', str(stub['root_config'])])
    status = parse_json(out)
    if code != 0 or not status:
        raise JoinError('status of the root failed: ' + (err.strip() or out.strip() or 'exit code ' + str(code)))
    row = next((r for r in status.get('jobs') or [] if isinstance(r, dict) and r.get('id') == job), None)
    item = None
    if row and row.get('subfleet'):
        tree = status.get('subfleets') or {}
        from fleetmux import subfleet
        found = [v for k, v in tree.items() if k == row['subfleet'] or k.startswith(row['subfleet'] + ' (')]
        live = [v for v in found if isinstance(v, dict) and v.get('state') in subfleet.ACTIVE]
        item = (live or found or [None])[-1]  # tree order is creation order: the newest attempt last
    return row, item


def root_result(stub, job, wait=0):
    """(exit code, parsed result) of ``result <job>`` on the root: 0 found, 3 pending, 2 unknown."""
    code, out, err = fleet_cli(['result', job, '--config', str(stub['root_config']), '--wait', str(wait)],
                               timeout=CLI_TIMEOUT + wait)
    return code, parse_json(out) or {'output': (out or err).strip()}


def cli(args, path, data):
    """``task`` / ``status`` / ``stop`` / ``result`` with ``--config <join stub>``: the exit code. A service stub
    (``kind: service``) takes ``request`` / ``results`` / ``result`` / ``status`` / ``stop`` (service_cli)."""
    stub = data[KEY]
    if stub.get('kind') == 'service':
        try:
            return service_cli(args, path, stub)
        except JoinError as exc:
            sys.stderr.write('fleetmux ' + args.cmd + ': ' + str(exc) + '\n')
            return 2
    job = (stub.get('jobs') or [None])[-1]
    try:
        stub = follow_home(path, data)
        if args.cmd == 'task' and getattr(args, 'recheck', None):  # the gate of a job: rechecked on the root
            _root(stub)
            return _passthrough(*fleet_cli(['task', '--recheck', args.recheck, '--config', str(stub['root_config'])],
                                           timeout=None))  # the gate runs the test command: no CLI cap
        if args.cmd == 'task':
            if not args.spec:
                raise JoinError('task needs a spec (JSON file or -), or --recheck ID')
            spec = json.loads(sys.stdin.read() if args.spec == '-' else Path(args.spec).read_text())
            if not isinstance(spec, dict):
                raise JoinError('the task spec must be a JSON object')
            job = submit(stub['root_config'], merged_spec(stub, spec))
            stub['jobs'] = list(stub.get('jobs') or []) + [job]
            _fleet().save(path, data)
            print(json.dumps({'status': 'accepted', 'job_id': job, 'root_config': stub['root_config'],
                              'result': 'fleetmux result --config ' + str(path)}, ensure_ascii=False))
            return 0
        if args.cmd not in ('status', 'stop', 'result'):
            raise JoinError(str(path) + ' is a join stub (attached to ' + str(stub.get('root_config')) + '); only '
                            'task / status / stop / result work on it. For the root itself use --config '
                            + str(stub.get('root_config')))
        if args.cmd == 'result':
            target = getattr(args, 'id', None) or job
            if not target:
                raise JoinError('no job submitted through ' + str(path) + ' yet')
            disk = mirrored_result(stub, target)
            if disk is not None and not _root_live(stub):  # the root is down: the result it kept on disk
                print(json.dumps(disk, ensure_ascii=False))
                return 0
            code, out = root_result(stub, target, getattr(args, 'wait', 0) or 0)
            if code == 2 and disk is not None:  # expired from the root's Redis (RESULT_TTL)
                out, code = disk, 0
            print(json.dumps(out, ensure_ascii=False))
            return code
        if not job:
            if args.cmd == 'stop':
                raise JoinError('no job submitted through ' + str(path) + ' yet; nothing to stop (the root is '
                                'never stopped from a join stub)')
            print(json.dumps({'join': str(path), 'root_config': stub.get('root_config'), 'job': None,
                              'state': 'attached, no job submitted'}, ensure_ascii=False, indent=2))
            return 0
        if args.cmd == 'stop' and not _root_live(stub):
            return cancel_while_down(stub, job)
        _root(stub)
        row, item = job_view(stub, job)
        if row is None:  # no longer open on the root: it ended (or the root never knew it)
            code, out = root_result(stub, job)
            if args.cmd == 'stop' and code == 0:
                print(json.dumps({'job': job, 'state': 'ended', 'stopped': False}, ensure_ascii=False))
                return 0
            if args.cmd == 'stop':
                raise JoinError('the root has no open job ' + job + ' and no result for it')
            print(json.dumps({'join': str(path), 'root_config': stub['root_config'], 'job': job, 'state': 'ended',
                              'result': out}, ensure_ascii=False, indent=2))
            return 0 if code == 0 else 2
        if args.cmd == 'status':
            print(json.dumps({'join': str(path), 'root_config': stub['root_config'], 'job': job,
                              'state': row.get('state'), 'row': row, 'subfleet': item}, ensure_ascii=False, indent=2))
            return 0
        return stop_job(stub, job, row, item)
    except JoinError as exc:
        sys.stderr.write('fleetmux ' + args.cmd + ': ' + str(exc) + '\n')
        return 2


def cancel_while_down(stub, job):
    """section 58 "Restart": the root is not running: the cancel is written to <root run>/job-cancels/<job id>.json and
    applied (before anything is resumed) when the root starts again."""
    fleet = _fleet()
    try:
        cfg = fleet.load(stub['root_config'])
    except (OSError, ValueError, TypeError) as exc:
        raise JoinError('the root fleet config ' + str(stub.get('root_config')) + ' is unreadable: ' + str(exc)) from exc
    run = Path(cfg.get('run') or Path(stub['root_config']).parent)
    note = run / fleet.JOB_CANCELS / (job + '.json')
    fleet.save(note, {'job': job, 'by': 'operator', 'at': time.time()})
    print(json.dumps({'job': job, 'state': 'cancel recorded', 'note': 'the root fleet is not running; the cancel is '
                      'applied when the root starts again (' + str(note) + ')'}, ensure_ascii=False))
    return 0


def stop_job(stub, job, row, item):
    """Cancel ``job`` on the root (`task --cancel`: a queued / starting / suspended job ends at once with a result;
    the cancel of a running one is recorded first), then, for a running one, stop its sub-fleet, never the root:
    ``stop --config <that sub-fleet's config>``."""
    fleet = _fleet()
    code, out, err = fleet_cli(['task', '--cancel', job, '--config', str(stub['root_config'])])
    answer = parse_json(out)
    if answer.get('status') in ('cancelled', 'ended', 'stopping'):
        print(json.dumps(answer, ensure_ascii=False))
        return 0
    if not item or not item.get('run'):
        raise JoinError('job ' + job + ' is ' + str(row.get('state') or 'queued') + ' and has no sub-fleet yet, and '
                        'the root did not cancel it: ' + str(answer.get('reason') or err.strip() or out.strip()
                                                                 or 'exit code ' + str(code)))
    child_config = Path(item['run']) / 'config.json'
    try:
        parent = fleet.load(child_config).get('parent_config')
    except (OSError, ValueError) as exc:
        raise JoinError('the sub-fleet config ' + str(child_config) + ' is unreadable: ' + str(exc)) from exc
    if not parent or _resolved(parent) != _resolved(stub['root_config']):
        raise JoinError(str(child_config) + ' is not a sub-fleet of ' + str(stub['root_config']) + '; not stopped')
    code, out, err = fleet_cli(['stop', '--config', str(child_config)])
    sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    return code


# ------------------------------------------------------------------ a service stub: the attached sub-fleet
def child_config_path(stub):
    """The attached sub-fleet's config path (a named sub-fleet always runs in <root run>/sub/<name>/)."""
    cfg = _root(stub)
    return Path(cfg.get('run') or Path(stub['root_config']).parent) / 'sub' / stub['child'] / 'config.json'


def _passthrough(code, out, err):
    sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    return code


def redirect(args, path, data):
    """The child's config.json when ``args.cmd`` is one a service stub runs on its sub-fleet's own config
    (CHILD_COMMANDS), else None. JoinError while the child is not up."""
    stub = data[KEY]
    if stub.get('kind') != 'service' or args.cmd not in CHILD_COMMANDS:
        return None
    return live_child(stub, path)


def applet_config(path, data):
    """``fleetmux applet ... --config <stub>``: the attached child's config.json (the applet host). Not checked for
    a live daemon: some applet commands read only files. JoinError on a task stub."""
    stub = data[KEY]
    if stub.get('kind') != 'service':
        raise JoinError('this is a join stub for jobs; applets run on a service fleet. Use --config with the host '
                        'fleet\'s config.json (stub: ' + str(path) + ')')
    return child_config_path(stub)


def live_child(stub, path):
    """The child's config path while its daemon runs, else JoinError naming its state."""
    config = child_config_path(stub)
    if not config.is_file() or not _fleet().daemon_running(config.parent):
        raise JoinError('sub-fleet ' + stub['child'] + ' is not up (yet); see `fleetmux status --config ' + str(path)
                        + '` and `fleetmux result --config ' + str(path) + '`')
    return config


def service_cli(args, path, stub):
    name = stub['child']
    if args.cmd == 'task':
        raise JoinError(str(path) + ' is the service sub-fleet ' + name + '; send it requests with `fleetmux request '
                        '--config ' + str(path) + ' -`')
    if args.cmd == 'request':
        text = sys.stdin.read() if args.spec == '-' else Path(args.spec).read_text()
        return _passthrough(*fleet_cli(['request', '--config', str(live_child(stub, path)), '-'], stdin=text))
    if args.cmd == 'results':
        argv = ['results', '--config', str(live_child(stub, path))] + (['--drain'] if args.drain else [])
        return _passthrough(*fleet_cli(argv))
    if args.cmd == 'result':
        wait = getattr(args, 'wait', 0) or 0
        if getattr(args, 'id', None):  # a request answered by the child
            argv = ['result', args.id, '--config', str(live_child(stub, path)), '--wait', str(wait)]
            return _passthrough(*fleet_cli(argv, timeout=CLI_TIMEOUT + wait))
        code, out = root_result(stub, stub['attach_id'], wait)  # the attach itself: the child's address
        print(json.dumps(out, ensure_ascii=False))
        return code
    if args.cmd == 'stop':  # a program down through the root: drain, then down; never the root itself
        try:
            _root(stub)
        except JoinError:
            if not stub.get('root_home'):
                raise
            # section 60: the root is down; the cancel is written where its next start reads it (never brought back)
            from fleetmux import restart_state
            restart_state.write_cancel({'fleet_home': stub['root_home']}, name, how='stop while the root was down')
            print(json.dumps({'child': name, 'cancelled': True, 'root': 'down', 'note': 'the next start of the root '
                              'does not bring it back'}, ensure_ascii=False))
            return 0
        spec = {'id': 'detach-' + uuid.uuid4().hex[:12], 'op': 'subfleet.detach', 'name': name}
        code, out, err = fleet_cli(['request', '--config', str(stub['root_config']), '-'], stdin=json.dumps(spec))
        answer = parse_json(out)
        if answer.get('status') != 'accepted':
            raise JoinError('the root did not take ' + name + ' down: '
                            + str(answer.get('reason') or err.strip() or out.strip() or 'exit code ' + str(code)))
        print(json.dumps({'child': name, 'stopping': True, 'id': answer.get('id'),
                          'result': 'fleetmux result ' + str(answer.get('id')) + ' --config ' + str(stub['root_config'])},
                         ensure_ascii=False))
        return 0
    if args.cmd == 'status':
        _root(stub)
        code, out, err = fleet_cli(['status', '--config', str(stub['root_config'])])
        status = parse_json(out)
        if code != 0 or not status:
            raise JoinError('status of the root failed: ' + (err.strip() or out.strip() or 'exit code ' + str(code)))
        tree = status.get('subfleets') or {}
        item = next((v for k, v in tree.items() if isinstance(v, dict) and (k == name or v.get('name') == name)), None)
        print(json.dumps({'join': str(path), 'root_config': stub['root_config'], 'child': name,
                          'state': (item or {}).get('state') or 'not started', 'subfleet': item},
                         ensure_ascii=False, indent=2))
        return 0
    raise JoinError(str(path) + ' is the service sub-fleet ' + name + ' (attached to ' + str(stub.get('root_config'))
                    + '); only request / results / result / status / stop / ' + ' / '.join(CHILD_COMMANDS)
                    + ' / applet work on it. For other commands use its own config: '
                    + str(stub.get('child_config') or '<root run>/sub/' + name + '/config.json'))
