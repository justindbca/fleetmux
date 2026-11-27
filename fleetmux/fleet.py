#!/usr/bin/env python3
"""fleetmux fleet commands: one supervisor + N workers in cmux tabs, coordinated by a daemon.

Agents use Codex by default, with optional Pi supervisors/workers and official Claude
Code workers. The daemon never reads screens: each CLI's hook/extension calls back at
the end of a turn. Replies route through a private Redis instance listening only on a
project-local Unix socket under .runtime/redis (never localhost:6379).

Subcommands
  start     provision the private Redis + the control surface (must run inside cmux)
  daemon    the coordinator loop (internal, spawned by ``start``)
  task      submit a goal to the supervisor (task mode)
  request   submit one request (service mode)
  results   print / drain finished request results (service mode)
  pause     stop delivering and rotating (service mode; requests keep queuing)
  resume    undo pause
  answer    answer a sub-fleet request that waits for you (report:<task>:<id>), without the supervisor
  status    task + per-agent state, context usage, inbox depth
  stop      cancel the task, shut the daemon and its Redis down (tabs stay open)
  chrome    run a vetted browser verb through the daemon (for sandboxed agents, optional)
  agents    list or match offline agent profiles (does not contact a fleet)
  plan      build an offline dynamic dispatch plan (does not contact a fleet)
  notify    Codex notify hook (internal)
  claude-stop  Claude Code Stop hook for claude agents (internal)
  claude-rewake  Claude Code asyncRewake Stop hook of hook-channel workers (internal)
  claude-stop-failure  Claude Code StopFailure hook of hook-channel / clear-after-result workers (internal)
  claude-clear  Claude Code SessionStart(clear) hook of clear-after-result workers (internal)
  claude-prompt  Claude Code UserPromptSubmit hook of a v2 claude supervisor (internal)
  pi-event  Pi settled/session extension callback (internal)
  actor     exec the configured CLI for one agent (internal)
"""
import os
import sys

if __name__ == '__main__' and sys.path and os.path.realpath(sys.path[0] or '.') == os.path.dirname(os.path.realpath(__file__)):
    sys.path.pop(0)  # run as a script (or via a symlink): fleetmux/platform.py must not shadow the stdlib module

import argparse
import collections
import copy
import fcntl
import hashlib
import json
import math
import re
import shlex
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # runnable as a script (actor/notify argv point at this file)
from fleetmux import advisor, brake, claude_hook, cli_pairs, codex_queue, contracts, decompose, discover, fleet_config, fleetmux_sidebar, handoff, history, job_chain, kit as kitmod, lifecycle, lifecycle_close, lifecycle_deliver, lifecycle_hold, model_usage, named_subfleet, night, peer, pi_adapter, profile_runtime, profile_launch, restart_state, roster, runtime_v2, redis_runtime, session_board, subfleet, tabs, terminal_theme, watchdog  # noqa: E402
from fleetmux.platform import keep_awake_prefix  # noqa: E402
from fleetmux import gui_keep  # noqa: E402
from fleetmux import applet_host  # noqa: E402
from fleetmux import applet_view  # noqa: E402
from fleetmux import applet_lease  # noqa: E402
from fleetmux import blocked_scope  # noqa: E402
from fleetmux import notify_route  # noqa: E402
from fleetmux import session_env  # noqa: E402
from fleetmux import heal  # noqa: E402
from fleetmux import clear_after_result, discover, dsh_adapter, midturn, opencode_adapter  # noqa: E402
from fleetmux import sandbox  # noqa: E402  (P8o)
from fleetmux import awake  # noqa: E402  (§35: deadlines in awake time)
from fleetmux import paths  # noqa: E402  (§50.1)
from fleetmux import releases  # noqa: E402  (§56)
from fleetmux import release_drift  # noqa: E402  (§56 "Release drift")
from fleetmux import baseline  # noqa: E402  (switch baseline)
from fleetmux import baseline_check  # noqa: E402  (switch baseline: the daemon's check)
from fleetmux.agent_registry import AgentRegistry, AgentRegistryError  # noqa: E402
from fleetmux.assignment import assign_profiles  # noqa: E402
from fleetmux.handoff import read_usage  # noqa: E402
from fleetmux.message_bus import BusError  # noqa: E402
from fleetmux.planner import build_dispatch_plan  # noqa: E402
from fleetmux.router import route_task  # noqa: E402

SESSION = 'fleetmux'  # registry / session name of the main fleet; a sub-fleet is 'fleetmux:<task id>'
HOME = paths.home()  # $FLEETMUX_HOME, else ~/.fleetmux
LATEST = HOME / 'latest.json'


def pin_paths(cfg):
    """Apply the locations a run pinned in its config; the config wins over the environment.

    cmux starts the daemon, actors and hooks in fresh login shells that need not inherit the
    caller's FLEETMUX_HOME / FLEETMUX_STATE / CMUX_BIN / FLEETMUX_CONFIG, so `start` writes them into
    config.json (the private config file as `private_config`: an absolute path or 'none')."""
    global HOME, LATEST
    from fleetmux import cmux_manager
    if 'private_config' in cfg:
        fleet_config.pin(cfg['private_config'])
    if cfg.get('ui_language'):  # §63: the language the run's start resolved
        from fleetmux import i18n
        i18n.pin(cfg['ui_language'])
    if cfg.get('fleet_home'):
        HOME = Path(cfg['fleet_home'])
        LATEST = HOME / 'latest.json'
    if cfg.get('cmux_bin'):
        cmux_manager.CMUX = cfg['cmux_bin']
    if cfg.get('cmux_state_dir'):
        cmux_manager.STATE_DIR = Path(cfg['cmux_state_dir'])
    return cfg


def _pin_from(cfg_path):
    try:
        pin_paths(load(cfg_path))
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # the entry point itself reports an unreadable config
USER_CODEX_HOME = Path(os.environ.get('CODEX_HOME') or (Path.home() / '.codex'))
EVENTS, TASK, LOG, NAMES = 'fx:events', 'fx:task', 'fx:log', 'fx:names'
ROSTER = 'fx:roster'  # notify=1 / rejected=<errors>: told to the supervisor with its next message (§16)
# Service mode: a resident fleet that handles a stream of requests instead of one goal.
REQ, REQQ, RESULT, RESULTS, SERVICE = 'fx:req:', 'fx:reqq', 'fx:result:', 'fx:results', 'fx:service'
REQ_ID_RE = r'[A-Za-z0-9][A-Za-z0-9:._-]{0,127}'
REQ_HEAD = re.compile(r'^\[Request (' + REQ_ID_RE + r')\]')
SUPERVISOR_INSTRUCTION_HEAD = re.compile(r'^\[Supervisor instruction ([^\]\n]+)\] ', re.MULTILINE)
WORKER_NAME_RE = r'[a-z][a-z0-9-]{0,30}'
KIT_FLEET_RE = r'[a-z][a-z0-9-]*'  # start --kit-fleet NAME: <worker kit>/fleets/NAME/ (design.md §30)
# Display name of the supervisor's first generation; successors are supervisor-1.1, supervisor-1.2, ...
# (same <slot>.<generation> pattern as worker-1 -> worker-1.1). Redis keys and the cwd stay 'supervisor'.
SUPERVISOR_LABEL = 'supervisor-1'
DECIDED = {'approve': 'approved', 'revise': 'revised', 'reject': 'rejected'}  # escalation answers, past tense
RESULT_TTL = 7 * 86400
RESUME_CLIS = ('codex', 'claude')  # CLIs whose live process proves a reattachable pane (cmux_manager._is_cli);
# pi / opencode are never reattached: their bridge never reconnects after a Redis outage (next generation instead)
# Sandboxed agents cannot drive the user's browser themselves (and must never be allowed
# to kill it). If the operator supplies a broker script, the daemon runs these verbs for them.
CHROME_VERBS = {'check', 'tabs', 'newtab', 'go', 'url', 'js'}
THREAD_RE = r'[0-9a-f-]{36}'
DEFAULTS = {'window': 100000, 'stall_timeout': 1200,  # context-handoff defaults: fleet.yaml#handoff
            'chrome_min_interval': 20, 'profile': 'fleet', 'worker_cli': 'codex', 'worker_model': 'sonnet',
            'supervisor_model': 'opus',
            'claude_window': 200000, 'mode': 'task', 'max_busy': 0, 'request_timeout': None,
            'apply_profiles_live': False, 'allow_profile_live_switch': False, 'profile_ready_timeout': 180,
            'transport_version': 1, 'allow_development_v2': False, 'max_attempts': 2,
            'clear_after_result': False, 'clear_timeout': 20}
CLAUDE_JSON = Path.home() / '.claude.json'
# opencode workers: the free OpenCode Zen tier serves a 200K context (paid: 1M); the model name itself comes only
# from config (--opencode-model). Concurrency of the free tier is untested: max_busy defaults to 2.
OPENCODE_WINDOW = 200000
OPENCODE_MAX_BUSY = 2
OPENCODE_MIN_VERSION = (2, 0, 29)
MODEL_KEYS = {'claude': 'worker_model', 'pi': 'pi_model', 'codex': 'codex_model', 'opencode': 'opencode_model',
              'dsh': 'dsh_model'}
CONTRACTS = 'fx:contracts'  # contract tasks only: one JSON state document (fleetmux/contracts.py)
ESCALATIONS = 'fx:escalations'  # task mode: {task id: worker's split request / approval / delegation} (§20)
ITEMS = 'fx:items'  # plain task mode: {dispatch id: {to, instruction}} for dispatch items that carry an id
SPLIT_STALE = 'fx:split:stale'  # P7: JSON {M<n>: request id} of service splits dropped because their request ended
JOBS = 'fx:jobs'  # service mode, §58: JSON {job id: open job} of `fleetmux task` jobs, each run by its own sub-fleet
JOB_PREFIX = 'job-'  # job ids
JOB_RETIRED = ('review_file',)  # a job spec written before the review removal: accepted, never carried
JOB_KEYS = ('goal', 'hours', 'budget', 'artifacts', 'lifecycle', 'workers', 'supervisor_profile', 'worker_profiles',
            'name', 'roster', 'roles', 'deliver', 'overrides', 'supervisor', 'worker', 'tree', 'by') + JOB_RETIRED
# carried to the child (subfleet.spawn_child job=); overrides: the attached start's own settings (join.py)
JOB_SPAWN = ('name', 'roster', 'roles', 'deliver', 'overrides')
JOB_ROSTER_REFUSED = ('subfleets', 'channels', 'applets')  # a job sub-fleet's roster reload would refuse these
JOB_HOT_BASE = 'worker'  # a worker added for a job is <base>-<n> (an added roster row ends with -<number>)
JOB_TRIES = 3  # a job whose sub-fleet could not start this many times fails
MODEL_CHAIN_FILE = 'model_chain.json'  # §58: a job sub-fleet's model_chain once a wall switch was recorded
# section 58 "Restart": a service root's open jobs survive a stop / restart of the root (docs/design.md section 58)
JOBS_FILE = 'jobs.json'  # <run>/jobs.json mirrors fx:jobs, so `start` sees open jobs while the run's Redis is down
JOBS_CARRIED = 'jobs.carried.json'  # <old run>/: the rows a new run took over (tombstone: never started here again)
JOB_RESULTS = 'job-results'  # <run>/job-results/<job id>.json: each published job result (past RESULT_TTL too)
JOB_CANCELS = 'job-cancels'  # <run>/job-cancels/<job id>.json: an operator stop written while the root was down
JOB_INDEX = 'job-runs'  # <fleet home>/job-runs/<run dir name>.json while a root run holds open jobs
JOB_NOTIFIED = 'fx:jobs:notified'  # hash: job ids whose failure went out on the outbound channel (once each)
JOB_SUSPEND_REASON = 'root fleet stopped; the job resumes when the root starts again'
# section 60: a continued run's supervisor is always a new generation, so its model settings apply on resume
SUPERVISOR_CONTINUE = ('supervisor_model', 'supervisor_pi_model', 'supervisor_pi_thinking', 'supervisor_profile')
SUPERVISOR_CONTINUE_BY_CLI = {'claude': ('supervisor_model',), 'codex': ('supervisor_profile',),
                              'pi': ('supervisor_pi_model', 'supervisor_pi_thinking')}  # brake.SUPERVISOR_MODEL_KEY
CONTINUE_APPLIED = SUPERVISOR_CONTINUE + ('no_restore',) + ('roster', 'max_attempts', 'lease_reclaim', 'security', 'no_security', 'advisor', 'extend_hours',
                    'resume', 'no_join', 'fleet_home', 'attach', 'gui', 'no_view')  # what `resume` honours
ESCALATION_DECISIONS = ('approve', 'revise', 'reject')
# Plan F' 8.3 / 8.4: escalations the supervisor rules on instead of the user (keys in fx:escalations).
DIALOG_KEY, BLOCKED_KEY = 'dialog:', 'blocked:'
# §46: a sub-fleet supervisor's report_up item, ruled on by the top supervisor (keys report:<task>:<id>)
REPORT_KEY = 'report:'
REPORTS_UP = 'fx:reports_up'  # sub-fleet side: {report key: {what, who, paths}} still waiting for the top's ruling
NO_BLOCK = ('', 'none', '-', '无', 'n/a', 'na', 'nothing')


class NoStrike(ValueError):
    """A decision sent back for one correction that does not count toward the three-strike limit."""


def report_blocked(text):
    """The Blocked: part of a worker report (v2 settled JSON or v1 raw text); '' when it says none."""
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if not (isinstance(value, dict) and 'done' in value):
        got = runtime_v2.tui_candidates('worker', 'worker', text or '')
        value = got[0]['value'] if got else {}
    raw = value.get('blocked') if isinstance(value, dict) else ''
    raw = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    rows = [x.strip().lstrip('-*• ').strip() for x in raw.splitlines()]
    return '\n'.join(x for x in rows if x.rstrip('.。').strip().lower() not in NO_BLOCK)
CMD_TIMEOUT, CMD_TIMEOUT_MAX = 120, 600  # artifact cmd_exit (contract tasks): seconds the daemon may block
BRAKE = 'fx:brake'  # budget card + counters + last trip of the current task / service (fleetmux/brake.py)
WALLS = 'fx:walls'  # walls this run hit, '<cli>:<model>' -> {cli, model, account, scope, at, agent}: skipped on the chain
# Both wall chains as resolved at daemon start / resume (Fleet.wall_chains_load): walls read only this key (T-47)
WALL_CHAINS = 'fx:wall:chains'
# Advisor online only (roster `advisor: on`): audit hash per consultation, its counter, dispatches awaiting
# advice per worker (instruction -> advise_note), and notes appended to the supervisor's next message.
ADVICE, ADVICE_SEQ, ADVICE_PENDING, ADVICE_NOTES = 'fx:advice:', 'fx:advice:seq', 'fx:advice:pending:', 'fx:advice:notes'
# Mid-task rescue (advisor plan §3.2): supervisor consult items waiting for an idle slot, relays per task+slot.
RESCUE, RESCUE_RELAYS = 'fx:advice:rescue', 'fx:advice:relays'
FRESH_HINTS = 'fx:fresh:hints'  # §37 [Hint] / [Fresh failed] lines appended to the supervisor's next message
FRESH_INTENT = 'fx:fresh:intent:'  # §45 + slot: [instruction, explicit fresh] per queued dispatch to an always worker
SPLIT_NOTES = 'fx:split:notes'  # §44 rulings superseded by an approved split, appended the same way
# Resident advisor slot (docs/design.md §27, v2 task mode): its tab sits in the supervisor's pane; the first
# generation is labelled 'advisor', successors advisor-1.1, advisor-1.2 (like supervisor-1.1).
ADVISOR, ADVISOR_BASE = advisor.SLOT, 'advisor-1'
ADVICE_WAIT = 180  # s a consultation may wait for the advisor (READY / queue), and again once it has it
ADVISOR_RESTART_GAP = 60  # s between two in-place restarts of a failed advisor tab
# The one rescue consultation out with the resident advisor; the tickets its handoff note lists.
RESCUE_PENDING, ADVICE_RECENT = 'fx:advice:rescue_pending', 'fx:advice:recent'
RESCUE_FIELDS = ('rescue_note', 'rescue_launch', 'rescue_hint', 'rescue_relay', 'advice_preset')


def load(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, path)


def connect(cfg, socket_timeout=5):
    import redis
    sock = redis_runtime.validate_socket(cfg)
    client = redis.Redis(unix_socket_path=str(sock), db=0, decode_responses=True,
                         socket_connect_timeout=3, socket_timeout=socket_timeout)
    client.ping()  # Fail closed: never fall back to localhost:6379.
    return client


def agent_cwd(cfg, name):
    if name == ADVISOR:
        return Path(cfg['run']) / 'advisor'  # the one-shot advisor's cwd too: one transcript directory per run
    if name == 'supervisor' and cfg.get('supervisor_cwd'):
        return Path(cfg['supervisor_cwd'])  # sub-fleet: the adopted worker pane keeps its own directory
    run = Path(cfg['run'])
    return run / 'agents' / name if name == 'supervisor' else run / 'workspace' / name


def key(kind, name):
    return 'fx:' + kind + ':' + name


def slot_base(name):
    """The label stem of a slot's successors: supervisor-1.1, advisor-1.1, worker-1.1."""
    return SUPERVISOR_LABEL if name == 'supervisor' else ADVISOR_BASE if name == ADVISOR else name


def manager_for(cfg):
    """The registry of one fleet: the main fleet is 'fleetmux', a sub-fleet 'fleetmux:<task id>'.

    Which terminal backend that registry talks to is a property of the run (``config.json``), decided once
    by ``start`` and followed by the daemon, the actors, ``--resume`` and every sub-fleet (docs/design.md
    §「终端后端」). A run's config without a backend (started before the choice existed) stays on cmux.
    """
    from fleetmux import backends
    extra = {k: cfg[k] for k in ('agents_title', 'control_title') if cfg.get(k)}
    if 'cmux_state_dir' in cfg:
        extra['state_dir'] = cfg['cmux_state_dir']
    mgr = backends.build(backends.normalize_backend(cfg.get('backend')) or 'cmux',
                         cfg.get('session') or SESSION, **extra)
    if cfg.get('fleetmux_page') and hasattr(mgr, 'bind_page'):  # fleetmux sub-fleet: the root's server, own page
        mgr.bind_page(cfg.get('fleetmux_root_session') or SESSION, cfg['fleetmux_page'])
    return mgr


def run_backend(cfg):
    """The backend name of a run (the value fleet's capability checks and layouts branch on)."""
    from fleetmux import backends
    return backends.normalize_backend(cfg.get('backend')) or 'cmux'


def resolve_backend(requested=None, platform=None):
    """`start --backend`: (name, reason-or-None) -- the backend to write into config.json.

    No flag: fleetmux on every platform (design §33.3). The chosen backend -- default or explicit -- is
    refused here when it is not usable, before anything is provisioned, with what is missing.
    """
    from fleetmux import backends  # bound before any except clause below can run
    unknown = backends.InterfaceError
    try:
        name = backends.normalize_backend(requested)
    except unknown as exc:  # --backend is validated by argparse; a config is not
        return str(requested), str(exc)
    if name is None:
        name = backends.default_backend(sys.platform if platform is None else platform)
    try:
        return backends.check_backend(name), None
    except unknown as exc:
        return name, str(exc)


def backends_check(name):
    """Is this run's backend usable right now? cmux asks ``cmux_manager.cmux_available()`` (the probe the
    operators and tests patch, as `start` always did); any other backend asks its own registry, which
    falls back to the backend class when the registry is a fake."""
    from fleetmux import backends, cmux_manager
    if name == 'cmux':  # one probe for both paths: tests (and operators) patch exactly this function
        try:
            return bool(cmux_manager.cmux_available())
        except Exception:
            return False
    try:
        probe = getattr(backends.build(name, SESSION), 'available', None)
        if probe is not None:  # the real registry (or a test fake that carries the interface method)
            return bool(probe())
        return bool(backends.backend_class(name).available())
    except Exception:
        return False


def unavailable_text(name):
    """What `start` / `resume` print when the chosen backend cannot run here (design §4.4, §5)."""
    from fleetmux import backends
    try:
        why = backends.unavailable_reason(name)
    except Exception as exc:  # a backend module that cannot even import
        why = str(exc)
    return 'backend ' + str(name) + ' cannot run here: ' + (why or 'not available')


def codex_home(cfg):
    """The Codex home the agents run in (isolated from the operator's own hooks/MCP servers)."""
    return Path(cfg.get('codex_home') or (HOME / 'codex-home'))


def sessions_dir(cfg):
    return codex_home(cfg) / 'sessions'


def prepare_codex_home(home, profiles):
    """Create the agents' Codex home: shared login, the chosen profile file, no hooks, no MCP.

    auth.json and <profile>.config.toml are symlinked from the operator's Codex home, so the
    login (and its token refreshes) stay shared without copying secrets. config.toml is written
    once; `trust_agent_dir` appends per-agent trust entries to it later.
    """
    profiles = [profiles] if isinstance(profiles, str) else list(dict.fromkeys(profiles))
    home.mkdir(parents=True, exist_ok=True)
    for name in ['auth.json'] + [p + '.config.toml' for p in profiles]:
        src, dst = USER_CODEX_HOME / name, home / name
        if src.exists() and not dst.exists():
            dst.symlink_to(src)
    for profile in profiles:
        if not (home / (profile + '.config.toml')).exists():
            raise SystemExit('profile file not found: ' + str(USER_CODEX_HOME / (profile + '.config.toml'))
                             + ' (Codex >= 0.156 keeps each profile in <CODEX_HOME>/<profile>.config.toml)')
    cfg_file = home / 'config.toml'
    if not cfg_file.exists():
        cfg_file.write_text('# Generated by fleetmux. Agents run with CODEX_HOME pointed here so the\n'
                            '# operator\'s own hooks, MCP servers and plugins do not run inside agents.\n'
                            '# auth.json and the profile file are symlinks into your real Codex home.\n'
                            'check_for_update_on_startup = false\n')
    return home


def trust_agent_dir(home, cwd):
    """Codex asks "Trust this folder?" per exact directory; pre-answer it for an agent cwd."""
    cfg_file = home / 'config.toml'
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    entry = '[projects.' + json.dumps(str(cwd)) + ']'
    text = cfg_file.read_text() if cfg_file.exists() else ''
    if entry not in text:
        with cfg_file.open('a') as f:
            f.write('\n' + entry + '\ntrust_level = "trusted"\n')


def agent_cli(cfg, name):
    if name == ADVISOR:
        return 'claude'  # always the official Claude CLI, whatever --worker-cli the run has (§27)
    supervisor = name == 'supervisor'
    # Unset = a config written before the CLI choice existed: the original Codex. Any other value must be a
    # CLI this code knows; an unrecognised one is refused, never run as Codex (opencode brief 2026-10-16).
    cli = cfg.get('supervisor_cli' if supervisor else 'worker_cli') or 'codex'
    known = cli_pairs.CLIS if supervisor else cli_pairs.WORKER_CLIS
    if cli not in known:
        raise ValueError('unknown ' + ('supervisor' if supervisor else 'worker') + ' CLI ' + repr(cli) + ' for '
                         + str(name) + ' (known: ' + ', '.join(known) + '); refusing rather than running it as codex')
    return cli


def opencode_start_problem(args, binary):
    """Why `start` must refuse opencode workers, or None (binary / version / model / transport checked first)."""
    if args.supervisor_cli == 'opencode':
        return 'opencode runs only as a worker; pick pi, codex or claude for --supervisor-cli'
    if args.transport_version != 2:
        return 'opencode workers run only on transport v2 (in-process plugin + agent_bridge.py)'
    if not args.opencode_model or '/' not in args.opencode_model.strip('/'):
        return '--opencode-model provider/model is required for opencode workers (e.g. opencode/deepseek-v4.1-flash-free)'
    if not args.opencode_window or args.opencode_window < 1:
        return '--opencode-window must be a positive integer'
    try:
        found = subprocess.run([binary, '--version'], text=True, capture_output=True, timeout=15, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 'opencode --version failed: ' + str(exc)
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', found.split()[-1] if found.split() else '')
    if not match or tuple(map(int, match.groups())) < OPENCODE_MIN_VERSION:
        return 'opencode >= ' + '.'.join(map(str, OPENCODE_MIN_VERSION)) + ' is required (v2 plugin API); found: ' + found
    return None


def agent_cfg(cfg, name):
    """cfg with this agent's own pi model / thinking resolved (no profile launch)."""
    return pi_adapter.for_agent(cfg, name)


def is_claude(cfg, name):
    return agent_cli(cfg, name) == 'claude'


def is_pi(cfg, name):
    return agent_cli(cfg, name) == 'pi'


def is_opencode(cfg, name):
    return agent_cli(cfg, name) == 'opencode'


def is_codex(cfg, name):
    return agent_cli(cfg, name) == 'codex'


def is_dsh(cfg, name):
    return agent_cli(cfg, name) == 'dsh'


def is_bridge(cfg, name):
    """pi, opencode and dsh run fleet's bridge (agent_bridge.py), not an official TUI hook."""
    return agent_cli(cfg, name) in ('pi', 'opencode', 'dsh')


def trust_claude_dir(cwd):
    """Claude Code asks "trust this folder?" per directory, and a trusted parent covers its
    subdirectories. Write ~/.claude.json only when neither the agent cwd nor any parent is
    trusted yet, and then trust the fleet home (or the cwd outside it) once, so running Claude
    sessions are not raced on every spawn."""
    cwd = Path(cwd).resolve()
    with open(str(CLAUDE_JSON) + '.fleet.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Unreadable or half-written config: refuse rather than replace the user's file with {}.
        data = json.loads(CLAUDE_JSON.read_text()) if CLAUDE_JSON.exists() else {}
        projects = data.setdefault('projects', {})
        changed = False
        if not any(projects.get(str(d), {}).get('hasTrustDialogAccepted') for d in [cwd, *cwd.parents]):
            root = HOME.resolve() if HOME.exists() and HOME.resolve() in cwd.parents else cwd
            projects.setdefault(str(root), {})['hasTrustDialogAccepted'] = True
            changed = True
        # "Allow external CLAUDE.md file imports?" is asked per exact cwd (a trusted parent does not cover
        # it): pre-answer No so a fleet worker never stops on it and never loads the operator's personal
        # imports (macmini 2026-10-14: an advisor-respawned worker sat on this dialog). Never overrides an
        # answer already recorded for that directory.
        entry = projects.setdefault(str(cwd), {})
        if not entry.get('hasClaudeMdExternalIncludesWarningShown'):
            entry.setdefault('hasClaudeMdExternalIncludesApproved', False)
            entry['hasClaudeMdExternalIncludesWarningShown'] = True
            changed = True
        if not changed:
            return
        tmp = CLAUDE_JSON.with_suffix('.json.fleet-tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.replace(CLAUDE_JSON)


def install_kit(kit, cwd):
    """Copy a worker kit (.claude/agents, .claude/skills, mcp.json) into a claude worker's cwd."""
    if not kit:
        return
    kit = Path(kit).expanduser()
    if (kit / '.claude').is_dir():
        shutil.copytree(kit / '.claude', cwd / '.claude', dirs_exist_ok=True)
    if (kit / 'mcp.json').exists():
        shutil.copy(kit / 'mcp.json', cwd / 'mcp.json')


PROJECT_FLEET_DIR = '.fleetmux'  # start --project-dir: runs under <project>/.fleetmux/runs (user 2026-11-11)


def exclude_project_fleet_dir(project):
    """Add ``.fleetmux/`` to the git exclude of the repository that holds ``project`` (info/exclude, never a
    tracked .gitignore), once: a line already there is left alone. Returns the exclude path, None outside git."""
    try:
        out = subprocess.run(['git', '-C', str(project), 'rev-parse', '--path-format=absolute', '--git-path',
                              'info/exclude'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    path = Path(out.stdout.strip())
    line = PROJECT_FLEET_DIR + '/'
    text = path.read_text() if path.exists() else ''
    if any(l.strip().strip('/') == PROJECT_FLEET_DIR for l in text.splitlines()):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as f:
        f.write(('' if not text or text.endswith('\n') else '\n') + line + '\n')
    return path


IMPORTED_MCP_MARK = '.fleet-imported-mcp'  # names this fleet merged into cwd/mcp.json (roster `import_mcp`)


def roster_imports(cfg):
    """(claude_md_paths, mcp_servers) of the run's roster ``import_claude_md`` / ``import_mcp`` (user 2026-11-11);
    ([], {}) without a roster, without the keys, or when the roster cannot be read now."""
    path = cfg.get('roster')
    try:
        return roster.imports(Path(path).read_bytes(), Path(path).parent) if path else ([], {})
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return [], {}


def imported_rules(cfg, native_user_memory=False):
    """The roster's ``import_claude_md`` files, appended to every agent's AGENTS.md / CLAUDE.md; '' without any.
    Copied, not an ``@path`` line: trust_claude_dir answers Claude Code's "Allow external CLAUDE.md file imports?"
    with No for each agent cwd (an unattended pane would sit on that dialog), so an import of a file outside the
    cwd would silently never load. Read at each spawn: an edited file reaches the next generation.
    ``native_user_memory``: this agent is a Claude Code session that loads ~/.claude/CLAUDE.md itself (no
    --setting-sources project), so that file is not copied a second time."""
    out = ''
    user_md = str((Path.home() / '.claude' / 'CLAUDE.md').resolve())
    for path in roster_imports(cfg)[0]:
        if native_user_memory and path == user_md:
            continue
        try:
            text = Path(path).read_text()
        except OSError:
            continue
        out += ('\n\n## Imported project rules (`' + path + '`)\n\n'
                'Copied from that file when this session started. Where it conflicts with the fleet rules above (the '
                'reply format, who may change your task, where you may write), the fleet rules win. Relative paths in '
                'it are relative to `' + str(Path(path).parent) + '`.\n\n' + text.rstrip() + '\n')
    return out


def install_imported_mcp(cfg, cwd):
    """Merge the roster's ``import_mcp`` servers into a Claude agent's cwd/mcp.json (passed as --mcp-config). A
    kit's own server of the same name wins. Without the key, the servers this fleet merged earlier are dropped:
    a cwd/mcp.json the fleet wrote alone (no kit mcp.json) is removed."""
    cwd = Path(cwd)
    target, mark = cwd / 'mcp.json', cwd / IMPORTED_MCP_MARK
    kit = cfg.get('worker_kit')
    kit_mcp = bool(kit) and (Path(kit).expanduser() / 'mcp.json').exists()  # copied into cwd at this spawn
    servers = roster_imports(cfg)[1]
    if not servers:
        if mark.exists():
            mark.unlink()
            if not kit_mcp and target.exists():
                target.unlink()
        return
    data = {}
    if target.exists() and (kit_mcp or not mark.exists()):  # the kit's copy, or a file the fleet never wrote
        try:
            data = json.loads(target.read_text())
        except ValueError:
            data = {}
    if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
        data = {}
    merged = data.setdefault('mcpServers', {})
    for name, entry in servers.items():
        merged.setdefault(name, entry)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    mark.write_text(json.dumps(sorted(servers)))


def claude_transcript_usage(path, window):
    """File-only reader shared by idle checks and the daemon probe thread."""
    return midturn.claude_transcript_usage(path, window)


CLAUDE_1M = ('[1m]', 'opus', 'fable', 'sonnet')  # built-in: Claude models with a native 1,000,000-token window (code.claude.com/docs/en/model-config)


def handoff_limits(cli, model, cfg, table=None, worker=None, card=None):
    """Effective (window, ratio, source) for one agent (docs/design.md §22).

    Window and ratio are resolved separately, first hit wins: the roster worker entry, the
    roster ``handoff`` table (``<cli>:<model>`` then ``<cli>``), the config ``handoff`` table
    (same keys), then the running profile's agent card (``card`` = (profile id, ratio): ratio only,
    design.md §12 model cards). The window then takes built-ins / --claude-window; the ratio takes a typed
    --handoff-ratio (the config key ``handoff_ratio``), else fleet.yaml#handoff. ``window`` None means
    "measured" (Codex rollout, Pi report). Only Claude needs a built-in: its transcript carries no window."""
    layers = [('worker', worker or {})]
    for where, tab in (('roster', table or {}), ('config', cfg.get('handoff') or {})):
        layers += [(where + ' ' + k, tab[k]) for k in ((cli + ':' + model) if model else None, cli) if k and k in tab]
    if card and card[1] is not None:
        layers.append(('card ' + card[0], {'ratio': card[1]}))
    window = ratio = None
    for where, entry in layers:
        if window is None and 'window' in entry:
            window, wsrc = int(entry['window']), where
        if ratio is None and 'ratio' in entry:
            ratio, rsrc = float(entry['ratio']), where
    if window is None:
        if cli == 'opencode':
            window, wsrc = int(cfg.get('opencode_window') or OPENCODE_WINDOW), 'opencode window'
        elif cli != 'claude':
            wsrc = 'measured'
        elif any(t in (model or '').lower() for t in CLAUDE_1M):
            window, wsrc = 1000000, 'built-in ' + (model or '')
        else:
            window, wsrc = int(cfg['claude_window']), '--claude-window'
    if ratio is None:
        rsrc = 'fleet.yaml#handoff' if cfg.get('handoff_ratio') is None else '--handoff-ratio'
        ratio = fleet_config.handoff_ratio(cfg)
    return window, ratio, 'window: ' + wsrc + '; ratio: ' + rsrc


def claude_stop(cfg_path, name, generation, raw):
    """Claude Code Stop hook. Pushes a turn event unless background work will resume the turn.

    A Claude worker on the hook channel (fleetmux/claude_hook.py) then waits here for its next
    delivery and returns it as ``{"decision": "block", "reason": text}``; the CLI prints that.
    """
    started = claude_hook.clock()  # Claude Code kills the hook on wall time, sleep included
    data = json.loads(raw or '{}')
    if data.get('background_tasks'):
        # Subagents still running in the background: the session will wake and stop again. Not settled,
        # not released; audited (a held slot waiting on this was invisible before, plan E-2) and posted so
        # the watchdog does not count the waiting session as quiet (§24).
        turn_deferred(cfg_path, name, generation, data.get('background_tasks'), data.get('session_id'))
        return False
    text = data.get('last_assistant_message')
    if text is None:
        text = ''
        try:
            for line in reversed(_tail(Path(data.get('transcript_path', '')), 262144)):
                row = json.loads(line)
                if row.get('type') == 'assistant' and not row.get('isSidechain'):
                    parts = (row.get('message') or {}).get('content') or []
                    text = '\n'.join(p.get('text', '') for p in parts if isinstance(p, dict) and p.get('type') == 'text')
                    if text:
                        break
        except (OSError, ValueError):
            pass
    cfg = load(cfg_path)
    hooked = claude_hook.enabled(cfg, name, claude=True)
    deadline = claude_hook.deadline(cfg, started)
    own = (cfg, name, int(generation))
    follow = hooked and following(cfg)
    if follow:
        # This pane may run a sub-fleet's supervisor on the hook channel (§19/§29): settle and wait where the
        # route points (stopping marked there too), and keep following the route while waiting.
        routed = subfleet.delegated_route(cfg, name, int(generation))
        if claude_hook.enabled(routed[0], routed[1], claude=True):
            cfg, name, generation = routed
    if hooked:
        hook_mark_stopping(cfg, name, int(generation), data.get('session_id'))
    if cfg.get('transport_version') == 2:
        cfg, name, generation = subfleet.delegated_route(cfg, name, int(generation))
        # An adopted worker pane that now runs a sub-fleet's supervisor gets typed input: never wait there.
        hooked = hooked and claude_hook.enabled(cfg, name, claude=True)
        if not hooked:
            return tui_turn(cfg, name, int(generation), thread=data.get('session_id'),
                            transcript=data.get('transcript_path', ''), text=text,
                            inputs=claude_user_prompts(data.get('transcript_path', '')))
        try:
            tui_turn(cfg, name, int(generation), thread=data.get('session_id'),
                     transcript=data.get('transcript_path', ''), text=text,
                     inputs=claude_user_prompts(data.get('transcript_path', '')))
        except Exception as exc:  # still arm the channel: the daemon holds/expires the lease itself
            print('fleet claude-stop: turn not reported: ' + str(exc)[:300], file=sys.stderr)
        return claude_hook_next(cfg, name, int(generation), data.get('session_id'), deadline,
                                transcript=data.get('transcript_path', ''), own=own if follow else None)
    # Which delivery this turn answers: the last user message in the transcript that points at an
    # .inbox/msg-*.md file. User messages are written when the turn starts, so they are present even
    # when the final assistant message has not been flushed yet.
    msg_id = ''
    try:  # scan the whole transcript: tool output after the pointer can be megabytes
        with open(data.get('transcript_path', ''), errors='replace') as f:
            for line in f:
                if '.inbox/msg-' in line and '"type":"user"' in line.replace(' ', ''):
                    m = re.search(r'\.inbox/(msg-[0-9]{8}-[0-9]{6}-[0-9a-f]{6})\.md', line)
                    if m:
                        msg_id = m.group(1)
    except OSError:
        pass
    r = connect(cfg)
    # No hook-side de-dup: the Stop payload carries no turn id and the transcript may lag the final
    # message. The daemon drops a turn for a claude worker that is already idle (see on_turn).
    if hooked and claude_hook_keepalive(cfg, r, name, int(generation), data.get('session_id')):
        r.close()
        return claude_hook_next(cfg, name, int(generation), data.get('session_id'), deadline,
                                transcript=data.get('transcript_path', ''))
    r.rpush(EVENTS, json.dumps({'kind': 'turn', 'agent': name, 'generation': int(generation),
                                'thread': data.get('session_id'), 'transcript': data.get('transcript_path', ''),
                                'msg_id': msg_id, 'text': text, 'at': time.time()}, ensure_ascii=False))
    if not hooked:
        return True
    r.close()
    return claude_hook_next(cfg, name, int(generation), data.get('session_id'), deadline,
                            transcript=data.get('transcript_path', ''))


def turn_deferred(cfg_path, name, generation, tasks, thread=None):
    """Audit a Stop deferred by background tasks and post it to the daemon (v2 only), which marks the slot
    (§24 bg_wait). Never raises: the hook's answer is unchanged."""
    try:
        cfg = load(cfg_path)
        if cfg.get('transport_version') != 2:
            return
        r = connect(cfg)
        try:
            bus = runtime_v2.bus_for(cfg, r)
            rows = [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []
            r.xadd(bus.audit_key, {'event': 'turn.deferred', 'slot': name, 'run_id': bus.run_id,
                                   'generation': str(generation), 'level': 'info',
                                   'tasks': str(len(tasks) if isinstance(tasks, (list, dict)) else 1),
                                   'types': ','.join(str(t.get('type') or '') for t in rows),
                                   'at': str(int(time.time() * 1000))})
            runtime_v2.post_event(bus, dict(kind='turn_deferred', agent=name, generation=int(generation),
                                            run_id=bus.run_id, thread=thread,
                                            tasks=[{k: str(t[k])[:200] for k in ('id', 'type', 'status', 'description')
                                                    if k in t} for t in rows]))
        finally:
            r.close(); r.connection_pool.disconnect()
    except Exception as exc:  # noqa: BLE001 - an audit must never change the hook's answer
        print('fleet claude-stop: turn.deferred not audited: ' + str(exc)[:300], file=sys.stderr)


def following(cfg):
    """A v2 run whose waiting hooks follow ``subfleet.delegated_route`` (start pins the switch, §19/§29)."""
    return cfg.get('transport_version') == 2 and cfg.get('adopted_supervisor_hook_delivery') is True


def route_moved(own, here):
    """The pane's route (from its pinned hook identity ``own``) no longer points at ``here`` (run, slot,
    generation). An unreadable route is not a move: the hook keeps its target rather than guess."""
    now = subfleet.delegated_route(*own, strict=True)
    return now is not None and (now[0].get('run'), now[1], int(now[2])) != here


def hook_keys(cfg, r, name, generation):
    """Redis keys of one slot's hook channel: run-scoped on v2, the fleet's fx: space on v1."""
    prefix = runtime_v2.bus_for(cfg, r).prefix if cfg.get('transport_version') == 2 else 'fx'
    return claude_hook.keys(prefix, name, generation)


def hook_bind(r, k):
    try:
        bind = json.loads(r.get(k['bind']) or '{}')
    except ValueError:
        return {}
    return bind if isinstance(bind, dict) else {}


def claude_hook_keepalive(cfg, r, name, generation, thread):
    """True once for the turn our own keepalive started: it is never settled nor reported."""
    k = hook_keys(cfg, r, name, generation)
    bind = hook_bind(r, k)
    if bind.get('kind') != 'keepalive' or bind.get('session_id') != thread or bind.get('generation') != generation:
        return False
    if cfg.get('transport_version') == 2:
        return bool(r.eval('''
            local ok, current = pcall(cjson.decode, redis.call('GET', KEYS[1]) or '{}')
            local expected = cjson.decode(ARGV[1])
            if not ok or type(current) ~= 'table' or current.kind ~= 'keepalive'
               or current.session_id ~= expected.session_id or current.generation ~= expected.generation
               or current.at ~= expected.at or current.via ~= expected.via then return 0 end
            redis.call('DEL', KEYS[1])
            return 1
        ''', 1, k['bind'], json.dumps(bind)))
    r.delete(k['bind'])
    return True


def hook_delivered(r, k, thread, lease):
    """The hook of this session handed this very delivery to Claude (v2 binding, design.md §21)."""
    bind = hook_bind(r, k)
    return (bind.get('kind') == 'delivery' and bool(lease.get('message_id'))
            and bind.get('message_id') == lease.get('message_id') and bind.get('session_id') == thread
            and (not lease.get('current_delivery') or bind.get('delivery_id') == lease.get('current_delivery')))


def hook_session(cfg, r, name, generation, thread, parent, transcript=''):
    """(keys, alive, accept) of one hook process; shared by the Stop hook and its rewake fallback.

    ``accept(entry, via)`` re-validates an entry at hand-over: a delivery must still be this
    session's received lease (v2) or the pointer the daemon sent last (v1); anything else is
    dropped as stale (audit ``hook.dropped``). It writes the bind on success and returns the text.
    The bind carries the size of ``transcript`` at that moment (``offset``): Claude Code writes the
    row of this hand-over after it, so an older row of the same text is not this delivery's.
    """
    bus = runtime_v2.bus_for(cfg, r) if cfg.get('transport_version') == 2 else None
    k = hook_keys(cfg, r, name, generation)

    def alive():
        if os.getppid() != parent:  # orphaned: never take a delivery a dead session cannot run
            return False
        info = r.hgetall(key('agent', name))
        # delegated: a sub-fleet adopts this pane and types its role change; let that prompt run
        return int(info.get('generation', -1)) == generation and info.get('state') not in ('dead', 'delegated')

    def audit(event, entry, **fields):
        if bus:
            r.xadd(bus.audit_key, dict({'event': event, 'slot': name, 'run_id': bus.run_id,
                                        'generation': str(generation), 'message_id': str(entry.get('message_id', '')),
                                        'at': str(int(time.time() * 1000))}, **fields))

    def drop(entry, why):
        audit('hook.dropped', entry, error=why, level='warn')
        return None

    def accept(entry, via='stop'):
        kind, info = entry.get('kind'), r.hgetall(key('agent', name))
        if entry.get('generation') != generation:
            return drop(entry, 'entry of another generation')
        if kind == 'delivery' and bus:
            lease = r.hgetall(bus.lease_key(name))
            if (lease.get('status') != 'received' or lease.get('generation') != str(generation)
                    or lease.get('session_id') != thread
                    or lease.get('message_id') != entry.get('message_id')
                    or lease.get('current_delivery') != entry.get('delivery_id')):
                return drop(entry, 'delivery is no longer this session\'s received lease')
        elif kind == 'nudge':
            if info.get('state') != 'starting':
                return drop(entry, 'startup nudge after startup')
        elif kind == 'pointer' and not bus:
            if info.get('msg_id') != entry.get('msg_id') or info.get('state') not in ('busy', 'handoff'):
                return drop(entry, 'pointer is not the last v1 delivery')
        else:
            return drop(entry, 'unknown hook entry')
        bind = {x: entry.get(x) for x in ('kind', 'message_id', 'delivery_id', 'msg_id') if x in entry}
        try:
            offset = os.path.getsize(transcript)
        except (OSError, TypeError, ValueError):
            offset = 0
        r.set(k['bind'], json.dumps(dict(bind, generation=generation, session_id=thread or '', via=via,
                                         at=time.time(), offset=offset)), ex=claude_hook.BIND_TTL)
        if via != 'stop':
            audit('hook.rewake', entry, level='warn')
        return entry.get('text')
    return k, alive, accept


def hook_on_sigterm():
    if threading.current_thread() is threading.main_thread():
        # Claude Code stops a hook with SIGTERM: unwind so a popped entry goes back to the mailbox.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))


def claude_hook_next(cfg, name, generation, thread, deadline, transcript='', own=None):
    """Wait for this generation's next delivery; the block decision for it, or None (let Claude idle).

    None also when the operator typed into the pane meanwhile (``transcript``: claude_hook.OperatorQueue):
    the hook yields so that line runs now instead of after the next delivery or the keepalive.

    ``own``: the pane's pinned hook identity (cfg, slot, generation) in a run that follows the route
    (``following``). The wait then goes where ``subfleet.delegated_route`` points: into the sub-fleet's
    supervisor mailbox (its own Redis) once the slot is delegated, back to the slot's mailbox once the
    parent took it back. While the sub-fleet is going away (its supervisor retired, its Redis gone) and the
    parent still delegates the slot, it keeps waiting for that change; it takes nothing meanwhile.
    """
    import redis
    hook_on_sigterm()
    queue = claude_hook.OperatorQueue(transcript) if transcript else None
    rearm_started = time.time()
    parent = os.getppid()  # Claude Code (or its hook shell): once it is gone, nobody reads our answer
    while claude_hook.clock() < deadline and parent != 1:
        if own is not None:
            route = subfleet.delegated_route(*own, strict=True)
            if route is not None:
                if not claude_hook.enabled(route[0], route[1], claude=True):
                    return None  # the route points at a typed channel: let Claude idle there
                cfg, name, generation = route
        try:
            r = connect(cfg, socket_timeout=claude_hook.POLL + 10)
        except (redis.RedisError, OSError, ValueError):
            time.sleep(claude_hook.POLL)
            continue
        try:
            k, alive, accept = hook_session(cfg, r, name, generation, thread, parent, transcript)
            here = (cfg.get('run'), name, int(generation))
            if own is not None:
                live = alive

                def alive():  # also the fence before a popped entry is taken: never across a route change
                    return live() and not route_moved(own, here)
            text, _ = claude_hook.wait(r, k, generation, thread, deadline, alive, accept, queue=queue)
            if text:
                return claude_hook.block(text)
            if own is not None and route_moved(own, here):
                continue  # delegated or taken back meanwhile: wait on the new target
            if (own is not None and here != (own[0].get('run'), own[1], own[2])
                    and claude_hook.clock() < deadline):
                # Routed into a sub-fleet whose supervisor is not live (still starting, or going away while
                # the parent still delegates the slot): keep waiting, the next change comes to this hook.
                # Only a yield to an operator line ends the wait (one queued meanwhile still runs now).
                if os.getppid() != parent:
                    return None
                ready = live()
                if not ready and queue is not None and queue.pending():
                    return None
                if not (ready and r.exists(k['human'])):
                    time.sleep(1)
                    continue
            if name == 'supervisor' and alive() and r.exists(k['human']):
                supervisor_yield_wait(cfg, r, name, generation, thread)
                return None
            if claude_hook.clock() < deadline or not alive():
                return None
            # Both deadline writers use the same atomic claim and the actual slot identity.
            if cfg.get('transport_version') == 2:
                if not supervisor_rewake_keepalive(cfg, r, k, generation, thread, None,
                                                   slot=name, started=rearm_started):
                    return None
                return claude_hook.block(claude_hook.KEEPALIVE)
            # v1 retains its original channel.
            r.set(k['bind'], json.dumps({'kind': 'keepalive', 'generation': generation, 'session_id': thread or '',
                                         'at': time.time()}), ex=claude_hook.BIND_TTL)
            return claude_hook.block(claude_hook.KEEPALIVE)
        except redis.RedisError:
            time.sleep(claude_hook.POLL)  # Redis restarting: keep the session armed until the deadline
        finally:
            r.close(); r.connection_pool.disconnect()
    return None


def hook_mark_stopping(cfg, name, generation, thread=None):
    """The Stop hook is settling (before it waits): the rewake fallback must not take over meanwhile."""
    try:
        r = connect(cfg)
    except Exception:
        return
    try:
        k = hook_keys(cfg, r, name, generation)
        r.set(k['stopping'], str(os.getpid()), ex=claude_hook.STOPPING_TTL)
        if cfg.get('transport_version') == 2:
            info = r.hgetall(key('agent', name))
            if info.get('generation') == str(generation) and thread and info.get('thread') == thread:
                r.delete(k['yield_wait'])  # this session reached an actual Stop boundary
                bind = hook_bind(r, k)
                # A Stop proves this handed-over turn ended, even if settlement later times out.
                # Pin the bind, not a wall clock: a newer hand-over must never borrow this proof.
                r.set(k['stopped'], json.dumps(dict(session_id=thread, generation=generation,
                      delivery_id=bind.get('delivery_id'), bind_at=bind.get('at'), stop_id=uuid.uuid4().hex)), ex=claude_hook.BIND_TTL)
    except Exception:
        pass
    finally:
        r.close()


def claude_rewake(cfg_path, name, generation, raw):
    """asyncRewake Stop hook: the fallback when no Stop hook waits (Escape, a killed hook at its timeout).

    It never recovers a StopFailure (API error) turn: that delivery was already taken from the mailbox.
    Task mode settles it and queues it again (claude_stop_failure); this fallback (also run at that
    StopFailure, task mode) hands the retry over like any entry. Service mode: the watchdog's stall path.

    It never acts while another turn of the session runs (``claude_hook.TurnWatch`` on the Stop's
    transcript): a turn the Stop hook did not start (operator after Escape or ctrl+x ctrl+s, a peer
    message, a background task) ends with its own Stop, whose hooks take over. Escape or a
    StopFailure ending that turn makes this fallback eligible again. Started at a StopFailure, it
    also counts a turn that queued entries started right at the failure, before this hook ran.

    Runs in the background at every Stop. Only the newest one per slot acts: if this generation's
    mailbox holds an entry while neither a waiting nor a settling Stop hook exists for
    ``claude_hook.GRACE`` seconds, it takes the entry and returns its text; the CLI writes it to
    stderr and exits 2, which wakes Claude Code with it (no typing). None: nothing to do.
    """
    import redis
    started = claude_hook.clock()
    data = json.loads(raw or '{}')
    cfg = load(cfg_path)
    generation = int(generation)
    own, returning = (cfg, name, generation), False
    # A run that follows the route (§19/§29 hook-channel sub-fleet supervisors): like claude_hook_next, watch
    # where the route points and move with it; never the typed-supervisor "returning" wait below.
    follow = claude_hook.enabled(cfg, name, claude=True) and following(cfg)
    if follow:
        routed = subfleet.delegated_route(cfg, name, generation)
        if claude_hook.enabled(routed[0], routed[1], claude=True):
            cfg, name, generation = routed
        else:
            follow = False
    if cfg.get('transport_version') == 2 and not follow:
        cfg, name, generation = subfleet.delegated_route(cfg, name, generation)
        if (not claude_hook.enabled(cfg, name, claude=True) and (name, generation) != own[1:]
                and claude_hook.enabled(own[0], own[1], claude=True)):
            # The session supervises a sub-fleet (typed there). Its own worker channel has no Stop hook
            # waiting when it comes back: keep watching it, idle while delegated (§29 return handoff).
            (cfg, name, generation), returning = own, True
    if not claude_hook.enabled(cfg, name, claude=True):
        return None
    hook_on_sigterm()
    thread, parent = data.get('session_id'), os.getppid()
    deadline = claude_hook.deadline(cfg, started)
    watch = claude_hook.TurnWatch(data.get('transcript_path') or '',
                                  failed=data.get('hook_event_name') == 'StopFailure')
    mine = None
    rearm_started = time.time()
    while claude_hook.clock() < deadline and parent != 1:
        if follow:
            route = subfleet.delegated_route(*own, strict=True)
            if route is not None and claude_hook.enabled(route[0], route[1], claude=True):
                if (route[0].get('run'), route[1], route[2]) != (cfg.get('run'), name, generation):
                    mine = None  # a new channel: become its newest fallback
                cfg, name, generation = route
        try:
            r = connect(cfg, socket_timeout=claude_hook.POLL + 10)
        except (redis.RedisError, OSError, ValueError):
            time.sleep(claude_hook.POLL)
            continue
        try:
            k, alive, accept = hook_session(cfg, r, name, generation, thread, parent,
                                            data.get('transcript_path') or '')
            here = (cfg.get('run'), name, int(generation))
            if mine is None:
                mine = r.incr(k['rewake'])
                r.expire(k['rewake'], claude_hook.BIND_TTL)
            def busy():
                if name == 'supervisor' and r.exists(k['yield_wait']):
                    return True  # slash/queued input completion is unknown until a real Stop or block
                if watch.poll():
                    try:
                        watch.ended(float(r.get(k['turnend']) or 0))
                    except ValueError:
                        pass
                if name == 'supervisor':
                    try:
                        blocked = json.loads(r.get(k['prompt_blocked']) or '{}')
                        if blocked.get('session_id') == thread and blocked.get('generation') == generation:
                            watch.blocked(int(blocked.get('offset', -1)))
                    except (ValueError, TypeError, AttributeError):
                        pass
                return watch.busy

            unarmed, moved = None, False
            while claude_hook.clock() < deadline:
                if returning and str(mine) == r.get(k['rewake']) and r.hget(key('agent', name), 'state') == 'delegated':
                    time.sleep(1)
                    continue
                if follow and str(mine) == r.get(k['rewake']) and not alive():
                    if route_moved(own, here):
                        moved = True
                        break  # delegated or taken back: watch the new channel
                    if here != (own[0].get('run'), own[1], own[2]) and os.getppid() == parent:
                        time.sleep(1)  # the sub-fleet is going away; the return comes to this channel's owner
                        continue
                if str(mine) != r.get(k['rewake']) or not alive():
                    return None  # a newer Stop has its own fallback, or this generation is gone
                if busy() or claude_hook.live(r, k) or not r.llen(k['mailbox']):
                    unarmed = None
                    time.sleep(1)
                    continue
                unarmed = unarmed or time.monotonic()
                if time.monotonic() - unarmed < claude_hook.GRACE:
                    time.sleep(1)
                    continue
                got = r.lpop(k['mailbox'])
                if not got:
                    continue
                try:
                    entry = json.loads(got)
                    text = accept(entry, via='rewake') if isinstance(entry, dict) else None
                except ValueError:
                    continue
                except BaseException:
                    r.lpush(k['mailbox'], got)
                    raise
                if text:
                    return text
            if moved:
                continue
            if (name == 'supervisor' and alive() and not busy()
                    and supervisor_rewake_keepalive(cfg, r, k, generation, thread, mine,
                                                   slot=name, started=rearm_started)):
                return claude_hook.KEEPALIVE
            return None
        except redis.RedisError:
            time.sleep(claude_hook.POLL)
        finally:
            r.close(); r.connection_pool.disconnect()
    return None


def claude_posttool(cfg_path, name, generation):
    """Claude Code PostToolUse hook (design.md §34): the mid-turn handoff prompt the daemon registered for
    this slot and generation, taken once (midturn_send_pending 1 -> 0); None when there is nothing to send.
    Never raises: any failure (Redis down, stale generation) means no output, so normal work is untouched."""
    try:
        cfg = load(cfg_path)
        r = connect(cfg, socket_timeout=2)
        k = key('agent', name)
        info = r.hgetall(k)
        if info.get('midturn_send_pending') != '1' and str(info.get('generation')) == str(generation):
            return split_nudge(r, k, info)
        if info.get('midturn_send_pending') != '1' or str(info.get('generation')) != str(generation):
            return None
        prompt = midturn.registered_prompt(info, info['generation'])
        if not prompt:
            return None
        # Take it once: only the call that flips 1 -> 0 sends it (several tool calls can finish together).
        if not r.eval("if redis.call('HGET', KEYS[1], 'midturn_send_pending') == '1' then "
                      "redis.call('HSET', KEYS[1], 'midturn_send_pending', '0') "
                      "redis.call('HINCRBY', KEYS[1], 'midturn_type_attempts', 1) return 1 end return 0", 1, k):
            return None
        return prompt
    except Exception:
        return None


def split_nudge(r, k, info):
    """§44: the long-running nudge registered for this generation's current delivery, taken once."""
    prompt = info.get('split_prompt') or ''
    if (info.get('split_send_pending') != '1' or info.get('split_g') != str(info.get('generation'))
            or not info.get('split_message') or info.get('msg_id') != info['split_message']
            or not prompt.startswith(midturn.SPLIT_PREFIX + info.get('split_id', '') + '] ')):
        return None
    if not r.eval("if redis.call('HGET', KEYS[1], 'split_send_pending') == '1' then "
                  "redis.call('HSET', KEYS[1], 'split_send_pending', '0') return 1 end return 0", 1, k):
        return None
    return prompt


def claude_stop_failure(cfg_path, name, generation, raw):
    """Claude Code StopFailure hook (output ignored): an API error ended the turn, so no Stop hook
    waits for the next delivery. Record it. A v2 agent (worker, supervisor, advisor) also posts a ``stop_failure``
    event (no wait):
    the daemon settles its in-flight delivery as not adopted and holds only that slot for a backoff
    (docs/design.md §24). Task mode re-delivers the same instruction; service mode fails the request
    (never re-sent) and, on a model refusal (``refusal``), gives the slot a fresh session.

    A clear-after-result worker (typed delivery, docs/design.md §28) instead posts a ``stop_failure``
    event: the daemon fails its in-flight request at once and clears the context."""
    data = json.loads(raw or '{}')
    cfg = load(cfg_path)
    if clear_after_result.enabled(cfg, name):
        return _claude_context_hook(cfg_path, name, generation, raw, 'stop_failure')
    fields = {'slot': name, 'generation': str(generation), 'error': str(data.get('error') or 'unknown')[:200],
              'session_id': str(data.get('session_id') or ''), 'level': 'warn',
              'text': str(data.get('last_assistant_message') or '')[-500:], 'at': str(int(time.time() * 1000))}
    if cfg.get('transport_version') == 2:
        # Route first, then connect (as claude_stop): a sub-fleet supervisor's turn end belongs in the child's Redis.
        cfg, name, generation = subfleet.delegated_route(cfg, name, int(generation))
    r = connect(cfg)
    try:
        if cfg.get('transport_version') == 2:
            bus = runtime_v2.bus_for(cfg, r)
            r.xadd(bus.audit_key, dict(fields, event='hook.stop_failure', run_id=bus.run_id, slot=name))
        else:
            r.rpush(LOG, json.dumps(dict(fields, time=time.time(), kind='hook.stop_failure'), ensure_ascii=False))
        if claude_hook.enabled(cfg, name, claude=True):
            # The turn ended without a Stop: a waiting rewake fallback may act again (TurnWatch.ended).
            # When it ended, by the transcript's clock: this hook runs after a queued entry may have
            # started the next turn, which the mark must not end.
            k = hook_keys(cfg, r, name, int(generation))
            ended = claude_hook.failure_end(claude_hook.tail_rows(data.get('transcript_path') or ''))
            r.set(k['turnend'], str(ended or time.time()), ex=claude_hook.BIND_TTL)
            if cfg.get('transport_version') == 2:
                # Workers, the supervisor (also a sub-fleet's adopted one, routed above) and the advisor all post it:
                # the daemon settles the turn (design.md §24 "Supervisor StopFailure"; the advisor takes §27's fault
                # branch). Before 2026-11-14 a supervisor posted nothing and stayed "busy" until the watchdog froze it.
                ev = dict(kind='stop_failure', agent=name, generation=int(generation), run_id=bus.run_id,
                          thread=data.get('session_id'), error=fields['error'],
                          text=str(data.get('last_assistant_message') or data.get('error_details') or '')[:1000],
                          refusal=claude_refused(data.get('transcript_path', '')),
                          # CLI-owned error fields (the safeguards class reads these, design section 23)
                          details=str(data.get('error_details') or '')[:1000], error_type=str(data.get('error') or ''),
                          api_text='')
                lease = r.hgetall(bus.lease_key(name))
                # Only a delivery this session's hook handed over ended with this turn; one still in the
                # mailbox belongs to a later turn (an operator turn failed meanwhile: the rewake hands it over).
                if lease.get('message_id') and hook_delivered(r, k, data.get('session_id'), lease):
                    msg = bus.message(lease['message_id'])
                    ev['delivery'] = {x: msg[x] for x in runtime_v2.IDENTITY}
                    ev['api_text'] = claude_api_error(data.get('transcript_path', ''), received_at(msg))[:1000]
                runtime_v2.post_event(bus, ev)
    finally:
        r.close()
    return True


def received_at(msg):
    """When the delivery reached the session (epoch seconds; the bus stores ms), or 0 when unknown."""
    try:
        at = float(msg.get('received_at') or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0
    return at / 1000.0 if at > 1e11 else at


def claude_api_error(transcript, since=0):
    """The text of this session's newest main-chain assistant row when the CLI wrote it as its own API error
    (``isApiErrorMessage``: e.g. "API Error: <model>'s safeguards flagged this message ..."), else ''. Model output is
    never returned: a reply quoting that sentence is not the CLI's error. ``since``: a row stamped before it belongs
    to an earlier turn (this turn's row may not be written yet when the hook runs) and is not returned.
    Unreadable means ''."""
    try:
        for line in reversed(_tail(Path(transcript or ''), 262144)):
            row = json.loads(line)
            if row.get('type') == 'assistant' and not row.get('isSidechain'):
                when = claude_hook.stamp(row)
                if not row.get('isApiErrorMessage') or (since and (when is None or when < since - 1)):
                    return ''
                content = (row.get('message') or {}).get('content')
                if isinstance(content, str):
                    return content
                return '\n'.join(str(c.get('text') or '') for c in content or () if isinstance(c, dict))
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return ''


def stop_failure_wall(ev):
    """design section 23: the walls class safeguards on a StopFailure event, from its CLI-owned text (``api_text`` =
    this turn's API-error row in the transcript, ``details`` = error_details). ``text`` (last_assistant_message) can be
    model output: it counts only when no API-error row of this turn could be read (the CLI may write it after the
    hook runs) AND the CLI's error type corroborates it (walls.safeguards_error_types, e.g. invalid_request). ``error``
    may fall back to the message text (clear-after-result hook) and is never read. Any other wall wording arriving
    through StopFailure keeps today's API-error handling (None)."""
    def safeguards(text):
        wall = brake.classify_wall(text, safeguards='cli')
        return wall if wall and wall.get('kind') == brake.SAFEGUARDS else None
    wall = safeguards('\n'.join(str(ev.get(k) or '') for k in ('api_text', 'details')))
    if wall or ev.get('api_text'):
        return wall
    if str(ev.get('error_type') or '') in brake.wall_table().get('safeguards_error_types', ()):
        return safeguards(str(ev.get('text') or ''))
    return None


def claude_refused(transcript):
    """Did this session's last main-chain assistant message end as a model refusal (``stop_reason: refusal``)?
    Read from the transcript tail; unreadable means no."""
    try:
        for line in reversed(_tail(Path(transcript or ''), 262144)):
            row = json.loads(line)
            if row.get('type') == 'assistant' and not row.get('isSidechain'):
                return (row.get('message') or {}).get('stop_reason') == 'refusal'
    except (OSError, ValueError, AttributeError):
        pass
    return False

# ---------------------------------------------------------------- trust gate
def validate_artifacts(artifacts, extended=False):
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError('artifacts must be a non-empty list')
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or 'checks' in item:
            raise ValueError('an artifact may only have path / min_chars / contains')
        path = Path(item['path'])
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise ValueError('artifact path must stay inside the workspace')
        minimum = item.get('min_chars', 1)
        if type(minimum) is not int or not 1 <= minimum <= 8_000_000:
            raise ValueError('invalid min_chars')
        contains = item.get('contains', [])
        if not isinstance(contains, list) or not all(isinstance(x, str) for x in contains):
            raise ValueError('contains must be a list of literal strings')
        if not extended:
            continue  # extended keys (not_contains / json_keys / cmd_exit) stay ignored, as before
        banned = item.get('not_contains', [])
        if not isinstance(banned, list) or not all(isinstance(x, str) and x for x in banned):
            raise ValueError('not_contains must be a list of literal strings')
        keys = item.get('json_keys')
        if keys is not None and (not isinstance(keys, list) or not keys
                                 or not all(isinstance(x, str) and x for x in keys)):
            raise ValueError('json_keys must be a non-empty list of key names')
        argv = item.get('cmd_exit')
        if argv is not None and (not isinstance(argv, list) or not argv
                                 or not all(isinstance(x, str) and x and '\x00' not in x for x in argv)):
            raise ValueError('cmd_exit must be a non-empty argv list of strings (no shell)')
        timeout = item.get('timeout_s', CMD_TIMEOUT)
        if type(timeout) is not int or not 1 <= timeout <= CMD_TIMEOUT_MAX:
            raise ValueError('timeout_s must be 1-' + str(CMD_TIMEOUT_MAX) + ' seconds')


def run_check_command(item, root, path):
    """Contract tasks: run an artifact's cmd_exit argv (no shell) in the workspace with a hard timeout
    and write its combined output to the artifact path; the literal checks then read that output.
    Returns a failure text or None. The daemon is blocked meanwhile, hence the short cap."""
    timeout = item.get('timeout_s', CMD_TIMEOUT)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(item['cmd_exit'], cwd=root, capture_output=True, text=True, errors='replace',
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        path.write_text('timed out after ' + str(timeout) + ' s\n')
        return 'command timed out after ' + str(timeout) + ' s: ' + item['path']
    except OSError as exc:
        return 'command could not start (' + str(exc) + '): ' + item['path']
    path.write_text(((done.stdout or '') + (done.stderr or ''))[-2_000_000:])
    return None if done.returncode == 0 else 'command exited ' + str(done.returncode) + ': ' + item['path']


def json_key_failures(text, keys):
    """json_keys: the file is one JSON object, or ends with one (a report closing with a verdict
    line like {"verdict": "PASS", "p0": 0}); every key must be present."""
    try:
        obj = json.loads(text)
    except ValueError:
        obj = contracts.last_json_object(text)[0]
    if not isinstance(obj, dict):
        return ['no JSON object (whole file or final line)']
    return ['missing JSON key ' + k for k in keys if k not in obj]


def check_artifacts(artifacts, root, extended=False, run_commands=False, commands=None):
    """Literal file checks only (exists / size / contains); returns failures (empty = pass).
    ``extended`` (contract tasks and frozen artifacts) adds not_contains, json_keys and cmd_exit;
    commands run only when ``run_commands`` (the COMPLETED gate), never from result.json / resume re-checks,
    and only those whose command_key is in ``commands`` (the caller's declared set); any other is reported, not run."""
    root = Path(root).resolve()
    failures = []
    for item in artifacts:
        path = (root / item['path']).resolve()
        if not path.is_relative_to(root):
            failures.append('outside workspace: ' + item['path'])
            continue
        if extended and run_commands and item.get('cmd_exit'):
            if command_key(item) not in (commands or ()):
                failures.append('cmd_exit not declared by the caller, not run: ' + item['path'])
                continue
            problem = run_check_command(item, root, path)
            if problem:
                failures.append(problem)
                continue
        if not path.is_file():
            failures.append('artifact not written: ' + item['path'])
        elif path.stat().st_size > 32_000_000:
            failures.append('artifact too large to check: ' + item['path'])
        else:
            text = path.read_text(errors='replace')
            banned = [x for x in item.get('not_contains', []) if x in text] if extended else []
            if len(text) < item.get('min_chars', 1) or not all(c in text for c in item.get('contains', [])):
                failures.append('artifact below requirements (length / required text): ' + item['path'])
            elif banned:
                failures.append('artifact contains forbidden text ' + json.dumps(banned, ensure_ascii=False)
                                + ': ' + item['path'])
            elif extended and item.get('json_keys'):
                failures += [x + ': ' + item['path'] for x in json_key_failures(text, item['json_keys'])]
    return failures


def extended_checks(task):
    """Contract tasks and caller-frozen artifacts use the extended checks (not_contains / json_keys / cmd_exit)."""
    return task.get('contracts') == '1' or task.get('artifacts_frozen') == '1'


def command_key(item):
    """Identity of an artifact's acceptance command: path + argv + timeout (all three are what runs)."""
    return json.dumps([item.get('path'), item.get('cmd_exit'), item.get('timeout_s', CMD_TIMEOUT)])


def caller_commands(task):
    """The cmd_exit commands the CALLER declared in the task spec (stored apart from the merged artifacts
    list at submit). A task stored without that field (older daemon) has none: nothing runs."""
    return {command_key(a) for a in json.loads(task.get('caller_artifacts') or '[]')
            if isinstance(a, dict) and a.get('cmd_exit')}


def command_guard(task, decision):
    """Supervisor decisions may not add or replace a cmd_exit: only an artifact identical to one the caller
    declared may carry one. Raises ValueError (the normal correction / three-strike path)."""
    allowed = caller_commands(task)
    for item in decision.get('artifacts') or []:
        if isinstance(item, dict) and 'cmd_exit' in item and command_key(item) not in allowed:
            raise ValueError('cmd_exit on ' + str(item.get('path')) + ' was not declared by the caller: a '
                             'supervisor decision may not add or replace acceptance commands (drop cmd_exit, or '
                             'repeat the caller\'s artifact unchanged)')


def validate_contract(raw):
    """Task spec ``contract``: {"artifacts_frozen": bool}. Returns the frozen flag."""
    raw = {} if raw is None else raw
    if (not isinstance(raw, dict) or set(raw) - {'artifacts_frozen'}
            or type(raw.get('artifacts_frozen', False)) is not bool):
        raise ValueError('contract may only have artifacts_frozen (true or false)')
    return raw.get('artifacts_frozen', False)


def validate_agent_registry_path(value):
    """Validate a task-provided registry path without shell or environment expansion."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError('agent_registry must be a non-empty directory path')
    try:
        path = Path(value.strip()).expanduser()
        if not path.is_dir():
            raise ValueError('agent_registry must exist and be a directory: ' + str(path))
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith('agent_registry '):
            raise
        raise ValueError('invalid agent_registry path: ' + str(exc)) from exc
    return path


def roster_file(cfg):
    """The roster the daemon follows now: the night roster while night is on (§25), else --roster, else
    <run>/roster.yaml (created by the advisor switch when absent)."""
    followed = night.roster_for(cfg)
    return Path(followed) if followed else Path(cfg['run']) / 'roster.yaml'


def roster_notice(cfg):
    """{{ROSTER}} in the supervisor prompt: the roster file(s) it may edit to add / retire workers (§16)."""
    if not cfg.get('roster'):
        return ('This run has no roster file (it was started with --workers / --worker-names): adding or retiring '
                'workers is not available in this run; skip the rest of this section.')
    text = ("This run's roster file is `" + str(cfg['roster']) + '`. You may edit it while the run goes on to add or '
            'retire your own workers.')
    if cfg.get('roster_night'):
        text += (' At night the daemon follows `' + str(cfg['roster_night']) + '` instead: make the same `workers:` '
                 'change in both files, or the day / night switch is refused.')
    return text


def advisor_rosters(cfg):
    """Every roster file the advisor switch writes: the followed one first, then the other of a day /
    night pair, so a night on / off never flips the advisor (it is per run, not per roster)."""
    paths = [roster_file(cfg)]
    for other in (cfg.get('roster'), cfg.get('roster_night')):
        if other and Path(other) not in paths:
            paths.append(Path(other))
    return paths


def parse_decision(text, workers, others_active, tree_ctx=None):
    """``tree_ctx`` (docs/design.md §62): None = the decomposition tree is off and nothing below changes; else
    {tree, root, prefix, params, running}: the tree keys are checked (decompose.validate_keys) and a valid tree
    change is the reply's progress, so it is not refused as "no worker is busy"."""
    decision = json.loads(text[text.index('{'):text.rindex('}') + 1])
    if not isinstance(decision, dict) or 'checks' in decision:
        raise ValueError('decision must be a JSON object and may not submit host commands (checks)')
    if decision.get('status') not in ('RUNNING', 'COMPLETED', 'BLOCKED'):
        raise ValueError('status must be RUNNING / COMPLETED / BLOCKED')
    completion = decision.get('completion')
    if isinstance(completion, bool) or not isinstance(completion, int) or not 0 <= completion <= 100:
        raise ValueError('completion must be an integer 0-100')
    dispatch = decision.setdefault('dispatch', [])
    if not isinstance(dispatch, list):
        raise ValueError('dispatch must be a list')
    for item in dispatch:
        if not isinstance(item, dict) or item.get('to') not in workers:
            raise ValueError('dispatch.to must be one of ' + ', '.join(workers))
        if not isinstance(item.get('instruction'), str) or len(item['instruction'].strip()) < 12:
            raise ValueError('instruction must state the action and the expected output (>= 12 chars)')
        if tree_ctx is None and 'id' in item and (not isinstance(item['id'], str)
                                                  or not re.fullmatch(contracts.M_RE, item['id'])):
            raise ValueError('every dispatch item needs an id (M<n>, e.g. M0)')  # §62 on: the tree checks the id
        if 'advise_note' in item and not isinstance(item['advise_note'], str):
            raise ValueError('dispatch.advise_note must be text')
        if 'fresh' in item and type(item['fresh']) is not bool:
            raise ValueError('dispatch.fresh must be true or false')
        item.pop('review', None)  # an older supervisor's review marker: read and ignored (no review gate)
        if 'leaf' in item and type(item['leaf']) is not bool:
            raise ValueError('dispatch.leaf must be true or false')
    escalations = validate_escalations(decision)
    report_up = validate_report_up(decision, workers)
    if 'advisor' in decision and decision['advisor'] not in ('on', 'off'):
        raise ValueError('advisor must be "on" or "off"')
    consult = decision.setdefault('consult', [])
    validate_consult(consult, workers)
    if 'artifacts' in decision:
        validate_artifacts(decision['artifacts'])
    withdraw = decision.setdefault('withdraw', [])
    if not isinstance(withdraw, list) or any(w not in workers for w in withdraw):
        raise ValueError('withdraw must be a list of worker names: ' + ', '.join(workers))
    if withdraw and decision['status'] != 'COMPLETED':
        raise ValueError('withdraw is only for COMPLETED: it drops the unsettled work of the listed workers')
    if 'waiting' in decision:
        decision['waiting'] = validate_waiting(decision['waiting'], decision)
    if decision.get('reconcile') is not None:  # section 60: rulings on the reconcile ledger
        restart_state.validate_decision(decision['reconcile'])
    if decision.get('heal') is not None:  # §67: reopen a sub-fleet supervisor / an own slot, clear a wall record
        heal.validate_decision(decision['heal'])
    if tree_ctx is not None and decompose.validate_keys(decision, tree_ctx):
        return decision
    if (decision['status'] == 'RUNNING' and not dispatch and not escalations and not consult and not others_active
            and not decision.get('waiting') and not report_up and not decision.get('reconcile')
            and not decision.get('heal')):
        raise ValueError('no worker is busy: RUNNING must dispatch at least one instruction')
    return decision


def validate_waiting(value, decision):
    """`decision['waiting']` (M15): an explicit, bounded dependency wait for a RUNNING reply with no dispatch.

    Shape: `{"for": <what external evidence>, "check": <how the supervisor will know it arrived>, "for_hours": 2}`.
    `for` / `check` are descriptions for the supervisor's own next turn — the daemon never executes them, so a
    shell command or a path is refused as a shape error, not run. `for_hours` is a finite number in (0, 12],
    default 2, and is clamped to the task's own deadline when the wait is recorded (`Fleet.apply_waiting`).
    """
    if not isinstance(value, dict):
        raise ValueError('waiting must be an object: {"for": "...", "check": "...", "for_hours": 2}')
    unknown = sorted(set(value) - {'for', 'check', 'for_hours'})
    if unknown:
        raise ValueError('waiting has unknown keys: ' + ', '.join(unknown) + ' (only for / check / for_hours)')
    if decision.get('status') != 'RUNNING':
        raise ValueError('waiting is only for RUNNING: a task that is ending does not wait')
    if decision.get('dispatch'):
        raise ValueError('waiting needs an empty dispatch: declare the wait instead of dispatching filler work')
    for key, what in (('for', 'what external evidence you are waiting for'),
                      ('check', 'how you will know that evidence arrived')):
        text = value.get(key)
        if not isinstance(text, str) or len(text.strip()) < 12:
            raise ValueError('waiting.' + key + ' must state ' + what + ' (>= 12 chars)')
        if re.match(r'^(\s|/|~|\./|\$)', text) or re.match(
                r'^\s*(sudo|rm|mv|cp|cd|curl|wget|bash|sh|zsh|python3?|node|git|ssh|scp|kill|pkill|launchctl|systemctl)\b',
                text):
            raise ValueError('waiting.' + key + ' is a description, not a host command or a path')
    hours = value.get('for_hours', 2)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) \
            or not 0 < hours <= 12:
        raise ValueError('waiting.for_hours must be a number > 0 and <= 12 (default 2)')
    return {'for': value['for'].strip(), 'check': value['check'].strip(), 'for_hours': hours}


def task_waiting(task):
    """The task's recorded `waiting` declaration (M15) as a dict, `{}` when there is none or it is unreadable."""
    raw = (task or {}).get('waiting') or ''
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def task_deadline(task, now=None):
    """The task's deadline in awake seconds (0 = none; fleetmux/awake.py, §35: the time the machine slept does not
    use the ``hours`` budget). While a sub-fleet waits for the top supervisor's ruling on a report_up (§46) its
    clock is stopped: ``report_wait_at`` (awake) is set and the deadline moves with the time. ``now``: awake."""
    deadline = float((task or {}).get('deadline') or 0)
    since = (task or {}).get('report_wait_at')
    if deadline and since:
        deadline += max(0.0, (awake.now() if now is None else now) - float(since))
    return deadline


def task_clock(task, now=None):
    """``status``: add ``clock_paused_s`` (time the task clock stood still for report_up rulings, §46) and
    ``clock`` (paused / running) to the task hash ``task`` when its clock was ever stopped, and the budget in
    awake time: ``budget_remaining_s`` and, from ``budget_s``, ``budget_used_s``; returns it. ``now``: the fleet's
    awake time (``awake.of(r).now()`` outside the daemon)."""
    if not isinstance(task, dict):
        return task
    now = awake.now() if now is None else now
    if task.get('report_wait_at') or task.get('report_wait_s'):
        since = float(task.get('report_wait_at') or 0)
        waited = now - since if since else 0
        task['clock_paused_s'] = round(float(task.get('report_wait_s') or 0) + waited, 1)
        task['clock'] = 'paused: report_up awaiting the top ruling' if since else 'running'
    deadline = task_deadline(task, now)
    if deadline:
        left = max(0.0, deadline - now)
        task['budget_remaining_s'] = round(left, 1)
        if task.get('budget_s'):
            task['budget_used_s'] = round(max(0.0, float(task['budget_s']) - left), 1)
    return task


def validate_escalations(decision):
    """decision['escalations']: answers to [escalate] (task mode), [dialog] and [blocked] requests."""
    escalations = decision.setdefault('escalations', [])
    if not isinstance(escalations, list):
        raise ValueError('escalations must be a list')
    for item in escalations:
        if (not isinstance(item, dict) or not isinstance(item.get('task'), str)
                or item.get('decision') not in ESCALATION_DECISIONS):
            raise ValueError('escalations: every item needs task and decision approve / revise / reject')
        workers_n = item.get('workers', 1)
        if isinstance(workers_n, bool) or not isinstance(workers_n, int) or workers_n < 1:
            raise ValueError('escalations: workers must be a positive integer')
        if not isinstance(item.get('note', ''), str):
            raise ValueError('escalations: note must be text')
        # §44: an approve may pick the models of the split (shape here; cards, hosts, capacity at apply)
        if ('supervisor_profile' in item or 'worker_profiles' in item) and item['decision'] != 'approve':
            raise ValueError('escalations: supervisor_profile / worker_profiles: only an approve picks models')
        if 'supervisor_profile' in item and not (isinstance(item['supervisor_profile'], str)
                                                 and re.fullmatch(roster.PROFILE_RE, item['supervisor_profile'])):
            raise ValueError('escalations: supervisor_profile must be an agent card id')
        wps = item.get('worker_profiles')
        if 'worker_profiles' in item and not (isinstance(wps, list) and wps and all(
                isinstance(p, str) and re.fullmatch(roster.PROFILE_RE, p) for p in wps)):
            raise ValueError('escalations: worker_profiles must be a list of agent card ids, one per sub-worker')
        if wps and 'workers' in item and item['workers'] != len(wps):
            raise ValueError('escalations: worker_profiles has ' + str(len(wps)) + ' entries but workers is '
                             + str(item['workers']))
        if 'paths' in item:  # §46: an approve of a sub-fleet report may narrow the paths granted (checked at apply)
            if item['decision'] != 'approve':
                raise ValueError('escalations: paths: only an approve grants paths')
            item['paths'] = grant_paths(item['paths'], 'escalations')
    if len({x['task'] for x in escalations}) != len(escalations):
        raise ValueError('escalations: one answer per task')
    return escalations


def validate_report_up(decision, workers=None):
    """decision['report_up'] (§46): what a sub-fleet supervisor may not grant itself, sent to the top supervisor.
    An item's optional ``paths`` (absolute, written to like a roster ``writable:`` root) become a grant the program
    issues to ``who`` when the top approves; so ``paths`` needs ``who``, one of ``workers``."""
    items = decision.setdefault('report_up', [])
    if not isinstance(items, list):
        raise ValueError('report_up must be a list of {"what": "...", "why": "...", "who": "worker-N"}')
    for item in items:
        if not (isinstance(item, dict) and all(isinstance(item.get(k), str) and item[k].strip() for k in ('what', 'why'))):
            raise ValueError('report_up: every item needs what and why (non-empty text)')
        if not isinstance(item.get('who', ''), str):
            raise ValueError('report_up: who must be a worker name')
        if 'paths' in item:
            item['paths'] = grant_paths(item['paths'], 'report_up')
            if not item.get('who', '').strip():
                raise ValueError('report_up: paths need who (the worker the program grants them to)')
            if workers is not None and item['who'].strip() not in workers:
                raise ValueError('report_up: who must be one of ' + ', '.join(workers))
    if items and decision.get('status', 'RUNNING') != 'RUNNING':
        raise ValueError('report_up is only for RUNNING: the piece waits for the top supervisor\'s ruling')
    return items


def grant_paths(value, where):
    """§46: a non-empty list of absolute local paths (normalised), the write grant asked for / approved."""
    if not (isinstance(value, list) and value and all(isinstance(p, str) and os.path.isabs(p) for p in value)):
        raise ValueError(where + ': paths must be a non-empty list of absolute paths (what the worker may write)')
    return list(dict.fromkeys(os.path.normpath(p) for p in value))


def path_within(path, roots):
    """True when ``path`` is one of ``roots`` or inside one (both normalised absolute paths)."""
    return any(path == r or path.startswith(r.rstrip('/') + '/') for r in roots)


def service_blocked(value, reply):
    """A service worker's 'I am stuck' answer (plan F' 8.4 in service mode): exactly {"blocked": "<text>"}, when
    the request's own reply keys do not use 'blocked'. Returns the reason, or '' for an ordinary answer."""
    if not isinstance(value, dict) or set(value) != {'blocked'} or not isinstance(value['blocked'], str):
        return ''
    if 'blocked' in reply.get('required', []) or 'blocked' in (reply.get('allowed') or []):
        return ''
    reason = value['blocked'].strip()
    return '' if reason.rstrip('.。').strip().lower() in NO_BLOCK else reason


SPLIT_KEYS = ('escalate', 'proposal', 'workers')


def service_split(value, reply):
    """A service worker's 'this request is too big' answer (brief v2.1 #3, the §44 escalate shape): keys within
    {escalate, proposal, workers}, escalate and proposal present, none of them a key of the request's own reply.
    Returns the reason ('' for an ordinary answer); the proposal itself is checked by escalate_check."""
    if not isinstance(value, dict) or not {'escalate', 'proposal'} <= set(value) or not set(value) <= set(SPLIT_KEYS):
        return ''
    if set(SPLIT_KEYS) & (set(reply.get('required', [])) | set(reply.get('allowed') or [])):
        return ''
    return value['escalate'].strip() if isinstance(value['escalate'], str) and value['escalate'].strip() else '-'


def validate_consult(consult, workers):
    """Supervisor's mid-task consult items (advisor plan §3.2): {worker, task, finding, want?}."""
    if not isinstance(consult, list):
        raise ValueError('consult must be a list')
    for item in consult:
        if not isinstance(item, dict) or item.get('worker') not in workers:
            raise ValueError('consult.worker must be one of ' + ', '.join(workers))
        if not isinstance(item.get('task'), str) or not re.fullmatch(contracts.M_RE, item['task']):
            raise ValueError('consult.task must be the dispatch id of the task (M<n>, e.g. M0)')
        if not isinstance(item.get('finding'), str) or not item['finding'].strip():
            raise ValueError('consult.finding must say in your own words what went wrong')
        if not isinstance(item.get('want', ''), str):
            raise ValueError('consult.want must be text')
    if len({x['task'] for x in consult}) != len(consult):
        raise ValueError('consult: one item per task')


def validate_request(spec, workers):
    """A service request: {id, payload, reply: {required, allowed?}, to?, instruction?, timeout_s?}."""
    if not isinstance(spec, dict):
        raise ValueError('a request must be a JSON object')
    if not isinstance(spec.get('id'), str) or not re.fullmatch(REQ_ID_RE, spec['id']):
        raise ValueError('id must match ' + REQ_ID_RE)
    if 'payload' not in spec or len(json.dumps(spec['payload'], ensure_ascii=False)) > 200_000:
        raise ValueError('payload is required and must be at most 200000 chars as JSON')
    reply = spec.get('reply')
    if not isinstance(reply, dict) or not isinstance(reply.get('required'), list) or not reply['required'] \
            or not all(isinstance(k, str) and k for k in reply['required']):
        raise ValueError('reply.required must be a non-empty list of key names')
    allowed = reply.get('allowed')
    if allowed is not None and (not isinstance(allowed, list) or not all(isinstance(k, str) for k in allowed)
                                or not set(reply['required']) <= set(allowed)):
        raise ValueError('reply.allowed must be a list of key names containing every required key')
    if spec.get('to') is not None and spec['to'] not in workers:
        raise ValueError('to must be one of ' + ', '.join(workers))
    if spec.get('instruction') is not None and not isinstance(spec['instruction'], str):
        raise ValueError('instruction must be a string')
    timeout = spec.get('timeout_s')
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not 10 <= timeout <= 86400):
        raise ValueError('timeout_s must be a number of seconds between 10 and 86400')
    if 'replay' in spec and type(spec['replay']) is not bool:
        raise ValueError('replay must be true or false (false: never re-run it on start --resume)')


def request_limit(spec, cfg):
    """Seconds a service request may take: its own ``timeout_s``, else the run's ``--request-timeout``. None: no
    wall-clock deadline (docs/design.md §43) -- health is the watchdog's; a deadline is the caller's freshness limit."""
    limit = spec.get('timeout_s') or cfg.get('request_timeout')
    return float(limit) if limit else None


def parse_reply(text, reply):
    """A worker's answer to a request: one JSON object with the required keys (and only allowed ones)."""
    try:
        obj = json.loads(text[text.index('{'):text.rindex('}') + 1])
    except ValueError:
        raise ValueError('no JSON object found')
    if not isinstance(obj, dict):
        raise ValueError('the reply must be a JSON object')
    missing = [k for k in reply['required'] if k not in obj]
    if missing:
        raise ValueError('missing keys: ' + ', '.join(missing))
    if reply.get('allowed') is not None:
        extra = sorted(set(obj) - set(reply['allowed']))
        if extra:
            raise ValueError('keys not allowed: ' + ', '.join(extra))
    return obj


def supervisor_split_check(item):
    """§44「监工直接开子 fleet」: shape of a dispatch item's ``split`` (ValueError). The worker named in ``to`` becomes
    the sub-fleet's supervisor; the program numbers the request M<n> and the pieces M<n>.<i>."""
    split = item['split']
    if not isinstance(split, dict) or not isinstance(split.get('reason'), str) or not split['reason'].strip():
        raise ValueError('dispatch.split must be an object with a one-sentence reason')
    proposal = split.get('proposal')
    if not isinstance(proposal, list) or not proposal or not all(
            isinstance(x, dict) and isinstance(x.get('instruction'), str) and len(x['instruction'].strip()) >= 12
            and isinstance(x.get('output'), str) and x['output'].strip() for x in proposal):
        raise ValueError('dispatch.split.proposal must be a non-empty list of {instruction (>= 12 chars), output}')
    workers = split.get('workers', len(proposal))
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError('dispatch.split.workers must be a positive integer')
    if 'supervisor_profile' in split and not isinstance(split['supervisor_profile'], str):
        raise ValueError('dispatch.split.supervisor_profile must be an agent card id')
    wps = split.get('worker_profiles')
    if wps is not None and not (isinstance(wps, list) and all(isinstance(x, str) for x in wps)):
        raise ValueError('dispatch.split.worker_profiles must be a list of agent card ids')
    if item.get('fresh'):
        raise ValueError('dispatch.split and dispatch.fresh do not go together (the split hands the worker over)')


def parse_service_decision(text, workers):
    decision = json.loads(text[text.index('{'):text.rindex('}') + 1])
    if not isinstance(decision, dict) or 'checks' in decision:
        raise ValueError('decision must be a JSON object and may not submit host commands (checks)')
    if decision.setdefault('status', 'RUNNING') not in ('RUNNING', 'BLOCKED'):
        raise ValueError('status must be RUNNING / BLOCKED')
    for field in ('dispatch', 'skip', 'answer'):
        if not isinstance(decision.setdefault(field, []), list):
            raise ValueError(field + ' must be a list')
    for item in decision['dispatch']:
        if not isinstance(item, dict) or item.get('to') not in workers:
            raise ValueError('dispatch.to must be one of ' + ', '.join(workers))
        if not isinstance(item.get('request_id'), str):
            raise ValueError('every dispatch item needs the request_id it answers')
        if not isinstance(item.get('instruction'), str) or len(item['instruction'].strip()) < 12:
            raise ValueError('instruction must state the action (>= 12 chars)')
        if 'fresh' in item and type(item['fresh']) is not bool:
            raise ValueError('dispatch.fresh must be true or false')
        if 'split' in item:
            supervisor_split_check(item)
    for item in decision['skip']:
        if not isinstance(item, dict) or not isinstance(item.get('request_id'), str) \
                or not isinstance(item.get('reason'), str):
            raise ValueError('every skip item needs request_id and reason')
    for item in decision['answer']:  # the supervisor answers the request itself; the reply schema is the daemon's
        if not isinstance(item, dict) or not isinstance(item.get('request_id'), str) \
                or not isinstance(item.get('result'), dict):
            raise ValueError('every answer item needs request_id and a result object')
    seen = {}
    for field in ('dispatch', 'skip', 'answer'):
        for rid in {item['request_id'] for item in decision[field]}:
            if rid in seen:
                raise ValueError('request ' + rid + ' is in both ' + seen[rid] + ' and ' + field
                                 + ': dispatch, skip or answer each request once')
            seen[rid] = field
    for item in validate_escalations(decision):  # plan F' [dialog] / [blocked], §46 report, P7 split M<n> (§44)
        if not (item['task'].startswith((DIALOG_KEY, BLOCKED_KEY, REPORT_KEY))
                or re.fullmatch(contracts.M_RE, item['task'])):
            raise ValueError('escalations: service mode answers only M<n> (a split) / dialog:<worker>:g<n> / '
                             'blocked:<worker> / report:<task>:<id>')
    validate_report_up(decision, workers)
    if decision.get('subfleet') is not None:  # §29: approve / reject one external sub-fleet up / down
        named_subfleet.validate_decision(decision['subfleet'])
    if decision.get('applet') is not None:  # applet-interface.md 3.5: lease / escalation / control answers
        applet_lease.validate_decision(decision['applet'])
    if decision.get('reconcile') is not None:  # section 60: rulings on the reconcile ledger
        restart_state.validate_decision(decision['reconcile'])
    if decision.get('heal') is not None:  # §67
        heal.validate_decision(decision['heal'])
    return decision


# ------------------------------------------------------------ codex rollouts
def rollouts(thread, sessions):
    base = Path(sessions)
    return sorted(base.glob('*/*/*/*' + thread + '*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)


def _meta(path):
    with path.open() as f:
        return json.loads(f.readline()).get('payload', {})


def _is_subagent(meta):
    return isinstance(meta.get('source'), dict) and 'subagent' in meta['source']


def _tail(path, size):
    with path.open('rb') as f:
        f.seek(max(0, path.stat().st_size - size))
        return f.read().decode('utf-8', 'replace').splitlines()


def completed_turn(cwd, data, sessions):
    """Codex also notifies for title-generation calls; require a real task_complete."""
    thread = data.get('thread-id') or data.get('thread_id')
    if not isinstance(thread, str) or not re.fullmatch(THREAD_RE, thread):
        return False
    text = (data.get('last-assistant-message') or data.get('last_assistant_message') or '').strip()
    turn = data.get('turn-id') or data.get('turn_id')
    for path in rollouts(thread, sessions):  # newest chunk of a resumed thread wins
        try:
            meta = _meta(path)
            # Codex records the real path (/tmp is /private/tmp on macOS); compare resolved paths.
            if _is_subagent(meta) or os.path.realpath(meta.get('cwd') or '') != os.path.realpath(cwd):
                continue
            for line in reversed(_tail(path, 262144)):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                payload = row.get('payload', {})
                if turn:
                    # Known turn id: judge that turn's own end, even if a later (queued) turn has started since.
                    if row.get('type') == 'event_msg' and payload.get('type') == 'task_complete' \
                            and payload.get('turn_id') == turn:
                        return (payload.get('last_agent_message') or '').strip() == text
                    if row.get('type') == 'event_msg' and payload.get('type') == 'task_started' and (
                            (payload.get('turn_attribution') or {}).get('turn_id') or payload.get('turn_id')) == turn:
                        return False  # this turn has not finished (yet)
                    continue
                if row.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                    return False
                if row.get('type') == 'event_msg' and payload.get('type') == 'task_complete':
                    return (payload.get('last_agent_message') or '').strip() == text
            return False
        except (OSError, ValueError):
            continue
    return False


def active_children(thread, sessions):
    """Native Codex subagents spawned by this thread that have not finished yet."""
    base = Path(sessions)
    roots = rollouts(thread, base) if thread else []
    if not roots:
        return []
    cutoff = getattr(roots[-1].stat(), 'st_birthtime', 0)
    children = {}
    for path in base.glob('*/*/*/*.jsonl'):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            meta = _meta(path)
            parent = meta.get('source', {}).get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
            if parent:
                children.setdefault(parent, []).append((meta.get('id'), path))
        except (OSError, ValueError, AttributeError):
            continue
    queue, busy, seen = [thread], [], set()
    while queue:
        parent = queue.pop()
        if parent in seen:
            continue
        seen.add(parent)
        for child, path in children.get(parent, []):
            queue.append(child)
            status = None
            for line in reversed(_tail(path, 65536)):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                kind = row.get('payload', {}).get('type')
                if row.get('type') == 'event_msg' and kind in ('task_started', 'task_complete', 'turn_aborted'):
                    status = kind
                    break
            if status not in ('task_complete', 'turn_aborted'):
                busy.append(child)
    return busy


# ------------------------------------------------------------ actor side
def notify(cfg_path, name, generation, payload, sessions=None):
    """Codex notify hook. The generation is baked into argv at spawn, so a late callback
    from a retired session keeps its old generation and is dropped as stale."""
    data = json.loads(payload)
    if data.get('type') != 'agent-turn-complete':
        return False
    cfg = load(cfg_path)
    sessions = sessions or sessions_dir(cfg)
    for _ in range(10):
        if completed_turn(agent_cwd(cfg, name), data, sessions):
            break
        time.sleep(.2)
    else:
        return False  # title/auxiliary call, not a turn end
    r = connect(cfg)
    digest = hashlib.sha256((name + str(generation) + payload).encode()).hexdigest()
    if not r.set('fx:seen:' + digest, '1', nx=True, ex=86400):
        return False
    if cfg.get('transport_version') == 2:
        r.close(); r.connection_pool.disconnect()
        inputs = data.get('input-messages') or data.get('input_messages') or []
        cfg, name, generation = subfleet.delegated_route(cfg, name, int(generation))
        return tui_turn(cfg, name, int(generation), thread=data.get('thread-id') or data.get('thread_id'),
                        transcript='', text=data.get('last-assistant-message') or data.get('last_assistant_message') or '',
                        inputs=inputs if isinstance(inputs, list) else [],
                        turn=data.get('turn-id') or data.get('turn_id') or '')
    r.rpush(EVENTS, json.dumps({'kind': 'turn', 'agent': name, 'generation': int(generation),
                                'thread': data.get('thread-id') or data.get('thread_id'),
                                'turn': data.get('turn-id') or data.get('turn_id') or '',
                                'text': data.get('last-assistant-message') or data.get('last_assistant_message') or '',
                                'at': time.time()}, ensure_ascii=False))
    return True


class Prompt(str):
    """A transcript prompt with where it came from: ``hook`` (a Stop-hook feedback or rewake row, i.e. a
    hook-channel delivery) or ``user`` (typed / CLI argument). Plain strings count as ``hook``.
    ``at``: the byte offset of its transcript row (None when unknown)."""
    origin, at = 'hook', None


def claude_user_prompts(path, count=3):
    """Last real user prompts of a Claude transcript (not tool results, meta or sidechains), as ``Prompt``.

    Scan the whole file like the v1 pointer scan: tool output after the prompt can be
    megabytes, so a tail read would lose the prompt and silently drop the settlement.
    """
    out = collections.deque(maxlen=count)
    try:
        with open(path, 'rb') as f:
            end = 0
            for raw in f:
                at, end = end, end + len(raw)
                line = raw.decode(errors='replace')
                if '"user"' not in line and '"queued_command"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get('isSidechain'):
                    continue
                if row.get('type') == 'attachment':
                    # A rewake notification Claude Code absorbed into the running turn has no user row, only a
                    # queued_command attachment: still a hook-channel delivery. Other absorbed lines are skipped.
                    att = row.get('attachment') if isinstance(row.get('attachment'), dict) else {}
                    came = att.get('origin') if isinstance(att.get('origin'), dict) else {}
                    text = (claude_hook.rewake_text(att.get('prompt')) if att.get('type') == 'queued_command'
                            and came.get('kind') == 'task-notification' else None)
                    if text:
                        prompt = Prompt(text)
                        prompt.origin, prompt.at = 'hook', at
                        out.append(prompt)
                    continue
                if row.get('type') != 'user':
                    continue
                message, came = row.get('message'), row.get('origin')
                content, origin = (message if isinstance(message, dict) else {}).get('content'), 'user'
                if (came if isinstance(came, dict) else {}).get('kind') == 'task-notification':
                    # A finished background task continues the current turn's work; a hook-channel
                    # rewake (Stop hook feedback notification) is a delivery.
                    content, origin = claude_hook.rewake_text(content), 'hook'
                    if content is None:
                        continue
                if row.get('isMeta'):  # only a hook-channel delivery (Stop-hook block reason) counts
                    content, origin = claude_hook.feedback_text(content), 'hook'
                if isinstance(content, list):
                    content = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
                if isinstance(content, str) and content.strip() and not content.startswith('[Request interrupted by user'):
                    prompt = Prompt(content)
                    prompt.origin, prompt.at = origin, at
                    out.append(prompt)
    except (OSError, TypeError):
        pass
    return list(out)


PROMPT_HOOK_TIMEOUT = 60  # seconds; Claude lets the prompt through when a hook times out or crashes
PROMPT_DEADLINE = 40  # the hook's own hard stop: it blocks the prompt itself well before Claude's timeout
PROMPT_WAIT = 20  # daemon acceptance wait, kept inside PROMPT_DEADLINE


class PromptDeadline(Exception):
    pass


def claude_prompt_main(cfg_path, name, generation, raw, deadline=PROMPT_DEADLINE):
    """Fail-closed wrapper of claude_prompt for the hook process: the decision JSON it returns
    always blocks unless claude_prompt explicitly let the prompt through. A crash (non-zero exit)
    or a hook timeout would make Claude Code pass the operator text to the model, bypassing the
    design.md §5 queue, so every error and the PROMPT_DEADLINE alarm end in a block instead."""
    def expire(signum, frame):
        raise PromptDeadline
    # The hook process runs this on its main thread; an embedding thread (tests) skips the alarm.
    armed = threading.current_thread() is threading.main_thread()
    if armed:
        previous = signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, deadline)
    try:
        reason = claude_prompt(cfg_path, name, generation, raw, wait=min(PROMPT_WAIT, max(1.0, deadline - 10)))
    except PromptDeadline:
        reason = 'Fleet 输入未确认（超过 ' + str(deadline) + ' 秒）；请先查看 control 再决定是否重试。'
    except Exception as exc:  # noqa: BLE001 - fail closed: never let operator text reach the model
        reason = 'Fleet 输入未受理（' + type(exc).__name__ + ': ' + str(exc)[:200] + '）；请先查看 control。'
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    return reason
IMAGE_PLACEHOLDER = re.compile(r'\[Image #\d+\]')


def prompt_hook_slot(cfg, name, generation=None):
    """A fleet-launched v2 Claude supervisor (not a §19 adopted pane, whose hooks are the parent's)."""
    return (name == 'supervisor' and cfg.get('transport_version') == 2 and is_claude(cfg, name)
            and not subfleet.adopted(cfg, name, generation))


def hook_bind_ended(r, k, generation, thread, bind):
    """A Stop ended this exact hand-over, not just some earlier turn of the same session."""
    try:
        ended = json.loads(r.get(k['stopped']) or '{}')
    except ValueError:
        return False
    return (isinstance(ended, dict) and ended.get('session_id') == thread
            and ended.get('generation') == generation and ended.get('delivery_id') == bind.get('delivery_id')
            and ended.get('bind_at') == bind.get('at'))


def supervisor_rewake_keepalive(cfg, r, k, generation, thread, mine, *, slot='supervisor', started=None):
    """Claim one rearm across Stop and rewake without overwriting a concurrent hand-over.

    An unflushed transcript is not idle proof: reject an un-ended delivery bind. Identity,
    the current bind, its Stop proof and other live hooks are checked atomically with the write.
    """
    bus = runtime_v2.bus_for(cfg, r)
    stopped = r.get(k['stopped']) or ''
    now = time.time()
    claim = json.dumps(dict(stopped=stopped, at=now))
    started = now if started is None else started
    keepalive = json.dumps(dict(kind='keepalive', generation=generation, session_id=thread or '',
                               via='stop' if mine is None else 'rewake', at=time.time()))
    return bool(r.eval('''
        if ARGV[1] ~= '' and redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
        if (redis.call('GET', KEYS[8]) or '') ~= ARGV[8] then return 0 end
        local claim_ok, claim = pcall(cjson.decode, redis.call('GET', KEYS[9]) or '{}')
        if not claim_ok or type(claim) ~= 'table' then return 0 end
        if claim.stopped == ARGV[8] or (claim.at and claim.at >= tonumber(ARGV[9])) then return 0 end
        if redis.call('HGET', KEYS[6], 'run_id') ~= ARGV[6]
           or redis.call('HGET', KEYS[6], 'slot') ~= ARGV[7] then return 0 end
        if redis.call('EXISTS', KEYS[2], KEYS[3], KEYS[4], KEYS[10]) > 0 then return 0 end
        for _, key in ipairs({KEYS[6], KEYS[7]}) do
            if redis.call('HGET', key, 'generation') ~= ARGV[2] then return 0 end
        end
        if redis.call('HGET', KEYS[6], 'session_id') ~= ARGV[3]
           or redis.call('HGET', KEYS[7], 'thread') ~= ARGV[3] then return 0 end
        local state = redis.call('HGET', KEYS[7], 'state')
        if state == 'dead' or state == 'delegated' then return 0 end
        local ok, bind = pcall(cjson.decode, redis.call('GET', KEYS[5]) or '{}')
        if not ok or type(bind) ~= 'table' then return 0 end
        if bind.kind == 'keepalive' and bind.generation == tonumber(ARGV[2])
           and bind.session_id == ARGV[3] then return 0 end
        if bind.kind == 'delivery' and bind.generation == tonumber(ARGV[2]) and bind.session_id == ARGV[3] then
            local ended_ok, ended = pcall(cjson.decode, redis.call('GET', KEYS[8]) or '{}')
            if not ended_ok or type(ended) ~= 'table' or not bind.at
               or ended.session_id ~= ARGV[3] or ended.generation ~= tonumber(ARGV[2])
               or ended.delivery_id ~= bind.delivery_id or ended.bind_at ~= bind.at then return 0 end
        end
        redis.call('SET', KEYS[5], ARGV[4], 'EX', ARGV[5])
        redis.call('SET', KEYS[9], ARGV[10], 'EX', ARGV[5])
        return 1
    ''', 10, k['rewake'], k['armed'], k['stopping'], k['human'], k['bind'], bus.lease_key(slot),
        key('agent', slot), k['stopped'], k['rearmed'], k['yield_wait'], '' if mine is None else str(mine), str(generation), thread or '',
        keepalive, claude_hook.BIND_TTL, bus.run_id, slot, stopped, started, claim))


def supervisor_yield_wait(cfg, r, name, generation, thread):
    """No CLI completion assumption: remember a bounded wait for a real Stop/block boundary."""
    if not claude_hook.enabled(cfg, name):
        return
    info = r.hgetall(key('agent', name))
    if info.get('generation') != str(generation) or info.get('thread') != thread:
        return
    bus = runtime_v2.bus_for(cfg, r)
    k = hook_keys(cfg, r, name, generation)
    # Persistent until resolved: expiry must not turn uncertainty into permission to deliver.
    mark = json.dumps(dict(run_id=bus.run_id, slot=name, generation=generation,
                           session_id=thread, deadline=awake.of(r).deadline(claude_hook.HUMAN_HOLD)))  # the hook is not the daemon
    r.set(k['yield_wait'], mark, nx=True)


def supervisor_prompt_blocked(cfg, r, name, generation, thread):
    """Release the wait yielded to a blocked operator line; no model turn/Stop will follow it.

    Only this live session, with no hook-delivered execution, may release the human hold. Record
    a transcript boundary so an asyncRewake polling late cannot mistake its dequeue for a live turn.
    This does not settle/release the delivery or alter any daemon state.
    """
    if not claude_hook.enabled(cfg, name):
        return
    info = r.hgetall(key('agent', name))
    if (info.get('generation') != str(generation) or info.get('thread') != thread
            or info.get('state') in ('starting', 'dead')):
        return
    k = hook_keys(cfg, r, name, generation)
    lease = r.hgetall(runtime_v2.bus_for(cfg, r).lease_key(name))
    if lease.get('message_id') and hook_delivered(r, k, thread, lease):
        bind = hook_bind(r, k)
        if not hook_bind_ended(r, k, generation, thread, bind):
            return  # this actual fleet turn has not ended; an old bind alone is not running proof
    try:
        offset = Path(info['transcript']).stat().st_size
    except (OSError, KeyError):
        return  # cannot prove which turn was blocked: fail closed
    r.set(k['prompt_blocked'], json.dumps(dict(session_id=thread, generation=generation, offset=offset)),
          ex=claude_hook.BIND_TTL)
    r.delete(k['human'], k['yield_wait'])


def supervisor_rewake_prompt(cfg, r, name, generation, thread, prompt, info):
    """Claude 2.1.307 submits asyncRewake notifications through UserPromptSubmit too.

    Only the wrapped text this session's rewake actually handed over may bypass registration.
    Plain retyped bodies never qualify; Stop settlement still requires transcript hook origin.
    """
    if not claude_hook.enabled(cfg, name) or info.get('thread') != thread:
        return False
    text = claude_hook.rewake_prompt_text(prompt)
    if text is None:
        return False
    k = hook_keys(cfg, r, name, generation)
    bind = hook_bind(r, k)
    if (bind.get('via') != 'rewake' or bind.get('session_id') != thread
            or bind.get('generation') != generation):
        return False
    if bind.get('kind') == 'keepalive':
        return text.strip() == claude_hook.KEEPALIVE
    if bind.get('kind') == 'nudge':
        return info.get('state') == 'starting' and text.strip() == runtime_v2.STARTUP_NUDGE
    bus = runtime_v2.bus_for(cfg, r)
    lease = r.hgetall(bus.lease_key(name))
    if lease.get('status') != 'received' or not hook_delivered(r, k, thread, lease):
        return False
    msg = bus.message(lease['message_id'])
    return (msg.get('generation') == generation and msg.get('session_id') == thread
            and text.strip() == msg['body']['text'].strip())


def claude_prompt(cfg_path, name, generation, raw, wait=PROMPT_WAIT):
    """UserPromptSubmit hook of a v2 Claude supervisor: the design.md §5 input boundary.

    Returns None for startup, registered midturn prompts and slash commands; otherwise blocks
    operator text after registering it through the original operator_input event. Accepted input
    returns through the supervisor's Stop-hook mailbox, never through the input box.
    """
    data = json.loads(raw or '{}')
    prompt = data.get('prompt') if isinstance(data.get('prompt'), str) else ''
    cfg = load(cfg_path)
    generation = int(generation)
    if (not prompt_hook_slot(cfg, name, generation) or not prompt.strip()
            or (prompt.lstrip().startswith('/') and not claude_hook.enabled(cfg, name))):
        return None
    r = connect(cfg)
    try:
        bus = runtime_v2.bus_for(cfg, r)
        info = r.hgetall(key('agent', name))
        if int(info.get('generation', -1)) != generation or info.get('state') == 'dead':
            return 'Fleet：这个终端属于已退役的监工代，输入未受理。'
        if prompt.lstrip().startswith('/'):
            supervisor_yield_wait(cfg, r, name, generation, data.get('session_id'))
            return None
        if supervisor_rewake_prompt(cfg, r, name, generation, data.get('session_id'), prompt, info):
            r.xadd(bus.audit_key, dict(event='hook.prompt_rewake', slot=name, run_id=bus.run_id,
                                      generation=str(generation), at=str(int(time.time() * 1000))))
            return None
        if (claude_hook.enabled(cfg, name)
                and claude_hook.rewake_prompt_text(prompt) == claude_hook.KEEPALIVE):
            supervisor_prompt_blocked(cfg, r, name, generation, data.get('session_id'))
            return 'Fleet：过期的 keepalive 通知已拦截，不登记为操作员指令。'
        if info.get('state') == 'starting':
            try:
                first = (Path(cfg['run']) / 'prompts' / (name + '-g' + str(generation) + '.md')).read_text()
            except OSError:
                first = ''
            if runtime_v2.turn_answers(first, [prompt]):
                return None
            return 'Fleet 尚未 READY，输入未受理；请先检查启动状态。'
        if prompt == midturn.registered_prompt(info, generation) and data.get('session_id') == info.get('thread'):
            return None
        lease = r.hgetall(bus.lease_key(name))
        if not claude_hook.enabled(cfg, name) and lease.get('status') == 'received' and lease.get('message_id'):
            msg = bus.message(lease['message_id'])
            if msg.get('generation') == generation and runtime_v2.turn_answers(msg['body']['text'], [prompt]):
                return None
        # Every path below blocks the prompt, including refusal or unconfirmed enqueue. No Stop
        # callback follows a blocked prompt; don't leave the hook's operator-yield hold behind.
        supervisor_prompt_blocked(cfg, r, name, generation, data.get('session_id'))
        if IMAGE_PLACEHOLDER.search(prompt):
            return 'Fleet 输入只支持文字，图片未转交；请改用图片文件路径后重发。'
        # Claude Code puts a blocked line back in the box; Enter again resubmits it (2.1.316, design §49).
        last_key = hook_keys(cfg, r, name, generation)['operator_last']
        digest = hashlib.sha256(prompt.strip().encode()).hexdigest()
        try:
            last = json.loads(r.get(last_key) or '{}')
        except ValueError:
            last = {}
        if (isinstance(last, dict) and last.get('sha') == digest and last.get('generation') == generation
                and last.get('session_id') == data.get('session_id')):
            r.xadd(bus.audit_key, dict(event='hook.prompt_resubmit', slot=name, run_id=bus.run_id,
                                      generation=str(generation), request_id=str(last.get('id') or ''),
                                      at=str(int(time.time() * 1000))))
            return ('Fleet：这段文字已登记过（' + str(last.get('id') or '') + '），没有再排队。输入框里留着的'
                    '是被拦下后放回的原文，按 Ctrl+C 清掉即可；确要再发一遍，请改动后再发。')
        correlation = 'ui-' + uuid.uuid4().hex
        try:
            runtime_v2.post_event(bus, dict(kind='operator_input', agent=name, generation=generation,
                                            run_id=bus.run_id, thread=data.get('session_id'), text=prompt,
                                            request_id=correlation), correlation)
            answer = runtime_v2.wait_reply(bus, correlation, timeout=wait)
        except (runtime_v2.redis.RedisError, runtime_v2.BusError, ValueError) as exc:
            return 'Fleet 输入未确认（' + str(exc)[:200] + '）；请先查看 control 再决定是否重试。'
        if answer.get('status') != 'rejected':  # queued, or still pending on the bus: either way it is in
            r.set(last_key, json.dumps(dict(sha=digest, generation=generation, session_id=data.get('session_id'),
                                            id=answer.get('id') or answer.get('task_id') or correlation)),
                  ex=claude_hook.BIND_TTL)
        if answer.get('status') == 'accepted':
            return ('Fleet：指令已排队（' + str(answer.get('id') or answer.get('task_id')) + '），'
                    '监工会按派活流程处理；这段输入不会直接进入当前对话。')
        if answer.get('status') == 'rejected':
            return 'Fleet 输入被拒绝：' + str(answer.get('reason') or '原因未给出')
        return 'Fleet 输入未确认（' + correlation + '）；请先查看 control 再决定是否重试。'
    finally:
        r.close(); r.connection_pool.disconnect()


def tui_turn(cfg, name, generation, *, thread, transcript, text, inputs, wait=None, settle_wait=30, turn=''):
    """v2 turn end of an official interactive CLI (Codex notify / Claude Stop hook).

    First READY turn: bind the session, register the slot and report startup. Later turns:
    settle only the delivery this generation received, only if that delivery started the turn
    (Claude: its typed text; Codex: the rollout proves the queued delivery started `turn`, see
    codex_queue.bound). The daemon validates identity, lease, fence and schema as for Pi.
    """
    if name not in ['supervisor', ADVISOR] + cfg['workers'] or not runtime_v2.tui_slot(cfg, name):
        raise ValueError('turn hook must name an official-CLI v2 agent in this fleet')
    # Events are durable either way; waiting long only keeps this generation's startup
    # sequence (bind -> register -> READY) intact while the daemon is still spawning tabs.
    wait = float(cfg.get('profile_ready_timeout') or 180) if wait is None else wait
    r = connect(cfg)
    bus = runtime_v2.bus_for(cfg, r)
    try:
        info = r.hgetall(key('agent', name))
        base = dict(agent=name, generation=generation, run_id=bus.run_id, thread=thread, transcript=transcript)
        if clear_after_result.enabled(cfg, name):
            base['hook_pid'] = os.getpid()

        def call(kind, **fields):
            correlation = 'h-' + uuid.uuid4().hex
            runtime_v2.post_event(bus, dict(base, kind=kind, request_id=correlation, **fields), correlation)
            # Events are durable; a long wait only matters for the READY sequence. A turn
            # end must not freeze a blocking Claude Stop hook for minutes.
            return runtime_v2.wait_reply(bus, correlation, timeout=wait if kind != 'settled' else settle_wait)

        def skipped(status, reason):
            # Visible in control/logs: an ignored turn otherwise leaves no trace.
            r.xadd(bus.audit_key, {'event': 'turn.' + status, 'slot': name, 'run_id': bus.run_id,
                                   'generation': str(generation), 'error': reason, 'level': 'warn',
                                   'at': str(int(time.time() * 1000))})
            return {'status': status, 'reason': reason}
        # The CLI of this generation, as delivery sees it (Fleet.runtime_cfg): a profile switch may change it.
        launch = json.loads(info.get('runtime_launch') or '{}')
        codex = codex_queue.is_codex(dict(cfg, **launch.get('overrides', {})), name)
        why = []

        def codex_bound(delivery):
            """Did this queued Codex delivery start the turn? Exact rollout binding, never the cumulative inputs."""
            # The record and its token are durable before the CLI runs, so a turn end faster than the CLI binds too.
            rec = codex_queue.record(r.hgetall(key('agent', name)))
            if rec['cq_delivery'] != delivery:
                why.append('queued delivery is ' + (rec['cq_delivery'] or 'none') + ', not ' + delivery)
                return False
            ok, reason = codex_queue.bound(rec, thread, turn, sessions_dir(cfg), generation)
            if not ok:
                why.append(reason)
                return False
            if not codex_queue.mark_bound(r, name, turn, rec['cq_token']):
                why.append('delivery abandoned or replaced while binding')
                return False
            return True
        if int(info.get('generation', -1)) != generation or info.get('state') == 'dead':
            return skipped('stale', 'hook generation is not the current runtime')
        if info.get('state') == 'starting':
            pending = codex_queue.record(info) if codex else {}
            if pending.get('cq_delivery') and codex_queue.unsettled(info):
                # A queued startup message (READY nudge / sub-fleet role change) must be the one this turn answers.
                if not codex_bound(pending['cq_delivery']):
                    return skipped('ignored', 'startup turn not bound to ' + pending['cq_delivery'] + ': ' + '; '.join(why))
            if info.get('thread') != thread:
                answer = call('bridge_session')
                if answer.get('status') != 'registered':
                    return answer
            bus.register_slot(name, generation, thread)
            return call('startup', text=text, failed=False, aborted=False)
        k = hook_keys(cfg, r, name, generation) if not codex and claude_hook.enabled(cfg, name) else None
        # Our own keepalive turn is never settled; it still ends a turn of this session (hold exit below).
        kept = bool(k) and claude_hook_keepalive(cfg, r, name, generation, thread)

        def answered(body_text, lease):
            if codex:  # official Codex: the queued delivery's token binding (codex_queue.bound)
                return codex_bound(lease.get('current_delivery') or '')
            # Hook channel: this session's Stop hook must have handed over this very delivery, and it
            # must be this turn's latest hook input (its isMeta "Stop hook feedback" row, its rewake row,
            # or the rewake notification absorbed into this running turn, see claude_user_prompts): an
            # older feedback row never settles a manual turn, nor
            # does an operator line that repeats the delivery text word for word (origin 'user').
            # The row must also lie after the hand-over (the bind's transcript offset): retries of one
            # instruction share their text, and the row of an earlier one is not this delivery's.
            if k is None:
                return runtime_v2.turn_answers(body_text, inputs)
            if not (inputs and getattr(inputs[-1], 'origin', 'hook') == 'hook'
                    and runtime_v2.turn_answers(body_text, inputs[-1:]) and hook_delivered(r, k, thread, lease)):
                return False
            at = getattr(inputs[-1], 'at', None)
            if at is not None and at < int(hook_bind(r, k).get('offset') or 0):
                why.append('the latest hook input was written before this delivery was handed over')
                return False
            return True

        def ignored(reason):
            # §21: a turn no delivery started (a peer SendMessage woke it, or the operator typed):
            # the daemon records it as unsolicited and forwards its Peer notices / question lines.
            if name != 'supervisor' and (text or '').strip():
                runtime_v2.post_event(bus, dict(base, kind='unsolicited', text=text[-8000:]))
            return skipped('ignored', reason)
        lease = r.hgetall(bus.lease_key(name))
        if (lease.get('status') in ('uncertain', 'expired') and lease.get('message_id')
                and info.get('state') in ('held', 'clearing')):
            # Hold exit (docs/design.md §24): any turn end of the held delivery's own session and
            # generation proves its execution stopped (Claude runs one turn at a time; a Stop with
            # background work never reaches here). Codex: its queue may still hold the item, so the
            # rollout must show the token's turn complete. The daemon releases; the result is not applied.
            msg = bus.message(lease['message_id'])
            if msg.get('generation') != generation or msg.get('session_id') != thread:
                return skipped('ignored', 'held delivery belongs to another generation/session')
            if codex:
                rec = codex_queue.record(info)
                ran = (codex_queue.token_turn(thread, rec['cq_token'], sessions_dir(cfg))
                       if rec['cq_token'] and rec['cq_delivery'] == lease.get('current_delivery') else None)
                if not ran or not ran['complete']:
                    return skipped('ignored', 'held Codex delivery ' + (lease.get('current_delivery') or '')
                                   + (' has not started' if not ran else ' is still running') + ' in the rollout')
                codex_queue.mark_bound(r, name, ran['turn'], rec['cq_token'])  # a live record: proven ran; abandoned stays
            # The turn's text rides along: a late service answer is audited (result.late), never applied (§43).
            return call('turn_ended', delivery={k: msg[k] for k in runtime_v2.IDENTITY}, text=(text or '')[-8000:])
        if kept:
            return skipped('ignored', 'hook channel keepalive turn')
        if lease.get('status') != 'received' or not lease.get('message_id'):
            return ignored('no received delivery (manual or duplicate turn)')
        msg = bus.message(lease['message_id'])
        if msg.get('generation') != generation or msg.get('session_id') != thread:
            return skipped('stale', 'delivery belongs to another generation/session')
        if not answered(msg['body']['text'], lease):
            return ignored('turn was not started by the delivery' + (': ' + '; '.join(why) if why else ''))
        return call('settled', delivery={k: msg[k] for k in runtime_v2.IDENTITY},
                    candidates=runtime_v2.tui_candidates(name, msg['body'].get('purpose'), text or '',
                                                         msg['body'].get('mode') or cfg.get('mode') or 'task'),
                    failed=False, aborted=False, unfinished=False,
                    wall_text=(text or '') if len((text or '').strip()) <= brake.TURN_WALL_MAX else '')
    finally:
        r.close(); r.connection_pool.disconnect()


def _claude_context_hook(cfg_path, name, generation, raw, kind):
    """Persist hooks without waiting inside Claude's lifecycle callback."""
    cfg, data = load(cfg_path), json.loads(raw or '{}')
    if (name not in cfg['workers'] or cfg.get('worker_cli') != 'claude'
            or cfg.get('transport_version') != 2):
        raise ValueError('context hook must name a Claude v2 worker')
    if not isinstance(data, dict):
        raise ValueError('hook JSON must be an object')
    if kind == 'cleared' and data.get('source') != 'clear':
        return False
    r = connect(cfg)
    try:
        bus = runtime_v2.bus_for(cfg, r)
        info = r.hgetall(key('agent', name))
        ev = dict(kind=kind, agent=name, generation=int(generation), run_id=bus.run_id,
                  thread=data.get('session_id'), transcript=data.get('transcript_path', ''),
                  hook_pid=os.getpid(), old_thread=info.get('thread'), clear_token=info.get('clear_token', ''))
        if kind == 'stop_failure':
            ev['error'] = str(data.get('error') or data.get('error_details')
                              or data.get('last_assistant_message') or 'Claude StopFailure')[:4000]
            ev['details'] = str(data.get('error_details') or '')[:1000]
            ev['error_type'] = str(data.get('error') or '')
            ev['text'] = str(data.get('last_assistant_message') or '')[:1000]
            ev['api_text'] = ''
            lease = r.hgetall(bus.lease_key(name))
            if lease.get('message_id'):
                ev['api_text'] = claude_api_error(data.get('transcript_path', ''),
                                                  received_at(bus.message(lease['message_id'])))[:1000]
            lease = r.hgetall(bus.lease_key(name))
            if lease.get('message_id'):
                msg = bus.message(lease['message_id'])
                ev['delivery'] = {k: msg[k] for k in runtime_v2.IDENTITY}
        runtime_v2.post_event(bus, ev)
        return True
    finally:
        r.close(); r.connection_pool.disconnect()


def claude_clear(cfg_path, name, generation, raw):
    return _claude_context_hook(cfg_path, name, generation, raw, 'cleared')


def agent_profile(cfg, name):
    """Codex profile for one agent: the role-specific one if set, else the shared --profile."""
    role = 'supervisor_profile' if name == 'supervisor' else 'worker_profile'
    return cfg.get(role) or cfg.get('profile') or DEFAULTS['profile']


def actor_argv(cfg_path, cfg, name, generation, prompt):
    callback = [cfg['python'], str(Path(__file__).resolve()), 'notify', str(cfg_path), name, str(generation)]
    argv = [cfg.get('codex') or 'codex', '--profile', agent_profile(cfg, name),
            '--no-alt-screen',
            '--sandbox', 'workspace-write', '-c', 'sandbox_workspace_write.network_access=true',
            '-c', 'approval_policy="never"',
            '-c', 'check_for_update_on_startup=false',  # the update dialog would eat the first paste
            # The operator's skills (~/.agents/skills) are listed to every session regardless of CODEX_HOME;
            # a local model took that list as its task and ran a skill instead of replying READY.
            '-c', 'skills.include_instructions=false',
            '-c', 'notify=' + json.dumps(callback),
            '-c', 'developer_instructions=' + json.dumps(
                'Your role, rules and reply format are in AGENTS.md in the current directory. Follow it.')]
    if name == 'supervisor':  # it edits the roster (§16) and writes release notes on the session board
        roots = [str(Path(cfg[k]).parent) for k in ('roster', 'roster_night') if cfg.get(k)]
        roots = list(dict.fromkeys(roots + [session_board.board_dir()]))
        argv += ['-c', 'sandbox_workspace_write.writable_roots=' + json.dumps(roots)]
    if cfg.get('codex_model'):
        argv += ['--model', cfg['codex_model']]
    if cfg.get('compact_limit'):
        # The daemon rotates sessions at handoff_ratio before each delivery; Codex's own
        # compaction is only a safety net for a single runaway turn.
        argv += ['-c', 'model_auto_compact_token_limit=' + str(int(cfg['compact_limit']))]
    from fleetmux import guard  # P8j: Codex PreToolUse hook only when probed and codex_hooks: on
    argv += guard.codex_hook_argv(cfg, cfg_path, name, generation, cfg['python'], Path(__file__).resolve())
    from fleetmux import sandbox  # P8o: after network_access=true above, so it wins; [] when mode off
    argv += sandbox.actor_codex_flags(sandbox.security.policy(cfg), cfg)
    return argv + [prompt]


# §44「监工直接开子 fleet」(switch supervisor_split): the service supervisor template's section 3 line, only when on
SUPERVISOR_SPLIT_DOC = (
    '- `dispatch[].split`: optional, for a request too big for one worker (several parts that can run in parallel, '
    'or long work): `{"reason": "one sentence", "proposal": [{"instruction": "what this part does", "output": "what '
    'it delivers"}, ...], "workers": <n, optional>, "supervisor_profile": "<card, optional>", "worker_profiles": '
    '["<card>", ...]}`. Without supervisor_profile the program picks the sub-fleet supervisor\'s model from the '
    'supervisor chain. The worker in `to` (idle, nothing queued) becomes the supervisor of a new task sub-fleet that '
    'runs the parts; you do not answer an `[escalate]` question for it. The sender first gets `delegated: M<n>`, the '
    'sub-fleet\'s own result follows as `<id>.done`. If the program refuses the split (limits), that worker gets the '
    'request as a normal dispatch and you are told. Do not combine with `fresh`. A section 4, if present, says when '
    'to split, dispatch or answer.\n')
# Every Claude Code the fleet starts: never self-update. An isolated config (CLAUDE_CONFIG_DIR) has no
# autoupdater setting of its own, and on 2026-10-14 01:36 such an auto-update broke the shared install.
CLAUDE_ENV = {'DISABLE_AUTOUPDATER': '1'}


def claude_argv(cfg_path, cfg, name, generation, prompt, resume=None):
    """``resume``: this generation continues that Claude session (``claude --resume``; a lost slot, §24)."""
    cwd = agent_cwd(cfg, name)
    hook = shlex.join([cfg['python'], str(Path(__file__).resolve()), 'claude-stop', str(cfg_path), name, str(generation)])
    settings = cwd / ('.fleet-settings-g' + str(generation) + '.json')
    # v2: the READY hook waits for the daemon, which may still be spawning other tabs.
    timeout = 30 if cfg.get('transport_version') != 2 else int(float(cfg.get('profile_ready_timeout') or 180)) + 30
    advised = cfg.get('advisor') is True and name != 'supervisor'
    hooked = claude_hook.enabled(cfg, name, claude=True)
    if hooked:
        timeout = claude_hook.hook_timeout(cfg)  # the hook waits here for the next delivery
    hooks = {'Stop': [{'hooks': [{'type': 'command', 'command': hook, 'timeout': timeout}]}]}
    if hooked:
        # Fallback when no Stop hook waits any more (Escape, hook timeout, StopFailure): a background
        # hook that wakes Claude with the delivery (exit 2) instead of typing it.
        hooks['Stop'][0]['hooks'].append({'type': 'command', 'timeout': timeout, 'asyncRewake': True,
                                          'rewakeMessage': claude_hook.REWAKE_MESSAGE, 'command': shlex.join(
            [cfg['python'], str(Path(__file__).resolve()), 'claude-rewake', str(cfg_path), name, str(generation)])})
        # An API error ends the turn with StopFailure instead of Stop: no hook waits afterwards.
        hooks['StopFailure'] = [{'hooks': [{'type': 'command', 'timeout': 30, 'command': shlex.join(
            [cfg['python'], str(Path(__file__).resolve()), 'claude-stop-failure', str(cfg_path), name, str(generation)])}]}]
        if name != ADVISOR:
            # After a StopFailure the slot gets work again (§24: task mode re-delivers the instruction, service
            # mode the next request; a supervisor its message); after a turn the rewake itself started fails, no
            # Stop's rewake is left to hand it over: this one does.
            hooks['StopFailure'][0]['hooks'].append(dict(hooks['Stop'][0]['hooks'][1]))
    if name != ADVISOR:
        # Mid-turn handoff (design.md §34): the daemon never types into a Claude pane; this hook hands the
        # registered prompt to the model after the next tool call (decision block + reason).
        hooks['PostToolUse'] = [{'hooks': [{'type': 'command', 'timeout': 10, 'command': shlex.join(
            [cfg['python'], str(Path(__file__).resolve()), 'claude-posttool', str(cfg_path), name, str(generation)])}]}]
    # P8i action gate: only with security / security_taint in the pinned config (no policy: settings unchanged).
    from fleetmux import guard
    pretool = guard.pretool_hooks(cfg, cfg_path, name, generation, cfg['python'], Path(__file__).resolve())
    if pretool:
        hooks['PreToolUse'] = pretool
    from fleetmux import sandbox  # P8o: sandbox / permissions / memory keys ({} without a policy)
    extra = sandbox.settings_for(cfg, name)
    if name == ADVISOR:
        # Resident advisor (§27): the one-shot advisor's flags, only Read, and this file's hooks.
        settings.write_text(json.dumps(sandbox.merge_settings({'hooks': hooks, 'autoMemoryEnabled': False,
                                        'env': session_env.settings_env(cfg, 'claude', dict(
                                            CLAUDE_ENV, **(claude_hook.ENV if hooked else {})))}, extra)))
        argv = advisor.resident_argv(cfg, cfg['run'], settings)
        if hooked:
            argv += ['--append-system-prompt', claude_hook.DECLARATION]
        return argv + ['--', prompt]  # --add-dir is variadic: end options first
    if clear_after_result.enabled(cfg, name):
        # exec ensures the PID reported by the hook is the process Claude waits for.
        hooks['Stop'][0]['hooks'][0]['command'] = 'exec ' + hook
        for event, verb in (('SessionStart', 'claude-clear'), ('StopFailure', 'claude-stop-failure')):
            entry = {'hooks': [{'type': 'command', 'timeout': 10, 'command': 'exec ' + shlex.join(
                [cfg['python'], str(Path(__file__).resolve()), verb, str(cfg_path), name, str(generation)])}]}
            if event == 'SessionStart':
                entry['matcher'] = 'clear'
            hooks[event] = [entry]
    if prompt_hook_slot(cfg, name, generation):
        # Operator input typed in the supervisor tab is queued as a daemon request (design.md §5).
        hooks['UserPromptSubmit'] = [{'hooks': [{'type': 'command', 'timeout': PROMPT_HOOK_TIMEOUT, 'command': shlex.join(
            [cfg['python'], str(Path(__file__).resolve()), 'claude-prompt', str(cfg_path), name, str(generation)])}]}]
    # Consent for --dangerously-skip-permissions rides here: Claude Code keeps it in ~/.claude/settings.json,
    # which --setting-sources project (advised slots) does not read, and --settings is always read. Without
    # it the pane waits on the Bypass Permissions dialog until the startup timeout (macmini 2026-10-30..11-06).
    data = {'hooks': hooks, 'env': session_env.settings_env(cfg, 'claude', dict(CLAUDE_ENV, **(claude_hook.ENV if hooked else {}))),
            'skipDangerousModePermissionPrompt': True}
    if advised:
        data['autoMemoryEnabled'] = False  # fleet workers have no memory layer (advisor plan §2)
    settings.write_text(json.dumps(sandbox.merge_settings(data, extra)))
    argv = [cfg.get('claude') or 'claude', '--dangerously-skip-permissions', '--model', claude_model(cfg, name),
            # Unattended panes: a choice dialog would wait for a human forever (acceptance 10/13 P1-5).
            '--disallowedTools', 'AskUserQuestion',
            '--settings', str(settings), '--add-dir', str(Path(cfg['run']) / 'workspace')]
    if cfg.get('task_dir') and cfg.get('parent_config'):  # a §44 / §20 sub-fleet's task directory (§46)
        argv += ['--add-dir', cfg['task_dir']]
    if name != 'supervisor':  # stable per slot across generations (peer messages address it)
        argv += ['--name', claude_session_name(cfg, name)]
    if advised:
        # Only cwd/.claude (the linked domains) and cwd/mcp.json (the full kit set): no user-level
        # skills / agents / MCP servers. The Stop hook rides on --settings and still fires.
        argv += ['--setting-sources', 'project', '--strict-mcp-config']
    if name != 'supervisor':  # the advisor's effort when advised, else discovery.worker_thinking (user 2026-11-11: high)
        argv += ['--effort', (cfg.get('worker_effort') if advised else None) or worker_thinking()]
    if hooked:
        argv += ['--append-system-prompt', (claude_hook.SUPERVISOR_DECLARATION if name == 'supervisor'
                                           else claude_hook.DECLARATION)]
    elif name != 'supervisor':  # typed / pasted delivery: the same promise for the pasted text
        argv += ['--append-system-prompt', claude_hook.TYPED_DECLARATION]
    if (cwd / 'mcp.json').exists():
        argv += ['--mcp-config', str(cwd / 'mcp.json')]
    if resume:
        argv += ['--resume', resume]
    return argv + ['--', prompt]  # --add-dir / --mcp-config are variadic: end options first


def claude_model(cfg, name):
    if name == ADVISOR:
        return advisor.MODEL
    if name == 'supervisor':
        return cfg.get('supervisor_model') or DEFAULTS['supervisor_model']
    return cfg.get('worker_model') or DEFAULTS['worker_model']


def claude_session_name(cfg, name):
    """f<main run token>[-<sub-fleet task id>]-<worker slot>, e.g. f1a2b3c4d-M0-worker-2."""
    return 'f' + subfleet.token(cfg) + ('-' + cfg['task_id'] if cfg.get('task_id') else '') + '-' + name


def pi_event(cfg_path, name, generation, raw):
    """Pi's bundled extension sends session metadata and final settlement over stdin."""
    cfg = load(cfg_path)
    if cfg.get('transport_version') == 2:
        raise ValueError('v2 Pi sessions must use the persistent bound bridge')
    if name not in ['supervisor'] + cfg['workers'] or not is_pi(cfg, name):
        raise ValueError('pi callback must name a pi agent in this fleet')
    if len(raw) > 1000000:
        raise ValueError('pi event too large')
    ev = pi_adapter.event(cfg, name, int(generation), json.loads(raw))
    ev['at'] = time.time()
    if ev['kind'] == 'operator_input':
        print(json.dumps(request(cfg, ev), ensure_ascii=False))
    else:
        connect(cfg).rpush(EVENTS, json.dumps(ev, ensure_ascii=False))


def actor(cfg_path, name, prompt, generation=None):
    cfg = agent_cfg(load(cfg_path), name)
    if name != ADVISOR:  # the advisor's CLI is its identity (§27), not a supervisor x worker pair choice
        runtime_v2.validate_config(cfg)
    r = connect(cfg)
    info = r.hgetall(key('agent', name))
    # Every daemon launch pins this value in argv, never adopts a newer generation.
    generation = int(info.get('generation') or 0) if generation is None else generation
    deadline = time.monotonic() + 60
    while True:
        if generation != int(info.get('generation', -1)) or info.get('state') == 'dead':
            raise RuntimeError('actor generation was retired or blocked before launch')
        if info.get('launch_ready', '1') == '1':
            break
        if info.get('advice_hold') == '1':
            deadline = time.monotonic() + 60  # held for the first advisor sheet: wait for it or the run's end
        if time.monotonic() >= deadline:
            raise RuntimeError('profile launch gate timed out; old runtime retirement unconfirmed')
        time.sleep(.1)
        info = r.hgetall(key('agent', name))
    cfg = agent_cfg(load(cfg_path), name)  # an advisor release may have rewritten this generation's snapshot
    # Fail closed (distributed-development.md): a routed session waits here, route_wait=1, while the applet port is closed.
    session_env.wait(cfg, agent_cli(cfg, name), r, key('agent', name), generation,
                     ready_s=float(cfg.get('profile_ready_timeout') or DEFAULTS['profile_ready_timeout']))
    os.chdir(agent_cwd(cfg, name))
    if is_pi(cfg, name):
        os.environ.update(pi_adapter.environment(cfg_path, cfg, name, generation))
        argv = pi_adapter.argv(cfg, name, generation, prompt, agent_cwd(cfg, name), label=info['label'])
        # pi rewrites its process title (ps shows only "pi"): keep the exact exec argv as launch evidence.
        record = Path(cfg['run']) / 'prompts' / (name + '-g' + str(generation) + '.argv.json')
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({'slot': name, 'label': info['label'], 'generation': generation,
                                      'argv': argv, 'at': time.time()}, ensure_ascii=False) + '\n')
    elif is_opencode(cfg, name):
        # opencode TUI with fleet's plugin in-process (it spawns agent_bridge.py, like the pi extension).
        os.environ.update(opencode_adapter.environment(cfg_path, cfg, name, generation, label=info['label'],
                                                       startup_prompt=prompt,  # the plugin binds by this text
                                                       task_dir=agent_cwd(cfg, name)))
        argv = opencode_adapter.argv(cfg, name, generation, prompt, agent_cwd(cfg, name), label=info['label'])
        # Process cwd = this generation's empty launch dir (the plugin checks it before the startup turn);
        # the task dir stays outside the launch tree, so its files are never loaded as nested instructions.
        os.chdir(opencode_adapter.launch_cwd(cfg, name, generation))
        record = Path(cfg['run']) / 'prompts' / (name + '-g' + str(generation) + '.argv.json')
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({'slot': name, 'label': info['label'], 'generation': generation,
                                      'argv': argv, 'at': time.time()}, ensure_ascii=False) + '\n')
    elif is_dsh(cfg, name):
        # dsh-TUI with fleet's bridge plugin in-process (it spawns agent_bridge.py, like the pi extension).
        # pi's models.json is read (patches, key) before HOME switches to the pane home. The key and the
        # startup prompt go into this process's environment only: never the command line, argv or a file.
        dsh_adapter.prepare(cfg, name, generation)
        os.environ.update(dsh_adapter.environment(cfg_path, cfg, name, generation, label=info['label'],
                                                  startup_prompt=prompt))
        argv = dsh_adapter.argv(cfg, name, generation)
        record = Path(cfg['run']) / 'prompts' / (name + '-g' + str(generation) + '.argv.json')
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({'slot': name, 'label': info['label'], 'generation': generation,
                                      'argv': argv, 'at': time.time()}, ensure_ascii=False) + '\n')
    elif is_claude(cfg, name):
        # A daemon started from inside a Claude Code session must not pass its session markers on:
        # with CLAUDE_CODE_CHILD_SESSION set, Claude Code 2.1.280 saves no transcript, and the turn
        # settlement reads that transcript.
        for var in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_SESSION_ID',
                    'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_PID'):
            os.environ.pop(var, None)
        os.environ.update(CLAUDE_ENV)
        if claude_hook.enabled(cfg, name, claude=True):
            os.environ.update(claude_hook.ENV)  # back-to-back deliveries are consecutive Stop blocks
        # Re-check trust at exec time: other Claude Code sessions rewrite ~/.claude.json and can drop
        # the entry spawn() wrote moments ago (real smoke 2026-10-13: worker-1 stuck on the trust dialog).
        trust_claude_dir(agent_cwd(cfg, name))
        argv = claude_argv(cfg_path, cfg, name, generation, prompt, resume=info.get('resume_session')
                           if info.get('resume_generation') == str(generation) else None)
    elif is_codex(cfg, name):
        argv = actor_argv(cfg_path, cfg, name, generation, prompt)
    else:  # agent_cli already refuses unknown CLIs; a new one must get its own branch here
        raise ValueError('no launch path for ' + agent_cli(cfg, name))
    session_env.apply(cfg, agent_cli(cfg, name))  # rules 1 and 2, after the per-CLI update, this process only
    from fleetmux import sandbox  # P8o (C15): mode os wraps the real exec; unavailable -> raise, never unsandboxed
    argv = sandbox.os_wrap(argv, sandbox.security.policy(cfg), str(agent_cwd(cfg, name)), cfg)
    os.execvp(argv[0], argv)


# ------------------------------------------------------------ daemon
class Fleet:
    def __init__(self, cfg_path, r, mgr, sessions=None):
        self.path = Path(cfg_path).resolve()
        self.cfg = dict(DEFAULTS, **load(self.path))
        self.transport_version = runtime_v2.validate_config(self.cfg)
        profile_runtime.validate_live_setting(self.cfg['apply_profiles_live'],
                                              allowed=self.cfg['allow_profile_live_switch'])
        timeout = self.cfg['profile_ready_timeout']
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('profile_ready_timeout must be a finite positive number')
        bad = roster.handoff_errors({'handoff': self.cfg.get('handoff')})  # static config table (§22)
        if bad:
            raise ValueError('config ' + '; '.join(bad))
        self.run_dir = Path(self.cfg['run'])
        self.r, self.mgr = r, mgr
        awake.join(r)  # §35: stamps written by this process are in the fleet's awake time from the start
        self.sessions = Path(sessions) if sessions else sessions_dir(self.cfg)
        self.workers = list(self.cfg['workers'])
        # The resident advisor tab (§27) is a slot (bus, hooks, watchdog, handoff), never a worker. A restarted
        # daemon finds it from Redis; a new run starts it in start_all.
        advisor_info = self.r.hgetall(key('agent', ADVISOR))
        self.advisor_slot = advisor_info.get('role') == 'advisor' and advisor_info.get('state') != 'retired'
        self.names = self.slot_names()
        # Hot roster (start --roster): workers missing from `desired` are retiring. They finish
        # queued work, then retire; until then they still count as busy but take no new work.
        self.desired = list(self.workers)
        self.roster_stamp = self.roster_digest = None
        self.midturn_probe = midturn.Probe()
        self.handoff_table, self.worker_handoff = {}, {}  # roster `handoff:` / per-worker overrides (§22)
        self.split_table, self.worker_split = {}, {}  # roster `split:` / per-worker overrides (§44)
        self.roster_followed = None  # day or night roster file (night.json picks it)
        self.max_busy_start = self.cfg.get('max_busy') or 0  # --max-busy; a roster `max_busy:` overrides it live
        self.tab_specs, self.tabs_opened = None, False  # roster `tabs:` (§31): opened once per daemon start
        self.tab_watch = {}  # tabs opened by this daemon: keepalive / `tab restart` state (tabs.watch, §31)
        self.service = self.cfg.get('mode') == 'service'
        self.stopping = False
        self.next_liveness = 0
        self.last_chrome = 0
        self.explicit_stop = False
        self.resume_requested = False  # set by `daemon --resume` (operator ran start --resume)
        self.lifecycle_close = None  # set by lifecycle_close.request_close: why/how this run is being closed
        self.close_workspace_pending = False
        self.notifier = None  # tests: replaces the TG / parent channels (lifecycle_close.notify)
        if self.service:
            brake.validate_budget(self.cfg.get('budget'))
        elif self.cfg.get('budget') is not None:
            raise ValueError('config budget is for service mode; a task carries its own budget')
        argv = self.cfg.get('brake_notify')
        if argv is not None and (not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv)):
            raise ValueError('brake_notify must be an argv list of strings (no shell)')
        self.pending_notify = []  # brake trips recorded inside a v2 event transaction (notified on tick)
        self.v2 = runtime_v2.RuntimeV2(self) if self.transport_version == 2 else None
        self.kit, self.advisor_runner = None, None  # advisor online only; tests inject advisor_runner
        # The roster's `advisor: on|off` is the only switch (advisor plan §2.1). self.cfg['advisor'] is its
        # in-memory mirror, never saved to config.json; per-generation snapshots carry it to the actor.
        self.cfg.pop('advisor', None)
        if self.roster_advisor():
            if self.cfg.get('worker_kit'):
                self.kit = kitmod.Kit(self.cfg['worker_kit'])  # KitError (ValueError): refuse the whole kit
            self.cfg['advisor'] = True
            self.r.hset(SERVICE, mapping={'advisor': 'on', 'advisor_by': 'user', 'advisor_at': time.time()})
            self.log('advisor.on', who='user', at_start=True)
        elif self.r.hget(SERVICE, 'advisor') == 'on':  # resumed after the roster turned it off
            self.r.hset(SERVICE, mapping={'advisor': 'off', 'advisor_by': 'user', 'advisor_at': time.time()})

    # -- advisor switch (advisor plan §2.1). Switch and readiness are two questions (design §27):
    # advisor_enabled() = the roster switch (holds first generations, accepts consults, off notes);
    # advisor_ready() = a consultation can be answered now (the resident tab is idle / busy).
    def advisor_enabled(self):
        return self.cfg.get('advisor') is True

    advisor_online = advisor_enabled  # the earlier name of the switch

    def advisor_resident(self):
        """Consultations go to the resident advisor tab: transport v2, a main fleet (task or service mode,
        10-14 Studio ops fleet). Otherwise (v1, a sub-fleet, or a test that injects advisor_runner) the
        one-shot `claude -p` runner answers them at once, as before."""
        return (bool(self.v2) and not self.cfg.get('adopt') and not self.cfg.get('parent_config')
                and self.advisor_runner is None)

    def advisor_ready(self):
        if not self.advisor_enabled():
            return False
        return not self.advisor_resident() or (self.advisor_slot and self.agent(ADVISOR).get('state') in ('idle', 'busy'))

    def slot_names(self):
        return ['supervisor'] + self.workers + ([ADVISOR] if self.advisor_slot else [])

    def roster_advisor(self):
        """advisor on/off from the roster file at daemon start (a bad file: off; roster_tick reports it)."""
        path = roster_file(self.cfg)
        try:
            raw = path.read_bytes()
            roster.parse(raw, path.parent)
            return roster.advisor(raw)
        except (OSError, ValueError):
            return False

    def set_advisor(self, on, who, *, write=False):
        """Turn the advisor on / off without a restart. ``write``: also set it in the roster file (the
        supervisor's decision key). Returns None, or why nothing changed. Idempotent on equal values."""
        kit = self.kit
        if on and not self.advisor_online() and self.cfg.get('worker_kit'):
            try:
                kit = kitmod.Kit(self.cfg['worker_kit'])  # a bad kit refuses the switch, like at start
            except (OSError, ValueError) as exc:
                self.log('advisor.rejected', level='error', who=who, error=str(exc)[:500])
                return 'worker kit rejected: ' + str(exc)[:500]
        if write:
            try:
                for path in advisor_rosters(self.cfg):  # the roster stays the truth (day and night alike)
                    roster.write_advisor(path, on, self.desired)
            except (OSError, ValueError) as exc:
                self.log('advisor.rejected', level='error', who=who, error=str(exc)[:500])
                return 'roster not written: ' + str(exc)[:500]
        if on == self.advisor_online():
            return None
        if on:
            self.kit = kit
            self.cfg['advisor'] = True
        else:
            # Running workers and their sheet-launched generations stay; a consultation already made
            # finishes (a queued hot-load still applies). No new consultations, no slot stays held.
            self.cfg.pop('advisor', None)
            for name in self.workers:
                if self.r.llen(ADVICE_PENDING + name):
                    self.r.delete(ADVICE_PENDING + name)
                self.drop_advice_pending(name, 'advisor turned off')  # the message goes out with its runtime now
                info = self.agent(name)
                if info.get('advice_hold') == '1' and info.get('launch_ready') == '0':
                    self.release_default(name)
                else:
                    self.drop_advice_wait(name)
            rescue = json.loads(self.r.get(RESCUE_PENDING) or 'null')
            if rescue:  # the consult out with the resident advisor: answered like one queued while on
                self.r.delete(RESCUE_PENDING)
                self.advisor_abandon(rescue['ticket'], 'advisor turned off')
                if self.running():
                    self.queue('supervisor', self.consult_off_note([rescue['req']]) + '\nReply with the JSON format from AGENTS.md.')
        state = 'on' if on else 'off'
        self.r.hset(SERVICE, mapping={'advisor': state, 'advisor_by': who, 'advisor_at': time.time()})
        self.log('advisor.' + state, who=who)
        return None

    def advisor_decision(self, decision):
        """The supervisor's `advisor` key, and the off-gate for `consult` (advisor plan §2.1)."""
        if decision.get('advisor') in ('on', 'off'):
            error = self.set_advisor(decision['advisor'] == 'on', 'supervisor', write=True)
            if error:
                self.r.rpush(ADVICE_NOTES, '[advisor] advisor: "' + decision['advisor'] + '" 未生效：' + error)
        if decision.get('consult') and decision['status'] == 'RUNNING' and not self.advisor_online():
            note = self.consult_off_note(decision['consult'])
            if decision['dispatch']:
                self.r.rpush(ADVICE_NOTES, note)  # rides on the next supervisor message (a report will come)
            else:  # consult-only reply: nothing else would wake the supervisor
                self.queue('supervisor', note + '\nReply with the JSON format from AGENTS.md.')

    def consult_off_note(self, items):
        """The one `[advisor off]` answer to consult items (at intake, or queued while on then turned off)."""
        which = ', '.join(x['task'] + ' (' + self.label(self.resolve(x.get('slot') or x['worker'])) + ')' for x in items)
        return ('[advisor off] consult ' + which + ' 未执行：顾问不在线。先在 fleet_decision 里发 advisor: "on"，'
                '再发 consult；或自己派纠正性 dispatch / BLOCKED')

    # -- small helpers
    def agent(self, name):
        return self.r.hgetall(key('agent', name))

    def runtime_cfg(self, name):
        if name == ADVISOR:  # fixed: official Claude CLI, fable / high, no launch record (§27)
            cfg = {k: v for k, v in self.cfg.items() if k != 'advisor'}
            cfg.update(worker_cli='claude', worker_model=advisor.MODEL, worker_effort=advisor.EFFORT)
            return cfg
        launch = json.loads(self.agent(name).get('runtime_launch') or '{}')
        cfg = pi_adapter.for_agent(self.cfg, name, launch.get('overrides'))
        if (launch.get('extras') or {}).get('effort'):
            cfg['worker_effort'] = launch['extras']['effort']  # advisor launch field (Claude --effort)
        return cfg

    def setf(self, name, **fields):
        self.r.hset(key('agent', name), mapping={k: str(v) for k, v in fields.items()})

    def task(self):
        return self.r.hgetall(TASK)

    # Each handoff successor is named slot.generation (worker-1 -> worker-1.1 -> worker-1.2);
    # the slot (redis key, cwd, notify argv) stays fixed so declared artifact paths survive.
    def label(self, slot):
        return self.agent(slot).get('label') or slot

    def labels(self):
        return [self.label(w) for w in self.active_workers()]

    def active_workers(self):
        return [w for w in self.workers if w in self.desired]

    def dispatch_targets(self):
        """Live labels plus older generation names, only for workers not retiring."""
        active = self.active_workers()
        return self.labels() + [n for n, slot in self.r.hgetall(NAMES).items() if slot in active]

    def resolve(self, name):
        return self.r.hget(NAMES, name) or name

    def running(self):
        return self.service or self.task().get('status') == 'running'

    SPEC_ONLY_LIFECYCLES = ('dev_task',)  # its task exists only as a spec (deliver)

    def typed_task_allowed(self):
        """Design §5 task mode: typed supervisor text becomes a goal-only task when nothing runs. Not in a
        lifecycle run: its task's end is the run's end (lifecycle_close holds or closes it), so text typed
        after that is out of bounds, never a new task; and a spec-only lifecycle takes its task from
        `task -` alone. Raises ValueError with the way in instead of a validation error about `deliver`."""
        lifecycle = self.cfg.get('lifecycle')
        if not lifecycle:
            return
        t = self.task()
        if t.get('status'):
            raise ValueError('task ' + t.get('id', '') + ' ended (' + t['status'] + '); this ' + lifecycle
                             + ' run is over and takes no operator input. Start a new run and submit its task spec '
                             'with `task -`')
        if lifecycle in self.SPEC_ONLY_LIFECYCLES:
            raise ValueError('a ' + lifecycle + ' run takes its task only as a spec submitted with `task -`, '
                             'not from typed text')

    def paused(self):
        return self.r.hget(SERVICE, 'paused') == '1'

    PAUSE_REF = ('slot', 'message_id', 'generation', 'subfleet_run', 'brake_kind')

    def pause(self, reason, kind=None, **ref):
        """Pause the run. ``kind`` (§11 "Pause cause"): the structured cause the daemon's pause review reads
        (``fx:service.cause`` = JSON {kind, **ref}, empty values left out); ``ref`` keys only from PAUSE_REF. No kind:
        no cause, and an older cause is dropped (the review then falls back to the reason text). On a run that is
        already paused, a kind the review may lift (``pause_review.LIFTABLE``: a worker hold, a dialog, ...) keeps the
        cause there, even an empty one; a never-lifted kind or no kind replaces it (ruling 29, review P0-01)."""
        from fleetmux.pause_review import LIFTABLE
        bad = set(ref) - set(self.PAUSE_REF)
        if bad:
            raise TypeError('pause: unknown cause key(s) ' + ', '.join(sorted(bad)))
        cause = json.dumps(dict({k: v for k, v in ref.items() if v not in (None, '')}, kind=kind),
                           ensure_ascii=False) if kind else ''
        kept = kind in LIFTABLE and self.paused()
        self.r.hset(SERVICE, mapping=dict({'paused': 1, 'reason': reason, 'at': time.time()},
                                          **({} if kept else {'cause': cause})))
        self.log('service.paused', reason=reason, **({'cause': kind} if kind else {}),
                 **({'cause_kept': self.r.hget(SERVICE, 'cause') or 'none'} if kept else {}))

    def resume(self):
        self.r.hset(SERVICE, mapping={'paused': 0, 'reason': '', 'at': time.time(), 'cause': '', 'review': ''})
        self.log('service.resumed')
        self.wall_baselines()

    def wall_baselines(self):
        """§67: what every slot's screen shows when the run is unpaused is old text. A wall counts later only
        when its line is below that position (wall_new); a same text printed after newer output is a new wall."""
        for name in self.names:
            if name == ADVISOR:
                continue
            info = self.agent(name)
            try:
                screen = self.capture_screen(name, info) if info.get('pane') else ''
            except Exception:  # noqa: BLE001  (never blocks a resume)
                screen = ''
            self.setf(name, wall_baseline=json.dumps({'generation': str(info.get('generation', '0')),
                                                      'lines': brake.screen_lines(screen)}, ensure_ascii=False))

    def wall_new(self, name, screen, wall):
        """Is the wall on ``screen`` new since the slot's baseline (same generation)? No baseline: yes."""
        try:
            base = json.loads(self.agent(name).get('wall_baseline') or 'null')
        except ValueError:
            base = None
        if not base or base.get('generation') != str(self.agent(name).get('generation', '0')):
            return True
        return brake.wall_after(base.get('lines') or [], screen, wall)

    def log(self, kind, **data):
        if self.v2:
            self.v2.log(kind, data)
            return
        self.r.rpush(LOG, json.dumps(dict(time=time.time(), kind=kind, **data), ensure_ascii=False))
        self.r.ltrim(LOG, -2000, -1)
        print(time.strftime('%H:%M:%S'), kind, json.dumps(data, ensure_ascii=False)[:180], flush=True)

    def queue(self, name, text, front=False, request_id=None, kind=None):
        """``front`` (v2): deliver before what is already queued (a correction of the reply just given).
        ``request_id`` / ``kind`` (v2): bind the message, e.g. a job notice (kind followup, §58)."""
        if name == 'supervisor' and self.r.hget(ROSTER, 'notify') == '1':
            # Appended, never prepended: the queue parses request/task headers at the start.
            self.r.hset(ROSTER, mapping={'notify': 0})
            text += '\n[Roster change] Workers you can dispatch to now: ' + ', '.join(self.labels())
        if name == 'supervisor' and self.r.hget(ROSTER, 'rejected'):
            text += ('\n[Roster rejected] ' + self.r.hget(ROSTER, 'rejected') + '; the roster in effect is unchanged: '
                     + ', '.join(self.labels()))
            self.r.hset(ROSTER, mapping={'rejected': ''})
        if name == 'supervisor':
            text += peer.drain(self)  # [peer CC] / unsolicited / violation notes (docs/design.md §21)
            if self.r.llen(ADVICE_NOTES):
                # Appended, never sent alone: a lone note forces a no-dispatch RUNNING (same as §16 / §21).
                text += '\n' + '\n'.join(self.r.lrange(ADVICE_NOTES, 0, -1))
                self.r.delete(ADVICE_NOTES)
            if self.r.llen(FRESH_HINTS):  # §37, appended like the advisor notes
                text += '\n' + '\n'.join(self.r.lrange(FRESH_HINTS, 0, -1))
                self.r.delete(FRESH_HINTS)
            if self.r.llen(SPLIT_NOTES):  # §44, appended like the advisor notes
                text += '\n' + '\n'.join(self.r.lrange(SPLIT_NOTES, 0, -1))
                self.r.delete(SPLIT_NOTES)
            if self.r.llen(decompose.TREE_NOTES):  # §62, appended like the advisor notes
                text += '\n' + '\n'.join(self.r.lrange(decompose.TREE_NOTES, 0, -1))
                self.r.delete(decompose.TREE_NOTES)
        if self.v2:
            return self.v2.queue(name, text, front=front, request_id=request_id, kind=kind)
        self.r.rpush(key('inbox', name), text)

    def inbox_depth(self, name):
        return self.v2.depth(name) if self.v2 else self.r.llen(key('inbox', name))

    def prepare_worker_profile(self, slot, instruction):
        """Authorize intent without spawning; recheck immediately before delivery."""
        task = self.task()
        try:
            registry = AgentRegistry(task.get('agent_registry') or None) if task.get('profile_assignments') else None
            return profile_runtime.prepare_profile(
                slot, instruction, task, self.agent(slot), registry,
                allow_profile_live_switch=self.cfg['allow_profile_live_switch'])
        except (OSError, TypeError, ValueError) as exc:
            blocked = profile_runtime.ProfileApplication(state='blocked', reason=str(exc))
            self.record_worker_profile(slot, blocked)
            self.log('profile.blocked', agent=slot, reason=str(exc))
            raise profile_runtime.ProfileRuntimeError(str(exc)) from exc

    def record_worker_profile(self, slot, application):
        self.setf(slot, **application.metadata(), profile_task=self.task().get('id', ''))

    def block_profile(self, name, reason, *, dead=False):
        fields = dict(profile_state='blocked', profile_reason=reason)
        if dead:
            fields.update(state='dead', launch_ready=0, runtime_ready=0, current_profile='')
        self.setf(name, **fields)
        self.log('profile.blocked', agent=name, reason=reason)
        self.finish('blocked', name + ': ' + reason)

    def preflight_profile(self, launch, cli_change_ok=False, slot=None):
        """Check dependencies before retiring anything. Never select a fallback CLI.
        cli_change_ok: only the roster (night) swap and the wall chain switch pass True -- the slots of the first
        already passed the pair table in night.resolve_launches (BC review r1 P1-01), the second checks it in
        wall_refused; every other path keeps the refusal. slot='supervisor': the launch changes the supervisor."""
        cfg = dict(self.cfg, **launch['overrides'])
        if (self.v2 and not cli_change_ok and cfg.get('worker_cli') != (self.cfg.get('worker_cli') or 'codex')):
            # The pair table admits whole-run supervisor x worker CLI pairs; a v2 profile live switch
            # that changes one slot's CLI (claude <-> pi bridge) mixes worker transports in one run,
            # which no smoke covers. Kept refused explicitly now that mixed pairs can be verified.
            raise ValueError('transport v2 profile live switch cannot change the worker CLI ('
                             + str(self.cfg.get('worker_cli')) + ' -> ' + str(cfg.get('worker_cli'))
                             + '); start a run with that worker CLI instead')
        runtime_v2.validate_config(cfg)  # reject unsupported v2 CLI before retirement
        cli = cfg['supervisor_cli' if slot == 'supervisor' else 'worker_cli']
        binary = cfg.get(cli) or cli
        if not shutil.which(binary):
            raise ValueError('profile CLI binary not found: ' + binary)
        if cli == 'pi':
            if not cfg.get('allow_unsandboxed_pi'):
                raise ValueError('pi profile requires --allow-unsandboxed-pi')
            version = subprocess.run([binary, '--version'], capture_output=True, text=True,
                                     timeout=15, check=True).stdout.strip()
            match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', version)
            if not match or tuple(map(int, match.groups())) < (1, 1, 0):
                raise ValueError('pi >= 1.1.0 required; found: ' + version)
            if not self.cfg.get('pi_home'):
                home = pi_adapter.prepare_home(self.run_dir / 'pi-home',
                                              cfg.get('pi_agent_dir') or str(Path.home() / '.pi/agent'),
                                              fleet_config.block('pi_context'))
                self.cfg['pi_home'] = str(home)
            if not Path(self.cfg['pi_home']).is_dir():
                raise ValueError('configured isolated pi_home does not exist')
        elif cli == 'codex':
            prepare_codex_home(codex_home(cfg), agent_profile(cfg, slot or 'worker'))

    def switch_profile(self, name, launch):
        """Same slot, successor generation, gated CLI exec until old tab is retired."""
        info = self.agent(name)
        if name not in self.workers or info.get('state') != 'idle':
            raise ValueError('profile switch requires an idle worker slot')
        if info.get('idle_confirmed') == '0':
            raise ValueError('worker idle state is unconfirmed after an abort; restart this dev slot before switching')
        # An online advisor authorizes its sheets' respawns; every other safety step below is unchanged.
        if (not self.advisor_online()
                and not profile_runtime.task_live_setting(self.task(), self.cfg['allow_profile_live_switch'])):
            raise ValueError('profile switch requires task apply_profiles_live=true')
        if agent_cli(self.runtime_cfg(name), name) == 'codex' and active_children(info.get('thread'), self.sessions):
            self.setf(name, profile_state='pending', profile_reason='waiting for native subagents to finish')
            return
        self.preflight_profile(launch)
        generation, old, prev = int(info['generation']), info['pane'], self.label(name)
        if not old or self.mgr.get_pane_status(old) is None:
            raise ValueError('old runtime tab is missing; cannot safely replace it')
        label = name + '.' + str(generation + 1)
        note = self.run_dir / 'handoffs' / (name + '-g' + str(generation) + '-profile.md')
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text('Idle profile switch: ' + prev + ' -> ' + label + '\n'
                        'Target profile: ' + launch['profile_id'] + '\n'
                        'The queued instruction is retained by the daemon. No task work before READY.\n')
        snapshot = note.with_suffix('.terminal.txt')
        self.mgr.capture_to(old, str(snapshot))
        history.record(self.run_dir, slot=name, label=prev, generation=generation,
                       handoff=str(note), snapshot=str(snapshot), source='profile-switch', timed_out=False)
        # Fence old callbacks before touching shared role files or creating a successor.
        self.setf(name, state='starting', generation=generation + 1, label=label, prev_label=prev,
                  runtime_launch=profile_launch.encode_launch(launch), current_profile='', runtime_ready=0,
                  runtime_profile='', profile_state='switching', launch_ready=0, retired_pane=old,
                  retired_launch=info.get('runtime_launch') or '')
        self.r.hset(NAMES, mapping={label: name})
        new = None
        try:
            new = self.spawn(name, 'You are fleet agent ' + label + '. Read AGENTS.md first. '
                             'This is an idle runtime profile switch. Do no task work. Reply exactly READY.',
                             launch_held=True)
            if not self.mgr.tab_into(new, old):
                raise RuntimeError('could not place successor in the worker pane')
            if not self.mgr.kill_pane(old) or self.mgr.get_pane_status(old) is not None:
                raise RuntimeError('could not confirm retirement of old runtime ' + prev)
            self.setf(name, retired_pane='', retired_launch='', launch_ready=1)
            self.refresh_history()
            self.log('profile.switch', agent=name, label=label, generation=generation + 1,
                     profile=launch['profile_id'], cli=launch['overrides']['worker_cli'])
        except (Exception, SystemExit) as exc:
            # Revoke launch before cleanup; even a late READY cannot revive this generation.
            self.setf(name, launch_ready=0, state='dead')
            new = new or self.agent(name).get('pane')
            if new == old:
                new = None
            cleaned = True
            if new:
                try:
                    if not self.mgr.kill_pane(new):
                        cleaned = False
                        self.log('profile.cleanup_failed', agent=name, pane=new)
                except Exception as cleanup:
                    cleaned = False
                    self.log('profile.cleanup_failed', agent=name, pane=new, reason=str(cleanup))
            # Roll back the reservation (model_usage): with the new tab gone the slot holds only what
            # its old generation runs, and only while that tab is still alive. A new tab that could
            # not be closed keeps holding the new model.
            if cleaned:  # the whole launch record, not only its runtime half
                self.setf(name, runtime_launch=info.get('runtime_launch') or '',
                          installed_domains=info.get('installed_domains') or '')
            try:
                gone = self.mgr.get_pane_status(old) is None
            except Exception:
                gone = False
            if gone:
                self.setf(name, retired_pane='', retired_launch='')
            self.block_profile(name, 'profile switch failed: ' + str(exc), dead=True)

    def workspace(self):
        return self.run_dir / 'workspace'

    def fleet_cmd(self):
        return shlex.join([self.cfg['python'], str(Path(__file__).resolve()), 'chrome', '--config', str(self.path)])

    def role_scope(self, name, cfg, launch):
        """The AGENTS.md text of one agent (also written by a sub-fleet over its adopted pane)."""
        role = 'supervisor' if name == 'supervisor' else 'worker'
        # v2 strips the [Request <id>] header (a transport id) before delivery and binds the id in
        # code, so an official-CLI service supervisor gets the JSON-text role without request ids.
        variant = ('_v2' if self.v2 and is_bridge(cfg, name) else
                   '_tui_v2' if self.v2 and self.service and role == 'supervisor' else '')
        scope = (HERE / 'agents' / (('service_' if self.service else '') + role + variant + '.md')).read_text()
        if launch.get('role_text') and launch['role_text'].strip() != scope.strip():
            scope += '\n## Assigned profile role\n\n' + launch['role_text']
        roles = self.cfg.get('worker_roles') or {}
        extra = (Path(roles[name]) if name in roles else
                 Path(self.cfg['roles']).expanduser() / (name + '.md') if self.cfg.get('roles') else None)
        if extra and extra.is_file():  # this agent's own prompt (one per worker in service mode)
            scope += '\n## 4. Your role (' + name + ')\n\n' + extra.read_text()
        if self.contract_phase():  # successors / roster-added workers inside a contract task
            scope += '\n' + self.contract_doc(role, name)
        if role == 'worker':
            scope += applet_lease.rule_section(self, name)  # an open lease's section
        task_dir = self.cfg.get('task_dir') if self.cfg.get('parent_config') else None
        if role == 'worker':  # roster `writable:` grants extra roots beyond the own directory (user 2026-11-03)
            # §46: grants the program issued from a top ruling sit right behind the rule (set_grants rewrites them)
            scope = scope.replace(OWN_DIR_RULE, OWN_DIR_RULE + program_grants_notice(self.grants(name))
                                  + granted_roots_notice(self.cfg, name)
                                  + (SUBFLEET_TASK_DIR_RULE.replace('{{TASK_DIR}}', task_dir) if task_dir else '')
                                  + (SUBFLEET_WORKER_RULE if self.cfg.get('parent_config') else ''))
        if role == 'supervisor' and self.cfg.get('parent_config'):  # §46: no exceptions granted inside a sub-fleet
            bounds = SUBFLEET_BOUNDS.replace('{{TASK}}', str(self.cfg.get('task_id')))
            if task_dir:  # §44 / §20 sub-fleets: the delegating worker's directory is inside the boundary
                bounds = bounds.replace(SUBFLEET_BOUNDS_ROOTS, SUBFLEET_BOUNDS_ROOTS + SUBFLEET_TASK_DIR_BOUND.replace(
                    '{{TASK_DIR}}', task_dir)).replace("writing in the parent fleet's folders",
                                                       "writing in the parent fleet's other folders")
            scope += bounds
        if not self.service and (self.decompose_on() if self.running() else decompose.enabled()):
            # §62: the tree section instead of "Leaf blocks"; a worker's split request carries both accounts
            scope = decompose.prompt_swap(scope) if role == 'supervisor' else decompose.worker_swap(scope)
        scope = scope.replace('{{SUPERVISOR_SPLIT}}\n', SUPERVISOR_SPLIT_DOC if self.cfg.get('supervisor_split') else '')
        scope = scope.replace('{{WORKSPACE}}', str(self.workspace())).replace('{{FLEET_CHROME}}', self.fleet_cmd())
        scope = scope.replace('{{ROSTER}}', roster_notice(self.cfg))
        scope = scope.replace('{{SESSION_BOARD}}', shlex.join([self.cfg['python'], str(HERE / 'session_board.py')]))
        sandbox_notice = ('You run inside a sandbox. Driving the user\'s browser directly (AppleScript, GUI automation) '
                          'fails there with an error. That error means the sandbox stopped you, not that the browser '
                          'is broken. Browser actions go only through the broker command below, if enabled.')
        scope = scope.replace('{{SANDBOX_NOTICE}}', sandbox_notice if agent_cli(cfg, name) == 'codex'
                              else dsh_adapter.SCOPE if is_dsh(cfg, name) else pi_adapter.UNSANDBOXED_SCOPE)
        if is_bridge(cfg, name):
            scope += '\n## Runtime permissions\n\n' + (dsh_adapter.SCOPE if is_dsh(cfg, name)
                                                         else pi_adapter.UNSANDBOXED_SCOPE) + '\n'
        if self.v2 and role == 'supervisor' and not self.cfg.get('parent_config'):
            scope += daemon_down_section(self.cfg, is_pi(cfg, name))
        # roster `import_claude_md` (user 2026-11-11), '' without it; an advised Claude slot runs with
        # --setting-sources project and so gets ~/.claude/CLAUDE.md only through this copy
        return scope + imported_rules(self.cfg, native_user_memory=is_claude(cfg, name) and not cfg.get('advisor'))

    # -- actors
    def spawn(self, name, prompt, *, launch_held=False, advice_hold=False, advised=None):
        """``advised``: only a slot's first generation (spawn_ready) passes it, from the advisor switch;
        every later generation replays the slot's launch record (advised_layout), whatever the switch says."""
        if name == 'supervisor':  # section 60: a snapshot before each new supervisor generation (never raises)
            restart_state.before_supervisor(self)
        prompt = applet_lease.start_prompt(self, name, prompt)  # a leased slot: the Lease block (applets.md 5)
        sandbox.preflight(self, name)  # P8o / M172: mode os without a wrapper -> audit + refuse (no policy: no-op)
        if name == ADVISOR:
            return self.spawn_advisor(prompt, launch_held=launch_held)
        cfg = self.runtime_cfg(name)
        info = self.agent(name)
        generation = int(info.get('generation', 0))
        launch = json.loads(info.get('runtime_launch') or '{}')
        cfg_path = self.path
        if advised is None:
            advised = self.advised_layout(name)
        if not is_claude(cfg, name) and info.get('advice_domains'):
            self.setf(name, advice_domains='')  # a codex / pi sheet stages no kit: never left behind
        # This generation's argv (--effort / --setting-sources project) follows the layout, never the
        # in-memory switch that runtime_cfg copies from self.cfg.
        cfg.pop('advisor', None)
        if advised and is_claude(cfg, name):
            cfg['advisor'] = True
        # A held first spawn gets its model / effort written here on release; an advised Claude
        # generation keeps `advisor` in its own snapshot (the actor builds its argv from it).
        if launch or advice_hold or (advised and is_claude(cfg, name)):
            cfg_path = self.run_dir / 'runtime' / (name + '-g' + str(generation) + '.json')
            save(cfg_path, cfg)  # generation-specific callback config; never edit old snapshots
        cwd = agent_cwd(self.cfg, name)
        cwd.mkdir(parents=True, exist_ok=True)
        if agent_cli(cfg, name) == 'codex' and not codex_queue.is_codex(cfg, name):
            self.log('queue.unsupported', agent=name, generation=generation, codex=cfg.get('codex') or '',
                     version=cfg.get('codex_version') or '', fallback='typed')
        role = 'supervisor' if name == 'supervisor' else 'worker'
        scope = self.role_scope(name, cfg, launch)
        if is_opencode(cfg, name):
            # The role lives only in this generation's fresh instance dir ($OPENCODE_CONFIG_DIR/AGENTS.md):
            # an AGENTS.md / CLAUDE.md in the cwd would be loaded as project / nested instructions.
            opencode_adapter.prepare_instance(cfg, name, generation, scope, task_dir=cwd)
            prompt = prompt.replace(' Read AGENTS.md in the current directory first.', '').replace(' Read AGENTS.md first.', '')
        else:
            (cwd / 'AGENTS.md').write_text(scope)
        if role == 'worker':  # written by releases with a review gate; no review text is left behind
            (cwd / 'REVIEWER.md').unlink(missing_ok=True)
        if is_claude(cfg, name) and advised:
            # CLAUDE.md = scope + core + domain rules. A held first generation has not started: its record
            # is written when the gate opens (release_hold with the sheet, release_default with nothing).
            self.install_advised_kit(name, cwd, scope, record=not advice_hold)
            # CLAUDE.md already carries the scope: do not make Claude read the same text twice.
            prompt = prompt.replace(' Read AGENTS.md in the current directory first.', '').replace(' Read AGENTS.md first.', '')
            trust_claude_dir(cwd)
        elif is_claude(cfg, name):
            (cwd / 'CLAUDE.md').write_text(scope)  # Claude Code reads CLAUDE.md, not AGENTS.md
            self.install_default_kit(cwd, scope)
            trust_claude_dir(cwd)
        elif is_codex(cfg, name):
            trust_agent_dir(codex_home(self.cfg), cwd)
        if codex_queue.unsettled(self.agent(name)):
            codex_queue.clear(self, name, 'respawn')  # audited; its token can never settle in the new session
        self.setf(name, role=role, generation=generation, state='starting', thread='', pane='',
                  transcript='', ctx_used=0, ctx_window=0, msg_id='', msg_done='',
                  busy_since=time.time(), delivered=0, retry_at=0, last_msg='', empty_tries=0,
                  launch_ready=int(not launch_held), runtime_ready=0, current_profile='', idle_confirmed=0,
                  **{k: '' for k in codex_queue.FIELDS})  # a new session starts without a queued delivery
        if self.v2:
            self.setf(name, bridge_available=0, startup_deadline=awake.deadline(self.cfg['profile_ready_timeout']))
        if advised or info.get('advice_hold'):  # a resumed, never-released held slot must not stay marked held
            self.setf(name, advice_hold=int(advice_hold))
        if launch:
            self.setf(name, profile_state='switching', runtime_profile=launch['profile_id'],
                      profile_ready_deadline=awake.deadline(self.cfg['profile_ready_timeout']))
        prompt_file = self.run_dir / 'prompts' / (name + '-g' + str(generation) + '.md')
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt)
        result = self.mgr.spawn_interactive_cli(
            window_name='fx-' + self.cfg.get('tab_prefix', '') + self.label(name), prompt_file=str(prompt_file),
            start_directory=str(opencode_adapter.launch_cwd(cfg, name, generation) if is_opencode(cfg, name) else cwd),
            cli_command=shlex.join([self.cfg['python'], str(Path(__file__).resolve()), 'actor', str(cfg_path), name,
                                    '--generation', str(generation)]),
            output_file=str(self.run_dir / 'prompts' / (name + '-g' + str(generation) + '.terminal.log')),
            auto_close=False, metadata={'type': role, 'cli_type': agent_cli(cfg, name),
                      'profile': (cfg.get('pi_model') if is_pi(cfg, name) else
                                  cfg.get('opencode_model') if is_opencode(cfg, name) else
                                  cfg.get('dsh_model') if is_dsh(cfg, name) else
                                  claude_model(cfg, name) if is_claude(cfg, name) else agent_profile(cfg, name))},
            env_vars=dict({'PATH': pane_path(cfg, os.environ.get('PATH', ''), is_pi(cfg, name))}, **(
                pi_adapter.environment(cfg_path, cfg, name, generation) if is_pi(cfg, name)
                else opencode_adapter.environment(cfg_path, cfg, name, generation, label=self.label(name),
                                                  startup_prompt=prompt, task_dir=cwd) if is_opencode(cfg, name)
                else {} if is_dsh(cfg, name)  # the actor sets the pane env (with the key): not on the command line
                else dict({'CODEX_HOME': str(codex_home(self.cfg))}, **(CLAUDE_ENV if is_claude(cfg, name) else {})))),
            **subfleet.tmux_spawn_args(self.cfg, name, generation, info))
        if not result.success:
            if result.pane_id:
                self.setf(name, pane=result.pane_id)
            raise RuntimeError('spawn failed: ' + name + ': ' + str(result.error))
        self.setf(name, pane=result.pane_id)
        self.log('spawn', agent=name, generation=generation, pane=result.pane_id)
        return result.pane_id

    def spawn_advisor(self, prompt, launch_held=False):
        """One generation of the resident advisor tab (§27): fixed argv (claude_argv), cwd <run>/advisor, the
        role prompt <run>/advisor.md; no AGENTS.md, no kit, no domains, no launch record."""
        name = ADVISOR
        generation = int(self.agent(name).get('generation') or 0)
        prompt = prompt.replace(' Read AGENTS.md in the current directory first.', '')  # its role is advisor.md
        cfg_path = self.run_dir / 'runtime' / (name + '-g' + str(generation) + '.json')
        save(cfg_path, self.runtime_cfg(name))
        cwd = agent_cwd(self.cfg, name)
        cwd.mkdir(parents=True, exist_ok=True)
        (self.run_dir / 'handoffs').mkdir(parents=True, exist_ok=True)  # --add-dir must exist
        (self.run_dir / 'advisor.md').write_text(advisor.PROMPT.read_text() + advisor.RESIDENT_NOTE)
        trust_claude_dir(cwd)
        self.setf(name, role='advisor', generation=generation, state='starting', thread='', pane='',
                  transcript='', ctx_used=0, ctx_window=0, msg_id='', msg_done='', busy_since=time.time(),
                  delivered=0, retry_at=0, last_msg='', empty_tries=0, launch_ready=int(not launch_held),
                  runtime_ready=0, idle_confirmed=0, bridge_available=0, spawn_failed=0, advisor_fault='',
                  startup_deadline=awake.deadline(self.cfg['profile_ready_timeout']))
        prompt_file = self.run_dir / 'prompts' / (name + '-g' + str(generation) + '.md')
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt)
        result = self.mgr.spawn_interactive_cli(
            window_name='fx-' + self.cfg.get('tab_prefix', '') + self.label(name), prompt_file=str(prompt_file),
            start_directory=str(cwd),
            cli_command=shlex.join([self.cfg['python'], str(Path(__file__).resolve()), 'actor', str(cfg_path), name,
                                    '--generation', str(generation)]),
            output_file=str(self.run_dir / 'prompts' / (name + '-g' + str(generation) + '.terminal.log')),
            auto_close=False, metadata={'type': 'advisor', 'cli_type': 'claude', 'profile': advisor.MODEL},
            env_vars=dict({'PATH': os.environ.get('PATH', '')}, **CLAUDE_ENV))
        if not result.success:
            if result.pane_id:
                self.setf(name, pane=result.pane_id)
            raise RuntimeError('spawn failed: ' + name + ': ' + str(result.error))
        self.setf(name, pane=result.pane_id)
        self.log('spawn', agent=name, generation=generation, pane=result.pane_id)
        return result.pane_id

    def advised_layout(self, name):
        """Whether this slot's next generation is laid out advised (domain kit links + RULES in CLAUDE.md,
        --setting-sources project, --strict-mcp-config, --effort), read only from the slot's launch record:
        installed_domains '' = the default layout (user-level settings, full kit copy), a JSON list (even
        []) = advised; a sheet staged in advice_domains for this spawn counts as advised. The advisor switch
        is never asked: a handoff / respawn / resume successor launches exactly like its predecessor, and
        only an applied advisor sheet changes the record (2026-10-14)."""
        if name == 'supervisor':
            return False
        info = self.agent(name)
        return bool(info.get('advice_domains') or info.get('installed_domains'))

    def init_worker(self, name):
        self.setf(name, label=name, current_profile='', runtime_profile='', advisory_profile='',
                  runtime_ready=0, runtime_launch='', profile_task='', installed_domains='', advice_domains='',
                  **profile_runtime.ProfileApplication().metadata())
        self.r.hset(NAMES, mapping={name: name})
        if named_subfleet.reserved(self, name, attached_only=True):  # a worker added for an attached sub-fleet
            named_subfleet.tmux_mark(self, name)
            named_subfleet.added_launch(self, name)  # on the supervisor model its start asked for

    def spawn_ready(self, name):
        # v2 + advisor online: a Claude worker waits behind the launch gate for its first sheet (no
        # default generation that the first dispatch would have to replace).
        # A named sub-fleet's supervisor_slot never waits: up / autostart need it idle and READY, and a sheet only
        # comes with a delivery, which a supervisor slot never gets before its sub-fleet is up (10-14 Studio deadlock).
        # D36 (Studio 2026-11-22): the same for a worker added for a queued job (job_hot_add, `hot_slot` of the job):
        # job_start adopts it as the job sub-fleet's supervisor only once it is idle and READY, and no delivery
        # (so no first sheet) ever comes to it before that; held, the job queued for good.
        sub_supervisor = (name in {e.get('supervisor_slot') for e in (self.cfg.get('subfleets') or [])}
                          or bool(named_subfleet.reserved(self, name))  # an attached child's slot too
                          or any(j.get('hot_slot') == name for j in self.job_state().values()))  # a job's added worker
        hold = (bool(self.v2) and self.advisor_online() and name != 'supervisor' and not sub_supervisor
                and is_claude(self.runtime_cfg(name), name))
        # The first generation is the one place the switch picks the layout; later ones replay the record.
        return self.spawn(name, 'You are fleet agent ' + name + '. Read AGENTS.md in the current directory first. '
                                'There is no task yet: your final reply must be exactly READY.',
                          launch_held=hold, advice_hold=hold,
                          advised=self.advisor_online() and name != 'supervisor')

    def start_all(self):
        if self.cfg.get('adopt'):
            return subfleet.start_adopted(self)  # sub-fleet: the delegated worker pane is the supervisor
        self.setf('supervisor', label=SUPERVISOR_LABEL)
        for name in self.workers:
            self.init_worker(name)
            self.apply_roster_launch(name)
        for name in self.names:
            self.spawn_ready(name)
        try:  # the fleet default is accepted as started; say when it is over a model card's ticket count
            over = model_usage.over_cap(self.model_usage(), AgentRegistry())
        except (AgentRegistryError, OSError):
            over = []
        if over:
            self.log('capacity.over', level='warning', models=over,
                     reason='card max_concurrent counts slots; later profile switches into these models wait')
        history.write_page(self.run_dir, self.history_slots(), self.history_links())
        # One page: supervisor above history/control on the left, worker tabs on the right.
        self.arrange_page()
        self.announce_history_page()
        if self.advisor_resident() and self.advisor_enabled():
            self.advisor_start()  # its tab next to the supervisor's; the held first workers wait for it

    def history_slots(self):
        # Workers removed from the roster keep their history (they have entries but no slot).
        retired = [s for s in dict.fromkeys(e.get('slot') for e in history.entries(self.run_dir))
                   if s and s not in self.names]
        shells = getattr(self.mgr, 'shell_slots', None)  # fleetmux: the sidebar bash tabs it took in (user 2026-11-09)
        return ([(n, self.label(n), SUPERVISOR_LABEL if n == 'supervisor' else n) for n in self.names + retired]
                + (shells() if shells else []))

    def history_links(self):
        return subfleet.page_links(self.cfg)

    # -- events
    def handle(self, ev, *, _v2_internal=False):
        if self.v2 and not _v2_internal:
            return runtime_v2.post_event(self.v2.bus, ev)
        kind = ev.get('kind')
        if kind == 'task':
            try:
                if self.service or self.job_root_busy() or ev.get('job'):
                    # §58: a job, run by its own task sub-fleet; never a task of this fleet. A task root takes jobs
                    # while its own task runs (§19 "Task root"); `job` (a join stub's task) is never its own task.
                    jid = self.job_submit(ev.get('spec'))
                    answer = {'status': 'accepted', 'job_id': jid, 'task_id': jid}
                else:
                    answer = {'status': 'accepted', 'task_id': self.submit(ev.get('spec'))}
            except (ValueError, TypeError) as exc:
                answer = {'status': 'rejected', 'reason': str(exc)}
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind == 'request':
            try:
                answer = {'status': 'accepted', 'id': self.request(ev.get('spec'))}
            except named_subfleet.InProgress as exc:  # §29: a retry of an op still open
                answer = {'status': 'in_progress', 'id': exc.rid, 'op_status': exc.status}
            except (ValueError, TypeError) as exc:
                answer = {'status': 'rejected', 'reason': str(exc)}
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind == 'operator_input':
            try:
                info = self.agent('supervisor')
                if ev.get('agent') != 'supervisor':
                    raise ValueError('operator input goes to the supervisor only')
                problem = operator_channel_problem(self.cfg)
                if problem:
                    raise ValueError(problem)
                if (ev.get('generation') != int(info.get('generation', -1))
                        or ev.get('thread') != info.get('thread')):
                    raise ValueError('this input belongs to an old supervisor session (generation '
                                     + str(ev.get('generation')) + ', current ' + str(info.get('generation')) + ')')
                if info.get('state') not in ('idle', 'busy'):
                    raise ValueError('supervisor is not ready (state ' + str(info.get('state') or 'unknown') + ')')
                if not isinstance(ev.get('text'), str) or not 1 <= len(ev['text'].strip()) <= 32000:
                    raise ValueError('operator input must be 1-32000 chars of text')
                if self.service:
                    rid = self.request({'id': 'ui-' + uuid.uuid4().hex,
                                        'payload': {'message': ev['text']},
                                        'instruction': 'The operator typed the payload message in your terminal. '
                                                       'Route it to a worker; do not invent request IDs. '
                                                       'The worker puts its answer text in the reply field.',
                                        'reply': {'required': ['reply'], 'allowed': ['reply']}})
                    self.r.hset(REQ + rid, mapping={'operator': '1'})
                    answer = {'status': 'accepted', 'id': rid}
                elif self.running():
                    self.queue('supervisor', '[Operator instruction] ' + ev['text']
                               + '\nContinue the current task using the AGENTS.md decision JSON.')
                    answer = {'status': 'accepted', 'task_id': self.task()['id']}
                else:
                    self.typed_task_allowed()
                    answer = {'status': 'accepted', 'task_id': self.submit({'goal': ev['text']})}
            except (ValueError, TypeError) as exc:
                answer = {'status': 'rejected', 'reason': str(exc)}
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind == 'reconcile':  # section 60: `fleetmux reconcile rule` (the operator's rulings)
            try:
                rulings = restart_state.validate_decision(ev.get('rulings'))
                answer = dict(restart_state.decide(self, rulings, by=str(ev.get('by') or 'operator'),
                                                   ledger=ev.get('ledger') or None), status='accepted')
            except (ValueError, TypeError) as exc:
                answer = {'status': 'rejected', 'reason': str(exc)}
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind == 'escalation_answer':  # §24: `fleetmux answer`, the user's own ruling on a report
            try:
                answer = blocked_scope.user_answer(self, ev)
            except (ValueError, TypeError, KeyError, OSError) as exc:
                answer = {'status': 'rejected', 'reason': str(exc)[:300]}
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind == 'job_cancel':  # section 58 "Restart": `task --cancel <job id>` (a join stub's stop)
            try:
                answer = self.job_cancel(str(ev.get('job') or ''), by=str(ev.get('by') or 'operator'))
            except (ValueError, TypeError) as exc:
                answer = {'status': 'rejected', 'reason': str(exc)}
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind == 'heal':  # §67: the parent's heal items for this sub-fleet (run by heal.tick)
            answer = heal.on_event(self, ev)
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind == 'recheck':  # `task --recheck`: the gate was recomputed by the CLI (lifecycle_recheck)
            from fleetmux import lifecycle_recheck
            try:
                answer = lifecycle_recheck.apply(self, ev)
            except (ValueError, TypeError) as exc:
                answer = {'status': 'rejected', 'reason': str(exc)}
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind in ('pause', 'resume'):
            answer = {'status': 'accepted'}
            if kind == 'pause':
                self.pause(ev.get('reason') or 'paused by operator', kind='operator')
            else:
                was_paused = self.paused()
                try:
                    self.brake_resume(ev.get('raise'))
                    self.resume()
                except (ValueError, TypeError) as exc:
                    answer = {'status': 'rejected', 'reason': str(exc)}
                if answer['status'] == 'accepted' and self.v2:
                    # §24: held slots get an explicit answer (released / regenerating / held with reason and next
                    # step); 'rejected' only when this call changed nothing at all and a slot stays held.
                    held = self.v2.resume_held()
                    answer.update(held)
                    if held['held'] and not (was_paused or ev.get('raise') or held['released']
                                             or held['regenerating']):
                        answer['status'] = 'rejected'
                        answer['reason'] = (', '.join(x['label'] for x in held['held'])
                                            + ' held and cannot be released now; see held[].next')
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind == 'stop':
            if not self.service and self.running():
                self.finish('cancelled', 'stopped by operator')
            self.stopping = True
            self.r.set('fx:reply:' + ev['request_id'], json.dumps({'status': 'accepted'}), ex=86400)
        elif kind == 'chrome':
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(self.chrome(ev.get('argv')), ensure_ascii=False), ex=3600)
        elif kind == 'peer':
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(peer.broker(self, ev), ensure_ascii=False), ex=3600)
        elif kind == 'pi_session':
            name = ev.get('agent')
            info = self.agent(name or '')
            if info and is_pi(self.runtime_cfg(name), name) and ev.get('generation') == int(info.get('generation', 0)):
                self.pi_metadata(name, ev)
        elif kind == 'turn':
            self.on_turn(ev)
        elif kind == 'dialog_user_answer':   # P6b: step two / reject from the phone (contracts §8)
            answer = self.v2.user_dialog_answer(ev) if self.v2 else {'status': 'stale', 'reason': 'no runtime'}
            if ev.get('request_id'):
                self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)

    def chrome(self, argv):
        script = self.cfg.get('chrome_script')
        if not script:
            return {'status': 'rejected', 'reason': 'browser broker disabled (start with --chrome-script)'}
        if not isinstance(argv, list) or not argv or argv[0] not in CHROME_VERBS \
                or not all(isinstance(a, str) for a in argv):
            return {'status': 'rejected', 'reason': 'allowed verbs: ' + ' / '.join(sorted(CHROME_VERBS))}
        if argv[0] == 'js':
            if len(argv) != 3:
                return {'status': 'rejected', 'reason': 'usage: js <tab-index> <script-file>'}
            path = Path(argv[2]).resolve()
            if not path.is_relative_to(self.run_dir.resolve()) or not path.is_file():
                return {'status': 'rejected', 'reason': 'js script must be a file inside this run directory'}
            argv = argv[:2] + [str(path)]
        # Pace page actions here, not in the prompt: models do not honour "wait N seconds",
        # and an unpaced night of page loads can get the operator's IP firewalled.
        wait = self.last_chrome + float(self.cfg['chrome_min_interval']) - time.time()
        if argv[0] != 'check' and wait > 0:
            time.sleep(wait)
        self.last_chrome = time.time()
        try:
            p = subprocess.run([script] + argv, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return {'status': 'error', 'rc': -1, 'out': 'browser broker timed out after 120 s'}
        self.log('chrome', argv=argv[:2], rc=p.returncode)
        return {'status': 'ok', 'rc': p.returncode, 'out': (p.stdout + p.stderr)[-200000:]}

    # -- interface contracts (task spec "contracts": true; docs/design.md §18)
    def contract_state(self):
        raw = None if self.service else self.r.get(CONTRACTS)
        return json.loads(raw) if raw else None

    def contract_phase(self):
        state = self.contract_state()
        return state['phase'] if state and self.running() else None

    def save_contract(self, state):
        self.r.set(CONTRACTS, json.dumps(state, ensure_ascii=False, separators=(',', ':')))
        self.sync_contract_files(state)

    def sync_contract_files(self, state=None):
        """Copy the registry view to <run>/contracts.json and <workspace>/contracts.json.
        Redis is the truth; tick() re-syncs after a v2 intent replay or a resume."""
        state = state or self.contract_state()
        if state is None:
            return
        text = json.dumps(contracts.view(state, self.cfg.get('worker_cli') or 'codex', peer.runtime(self)),
                          ensure_ascii=False, indent=2)
        if text == getattr(self, 'contracts_text', None):
            return
        for path in (self.run_dir / 'contracts.json', self.workspace() / 'contracts.json'):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(text + '\n')
            os.replace(tmp, path)
        self.contracts_text = text

    def contract_log(self, action, **data):
        with open(self.run_dir / 'contracts.log.jsonl', 'a') as out:
            out.write(json.dumps(dict(time=time.time(), action=action, **data), ensure_ascii=False) + '\n')
        self.log('contract.' + action, **data)

    # -- decomposition tree (docs/design.md §62): fx:tree beside fx:items, only while the task's switch is on
    def decompose_on(self):
        """The task was submitted with the tree switch on (read once at submit; a task mode fleet only)."""
        return not self.service and self.task().get('decompose') == '1'

    def tree_root(self):
        """(root id, id prefix of this level): ('root', 'M') in a top fleet, (M1, 'M1.') in sub-fleet M1."""
        task_id = self.cfg.get('task_id')
        return (task_id, task_id + '.') if task_id else ('root', 'M')

    def tree_state(self):
        raw = self.r.get(decompose.TREE)
        try:
            tree = json.loads(raw) if raw else None
        except ValueError:
            tree = None
        return tree if isinstance(tree, dict) and isinstance(tree.get('nodes'), list) else None

    def tree_params(self):
        """The decompose parameters as this fleet runs them: Redis (loaded at start), else the configuration."""
        try:
            return decompose.params_get(self.r)
        except (OSError, ValueError, KeyError):
            return decompose.merged({})

    def decompose_load(self):
        """Daemon start / resume: the decompose block into this fleet's Redis (task and service fleets alike)."""
        decompose.params_load(self.r)

    def decompose_flush(self, force=False):
        """Tick (and exit with ``force``): a changed decompose block back to the private fleet.yaml. Only a root
        fleet writes back, a service root too; a sub-fleet's copy is never written (its tune goes to the root)."""
        if getattr(self, 'r', None) is None or self.cfg.get('parent_config'):
            return False
        written = decompose.params_flush(self.r, force=force)
        if written:
            self.log('decompose.flushed')
        return written

    def tree_context(self):
        """parse_decision's ``tree_ctx``: None while the switch is off for this task."""
        if not self.decompose_on():
            return None
        root, prefix = self.tree_root()
        tree = self.tree_state() or decompose.new_tree(root, self.task().get('goal', ''))
        return {'tree': tree, 'root': root, 'prefix': prefix, 'params': self.tree_params(),
                'running': [n['id'] for n in tree['nodes'] if n.get('state') == 'running']}

    def save_tree(self, tree):
        tree['review'] = decompose.review(tree)  # §62: the review table is recomputed on every save
        self.r.set(decompose.TREE, json.dumps(tree, ensure_ascii=False, separators=(',', ':')))
        self.sync_tree_files(tree)

    def sync_tree_files(self, tree=None):
        """Copy fx:tree to <run>/tree.json and <workspace>/tree.json (Redis is the truth; the tick re-syncs)."""
        tree = tree or self.tree_state()
        if tree is None:
            return
        text = json.dumps(tree, ensure_ascii=False, indent=2)
        if text == getattr(self, 'tree_text', None):
            return
        for path in (self.run_dir / 'tree.json', self.workspace() / 'tree.json'):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(text + '\n')
            os.replace(tmp, path)
        self.tree_text = text

    def tree_restore(self):
        """Daemon start: a running task's tree missing from Redis (no AOF) comes back from <run>/tree.json."""
        if not self.decompose_on() or not self.running() or self.tree_state() is not None:
            return
        try:
            tree = json.loads((self.run_dir / 'tree.json').read_text())
        except (OSError, ValueError):
            return
        if isinstance(tree, dict) and isinstance(tree.get('nodes'), list):
            self.r.set(decompose.TREE, json.dumps(tree, ensure_ascii=False, separators=(',', ':')))
            self.log('tree.restored', rev=tree.get('rev'))

    def tree_decision(self, decision):
        """Save the tree change of a valid decision (parse_decision left it in ``_tree``). Returns the new tree, or
        None when the reply changed nothing. The first tree the program accepts sets ``accepted`` and queues the
        opening summary for the tick."""
        new = decision.pop('_tree', None)
        if new is None or not self.decompose_on():
            return None
        first = not new.get('accepted')
        new['rev'] = int(new.get('rev') or 0) + 1
        if first:
            new['accepted'] = new['rev']
        decompose.judge(new, self.tree_params(), time.time())
        old = {n['id'] for n in (self.tree_state() or {'nodes': []})['nodes']}
        self.save_tree(new)
        ids = {n['id'] for n in new['nodes']}
        with open(self.run_dir / 'tree.log.jsonl', 'a', encoding='utf-8') as out:
            out.write(json.dumps({'time': time.time(), 'rev': new['rev'], 'by': 'supervisor', 'added': sorted(ids - old),
                                  'dropped': sorted(old - ids), 'tree': decision.get('tree') or [],
                                  'tree_drop': decision.get('tree_drop') or []}, ensure_ascii=False) + '\n')
        self.log('tree.accepted', rev=new['rev'], nodes=len(new['nodes']) - 1, chain=new['chain']['ids'],
                 chain_minutes=new['chain']['minutes'], width=new['width'], first=first)
        if first:
            self.r.set(decompose.TREE_SUMMARY, json.dumps({'text': decompose.summary_text(new), 'rev': new['rev'],
                                                           'sent_at': 0}, ensure_ascii=False))
        return new

    def tree_dispatched(self, dispatch):
        """The blocks just dispatched are out: state running, who has it, first start, dispatch count."""
        tree = self.tree_state()
        if tree is None:
            return
        nodes, now = decompose.by_id(tree), time.time()
        for item in dispatch:
            node = nodes.get(item.get('id'))
            if node is None:
                continue
            actual = node.setdefault('actual', decompose.blank_actual())
            node.update(state='running', to=self.resolve(item['to']))
            actual['dispatches'] = int(actual.get('dispatches') or 0) + 1
            actual['started'] = actual.get('started') or now
            actual['out_since'] = now  # session minutes: each dispatch until its report
            actual['dispatched_at'] = now  # which block a report answers (tree_report_node)
        decompose.judge(tree, self.tree_params(), now)
        self.save_tree(tree)

    @staticmethod
    def tree_report_node(tree, name):
        """The block a report of worker ``name`` answers, by the tree (never by the order of fx:items keys): of
        the blocks running with ``name``, the oldest dispatch whose clock runs (deliveries reach a worker in
        order); a block whose earlier report stopped its clock (Blocked / escalate, waiting for a ruling) only
        when no other is out. None when the worker runs no block."""
        mine = [n for n in decompose.level_nodes(tree) if n.get('state') == 'running' and n.get('to') == name]
        out = [n for n in mine if (n.get('actual') or {}).get('out_since')] or mine

        def order(n):
            actual = n.get('actual') or {}
            return float(actual.get('dispatched_at') or actual.get('started') or 0), n['id']
        return min(out, key=order) if out else None

    def tree_reported(self, name, item, text=''):
        """A worker's report settles the block it was running (its wall-clock minutes so far), found in the tree
        (``tree_report_node``; ``item`` from fx:items is not used: a correction re-dispatch keeps its old key
        position). A report that asks to split it or says it is blocked does not settle it: the block stays
        running, its session minutes so far are counted."""
        tree = self.tree_state() if self.decompose_on() else None
        node = self.tree_report_node(tree, name) if tree else None
        if node is None:
            return
        now = time.time()
        actual = node.setdefault('actual', decompose.blank_actual())
        if actual.get('out_since'):
            actual['session_minutes'] = round(float(actual.get('session_minutes') or 0)
                                              + (now - float(actual['out_since'])) / 60, 1)
            actual['out_since'] = 0
        if not (report_blocked(text) or contracts.report_fields(text).get('escalate') is not None):
            actual['ended'] = now
            actual['minutes'] = round((now - float(actual.get('started') or now)) / 60, 1)
            node['state'] = 'reported'
        decompose.judge(tree, self.tree_params(), now)
        self.save_tree(tree)

    def tree_seed(self, task_id, rec):
        """§62: the seed file of sub-fleet ``task_id`` (its path), or None while the switch is off. A job with a
        tree file: that file renumbered into the child's level. A split: this tree's node (or the request's
        instruction) as the child's root, the approved pieces as its first-level blocks."""
        if not decompose.enabled():
            return None
        path = str(self.run_dir / 'tree-seeds' / (task_id + '-' + uuid.uuid4().hex[:8] + '.json'))
        root = {'id': task_id, 'goal': rec.get('instruction') or ''}
        job = (self.job_state().get(rec['job']) or {}) if rec.get('job') else {}
        if job.get('tree'):
            try:
                data = json.loads(Path(job['tree']).read_text())
            except (OSError, ValueError) as exc:
                self.log('tree.seed_unreadable', task=task_id, job=rec['job'], error=str(exc)[:300])
                data = None
            if isinstance(data, dict):
                data = decompose.renumber(data, task_id + '.')
                parents = {n.get('parent') for n in data['nodes'] if n.get('parent')}
                return decompose.write_seed(path, root, [n for n in data['nodes'] if n['id'] not in parents],
                                            data['decisions'], by=job.get('by') or data.get('by'))
        node = decompose.by_id(self.tree_state() or {'nodes': []}).get(task_id) if self.decompose_on() else None
        if node:
            root.update({k: node[k] for k in ('goal', 'accept', 'est', 'owns') if node.get(k)})
        pieces = [{'id': p['id'], 'goal': p.get('instruction') or ''} for p in rec.get('proposal') or []
                  if isinstance(p, dict) and p.get('id')]
        return decompose.write_seed(path, root, pieces, by=rec.get('slot') or None)

    def tree_summary_tick(self):
        """Tick: send the opening summary of the first accepted tree once (outside any v2 transaction). A sub-fleet
        writes it as a notice its parents pass up (relay_notices); the top sends it like a held slot's notice."""
        raw = self.r.get(decompose.TREE_SUMMARY)
        try:
            pending = json.loads(raw) if raw else None
        except ValueError:
            pending = None
        if not isinstance(pending, dict) or pending.get('sent_at') or not pending.get('text'):
            return
        if self.cfg.get('parent_config'):
            folder = Path(load(self.cfg['parent_config'])['run']) / 'subfleet-notices'
            folder.mkdir(parents=True, exist_ok=True)
            save(folder / ('tree-' + uuid.uuid4().hex[:12] + '.json'),
                 {'kind': 'tree', 'task': self.cfg.get('task_id'), 'origin_run': str(self.run_dir),
                  'text': pending['text']})
            status = 'parent'
        else:
            status = self.tree_notify(pending['text'])
        pending['sent_at'] = time.time()
        self.r.set(decompose.TREE_SUMMARY, json.dumps(pending, ensure_ascii=False))
        self.log('tree.summary', rev=pending.get('rev'), status=status)

    def tree_notify(self, text):
        """A decomposition summary for the user: a managed root's lifecycle channels, else a root's own channels."""
        if lifecycle_close.managed(self):
            try:
                policy = lifecycle.get_mode(self.cfg['lifecycle'])
            except ValueError:
                policy = {}
            out = lifecycle_close.notify(self, 'decompose_summary', '[fleet ' + lifecycle_close.fleet_name(self) + '] '
                                         + text, policy)
            return ','.join(str(x.get('status')) for x in out)
        if not self.cfg.get('parent_config'):
            return str(notify_route.unmanaged_send(self, 'decompose_summary', text))
        return 'none'

    def tree_overrun_tick(self):
        """Tick (§62): a running block on the longest chain (as planned) whose time out passed est x overrun_ratio is
        reported to the supervisor once; blocks off the chain are not. The worker is never nudged."""
        tree = self.tree_state() if self.decompose_on() and self.running() else None
        if tree is None:
            return
        params, now = self.tree_params(), time.time()
        ratio = float(params.get('overrun_ratio') or 0)
        planned = decompose.schedule(tree)  # remaining = estimate: an overrun block stays on its planned chain
        changed = False
        for n in decompose.level_nodes(tree):
            actual = n.get('actual') or {}
            est = float((n.get('est') or {}).get('minutes') or 0)
            if n.get('state') != 'running' or not est or actual.get('overrun_noted') or not actual.get('started'):
                continue
            minutes = (now - float(actual['started'])) / 60
            if minutes / est <= ratio or n['id'] not in planned['critical']:
                continue
            actual['overrun_noted'] = True
            changed = True
            self.log('tree.overrun', task=n['id'], minutes=round(minutes, 1), est=est, ratio=ratio)
            self.queue('supervisor', '[overrun] ' + n['id'] + ' is on the longest chain and has run '
                       + decompose._fmt(minutes) + ' min against est ' + decompose._fmt(est) + ' (x'
                       + decompose._fmt(minutes / est) + ' > ' + decompose._fmt(ratio) + '), with ' + (self.label(n['to']) if n.get('to') else 'its worker')
                       + '. Re-estimate it, merge it with a block it waits on, extract a decision several blocks ask '
                       'about, or split it in the tree (a split needs dT >= k x dC). The worker was not asked to '
                       'split. Reply with the JSON format from AGENTS.md.')
        if changed:
            self.save_tree(tree)

    def tree_return(self, task_id, rec, answer):
        """§62: any ruling on a running block (a [blocked] request, or a split request not approved) restarts its
        session-minute clock. One with a ``cause`` also counts one return of the block; at returns_alert returns the
        supervisor's next message gets one line with the block's causes (once per block, never a refusal)."""
        cause = answer.get('cause')
        tree = self.tree_state() if self.decompose_on() else None
        nid = rec.get('item') if rec.get('kind') == 'blocked' else task_id if not rec.get('kind') else None
        node = decompose.by_id(tree).get(nid) if tree and nid else None
        if node is None:
            return
        actual = node.setdefault('actual', decompose.blank_actual())
        if node.get('state') == 'running' and (answer.get('decision') != 'approve' or rec.get('kind') == 'blocked'):
            actual['out_since'] = time.time()  # any ruling: the worker goes on with the block, its clock runs again
        if not cause:
            self.save_tree(tree)
            return
        actual['returns'] = int(actual.get('returns') or 0) + 1
        causes = actual.setdefault('causes', {})
        causes[cause] = int(causes.get(cause) or 0) + 1
        self.save_tree(tree)
        if actual['returns'] == int(self.tree_params().get('returns_alert') or 0):
            shown = ', '.join(k + ' ' + str(v) for k, v in sorted(causes.items(), key=lambda kv: (-kv[1], kv[0])))
            self.r.rpush(decompose.TREE_NOTES, '[tree] ' + nid + ' came back ' + str(actual['returns']) + ' times: '
                         + shown + '. Consider merging it with the block it waits on, extracting the decision it keeps '
                         'asking about, or splitting it in the tree.')
            self.log('tree.returns_alert', task=nid, causes=causes)

    def tree_split_account(self, task_id, rec):
        """§62: the program's account of a worker's split of block ``task_id`` into a sub-fleet, or None (no tree,
        not a block of this level). Pieces with an ``est`` are taken as written, else the block's remaining time is
        cut evenly. dT used = the worker's delta_t capped by the program's bound."""
        tree = self.tree_state() if self.decompose_on() else None
        node = decompose.by_id(tree).get(task_id) if tree else None
        if node is None or node.get('parent') != tree['root']:
            return None
        params, now = self.tree_params(), time.time()
        proposal = rec.get('proposal') or []
        if proposal and all(decompose._number(p.get('est'), above=True) for p in proposal):
            pieces = [{'id': p['id'], 'est': float(p['est'])} for p in proposal]
        else:
            n = max(len(proposal), 1)
            pieces = [{'id': 'p' + str(i), 'est': decompose.weight(node, now) / n} for i in range(n)]
        acc = decompose.split_account(tree, task_id, pieces, params, subfleet=True, now=now)
        k, margin = decompose.split_param(params, 'k'), decompose.split_param(params, 'margin')
        used = min(float(rec['delta_t']), acc['dT']) if rec.get('delta_t') is not None else acc['dT']
        a, c = round(used, 6), round(k * acc['dC'], 6)
        if acc['reason'].startswith('off the longest chain'):
            split, reason = False, acc['reason']
        elif a <= 0 or a < c:
            split, reason = False, 'dT ' + decompose._fmt(used) + ' < k x dC = ' + decompose._fmt(k) + ' x ' \
                + decompose._fmt(acc['dC'])
        elif a < round(margin, 6):
            split, reason = False, 'dT ' + decompose._fmt(used) + ' < margin ' + decompose._fmt(margin)
        else:
            split, reason = True, 'dT ' + decompose._fmt(used) + ' >= k x dC = ' + decompose._fmt(k) + ' x ' \
                + decompose._fmt(acc['dC'])
        return {'bound': acc['dT'], 'dT': decompose._num(used), 'dC': acc['dC'], 'k': k, 'split': split,
                'reason': reason}

    def tree_split_approved(self, task_id, rec, account):
        """§62: an approved split in this tree: the block is delegated (leaf=false, its pieces as its children,
        delegated too) and the split is recorded with both accounts."""
        tree = self.tree_state() if self.decompose_on() else None
        node = decompose.by_id(tree).get(task_id) if tree else None
        if node is None:
            return
        now, ids = time.time(), []
        nodes = decompose.by_id(tree)
        for p in rec.get('proposal') or []:
            if not isinstance(p, dict) or not p.get('id') or p['id'] in nodes:
                continue
            ids.append(p['id'])
            tree['nodes'].append(dict({k: [] for k in decompose.LIST_FIELDS}, id=p['id'], parent=task_id,
                                      goal=p.get('instruction') or '', cut=None, state='delegated',
                                      actual=decompose.blank_actual()))
        prior = {'state': node.get('state'), 'to': node.get('to')}
        node['state'], node['delegated_at'] = 'delegated', now
        tree.setdefault('splits', []).append({'of': task_id, 'pieces': ids, 'dT': (account or {}).get('dT'),
                                              'dC': (account or {}).get('dC'), 'claimed': {
                                                  'dT': rec.get('delta_t'), 'dC': rec.get('delta_c')},
                                              'by': 'worker', 'at': now, 'prior': prior})
        decompose.judge(tree, self.tree_params(), now)
        self.save_tree(tree)
        self.log('tree.split', of=task_id, pieces=ids, dT=(account or {}).get('dT'), dC=(account or {}).get('dC'))

    def tree_split_rollback(self, task_id, why, tree=None, planned=False):
        """§62: an approved split that did not happen (its sub-fleet could not start) or failed: the pieces the
        program wrote under block ``task_id`` go and the block is dispatchable again: back to the state it had
        before the approve (the worker still holds it), or ``planned`` after a failed sub-fleet. The split stays in
        ``splits`` with ``rolled_back`` (the review counts what it cost). Saves the tree unless ``tree`` is given."""
        own = tree is None
        tree = self.tree_state() if own and self.decompose_on() else tree
        node = decompose.by_id(tree).get(task_id) if tree else None
        if node is None or node.get('state') not in ('delegated', 'reported') and not decompose.children(tree, task_id):
            return None
        kids = set(decompose.children(tree, task_id))
        tree['nodes'] = [n for n in tree['nodes'] if n['id'] not in kids]
        split = next((s for s in reversed(tree.get('splits') or []) if s.get('of') == task_id
                      and not s.get('rolled_back')), None)
        prior = (split or {}).get('prior') or {}
        node.pop('delegated_at', None)
        if planned or prior.get('state') not in ('running', 'planned'):
            node['state'] = 'planned'
            node.pop('to', None)
        else:
            node['state'] = prior['state']
            if prior.get('to'):
                node['to'] = prior['to']
        if split is not None:
            split['rolled_back'] = why
        self.log('tree.split_rolled_back', of=task_id, pieces=sorted(kids), reason=why[:300])
        if own:
            decompose.judge(tree, self.tree_params(), time.time())
            self.save_tree(tree)
        return split

    def tree_collected(self, rec, child_run, ok):
        """§62: a sub-fleet returned: its blocks' states and actuals fill in the delegated block and its pieces."""
        tree = self.tree_state() if self.decompose_on() else None
        node = decompose.by_id(tree).get(rec.get('task_id')) if tree else None
        if node is None:
            return
        try:
            child = json.loads((Path(child_run) / 'tree.json').read_text())
        except (OSError, ValueError):
            child = None
        nodes, total, now = decompose.by_id(tree), 0.0, time.time()
        for c in decompose.level_nodes(child) if isinstance(child, dict) and child.get('nodes') else []:
            mine = nodes.get(c['id'])
            if mine is None:
                mine = dict({k: [] for k in decompose.LIST_FIELDS}, id=c['id'], parent=node['id'],
                            goal=c.get('goal') or '', cut=c.get('cut'))
                tree['nodes'].append(mine)
            mine['state'], mine['actual'] = c.get('state'), c.get('actual') or decompose.blank_actual()
            total += float((c.get('actual') or {}).get('session_minutes') or 0)
        actual = node.setdefault('actual', decompose.blank_actual())
        if actual.get('out_since'):
            total += (now - float(actual['out_since'])) / 60
            actual['out_since'] = 0
        actual['session_minutes'] = round(float(actual.get('session_minutes') or 0) + total, 1)
        actual['outcome'] = 'completed' if ok else 'failed'
        split = next((s for s in reversed(tree.get('splits') or []) if s.get('of') == node['id']
                      and not s.get('rolled_back')), None)
        if split is not None:
            split['session_minutes'] = round(total, 1)
        if ok:
            actual['ended'] = now
            actual['minutes'] = round((now - float(actual.get('started') or node.get('delegated_at') or now)) / 60, 1)
            node['state'] = 'reported'
        else:  # the block is not done: its pieces go, it can be dispatched again (its cost stays counted)
            self.tree_split_rollback(node['id'], 'sub-fleet failed', tree=tree, planned=True)
        decompose.judge(tree, self.tree_params(), now)
        self.save_tree(tree)

    def tree_contract_fill(self, decision):
        """§62 with contracts on: a contract dispatch item without edits / depends_on / serial_reason takes them
        from its tree block (owns / depends_on / serial_reason); written and different from the tree: ValueError.
        The contract gates (P0, P1, readiness) are not touched."""
        tree = decision.get('_tree') or self.tree_state()
        nodes = decompose.by_id(tree) if tree else {}
        for item in decision.get('dispatch') or []:
            node = nodes.get(item.get('id'))
            if node is None:
                continue
            for key, field in (('edits', 'owns'), ('depends_on', 'depends_on'), ('serial_reason', 'serial_reason')):
                want = node.get(field) or (None if key == 'serial_reason' else [])
                if key not in item:
                    if want:
                        item[key] = copy.deepcopy(want)
                    continue
                have = item[key]
                same = (sorted(have) == sorted(want)) if isinstance(have, list) else have == want
                if not same:
                    raise ValueError(item['id'] + ' ' + key + ' differ' + ('s' if key == 'serial_reason' else '')
                                     + ' from the tree (' + field + '): change the tree, not the dispatch')

    def tree_tune(self, decision):
        """§62: the closing COMPLETED's ``decompose_tune`` (it passed verification) into this fleet's parameters in
        Redis, marked for write-back (a root writes back on its tick; a sub-fleet forwards it at collect)."""
        tune = decision.get('decompose_tune')
        if not tune or not self.decompose_on():
            return
        try:
            rows = decompose.tune_apply(self.r, tune, self.cfg.get('task_id') or 'root')
        except ValueError as exc:
            self.log('decompose.tune_failed', error=str(exc)[:300])
            return
        self.log('decompose.tune', items=json.dumps(rows, ensure_ascii=False))

    def decompose_collect(self, child_run):
        """§62: a returned sub-fleet's applied tune (its result.json ``decompose.tune``) goes into this fleet's
        parameters, task or service fleet alike; a sub-fleet passes it on in its own result.json, only the root
        writes back."""
        try:
            result = load(Path(child_run) / 'result.json')
        except (OSError, ValueError):
            return
        tune = (result.get('decompose') or {}).get('tune') if isinstance(result, dict) else None
        if not tune:
            return
        try:
            rows = decompose.tune_apply(self.r, tune, '')
        except ValueError as exc:
            self.log('decompose.tune_failed', error=str(exc)[:300], run=str(child_run))
            return
        self.log('decompose.tune_forwarded', items=json.dumps(rows, ensure_ascii=False), run=str(child_run))

    def tree_final(self):
        """§62: result.json's ``decompose``: the final review table, the tree's chain and the tune applied here
        (own and forwarded). None while the switch is off and nothing was tuned."""
        tree = self.tree_state() if self.decompose_on() else None
        tune = decompose.tuned(self.r)
        if tree is None and not tune:
            return None
        out = {'tune': tune}
        if tree is not None:
            self.save_tree(tree)
            out.update(rev=tree.get('rev'), chain=tree.get('chain'), width=tree.get('width'), review=tree['review'])
        return out

    def tree_audit(self, decision):
        """The dispatch audit's per-block fields (switch on): id, owner, parent, estimate, chain, leaf."""
        tree = self.tree_state() if self.decompose_on() else None
        if tree is None:
            return {}
        nodes = decompose.by_id(tree)
        rows = []
        for item in decision['dispatch']:
            n = nodes.get(item.get('id')) or {}
            est = n.get('est') or {}
            rows.append({'id': item.get('id'), 'to': n.get('to') or self.resolve(item['to']), 'parent': n.get('parent'),
                         'est_minutes': est.get('minutes'), 'est_sessions': est.get('sessions', 1),
                         'critical': n.get('critical'), 'leaf': n.get('leaf')})
        return {'items': rows}

    def contract_queue(self, state, slot, text, owner=None):
        """Queue to a worker and record what its report will answer (a task id, or None for a notice)."""
        state['owners'].setdefault(slot, []).append(owner)
        self.queue(slot, text)

    def contract_notify(self, state, task_ids, text):
        """[contract] notices go to the workers of running tasks only; others read contracts.json."""
        slots = dict.fromkeys(state['tasks'][m]['to'] for m in task_ids
                              if m in state['tasks'] and state['tasks'][m]['status'] == 'running')
        for slot in slots:
            self.contract_queue(state, slot, text + '\nAct on it per CONTRACTS.md; reply in the AGENTS.md format '
                                '(Done / Evidence / Not done / Blocked) when done.')

    def contract_doc(self, role, name=None):
        text = (HERE / 'agents' / ('contracts_' + role + '.md')).read_text()
        if self.decompose_on():  # §62: leaf comes from the tree; a split request carries both accounts
            text = decompose.contracts_swap(text) if role == 'supervisor' else decompose.worker_swap(text)
        text += peer.doc(self, role, name)  # §21: 5-A (Claude) or 5-B (Pi / Codex) by this agent's CLI
        return text.replace('{{WORKSPACE}}', str(self.workspace()))

    def contract_decision(self, state, decision):
        """Validate a decision against the contract phase. Returns (state, rows to queue now, held rows,
        approved/rejected changes, skeleton?) without side effects; ValueError carries the spec texts."""
        s = copy.deepcopy(state)
        if 'artifacts' in decision:  # before any status: COMPLETED runs cmd_exit right away
            validate_artifacts(decision['artifacts'], extended=True)
        if decision['status'] != 'RUNNING':
            return s, [], [], [], False
        if s['phase'] == 'interfaces':
            s['plan'], s['interfaces'] = contracts.p0(decision)
            s['tasks'] = {p['id']: {'id': p['id'], 'status': 'pending', 'attempts': 0, 'stale': [],
                                    'provides': p['provides'], 'consumes': p['consumes'], 'depends_on': []}
                          for p in s['plan']}
            s['phase'], s['skeleton_tries'] = 'skeleton', s['skeleton_tries'] + 1
            return s, [], [], [], True
        if s['phase'] == 'skeleton':
            if decision['dispatch'] or decision.get('interfaces') or decision.get('phase'):
                raise ValueError('P1: the skeleton run is in progress; do not dispatch or change interfaces until '
                                 '"[contract] interfaces frozen at v1"')
            return s, [], [], [], False
        if decision.get('phase') == 'interfaces':
            raise ValueError('interfaces are frozen at v1: change one with change "compatible" or "breaking"')
        changes = contracts.supervisor_interfaces(s, decision.get('interfaces'))
        now, held = contracts.dispatch(s, decision['dispatch'], self.resolve)
        return s, now, held, changes, False

    def contract_apply(self, t, state, changes, skeleton):
        """Side effects of an accepted decision: change notices, the P1 skeleton delivery."""
        for key_, kind, notify in changes:
            if kind == 'veto':  # the sub-fleet that registered it withdraws it on its next tick
                folder = Path(notify['run']) / 'subfleet-vetoes'
                folder.mkdir(parents=True, exist_ok=True)
                save(folder / (uuid.uuid4().hex[:12] + '.json'), {'ref': key_, 'note': notify['note']})
                self.contract_log('vetoed', by='supervisor', iface=key_, run=notify['run'], note=notify['note'])
                continue
            self.contract_log('approved' if kind != 'rejected' else 'rejected', by='supervisor', iface=key_, change=kind)
            self.contract_notify(state, notify, '[contract] ' + ('rejected ' + key_ if kind == 'rejected'
                                 else 'approved ' + key_ + ' (' + kind + '); read it in contracts.json'))
        if skeleton:
            worker = state['skeleton_worker']
            self.contract_log('p0.passed', interfaces=sorted(state['interfaces']), attempt=state['skeleton_tries'])
            self.save_contract(state)  # the skeleton worker reads contracts.json first
            self.contract_queue(state, worker, '[Supervisor instruction ' + t.get('id', '') + '] [skeleton] '
                                'P1 skeleton run (interfaces are not frozen yet; no real work). Read '
                                + str(self.workspace() / 'contracts.json') + '; in dependency order feed each upstream '
                                'sample into its consumer (code: a minimal stub per interface, real importable '
                                'signatures with raise NotImplementedError, one call chain; research: walk the outline '
                                'using the samples). Write ./skeleton.md, one line per interface: '
                                '`IF-x@1 · producer → consumer · PASS/FAIL · evidence`. Never edit the samples.'
                                '\nShared output directory: ' + str(self.workspace()) + ' (write only inside your own '
                                'subdirectory ' + worker + '/). Reply in the AGENTS.md format when done.',
                                contracts.SKELETON)
        self.notify_parent(state, [(k, kind) for k, kind, _ in changes if kind not in ('rejected', 'veto')])

    def contract_message(self, task_id, row):
        def names(k):
            return ', '.join(row.get(k) or []) or 'none'
        return ('[Supervisor instruction ' + task_id + '] [' + row['id'] + '] ' + row['instruction']
                + '\nInterface contracts are on (follow CONTRACTS.md in your directory; shapes in '
                + str(self.workspace() / 'contracts.json') + '). Task ' + row['id'] + ': provides ' + names('provides')
                + '; consumes ' + names('consumes') + '; edits ' + names('edits') + '.'
                + '\nShared output directory: ' + str(self.workspace()) + ' (write only inside your own '
                'subdirectory ' + row['to'] + '/). Reply in the AGENTS.md format when done.')

    def contract_gate(self, state):
        """COMPLETED: every non-deprecated interface must be ready."""
        out = []
        for key_, e in sorted(state['interfaces'].items()):
            if e['status'] not in ('ready', 'deprecated'):
                detail = contracts.file_failures(e, self.workspace()) or ['the producer has not announced ready']
                out.append(key_ + ' not ready (' + '; '.join(detail) + ')')
        return out

    def contract_report(self, name, text):
        """Apply a worker report to the task table. Returns extra lines for the supervisor,
        or None when the report ended the task."""
        state = self.contract_state()
        if state is None:
            return ''
        s, ws, notes = copy.deepcopy(state), self.workspace(), []
        owners = s['owners'].get(name) or []
        owner = owners.pop(0) if owners else None
        fields = contracts.report_fields(text)
        if owner == contracts.SKELETON:
            path = ws / name / 'skeleton.md'
            failures = contracts.skeleton_verdict(path.read_text(errors='replace') if path.is_file() else None,
                                                  s['interfaces'])
            self.contract_log('skeleton', by=name, attempt=s['skeleton_tries'], failures=failures)
            if not failures:
                for e in s['interfaces'].values():
                    e.update(status='declared', approved_by='supervisor')
                s['phase'] = 'parallel'
                self.notify_parent(s, [(k, 'new') for k in sorted(s['interfaces'])])
                notes.append('[contract] interfaces frozen at v1. Dispatch M0…Mn now: every dispatch item has id, '
                             'provides and consumes (id@version) per the plan; consumers start from the samples.')
            elif contracts.SKELETON_TRIES and s['skeleton_tries'] >= contracts.SKELETON_TRIES:
                self.save_contract(s)
                self.finish('blocked', 'P1: skeleton run failed ' + str(s['skeleton_tries']) + ' times: '
                            + '; '.join(failures))
                return None
            else:
                s['phase'] = 'interfaces'
                notes.append('[contract] skeleton run FAILED (try ' + str(s['skeleton_tries'])
                             + (' of ' + str(contracts.SKELETON_TRIES) if contracts.SKELETON_TRIES else '')
                             + '): ' + '; '.join(failures)
                             + '\nFix the interfaces in a new reply with phase "interfaces" (full plan + interfaces, '
                             'no dispatch).')
        row = s['tasks'].get(owner) if owner and owner != contracts.SKELETON else None
        mine = row or next((t for t in s['tasks'].values() if t.get('to') == name and t['status'] == 'running'), None)
        hold = None  # an escalate report is not a completion: the task row stays open
        if fields.get('escalate') is not None:
            task_id, ok, note, reply = self.escalate_intake(name, fields, s)
            hold = task_id if task_id in s['tasks'] else (mine or {}).get('id')
            if hold and ok is None:  # sub-fleet attempts used up (escalate_intake): no new spawn
                s['tasks'][hold].update(status='failed', reason='escalate refused: ' + subfleet.EXHAUSTED
                                        + ' reached, no new sub-fleet')
                self.contract_log('task.failed', task=hold, by='program', reason=s['tasks'][hold]['reason'])
            elif hold:
                s['tasks'][hold].update(status='escalated' if ok else 'running',
                                        reason='asked to split' if ok else 'escalate refused; worker continues')
            if reply:
                self.contract_queue(s, name, reply, hold)
            notes.append(note)
        change = fields.get('interface_change')
        if change is not None:
            try:
                if mine is None:
                    raise ValueError('interface_change: you have no task in the task table')
                proposal = contracts.worker_change(s, change, name, mine['id'], ws)
                p = s['pending'][proposal]
                self.contract_log('proposed', by=name, iface=proposal, change=p['change'], reason=p['reason'])
                notes.append('[contract] proposal ' + proposal + ' (' + p['change'] + ') from ' + self.label(name)
                             + ' / ' + mine['id'] + ': ' + p['reason'] + '\nDecide it in your next JSON: '
                             'interfaces: [{"id": "' + p['id'] + '", "version": ' + str(p['version'])
                             + ', "decision": "approve"}] (or "reject").')
            except ValueError as exc:
                self.contract_log('rejected', by=name, error=str(exc))
                self.contract_queue(s, name, '[contract] your interface_change was rejected: ' + str(exc)
                                    + '\nFix it and propose again; keep working on the current version. Reply in '
                                    'the AGENTS.md format (Done / Evidence / Not done / Blocked).')
                notes.append('[contract] rejected interface_change from ' + self.label(name) + ': ' + str(exc))
        for iface, version in contracts.ready_claims(str(fields.get('interfaces', ''))):
            key_ = iface + '@' + str(version)
            e = s['interfaces'].get(key_)
            producer = s['tasks'].get(e['producer']) if e else None
            if not e or e['status'] == 'deprecated' or not producer or producer.get('to') != name:
                notes.append('[contract] not ready ' + key_ + ': only the producer may announce a registered, '
                             'current interface ready')
                continue
            failures = contracts.file_failures(e, ws)
            if failures:
                self.contract_notify(s, [e['producer']] + e['consumers'], '[contract] not ready ' + key_ + ': '
                                     + '; '.join(failures))
            else:
                e['status'] = 'ready'
                self.contract_notify(s, e['consumers'], '[contract] ready ' + key_ + ': switch your input from '
                                     './stubs/ to ' + e['path'] + ' and re-run.')
            self.contract_log('ready' if not failures else 'not_ready', by=name, iface=key_, failures=failures)
            notes.append('[contract] ' + ('ready ' if not failures else 'not ready ') + key_
                         + ((': ' + '; '.join(failures)) if failures else ''))
        if row and row['id'] != hold:
            current = [contracts.current(s, i) for i in row.get('provides') or []]
            unready = [r for r, e in zip(row.get('provides') or [], current) if not e or e['status'] != 'ready']
            row['status'] = 'failed' if unready else 'done'
            row['reason'] = ('reported, but not ready: ' + ', '.join(unready)) if unready else 'outputs verified'
            claimed = {i + '@' + str(v) for i, v in contracts.ready_claims(str(fields.get('interfaces', '')))}
            unclaimed = [contracts.ref(e) for e in current if e and e['status'] != 'ready'
                         and contracts.ref(e) not in claimed and not contracts.file_failures(e, ws)]
            if unclaimed:  # file passes, announcement line missing/garbled: say which literal line
                line = 'Interfaces: ' + ', '.join(x + ' provided: ready' for x in unclaimed)
                row['reason'] += ' (the report has no line `' + line + '`; ' + self.label(name) + ' was asked for it)'
                self.contract_queue(s, name, '[contract] ' + row['id'] + ' is not done: your report did not announce '
                                    + ', '.join(unclaimed) + '. If the file is written, reply in the AGENTS.md format '
                                    'with this exact line: ' + line, row['id'])
                notes.append('[contract] ' + row['id'] + ': the file passes but the report misses `' + line + '`; '
                             + self.label(name) + ' was asked for that line (no re-dispatch needed)')
            self.contract_log('task.' + row['status'], task=row['id'], by=name, reason=row['reason'])
            for held in contracts.releasable(s) if not unready else []:
                held['status'] = 'running'
                held['attempts'] = held.get('attempts', 0) + 1
                self.contract_queue(s, held['to'], held['message'], held['id'])
                self.contract_log('task.released', task=held['id'])
        if fields.get('peer_notices') and str(fields['peer_notices']).strip().lower() != 'none':
            self.contract_log('peer_notices', by=name, line=str(fields['peer_notices']))
            notes += peer.violations(self, name, fields['peer_notices'])
        question = str(fields.get('question', '')).strip()
        if question and question.lower() != 'none':
            notes.append('[worker question] ' + self.label(name) + ': ' + question)
        notes.append(contracts.table_line(s))
        self.save_contract(s)
        return '\n' + '\n'.join(notes)

    # ------------------------------------------------------------ escalate: worker asks to split (§20)
    def escalation_state(self):
        # Service mode: plan F' [dialog] / [blocked] records, and P7 split requests (M<n> with a 'request' id).
        raw = self.r.get(ESCALATIONS)
        return json.loads(raw) if raw else {}

    def save_escalations(self, state):
        self.r.set(ESCALATIONS, json.dumps(state, ensure_ascii=False, separators=(',', ':')))

    def escalation_item(self, task_id, state):
        """(worker slot, original instruction) of a task id: the contract task table, else plain dispatch ids."""
        if state is not None:
            row = state['tasks'].get(task_id) or {}
            return row.get('to'), row.get('instruction'), row.get('status')
        row = json.loads(self.r.get(ITEMS) or '{}').get(task_id) or {}
        return row.get('to'), row.get('instruction'), 'running' if row else None

    def escalate_check(self, name, fields, state):
        reason, proposal = fields.get('escalate'), fields.get('proposal')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('escalate must be a one-sentence reason')
        if not isinstance(proposal, list) or not proposal or not all(
                isinstance(p, dict) and isinstance(p.get('id'), str) and isinstance(p.get('instruction'), str)
                and len(p['instruction'].strip()) >= 12 and isinstance(p.get('output'), str) and p['output'].strip()
                for p in proposal):
            raise ValueError('proposal must be a non-empty list of {id, instruction (>= 12 chars), output}')
        ids = [p['id'] for p in proposal]
        shaped = all(re.fullmatch(contracts.M_RE + r'\.\d+', i) for i in ids)
        parents = {i.rsplit('.', 1)[0] for i in ids}
        if not shaped or len(parents) != 1 or len(set(ids)) != len(ids):
            raise ValueError('proposal ids must be distinct and all <your task id>.<n> (e.g. M0.0, M0.1)')
        task_id = parents.pop()
        slot, instruction, status = self.escalation_item(task_id, state)
        if slot is None and state is None and not json.loads(self.r.get(ITEMS) or '{}'):
            raise ValueError('escalate needs a task id: the supervisor must dispatch with "id" (M<n>) '
                             'or run a contracts task')
        if slot != name or status not in ('running', 'escalated'):
            raise ValueError(task_id + ' is not a running task of ' + self.label(name))
        workers = fields.get('workers', len(proposal))
        if isinstance(workers, bool) or not isinstance(workers, int):
            raise ValueError('workers must be a positive integer')
        numbers = {}
        if self.decompose_on():  # §62: a split request writes both accounts
            numbers = {k: fields.get(k) for k in ('delta_t', 'delta_c')}
            if not all(decompose._number(v) for v in numbers.values()):
                raise ValueError('decompose is on: an escalate needs delta_t (minutes the whole task finishes earlier) '
                                 'and delta_c (extra session minutes: cold starts, agreeing, merging, the sub-fleet '
                                 'supervisor), both numbers >= 0')
        refused = subfleet.check(self, name, task_id, workers, ready=False)
        if refused:
            raise ValueError(refused)
        return task_id, dict({'slot': name, 'reason': reason.strip(), 'proposal': proposal, 'workers': workers,
                              'instruction': instruction or '', 'status': 'escalated'}, **numbers)

    def escalate_intake(self, name, fields, state):
        """A worker report carrying ``escalate``. Returns (task id, accepted?, note for the supervisor,
        reply for the worker or None). Refusals carry the program's reason to both."""
        try:
            task_id, rec = self.escalate_check(name, fields, state)
        except ValueError as exc:
            self.log('escalate.refused', agent=name, reason=str(exc))
            if state is not None and str(exc).startswith(subfleet.EXHAUSTED):
                # Contract task out of sub-fleet attempts: the task fails here, the supervisor decides.
                return None, None, ('\n[escalate] refused for ' + self.label(name) + ': ' + str(exc)
                                    + '. The task is marked failed; re-dispatch it (another approach or worker) '
                                    'or end the task.'), None
            return None, False, ('\n[escalate] refused for ' + self.label(name) + ': ' + str(exc)
                                 + ' (the worker was told and continues its task)'), \
                ('[escalate] your split request was refused by the program: ' + str(exc)
                 + '\nContinue your current task yourself (or fix the escalate JSON and send it again). '
                 'Reply in the AGENTS.md format (Done / Evidence / Not done / Blocked).')
        esc = self.escalation_state()
        esc[task_id] = rec
        self.save_escalations(esc)
        self.log('escalate.asked', agent=name, task=task_id, workers=rec['workers'], reason=rec['reason'])
        who = self.label(name)
        pieces = '\n'.join('- ' + p['id'] + ': ' + p['instruction'].strip() + ' -> ' + p['output']
                           + (' (' + p['kind'] + ')' if isinstance(p.get('kind'), str) and p['kind'].strip() else '')
                           for p in rec['proposal'])
        note = ('\n[escalate] ' + who + ' asks to split ' + task_id + ' into ' + str(len(rec['proposal']))
                + ' pieces run by a sub-fleet of ' + str(rec['workers']) + ' workers. Reason: ' + rec['reason']
                + '\n' + pieces + '\nAnswer in your next JSON: "escalations": [{"task": "' + task_id
                + '", "decision": "approve" | "revise" | "reject", "workers": <n, optional>, "note": "<optional>", '
                '"supervisor_profile": "<card, optional>", "worker_profiles": ["<card per sub-worker>", ...]}]. '
                'approve: the program starts sub-fleet ' + task_id + ' with ' + who + ' as its supervisor (possibly a '
                'new session of ' + who + ' after a handoff note: on supervisor_profile; without it, on the model the '
                'program picks from the supervisor chain); '
                'revise: your note goes back to ' + who + '; reject: ' + who + ' does ' + task_id + ' itself. '
                'Do not dispatch to ' + who + ' in the reply that approves.' + self.split_cards())
        account = self.tree_split_account(task_id, rec) if 'delta_t' in rec else None
        if account:  # §62: the worker's two numbers beside the program's, from the tree
            note += ('\nAccounts: worker dT ' + decompose._fmt(rec['delta_t']) + ' / dC ' + decompose._fmt(rec['delta_c'])
                     + '; program dT <= ' + decompose._fmt(account['bound']) + ', dC ' + decompose._fmt(account['dC'])
                     + ' (sub-fleet, k=' + decompose._fmt(account['k']) + '): '
                     + ('pays' if account['split'] else 'does not pay') + '. An approve the program finds not worth '
                     'it is refused.')
        return task_id, True, note, None

    def split_cards(self):
        """The agent cards an approve may pick (§44), for the [escalate] note; '' when unreadable."""
        try:
            cards = AgentRegistry(self.cfg.get('agent_registry') or None).cards
        except (AgentRegistryError, OSError):
            return ''
        return ('\nCards: ' + ', '.join(c.id + ' (' + c.cli + (' ' + c.model if c.model else '') + ')' for c in cards)
                + '; supervisor_profile must be a claude card.')

    def next_item_id(self, dispatch):
        """The next free plain-task id: M<n>, or <this sub-fleet's id>.<n> inside a sub-fleet (§44)."""
        prefix = self.cfg['task_id'] + '.' if self.cfg.get('task_id') else 'M'
        used = set(json.loads(self.r.get(ITEMS) or '{}')) | {x.get('id') for x in dispatch}
        n = 0
        while prefix + str(n) in used:
            n += 1
        return prefix + str(n)

    def report_item(self, name):
        """The task id a worker's next report answers ('' if none): its contract owner, else its running row,
        else the last plain dispatch id given to it."""
        state = self.contract_state()
        if state is not None:
            owners = state['owners'].get(name) or []
            if owners and owners[0] in state['tasks']:
                return owners[0]
            return next((t['id'] for t in state['tasks'].values() if t.get('to') == name and t['status'] == 'running'), '')
        ids = [i for i, row in json.loads(self.r.get(ITEMS) or '{}').items() if row.get('to') == name]
        return ids[-1] if ids else ''

    def blocked_intake(self, name, item, reason):
        """Plan F' 8.4: a worker's Blocked: goes to the supervisor to rule on (not straight to BLOCKED).
        Returns (escalation key, note appended to the report for the supervisor)."""
        key_ = BLOCKED_KEY + name
        esc = self.escalation_state()
        # Bound to the generation that asked (a ruling for it never reaches a later one: ruling_stale)
        esc[key_] = {'kind': 'blocked', 'slot': name, 'item': item, 'reason': reason[:2000], 'status': 'escalated',
                     'asked_at': time.time(), 'generation': str(self.agent(name).get('generation') or 0),
                     'label': self.label(name)}
        self.save_escalations(esc)
        self.log('blocked.asked', agent=name, item=item, reason=reason[:300])
        who = self.label(name)
        return key_, ('\n[blocked] ' + who + ' says it is stuck on: ' + reason[:2000].replace('\n', '; ')
                      + '\nAnswer in your next JSON: "escalations": [{"task": "' + key_ + '", "decision": "approve" | '
                      '"revise" | "reject", "note": "..."}]. approve = it may go on the way it proposed (say in note '
                      'how far that authorisation goes); revise = another way (note required); reject = drop this '
                      'part and go on with the rest as the note says. Dispatching a new instruction to ' + who
                      + ' in the same reply also answers it. Reply status BLOCKED only for a delete / move, or for '
                      'what truly only the user can decide, and say which in reason.')

    def escalations_missed(self, decision):
        """Open dialog / blocked escalations whose message the supervisor already has but this RUNNING reply
        neither answers nor (blocked) dispatches over. The first miss is sent back without a strike; then a
        dialog goes to the user and a blocked miss counts as a strike."""
        if self.v2 is None or decision['status'] != 'RUNNING':
            return
        esc = self.escalation_state()
        answered = {x['task'] for x in decision['escalations']}
        # Task mode: a new instruction to (or consult on) the worker answers its Blocked. Service mode: a dispatch
        # routes another request, so it answers nothing.
        dispatched = set() if self.service else {BLOCKED_KEY + self.resolve(x['to']) for x in decision['dispatch']}
        dispatched |= {BLOCKED_KEY + self.resolve(c['worker']) for c in decision.get('consult', [])}
        # A report notice cancelled while still queued was never seen: no nudge for it (report_reask asks again).
        missed = [k for k, rec in esc.items() if k.startswith((DIALOG_KEY, BLOCKED_KEY, REPORT_KEY))
                  and rec.get('status') == 'escalated' and rec.get('message_id') and k not in answered
                  and k not in dispatched and self.v2.bus.status(rec['message_id']) not in ('', 'queued')
                  and not (k.startswith(REPORT_KEY)
                           and self.v2.bus.r.hget(self.v2.bus.msg_key(rec['message_id']), 'cancel_reason'))]
        if not missed:
            return
        first = [k for k in missed if not esc[k].get('nudged')]
        for k in first:
            esc[k]['nudged'] = 1
        if first:
            self.save_escalations(esc)
            raise NoStrike('answer ' + ', '.join(first) + ' first: "escalations": [{"task": "' + first[0]
                           + '", "decision": ...}] (see the [dialog] / [blocked] / [sub-fleet report] lines)')
        for k in [k for k in missed if k.startswith(DIALOG_KEY)]:
            rec = esc.pop(k)
            self.save_escalations(esc)
            self.log('dialog.to_human', agent=rec['slot'], task=k, reason='supervisor did not answer')
            self.v2.audit('dialog.to_human', slot=rec['slot'], level='warn', reason='supervisor_unanswered', task=k)
            self.v2.prompt_wait(rec['slot'], rec['scope'], 'dialog ' + rec['prompt'] + ' not answered by the '
                                'supervisor twice; handed to the user', rec['screen'], kind='other')
        blocked = [k for k in missed if k.startswith((BLOCKED_KEY, REPORT_KEY))]  # never auto-approved (§46)
        if blocked:
            raise ValueError('escalations: ' + ', '.join(blocked) + ' still waits for your answer (approve / revise / '
                             'reject, or dispatch to that worker; a report:<task>:<id> takes approve / reject only)')

    def escalation_decision(self, decision, state):
        """Validate the supervisor's escalations against the open requests (no side effects)."""
        open_ = self.escalation_state()
        busy = {self.resolve(x['to']) for x in decision['dispatch']}
        for task_id, rec in open_.items():  # §44: approved, not yet delegated: the slot is the split's
            if rec.get('kind', 'split') == 'split' and rec.get('status') in ('approved', 'rotating') and rec['slot'] in busy:
                raise ValueError('dispatch: the approved split of ' + task_id + ' hands ' + self.label(rec['slot'])
                                 + ' over to sub-fleet ' + task_id + '; do not dispatch to it until [Sub-fleet '
                                 + task_id + ' completed]')
        out = []
        for item in decision['escalations']:
            rec = open_.get(item['task'])
            kind = (rec or {}).get('kind', 'split')
            awaiting = kind == 'report' and (rec or {}).get('status') == blocked_scope.AWAITING  # §24: the user's, or yours
            if (item['task'].startswith((DIALOG_KEY, BLOCKED_KEY, REPORT_KEY)) and (not rec or rec.get('status') != 'escalated')
                    and not awaiting):
                # The dialog went away / was answered in the pane / went to the user meanwhile: not a bad reply.
                self.log('escalate.stale_answer', task=item['task'], decision=item['decision'])
                continue
            stale = json.loads(self.r.get(SPLIT_STALE) or '{}') if not rec and self.service else {}
            if item['task'] in stale:
                # P7: the split was dropped because its request ended (split.stale): a late answer, not a bad reply
                self.log('escalate.stale_answer', task=item['task'], decision=item['decision'],
                         request_id=stale[item['task']])
                continue
            if not rec or (rec.get('status') != 'escalated' and not awaiting):
                raise ValueError('escalations: ' + item['task'] + ' has no open escalate request')
            if kind != 'split' and ('supervisor_profile' in item or 'worker_profiles' in item):
                raise ValueError('escalations: ' + item['task'] + ': supervisor_profile / worker_profiles only answer a split')
            if 'paths' in item and kind != 'report':
                raise ValueError('escalations: ' + item['task'] + ': paths only answer a sub-fleet report')
            wider = [x for x in item.get('paths', []) if not path_within(x, rec.get('paths') or [])]
            if wider:  # §46: the top may grant less than the sub-fleet asked for, never more
                raise ValueError('escalations: ' + item['task'] + ': paths may only narrow what the sub-fleet reported ('
                                 + (', '.join(rec.get('paths') or []) or 'no paths') + '); not inside it: '
                                 + ', '.join(wider))
            if kind in ('dialog', 'report'):
                if item['decision'] not in ('approve', 'reject'):
                    raise ValueError('escalations: ' + item['task'] + ' is a ' + ('dialog' if kind == 'dialog' else
                                     'sub-fleet report') + ': answer approve or reject')
                out.append((item['task'], rec, item))
                continue
            if kind == 'blocked':
                if item['decision'] == 'revise' and not item.get('note', '').strip():
                    raise ValueError('escalations: revise of ' + item['task'] + ' needs a note saying the other way')
                if rec['slot'] in busy and not self.service:
                    # The new dispatch is the answer (the [blocked] note says so); the ruling is not sent on as a
                    # second message. Was a bad reply until 2026-10-28: a rotated supervisor did both twice and the
                    # strikes blocked the fleetmux run.
                    self.log('escalate.answered_by_dispatch', task=item['task'], decision=item['decision'],
                             note=item.get('note', '')[:500])
                    continue
                out.append((item['task'], rec, item))
                continue
            if item['task'] in {x.get('id') for x in decision['dispatch']}:
                raise ValueError('escalations: ' + item['task'] + ' is also dispatched in this reply; do one or the other')
            if item['decision'] == 'approve' and rec['slot'] in busy:
                raise ValueError('escalations: approve ' + item['task'] + ' hands ' + self.label(rec['slot'])
                                 + ' over to a sub-fleet; do not dispatch to it in the same reply')
            out.append((item['task'], rec, item))
        return out

    def escalation_apply(self, t, state, task_id, rec, answer):
        if rec.get('kind') == 'dialog':
            return self.dialog_answer(task_id, answer)
        if rec.get('kind') == 'blocked':
            return self.blocked_ruling(t, state, task_id, rec, answer)
        if rec.get('kind') == 'report':
            return self.report_ruling(task_id, rec, answer)
        esc = self.escalation_state()
        note = answer.get('note', '').strip()
        row = state['tasks'].get(task_id) if state is not None else None
        slot = rec['slot']
        if answer['decision'] == 'approve':
            wps = answer.get('worker_profiles')
            workers = answer.get('workers', len(wps) if wps else rec['workers'])
            profiles = {'worker-' + str(i + 1): p for i, p in enumerate(wps or [])}
            req_id = rec.get('request')  # P7 service split: the request must still be this worker's, .done free
            req = self.r.hgetall(REQ + req_id) if req_id else {}
            refused = (('request ' + req_id + ' is no longer ' + self.label(slot) + "'s (it moved on or was settled)")
                       if req_id and (
                req.get('status') != 'dispatched' or req.get('worker') != slot) else
                ('result id ' + req_id + '.done is already taken') if req_id and (
                    self.r.hgetall(REQ + req_id + '.done') or self.r.get(RESULT + req_id + '.done')) else
                subfleet.check(self, slot, task_id, workers, ready=False, profiles=profiles,
                               job=self.job_spawn_args(rec['job'])['job'] if rec.get('job') else None))
            account = None
            if not refused and not rec.get('job') and not req_id:  # §62: a task split passes the program's account
                account = self.tree_split_account(task_id, rec)
                if account and not account['split']:
                    refused = ('the split does not pay: ' + account['reason'] + ' (program; worker: dT '
                               + decompose._fmt(rec.get('delta_t') or 0) + ', dC ' + decompose._fmt(rec.get('delta_c') or 0)
                               + ')')
            launch, sup_trace = None, None
            if not refused and not rec.get('job'):  # §44 split (D27): a card, else the supervisor chain's pick
                if answer.get('supervisor_profile'):
                    card = answer['supervisor_profile']
                    try:
                        model = AgentRegistry(self.cfg.get('agent_registry') or None).get(card).model
                    except (AgentRegistryError, KeyError, OSError):
                        model = None
                    sup_trace = job_chain.trace({'entry': {'cli': 'claude', 'model': model}, 'profile': card,
                                                 'skipped': []}, 'card')
                else:
                    fields, sup_trace, refused = self.supervisor_chain_pick(slot)
                    answer = dict(answer, **fields)
                    refused = refused or None
            if not refused and (answer.get('supervisor_profile') or answer.get('supervisor_model')):
                # supervisor_model: a job's chain picked a Claude model that has no card (§58 model chains)
                launch, refused = self.split_supervisor_launch(slot, answer.get('supervisor_profile'),
                                                               answer.get('supervisor_model'))
            if refused:  # a limit, not a malformed reply: no strike, the request stays open
                self.log('escalate.approve_refused', task=task_id, reason=refused)
                if req_id:
                    self.log('split.approve_refused', task=task_id, request_id=req_id, reason=refused)
                if rec.get('job'):
                    return  # §58: nobody to ask; job_start puts the job back in the queue
                self.queue('supervisor', '[escalate] approve of ' + task_id + ' refused by the program: ' + refused
                           + '. ' + task_id + ' still waits for your answer (approve with fewer workers / revise / '
                           'reject). Reply with the JSON format from AGENTS.md.')
                return
            output, outputs, goal = None, None, rec['instruction']
            if row is not None:
                entries = [e for e in (contracts.current(state, i) for i in row.get('provides') or []) if e]
                outputs = [str(self.workspace() / e['path']) for e in entries]  # parent paths stay parent paths
                output = outputs[0] if outputs else None
                if entries:  # refs only: the child writes inside its own workspace (subfleet.goal_text)
                    goal += ('\nThe parent fleet verifies these interfaces of ' + task_id + ' (shapes in '
                             + str(self.workspace() / 'contracts.json') + ', read only): '
                             + ', '.join(contracts.ref(e) for e in entries))
                row.update(status='delegated', attempts=row.get('attempts', 0) + 1, reason='sub-fleet ' + task_id)
            if note:
                goal += '\nSupervisor note: ' + note
            spec = dict(slot=slot, task_id=task_id, goal=goal, workers=workers, output=output, proposal=rec['proposal'],
                        contracts=state is not None, outputs=outputs, worker_profiles=wps)
            if req_id:  # P7: the split's own budget, the request it answers, and no child lifecycle (brief §2.1-11)
                spec.update(hours=subfleet.limits(self.cfg)['split_budget_h'], split=req_id, lifecycle=None)
            if rec.get('job'):  # §58: the job's own hours, lifecycle, artifacts and budget card
                spec.update(self.job_spawn_args(rec['job']))
            elif sup_trace:  # §44 (D27): the supervisor's model and why the entries before it were skipped
                spec['model_chain'] = {'supervisor': sup_trace, 'workers': {}}
            seed = self.tree_seed(task_id, rec)  # §62: the sub-fleet's tree starts from this node
            if seed:
                spec['tree'] = seed
            self.tree_split_approved(task_id, rec, account)  # §62: the approved pieces go into this tree
            # The sub-fleet supervisor keeps a live tab in this fleet's worker area (tmux dual display, user
            # 2026-11-09): the slot's generations run in the fleet's tmux from now on, also after the return.
            # A slot still in a plain pane hands off to such a generation first, on the same model.
            plain = False
            if getattr(self.mgr, 'tmux_view_info', None) is not None:
                self.setf(slot, tmux_view=1)
                plain = subfleet.parent_view(self, slot, self.agent(slot).get('pane')) is None
            if plain and not launch:
                rec.update(status='rotating', spec=spec, launch='', supervisor_profile='', workers=workers, note=note,
                           from_generation=int(self.agent(slot).get('generation') or 0))
                self.log('escalate.approved', task=task_id, workers=workers, rotate='same model, into tmux',
                         worker_profiles=wps or [])
            elif launch:  # §44: hand off to a new generation on the supervisor's pick first (escalation_tick)
                restore = self.split_settings(slot)['restore_model']
                if not self.agent(slot).get('split_restore'):  # a re-approve after a refused spawn keeps the original
                    self.setf(slot, split_restore=json.dumps({'launch': self.agent(slot).get('runtime_launch') or ''})
                              if restore else '')
                rec.update(status='rotating', spec=spec, launch=profile_launch.encode_launch(launch),
                           supervisor_profile=answer.get('supervisor_profile') or '', workers=workers, note=note,
                           from_generation=int(self.agent(slot).get('generation') or 0))
                self.log('escalate.approved', task=task_id, workers=workers,
                         rotate=answer.get('supervisor_profile') or answer.get('supervisor_model'),
                         worker_profiles=wps or [])
            else:
                rid = subfleet.request(self.run_dir, **spec)
                rec.update(status='approved', rid=rid, workers=workers, note=note)
                self.log('escalate.approved', task=task_id, request=rid, workers=workers, worker_profiles=wps or [])
            esc[task_id] = rec
            self.split_hold(slot, task_id)
        else:
            esc.pop(task_id, None)
            if self.agent(slot).get('split_restore'):  # §44: rotated for a split that will not happen: back next generation
                self.setf(slot, split_launch=self.agent(slot)['split_restore'], split_restore='')
            rid = rec.get('request')
            mine = 'answer request ' + rid if rid else 'continue ' + task_id
            text = ('[escalate] the supervisor asks you to revise your split of ' + task_id + ': ' + (note or '-')
                    + '\nSend a new escalate JSON' + (' as your answer' if rid else ' at the end of your report')
                    + ', or ' + mine + ' yourself.'
                    if answer['decision'] == 'revise' else
                    '[escalate] the supervisor rejected splitting ' + task_id + (': ' + note if note else '')
                    + '\n' + ('Answer request ' + rid if rid else 'Do ' + task_id) + ' yourself now.')
            if rid:  # P7 service split: back to the worker as the same request; the M<n> number is freed
                items_ = json.loads(self.r.get(ITEMS) or '{}')
                if items_.pop(task_id, None) is not None:
                    self.r.set(ITEMS, json.dumps(items_, ensure_ascii=False))
                req = self.r.hgetall(REQ + rid)
                if req.get('status') == 'dispatched' and req.get('worker') == slot:
                    self.queue(slot, self.request_message(rid, json.loads(req['spec']), text, slot=slot))
                else:  # timed out / settled meanwhile: nothing to send back
                    self.log('split.stale', task=task_id, request_id=rid, status=req.get('status', ''))
                self.log('escalate.' + DECIDED[answer['decision']], task=task_id, note=note, request_id=rid)
                self.save_escalations(esc)
                return
            text = '[Supervisor instruction ' + t.get('id', '') + '] [' + task_id + '] ' + text + \
                '\nReply in the AGENTS.md format (Done / Evidence / Not done / Blocked) when done.'
            if row is not None:
                row.update(status='running', reason=DECIDED[answer['decision']] + ' split')
                self.contract_queue(state, slot, text, task_id)
            else:
                self.queue(slot, text)
            self.log('escalate.' + DECIDED[answer['decision']], task=task_id, note=note)
        self.save_escalations(esc)

    def split_hold(self, slot, task_id):
        """§44: an approved split takes hold of the slot at once. A worker in a turn gets the approve through the
        long-running nudge's PostToolUse channel (program text, one line, taken once by ``split_nudge``): finish the
        step, start nothing new, end the turn. An idle one gets nothing: its next delivery is the handoff
        (split_rotate) or the adoption, and other deliveries on this task are held back (blocked_ruling, dispatch)."""
        info = self.agent(slot)
        if info.get('state') != 'busy' or not info.get('msg_id') or not is_claude(self.runtime_cfg(slot), slot):
            return
        sid = 'fx-' + uuid.uuid4().hex[:16]
        self.setf(slot, split_id=sid, split_prompt=midturn.SPLIT_APPROVED_PROMPT.format(split_id=sid, task=task_id),
                  split_g=str(info.get('generation') or 0), split_message=info['msg_id'], split_send_pending=1)
        self.log('split.held', agent=slot, task=task_id, split_id=sid)

    def split_supervisor_launch(self, slot, profile, model=None):
        """(launch, None) for the next generation of ``slot`` on card ``profile`` (§44), or (None, why not):
        the same card resolver, CLI pair check, host / max_concurrent gate and CLI preflight as a roster swap.
        ``profile`` None + ``model`` (§58 model chains, a Claude model with no card): the overrides a card would give
        (worker_cli claude, worker_model), capped by that model's card like any launch."""
        if not profile:
            where = 'supervisor model ' + str(model) + ': '
            try:
                registry = AgentRegistry(self.cfg.get('agent_registry') or None)
                launch = {'profile_id': '', 'role_text': '',
                          'overrides': {'worker_cli': 'claude', 'worker_model': model}}
                card = self.job_model_card('claude', model, registry)
                gate = self.card_gate(slot, card, registry) if card else None
                if gate:
                    return None, where + gate[1]
                self.preflight_profile(launch)
                return launch, None
            except (AgentRegistryError, OSError, ValueError, subprocess.SubprocessError) as exc:
                return None, where + str(exc)
        where = 'supervisor_profile ' + profile + ': '
        try:
            registry = AgentRegistry(self.cfg.get('agent_registry') or None)
            try:
                card = registry.get(profile)
            except KeyError:
                return None, where + 'unknown agent profile ' + profile
            if card.cli != 'claude':  # subfleet.check: only official Claude Code panes are handed over
                return None, ('supervisor_profile ' + profile + ' runs ' + card.cli + ': only an official Claude Code '
                              'session can become a sub-fleet supervisor')
            launch = night.resolve_launches(self.cfg, {slot: profile})[slot]
            gate = self.card_gate(slot, card, registry)
            if gate:
                return None, where + gate[1]
            self.preflight_profile(launch)
            return launch, None
        except (AgentRegistryError, OSError, ValueError, subprocess.SubprocessError) as exc:
            return None, where + str(exc).replace(slot + ': profile ' + profile + ': ', '')

    def split_rotate(self, task_id, rec):
        """escalation_tick, outside transactions (§44). Once the worker is idle: it writes its handoff note and
        the slot's next generation starts on the supervisor's pick (finish_rotation applies ``split_launch``;
        no §12 live switch), or on its own model when the approve picked none (the handoff only moves a plain
        pane into the fleet's tmux). Once that generation is READY: the spawn request, as for a plain approve."""
        slot = rec['slot']
        info = self.agent(slot)
        settled = (info.get('state') == 'idle' and info.get('runtime_ready') == '1'
                   and not (self.v2 and self.r.hget(self.v2.bus.lease_key(slot), 'current_delivery')))
        if int(info.get('generation') or 0) == rec['from_generation']:
            if settled and not rec.get('rotating_at'):
                if rec['launch']:  # '': same model, the handoff only moves the slot into the fleet's tmux
                    self.setf(slot, split_launch=json.dumps({'launch': rec['launch']}))
                used, window = self.usage(slot)
                self.start_rotation(slot, used, window, prompt=handoff.SPLIT_PROMPT.replace('<task>', task_id))
                rec['rotating_at'] = time.time()
                self.log('escalate.rotate', task=task_id, slot=slot, profile=rec['supervisor_profile'])
                return True
            return False
        if not settled:
            return False
        rec.update(status='approved', rid=subfleet.request(self.run_dir, **rec.pop('spec')))
        self.log('escalate.rotated', task=task_id, slot=slot, request=rec['rid'], generation=info.get('generation'))
        return True

    def dialog_answer(self, task_id, answer):
        """Plan F' 8.3: record the supervisor's ruling only. The watchdog presses the keys on its next tick,
        after re-checking identity, the screen digest and the delete classifier (no side effect here)."""
        esc = self.escalation_state()
        rec = esc[task_id]
        if rec.get('channel') == 'permission_reply':
            return self.permission_answer(task_id, rec, answer)
        rec.update(status='answered', decision=answer['decision'], note=answer.get('note', '').strip(),
                   answered_at=time.time())
        self.save_escalations(esc)
        self.log('dialog.answered', task=task_id, decision=answer['decision'])
        if self.v2:
            self.v2.audit('dialog.answered', slot=rec['slot'], level='info', task=task_id, decision=answer['decision'])

    def permission_answer(self, task_id, rec, answer):
        """opencode: the supervisor's ruling on a non-delete permission request goes to the slot's bridge as
        one permission_reply UI card naming this run / slot / generation / session / request (the bridge
        forwards it only if every field matches what it reported). No key is pressed. A request whose slot
        moved on (generation, session or delivery changed) is dropped and nothing is sent."""
        esc = self.escalation_state()
        esc.pop(task_id, None)
        self.save_escalations(esc)
        slot = rec['slot']
        info = self.agent(slot)
        lease = self.r.hgetall(self.v2.bus.lease_key(slot)) if self.v2 else {}
        pending = json.loads(info.get('oc_permission') or 'null') or {}
        scope = lease.get('current_delivery') if lease.get('status') == 'received' else 'startup-g' + str(info.get('generation', '0'))
        if (not self.v2 or str(info.get('generation', '')) != rec['generation'] or info.get('thread', '') != rec['session_id']
                or pending.get('request_id') != rec['request_id'] or scope != rec['scope']):
            self.log('dialog.stale_answer', task=task_id, decision=answer['decision'])
            if self.v2:
                self.v2.audit('dialog.stale', slot=slot, level='info', reason='identity', task=task_id)
            return
        decision = 'once' if answer['decision'] == 'approve' else 'reject'
        card = {'kind': 'permission_reply', 'run_id': self.v2.bus.run_id, 'slot': slot,
                'generation': int(rec['generation']), 'session_id': rec['session_id'], 'request_id': rec['request_id'],
                'class': 'other', 'decision': decision,
                'metadata': {'event_id': 'ui-ocreply-' + hashlib.sha256((slot + ':' + rec['request_id']).encode()).hexdigest()[:32]}}
        self.v2.ui(slot, card)
        self.setf(slot, prompt_wait='', prompt_wait_at=0, prompt_route='', prompt_channel='', oc_permission='')
        self.log('dialog.answered', task=task_id, decision=answer['decision'])
        self.v2.audit('opencode.permission_reply', slot=slot, level='info', task=task_id, decision=decision,
                      request_id=rec['request_id'], note=answer.get('note', '').strip())

    def blocked_ruling(self, t, state, task_id, rec, answer):
        """Plan F' 8.4: the supervisor's ruling goes to the worker as an instruction (contract task: under its row)."""
        esc = self.escalation_state()
        esc.pop(task_id, None)
        self.save_escalations(esc)
        slot, item, note = rec['slot'], rec.get('item') or '', answer.get('note', '').strip()
        if self.ruling_stale(task_id, rec):
            return
        if self.service:
            return self.service_ruling(task_id, rec, answer, note)
        split = self.escalation_state().get(item) if item else None
        if (split and split.get('kind', 'split') == 'split' and split.get('slot') == slot
                and split.get('status') in ('approved', 'rotating', 'delegated')):
            # §44: the approved split holds the slot; this task goes to the sub-fleet, the ruling is not sent
            # (the 2026-11-08 trial: "[M0] reject ... carry on" read as the split being rejected). Applied after
            # the split answers of the same decision (on_supervisor), so the order inside it does not matter.
            self.log('blocked.superseded', slot=slot, task=task_id, split=item, decision=answer['decision'])
            self.r.rpush(SPLIT_NOTES, '[escalate] your ' + answer['decision'] + ' of ' + task_id + ' was not sent: it is '
                         'superseded by the approved split of ' + item + ' (' + self.label(slot) + ' hands ' + item
                         + ' over to its sub-fleet). Nothing to do for it.')
            return
        text = ('[Supervisor instruction ' + t.get('id', '') + '] [Supervisor ruling] ' + ('[' + item + '] ' if item else '')
                + answer['decision'] + ((': ' + note) if note else '') + '\n(on what you reported as Blocked: '
                + rec.get('reason', '')[:500].replace('\n', '; ') + ')\nCarry on accordingly; reply in the AGENTS.md '
                'format (Done / Evidence / Not done / Blocked) when done.')
        row = state['tasks'].get(item) if state is not None and item else None
        if row is not None:
            row.update(status='running', reason='supervisor ruled on Blocked: ' + answer['decision'])
            self.contract_queue(state, slot, text, item)
        else:
            self.queue(slot, text)
        self.log('blocked.ruled', agent=slot, task=task_id, item=item, decision=answer['decision'])
        if self.v2:
            self.v2.audit('blocked.ruled', slot=slot, level='info', task=task_id, decision=answer['decision'])

    def ruling_stale(self, task_id, rec):
        """A [blocked] ruling answers the one that asked, else it is logged ``blocked.stale`` and never delivered:
        service mode its request (still dispatched: not timed out / answered meanwhile), task mode the slot's
        generation and label that reported Blocked (not a later session, e.g. after a sub-fleet's return)."""
        slot = rec['slot']
        if self.service:
            rid = rec.get('request') or ''
            where = dict(request=rid)
            stale = (self.r.hget(REQ + rid, 'status') if rid else None) != 'dispatched'
        else:  # a record from before the binding (no generation) is not judged
            now = str(self.agent(slot).get('generation') or 0)
            where = dict(generation=rec.get('generation', ''), now=now)
            stale = 'generation' in rec and (now != rec['generation'] or self.label(slot) != rec.get('label'))
        if stale:
            self.log('blocked.stale', agent=slot, task=task_id, **where)
        return stale

    def service_ruling(self, task_id, rec, answer, note):
        """Service mode: the ruling goes back to the worker as the same request (its answer still settles it)."""
        slot, rid = rec['slot'], rec.get('request') or ''
        req = self.r.hgetall(REQ + rid)  # still dispatched (ruling_stale)
        self.queue(slot, self.request_message(rid, json.loads(req['spec']), (
            '[Supervisor ruling] ' + answer['decision'] + ((': ' + note) if note else '') + '\n(on what you reported '
            'as blocked: ' + rec.get('reason', '')[:500].replace('\n', '; ') + ')\nCarry on accordingly and answer the '
            'request.'), slot=slot))
        self.log('blocked.ruled', agent=slot, task=task_id, request=rid, decision=answer['decision'])
        if self.v2:
            self.v2.audit('blocked.ruled', slot=slot, level='info', task=task_id, decision=answer['decision'], request_id=rid)

    def service_blocked_intake(self, slot, rid, reason):
        """Service mode, plan F' 8.4: a worker answered {"blocked": ...}; the request stays dispatched and the
        supervisor rules on it. The [blocked] note goes to the supervisor bound to that request."""
        if applet_lease.take_blocked(self, slot, rid, {'blocked': reason}):  # a leased worker: back to the applet
            return ''
        key_ = BLOCKED_KEY + slot
        who = self.label(slot)
        text = ('[blocked] ' + who + ' (request ' + rid + ') says it is stuck on: ' + reason[:2000].replace('\n', '; ')
                + '\nThe request is still open. Answer in your next JSON: "escalations": [{"task": "' + key_
                + '", "decision": "approve" | "revise" | "reject", "note": "..."}] with status RUNNING, dispatch [] and '
                'skip []. approve = it may go on the way it proposed (say in note how far that goes); revise = another '
                'way (note required); reject = do not do that part, answer with what it has. Reply status BLOCKED only '
                'for a delete / move, or for what truly only the user can decide, and say which in reason.')
        mid = self.v2.queue('supervisor', text, request_id=rid, kind='followup') if self.v2 else self.queue('supervisor', text)
        esc = self.escalation_state()
        esc[key_] = {'kind': 'blocked', 'slot': slot, 'item': '', 'request': rid, 'reason': reason[:2000],
                     'status': 'escalated', 'asked_at': time.time(), 'message_id': mid or ''}
        self.save_escalations(esc)
        self.log('blocked.asked', agent=slot, request=rid, reason=reason[:300])
        return key_

    def service_split_intake(self, slot, rid, fields, instruction=None, quiet=False):
        """Service mode, P7 (brief v2.1 #3): a worker answered request ``rid`` with the §44 escalate shape. The
        request stays dispatched. The program numbers it M<n> (the request's own number if it has one), rewrites
        the proposal ids to M<n>.<i>, and hands it to the §44 intake; the supervisor answers "task": "M<n>".
        Refusals go back to the worker as the same request and to the supervisor."""
        if applet_lease.take_blocked(self, slot, rid, {'blocked': 'split proposal', 'proposal': fields}):
            return None  # applets.md section 4: a leased worker's proposal goes back to the applet
        req = self.r.hgetall(REQ + rid)
        if req.get('status') != 'dispatched' or req.get('worker') != slot:
            self.log('split.stale_owner', agent=slot, request_id=rid, status=req.get('status', ''))
            return None
        spec, who = json.loads(req['spec']), self.label(slot)

        def tell(text):
            return self.v2.queue('supervisor', text, request_id=rid, kind='followup') if self.v2 else \
                self.queue('supervisor', text)

        def refuse(why, event, notify=True, **extra):
            self.log(event, agent=slot, request_id=rid, reason=why, **extra)
            self.queue(slot, self.request_message(rid, spec, (
                '[escalate] your split request was refused by the program: ' + why + '\nAnswer the request '
                'yourself (or fix the escalate JSON and send it again).'), slot=slot))
            if notify:
                tell('[escalate] refused for ' + who + ': ' + why + ' (request ' + rid
                     + ' stays open; the worker was told and answers it itself)')
            return None
        if self.cfg.get('split_judge', 'worker') == 'off':
            return refuse('split_judge is off in this fleet: requests are not split', 'split.refused')
        items_ = json.loads(self.r.get(ITEMS) or '{}')
        key_ = next((k for k, row in items_.items() if row.get('request') == rid), '') or self.next_item_id([])
        old = self.escalation_state().get(key_)
        if old and old.get('status') != 'escalated':  # approved / rotating / delegated: never replaced or re-spawned
            return refuse(key_ + ' of request ' + rid + ' is already ' + old.get('status', '') + '; no second split',
                          'split.duplicate_refused', notify=False, task=key_, status=old.get('status', ''))
        prev = items_.get(key_)
        if old and old.get('slot') != slot:  # the request moved to this worker (replay): the current owner asks
            self.log('split.owner_replaced', task=key_, request_id=rid, previous=old.get('slot', ''), agent=slot)
        items_[key_] = {'to': slot, 'request': rid, 'instruction': (instruction or (prev or {}).get('instruction')
                                                                    or spec.get('instruction') or '')}
        self.r.set(ITEMS, json.dumps(items_, ensure_ascii=False))
        proposal = fields.get('proposal')
        if isinstance(proposal, list):
            fields = dict(fields, proposal=[dict(x, id=key_ + '.' + str(i)) if isinstance(x, dict) else x
                                            for i, x in enumerate(proposal)])
        task_id, accepted, note, _ = self.escalate_intake(slot, fields, None)
        if not accepted:
            items_ = json.loads(self.r.get(ITEMS) or '{}')
            if prev is None:
                items_.pop(key_, None)
            else:
                items_[key_] = prev
            self.r.set(ITEMS, json.dumps(items_, ensure_ascii=False))
            why = note.strip().split(': ', 1)[-1].rsplit(' (the worker was told', 1)[0]
            return refuse(why, 'split.refused', task=key_)
        stale = json.loads(self.r.get(SPLIT_STALE) or '{}')
        if stale.pop(task_id, None) is not None:  # the number is in use again
            self.r.set(SPLIT_STALE, json.dumps(stale, ensure_ascii=False))
        esc = self.escalation_state()
        esc[task_id]['request'] = rid
        # quiet: the supervisor gave this split itself (supervisor_split) and approves it in the same step
        esc[task_id]['message_id'] = '' if quiet else (tell(
            note.strip() + '\nThis split answers service request ' + rid + ': ' + who + ' (request ' + rid
            + ') keeps it open until the sub-fleet starts.') or '')
        self.save_escalations(esc)
        self.log('split.asked', agent=slot, request_id=rid, task=task_id, replaced=bool(old))
        return task_id

    def escalation_tick(self):
        """Approved splits: read the spawn answer; a refusal reopens the request for the supervisor.
        P7 service splits (records with a ``request``): accepted -> the request is answered ``ok`` + ``delegated``
        (split_delegated); a split whose request is no longer open is dropped (split.stale), never rotated or
        spawned."""
        if not self.running():
            return
        esc, changed = self.escalation_state(), False
        for task_id, rec in list(esc.items()):
            if task_id.startswith(REPORT_KEY):  # §24: an awaiting report whose sub-fleet ended is dropped
                changed = blocked_scope.awaiting_tick(self, esc, task_id, rec) or self.report_reask(task_id, rec) or changed
                continue
            rid = rec.get('request')
            if rid and rec.get('status') in ('escalated', 'rotating') and not self.r.hgetall(REQ + rid):
                self.split_drop(esc, task_id, rec, 'request ' + rid + ' ended before the split started')
                changed = True
                continue
            if rec.get('status') == 'rotating':
                changed = self.split_rotate(task_id, rec) or changed
                continue
            if rec.get('status') != 'approved':
                continue
            try:
                answer = load(subfleet.answer_path(self.run_dir, rec['rid']))
            except (OSError, ValueError):
                continue
            changed = True
            if answer.get('status') == 'accepted':
                if rid:
                    self.split_delegated(task_id, rec, answer)
                    continue
                rec.update(status='delegated', run=answer.get('run'))
                self.log('escalate.delegated', task=task_id, run=answer.get('run'))
                continue
            if rid and not self.r.hgetall(REQ + rid):
                self.split_drop(esc, task_id, rec, 'request ' + rid + ' ended before the split started: '
                                + str(answer.get('reason')))
                continue
            if rec.get('job'):  # §58: never back to the supervisor; the job waits for a slot again
                self.split_drop(esc, task_id, rec, 'sub-fleet ' + task_id + ' could not start: '
                                + str(answer.get('reason')))
                continue
            rec['status'] = 'escalated'
            state = self.contract_state()
            if state is not None and task_id in state['tasks']:
                state['tasks'][task_id].update(status='escalated', reason='sub-fleet refused')
                self.save_contract(state)
            self.log('escalate.spawn_refused', task=task_id, reason=answer.get('reason'))
            self.tree_split_rollback(task_id, 'sub-fleet could not start: ' + str(answer.get('reason')))
            self.queue('supervisor', '[escalate] sub-fleet ' + task_id + ' could not start: ' + str(answer.get('reason'))
                       + '\n' + task_id + ' waits for your answer again (escalations: approve with fewer workers / '
                       'revise / reject). Reply with the JSON format from AGENTS.md.')
        if changed:
            self.save_escalations(esc)

    def split_drop(self, esc, task_id, rec, why, count=True):
        """P7: a service split whose request is gone (timed out, settled, not replayed): the record and its M<n>
        row go, a late answer to it is only logged (SPLIT_STALE), and a slot rotated for it gets its own model
        back next generation. ``esc`` is changed in place; the caller saves it."""
        esc.pop(task_id, None)
        items_ = json.loads(self.r.get(ITEMS) or '{}')
        if items_.pop(task_id, None) is not None:
            self.r.set(ITEMS, json.dumps(items_, ensure_ascii=False))
        stale = json.loads(self.r.get(SPLIT_STALE) or '{}')
        stale[task_id] = rec.get('request', '')
        self.r.set(SPLIT_STALE, json.dumps(stale, ensure_ascii=False))
        slot = rec['slot']
        if (rec.get('status') == 'rotating' or rec.get('job')) and self.agent(slot).get('split_restore'):
            self.setf(slot, split_launch=self.agent(slot)['split_restore'], split_restore='')
        self.log('split.stale', task=task_id, request_id=rec.get('request', ''), status=rec.get('status', ''), reason=why)
        self.tree_split_rollback(task_id, why)
        if rec.get('job'):
            self.job_release(rec['job'], why, count=count and not self.stopping)

    def split_delegated(self, task_id, rec, answer):
        """P7: the task sub-fleet of a service split was accepted: the original request is answered now
        (``ok`` + ``delegated``; the sub-fleet's own outcome follows as ``<request>.done``). ``rec`` is changed
        in place; the caller saves the escalations."""
        rid = rec['request']
        rec.update(status='delegated', run=answer.get('run'))
        if rec.get('job'):  # §58: a job has one result, the sub-fleet's outcome (split_finished)
            self.job_update(rec['job'], state='running', run=str(answer.get('run') or ''))
            self.log('job.running', job=rec['job'], task=task_id, run=answer.get('run'))
            return
        self.publish(rid, 'ok', worker=self.label(rec['slot']), result={
            'reply': '已转 task 子 fleet ' + task_id + ' 处理；子 fleet 结束后另发结果 ' + rid + '.done。',
            'delegated': task_id})
        self.log('split.delegated', task=task_id, request_id=rid, run=answer.get('run'))

    def split_owns(self, rid):
        """P7: does a service split hold ``<rid>.done`` (an open split of request ``rid``, or its delegated answer)?"""
        if any(v.get('request') == rid for v in self.escalation_state().values()):
            return True
        raw = self.r.get(RESULT + rid)
        return bool(raw) and bool((json.loads(raw).get('result') or {}).get('delegated'))

    def split_settle_accepted(self):
        """P7 (S11), before a resume replays requests: service splits whose spawn was already accepted settle their
        request now, so it is not routed again. Only approved records with a ``request`` and an accepted answer;
        nothing else is touched (no rotation, no refused answers, no task-mode records, no queues)."""
        esc, changed = self.escalation_state(), False
        for task_id, rec in esc.items():
            if not rec.get('request') or rec.get('status') != 'approved':
                continue
            try:
                answer = load(subfleet.answer_path(self.run_dir, rec['rid']))
            except (OSError, ValueError, KeyError):
                continue
            if answer.get('status') == 'accepted':
                self.split_delegated(task_id, rec, answer)
                changed = True
        if changed:
            self.save_escalations(esc)

    def split_pin_on_resume(self):
        """P7, in a resume before respawn and the request replay: an approved / rotating service split keeps its
        request (``dispatched`` to its slot, as before the restart; not a replay) so the spawn still runs; a
        rotating one not yet handed off gets ``split_launch`` now, so the respawn starts the slot on the
        supervisor's pick (split_rotate's second branch then writes the spawn request). The approval is dropped
        only when it cannot be kept (request ended, slot gone, request now someone else's): split_drop plus one
        line to the supervisor. Returns the pinned request ids (requeue_requests skips them)."""
        esc, pinned, changed = self.escalation_state(), set(), False
        for task_id, rec in list(esc.items()):
            rid, slot = rec.get('request'), rec.get('slot')
            if rec.get('kind', 'split') != 'split' or not rid or rec.get('status') not in ('approved', 'rotating'):
                continue
            req = self.r.hgetall(REQ + rid)
            why = ('request ' + rid + ' ended before the restart' if not req else
                   self.label(slot) + ' is no longer a worker of this fleet' if slot not in self.workers else
                   'request ' + rid + ' is now ' + req['worker'] + "'s" if (req.get('worker') or slot) != slot else '')
            if why:
                changed = True
                self.split_drop(esc, task_id, rec, why + ' (resume)', count=False)
                if rec.get('job'):
                    continue  # §58: back in the job queue (split_drop), nothing for the supervisor to route
                self.queue('supervisor', '[escalate] ' + task_id + ' approval lapsed on resume: ' + why
                           + '. Route or answer ' + rid + ' as usual if it is still open.')
                continue
            self.r.hset(REQ + rid, mapping={'status': 'dispatched', 'worker': slot})
            if rec['status'] == 'rotating' and not rec.get('rotating_at') and rec.get('launch'):
                self.setf(slot, split_launch=json.dumps({'launch': rec['launch']}))
            pinned.add(rid)
            self.log('split.pinned', task=task_id, request_id=rid, agent=slot, status=rec['status'])
        if changed:
            self.save_escalations(esc)
        return pinned

    def split_finished(self, child, child_run):
        """P7: sub-fleet ``child`` (its record, just collected) answered a service split: publish the follow-up
        result ``<request>.done`` once (ok only for a child result 'completed'; its result.json decides, also on a
        parent stop) and drop the split. Returns a line for the supervisor's notice ('' for any other sub-fleet)."""
        task_id = child['task_id']
        esc = self.escalation_state()
        rec = esc.get(task_id)
        if not rec or not rec.get('request') or rec.get('status') != 'delegated':
            return ''
        rid = rec['request']
        outcome = (subfleet.child_result(child_run) or {}).get('status') or child.get('outcome') or 'cancelled'
        reason = str(child.get('reason') or '')
        if rec.get('job'):
            esc.pop(task_id, None)  # its M<n> row stays, as below
            self.save_escalations(esc)
            return self.job_finished(rec['job'], task_id, rec['slot'], child_run, outcome, reason)
        if outcome == 'completed':
            self.publish(rid + '.done', 'ok', worker=self.label(rec['slot']), result={
                'reply': 'task 子 fleet ' + task_id + ' 已完成' + ('：' + reason if reason else '。'),
                'delegated': task_id, 'run': str(child_run), 'outcome': outcome})
        else:
            self.publish(rid + '.done', 'failed', worker=self.label(rec['slot']),
                         error='task sub-fleet ' + task_id + ' ended ' + outcome + (': ' + reason if reason else ''))
        esc.pop(task_id, None)  # its M<n> row stays: sub/<M<n>> exists, the number is never given out again
        self.save_escalations(esc)
        self.log('split.done', task=task_id, request_id=rid, outcome=outcome)
        return (' Request ' + rid + ' was answered when ' + task_id + ' started; its follow-up result ' + rid + '.done ('
                + ('ok' if outcome == 'completed' else 'failed') + ') went out now.')

    # -- §58 jobs: `fleetmux task` on a service fleet, each run by its own task-mode sub-fleet
    def job_state(self):
        raw = self.r.get(JOBS)
        return json.loads(raw) if raw else {}

    def save_jobs(self, jobs):
        self.r.set(JOBS, json.dumps(jobs, ensure_ascii=False, separators=(',', ':')))
        self.job_mirror(jobs)

    def job_mirror(self, jobs=None):
        """section 58 "Restart": <run>/jobs.json follows fx:jobs (a service root only), and <fleet home>/job-runs/ names
        this run while it holds open jobs, so a later `start` finds them while this run's Redis is down."""
        if not self.service or self.cfg.get('parent_config'):
            return
        jobs = self.job_state() if jobs is None else jobs
        try:
            write_job_mirror(self.cfg, self.run_dir, jobs)
        except OSError as exc:
            self.log('job.mirror_failed', error=str(exc)[:200])

    def job_stopped(self):
        """The explicit-stop tail: the last mirror write before the run's Redis goes down."""
        self.job_mirror()

    def job_publish(self, jid, status, **fields):
        """Publish job ``jid``'s one result and keep a copy in <run>/job-results/<job id>.json, which a join stub's
        `result` reads when the root is down or the Redis copy expired (RESULT_TTL)."""
        self.publish(jid, status, **fields)
        raw = self.r.get(RESULT + jid)
        try:
            save(self.run_dir / JOB_RESULTS / (jid + '.json'), json.loads(raw) if raw else dict(
                id=jid, status=status, at=time.time(), **{k: v for k, v in fields.items() if k != 'replace'}))
        except OSError as exc:
            self.log('job.result_mirror_failed', job=jid, error=str(exc)[:200])

    def job_has_result(self, jid):
        return bool(self.r.exists(RESULT + jid)) or (self.run_dir / JOB_RESULTS / (jid + '.json')).exists()

    def job_notify_failed(self, jid, reason):
        """A job that could not be carried over: one line on the root's outbound channel (lifecycle_close.notify:
        tg / channels / parent mailbox, notifications.jsonl), once per job id."""
        if not self.r.hsetnx(JOB_NOTIFIED, jid, str(time.time())):
            return
        policy = {}
        if self.cfg.get('lifecycle'):
            try:
                policy = lifecycle.get_mode(self.cfg['lifecycle'])
            except ValueError:
                policy = {}
        text = '[fleet ' + lifecycle_close.fleet_name(self) + '] job ' + jid + ' failed: ' + str(reason)[:400]
        try:
            lifecycle_close.notify(self, 'job_failed', text, policy)
        except Exception as exc:  # the outbound channel never stops the daemon
            self.log('job.notify_failed', job=jid, error=str(exc)[:200])

    def job_fail(self, jid, outcome, why, notify=False):
        """Pop job ``jid`` and publish it failed with ``why`` (outcome ``cancelled`` / ``not_carried``)."""
        jobs = self.job_state()
        job = jobs.pop(jid, None)
        if job is not None:
            esc = self.escalation_state()
            if job.get('task') and (esc.get(job['task']) or {}).get('job') == jid:
                rec = esc.pop(job['task'])
                if self.agent(rec['slot']).get('split_restore'):
                    self.setf(rec['slot'], split_launch=self.agent(rec['slot'])['split_restore'], split_restore='')
                self.save_escalations(esc)
            self.job_hot_drop(job)
            self.save_jobs(jobs)
        if not self.r.hgetall(REQ + jid):  # a carried row (job_carry_in) or an expired hash: one is made for it
            self.r.hset(REQ + jid, mapping={'status': 'job_queued', 'created': awake.now(), 'worker': '', 'job': 1})
        self.job_publish(jid, 'failed', worker='fleet', error=why, result={'job': jid, 'outcome': outcome,
                                                                          'reason': why})
        self.log('job.failed', job=jid, outcome=outcome, reason=why[:300])
        if notify:
            self.job_notify_failed(jid, why)

    def job_carry_problem(self, job):
        """Why job row ``job`` cannot be carried over / resumed (its roster, roles or repo is gone), else ''."""
        spawn = job.get('spawn') or {}
        for key in ('roster', 'roles'):
            if spawn.get(key) and not Path(spawn[key]).exists():
                return 'its ' + key + ' ' + spawn[key] + ' no longer exists'
        repo = (spawn.get('deliver') or {}).get('repo')
        if repo and not Path(repo).exists():
            return 'its repository ' + str(repo) + ' no longer exists'
        return ''

    def job_cancel(self, jid, by='operator'):
        """section 58 "Restart" / stop on a join stub: cancel job ``jid``. Without a running sub-fleet it is cancelled at
        once (no requeue) and published failed (outcome cancelled); with one the cancel is recorded on the row and
        the sub-fleet gets a plain stop (a cancel, never a suspend): its collect publishes the result."""
        jobs = self.job_state()
        job = jobs.get(jid)
        if job is None:
            return {'status': 'ended', 'job': jid}
        live = [d for d in subfleet.job_children(self.run_dir, jid)
                if subfleet.record(d).get('state') in subfleet.ACTIVE or daemon_running(d)]
        if live:
            job['cancel'] = {'by': by, 'at': time.time()}
            self.save_jobs(jobs)
            for d in live:
                rec = subfleet.record(d)
                if rec.get('state') != 'stopping' or rec.get('outcome') == 'suspended':
                    rec.update(state='stopping', outcome='cancelled', reason='cancelled by ' + by)
                    subfleet.save_record(d, rec)
                try:
                    subfleet.post_stop(load(d / 'config.json'))
                except (OSError, ValueError):
                    pass
            self.log('job.cancelling', job=jid, by=by)
            return {'status': 'stopping', 'job': jid}
        folder = self.run_dir / 'subfleet-requests'
        for path in (folder.glob('sf-*.json') if folder.is_dir() else []):  # an approved spawn never runs
            try:
                if not path.name.endswith('.answer.json') and (load(path).get('job') or {}).get('id') == jid:
                    path.unlink()
            except (OSError, ValueError):
                pass
        state = job.get('state') or 'queued'
        self.job_fail(jid, 'cancelled', 'cancelled by ' + by + ' while ' + state + (
            '' if state == 'suspended' else ', before its sub-fleet started'))
        return {'status': 'cancelled', 'job': jid}

    def job_suspended(self, jid, task_id, child_run, reason=''):
        """section 58 "Restart": the sub-fleet of job ``jid`` was suspended (root stop, or a root restart that found its
        delegated session gone). Nothing is published: the row waits as ``suspended`` and job_start hands the same
        sub-fleet to a fresh slot of this root (subfleet.readopt_child)."""
        jobs = self.job_state()
        job = jobs.get(jid)
        esc = self.escalation_state()
        rec = esc.pop(task_id, None)
        if rec is not None:
            if self.agent(rec['slot']).get('split_restore'):
                self.setf(rec['slot'], split_launch=self.agent(rec['slot'])['split_restore'], split_restore='')
            self.save_escalations(esc)
        if job is None:
            return
        if job.get('cancel'):
            self.job_fail(jid, 'cancelled', 'cancelled by ' + job['cancel'].get('by', 'operator'))
            return
        job.update(state='suspended', slot='', task=task_id, run=str(child_run), resume_run='', waiting='',
                   suspended_at=time.time())
        self.save_jobs(jobs)
        self.r.hset(REQ + jid, mapping={'status': 'job_queued', 'worker': ''})
        self.log('job.suspended', job=jid, task=task_id, reason=str(reason or '')[:200])

    def job_carry_in(self):
        """section 58 "Restart", first boot of a new run whose `start` could not continue an earlier run of this fleet home
        (``carry_from``): that run's never-started rows (queued, or starting with no sub-fleet) are taken over with
        their ids and ``created`` (order kept); a started one, or one whose roster / roles / repo is gone, is
        published failed with the reason. The old jobs.json becomes jobs.carried.json (the tombstone)."""
        for src in self.cfg.get('carry_from') or []:
            old = Path(src['run'])
            why = str(src.get('reason') or 'the run could not be continued')
            path = old / JOBS_FILE
            try:
                rows = load(path)
            except (OSError, ValueError):
                continue
            if not isinstance(rows, dict):
                continue
            jobs = self.job_state()
            for jid, row in sorted(rows.items(), key=lambda x: x[1].get('created') or 0):
                if jid in jobs or self.job_has_result(jid):
                    continue
                children = subfleet.job_children(old, jid)
                started = row.get('state') in ('running', 'suspended') or bool(children)
                problem = ('its sub-fleet ' + str(row.get('task') or '?') + ' runs under ' + str(old) + ', which could '
                           'not be continued: ' + why) if started else self.job_carry_problem(row)
                goal = row.get('goal') or ''
                spec = {'id': jid, 'payload': {'goal': goal}, 'instruction': 'Job: ' + goal.splitlines()[0][:200] if goal
                        else 'Job', 'reply': {'required': ['reply']}}
                self.r.hset(REQ + jid, mapping={'spec': json.dumps(spec, ensure_ascii=False), 'status': 'job_queued',
                                                'created': awake.now(), 'attempts': 0, 'worker': '', 'job': 1})
                if problem:
                    self.job_fail(jid, 'not_carried', 'not carried over from ' + str(old) + ': ' + problem, notify=True)
                    continue
                jobs = self.job_state()
                jobs[jid] = dict(row, state='queued', slot='', task='', run='', resume_run='', waiting='', hot_slot='',
                                 carried_from=str(old))
                self.save_jobs(jobs)
                self.log('job.carried', job=jid, source=str(old))
                if self.service:
                    self.queue('supervisor', '[job ' + jid + '] carried over from ' + str(old) + ' (' + why + '); the '
                               'program runs it in its own sub-fleet. This is not a request for you: do not dispatch, '
                               'answer or skip it. Reply with the usual service decision JSON: status RUNNING, dispatch '
                               '[], skip [].', request_id=jid, kind='followup')
            try:
                save(old / JOBS_CARRIED, {'carried_to': str(self.run_dir), 'reason': why, 'jobs': rows})
                path.unlink(missing_ok=True)
                try:
                    job_index(load(old / 'config.json'), old, False)
                except (OSError, ValueError):
                    pass
            except OSError as exc:
                self.log('job.carry_tombstone_failed', source=str(old), error=str(exc)[:200])

    def job_resume_reconcile(self):
        """section 58 "Restart", in `start --resume` / a continued run, before respawn and the sub-fleet relaunch: drop rows
        a newer run took over (jobs.carried.json), put back mirror rows the reloaded Redis lost (never one with a
        result, in Redis or in job-results/), apply cancels written while the root was down, and suspend the job
        sub-fleets whose delegated session is gone (service root)."""
        if self.cfg.get('parent_config') or not self.v2:
            return
        jobs, changed = self.job_state(), False
        for job in jobs.values():  # rows that live through this restart: job_start checks their files again
            if not job.get('restarted'):
                job['restarted'] = True
                changed = True
        try:
            carried = (load(self.run_dir / JOBS_CARRIED).get('jobs') or {})
        except (OSError, ValueError, AttributeError):
            carried = {}
        for jid in carried:
            if jid in jobs:
                jobs.pop(jid)
                self.r.delete(REQ + jid)
                self.log('job.carried_away', job=jid)
                changed = True
        try:
            mirror = load(self.run_dir / JOBS_FILE) if self.service else {}
        except (OSError, ValueError):
            mirror = {}
        for jid, row in (mirror.items() if isinstance(mirror, dict) else []):
            if jid in jobs or jid in carried or self.job_has_result(jid):
                continue
            kids = subfleet.job_children(self.run_dir, jid)
            suspended = [d for d in kids if subfleet.record(d).get('state') == 'suspended']
            row = dict(row, slot='', waiting='', resume_run='')
            if suspended:
                row.update(state='suspended', run=str(suspended[-1]))
            elif not kids:
                row.update(state='queued', task='', run='')
            else:
                continue  # an active sub-fleet: its collect decides
            goal = row.get('goal') or ''
            spec = {'id': jid, 'payload': {'goal': goal}, 'instruction': 'Job: ' + goal.splitlines()[0][:200] if goal
                    else 'Job', 'reply': {'required': ['reply']}}
            self.r.hset(REQ + jid, mapping={'spec': json.dumps(spec, ensure_ascii=False), 'status': 'job_queued',
                                            'created': awake.now(), 'attempts': 0, 'worker': '', 'job': 1})
            jobs[jid] = row
            self.log('job.restored', job=jid, state=row['state'])
            changed = True
        if changed:
            self.save_jobs(jobs)
        folder = self.run_dir / JOB_CANCELS
        for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
            try:
                by = (load(path) or {}).get('by') or 'operator'
            except (OSError, ValueError):
                by = 'operator'
            if path.stem in self.job_state():
                self.job_cancel(path.stem, by=by)
            path.unlink(missing_ok=True)
        if self.service:
            self.job_suspend_lost()

    def job_suspend_lost(self):
        """section 58 "Restart": a root restart found the delegated session of a running job sub-fleet gone (a reboot or a
        stop ended the fleet's tmux). Its daemon gone: suspended now. Its daemon alive: it gets a suspend stop and the
        normal collect suspends it once it exits; nothing is relaunched or re-delegated while it lives."""
        probe = getattr(self.mgr, 'tmux_session_alive', None)
        if probe is None:
            return
        jobs = self.job_state()
        for d in subfleet.active_children(self.run_dir):
            rec = subfleet.record(d)
            jid = rec.get('job')
            if not jid or jid not in jobs or jobs[jid].get('cancel') or rec.get('state') != 'running':
                continue
            pane = self.agent(rec['slot']).get('delegated_pane') or rec.get('pane')
            if not pane or probe(pane) is not False:
                continue
            if (subfleet.child_result(d) or {}).get('status') not in (None, 'suspended'):
                continue  # it ended by itself: the normal collect publishes it
            why = 'root restarted; the delegated session of ' + self.label(rec['slot']) + ' is gone'
            if daemon_running(d):
                rec.update(state='stopping', outcome='suspended', reason=why)
                subfleet.save_record(d, rec)
                try:
                    subfleet.post_stop(load(d / 'config.json'), suspend=True)
                except (OSError, ValueError):
                    pass
                self.log('job.suspending', job=jid, task=rec['task_id'], reason=why)
                continue
            subfleet.suspend_dead(self, d, rec, why)

    def job_update(self, jid, **fields):
        jobs = self.job_state()
        if jid in jobs:
            jobs[jid].update(fields)
            self.save_jobs(jobs)

    def job_holding(self, slot):
        """The open job whose sub-fleet ``slot`` supervises (or is about to), or that added ``slot`` for itself, else ''."""
        return next((jid for jid, job in self.job_state().items()
                     if (job.get('slot') == slot and job.get('state') in ('starting', 'running'))
                     or job.get('hot_slot') == slot), '')

    def job_spawn_fields(self, spec):
        """§58: the job's own sub-fleet fields (name, roster, roles, deliver), checked now; ValueError
        when one is bad. Paths must be absolute: the daemon's directory is not the caller's."""
        out = {}
        if 'name' in spec:
            name = spec['name']
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60 or '\n' in name:
                raise ValueError('name must be one line of 1 to 60 characters')
            out['name'] = ' '.join(name.split())
        if spec.get('roster') is not None:
            path = spec['roster']
            if not isinstance(path, str) or not os.path.isabs(path) or not Path(path).is_file():
                raise ValueError('roster must be the absolute path of a roster YAML file: ' + repr(path))
            if 'workers' in spec or 'worker_profiles' in spec:
                raise ValueError('roster names the sub-fleet workers and their cards; do not also give workers or '
                                 'worker_profiles')
            try:
                roster.load_entries(path)
                import yaml
                data = yaml.safe_load(Path(path).read_bytes())
            except (OSError, ValueError) as exc:
                raise ValueError('roster ' + path + ' rejected: ' + str(exc).replace('\n', '; '))
            refused = [k for k in JOB_ROSTER_REFUSED if data.get(k) is not None]
            if refused:
                raise ValueError('roster ' + path + ': a job sub-fleet takes no ' + ', '.join(refused))
            out['roster'] = str(Path(path).resolve())
        if spec.get('roles') is not None:
            path = spec['roles']
            if not isinstance(path, str) or not os.path.isabs(path) or not Path(path).is_dir():
                raise ValueError('roles must be the absolute path of a directory of <agent-name>.md files: ' + repr(path))
            out['roles'] = str(Path(path).resolve())
        overrides = subfleet.check_overrides(spec.get('overrides'))
        if overrides:
            out['overrides'] = overrides
        life = spec.get('lifecycle')
        if life == 'dev_task':  # the same checks as a root dev_task run (Fleet.submit); the gate runs in the child
            out['deliver'] = lifecycle_deliver.validate_deliver(spec.get('deliver'))
        elif spec.get('deliver') is not None:
            raise ValueError('deliver needs lifecycle dev_task')
        return out  # review_file (JOB_RETIRED) of an older spec is not carried

    def job_settings(self, spec, fields=None):
        """The sub-fleet of a job: fleet.yaml#job_subfleet, then the job spec's own keys. ValueError when bad.
        ``fields`` (job_spawn_fields): a roster sets the workers and their cards."""
        base = fleet_config.job_subfleet()
        out = {k: spec.get(k, base[k]) for k in base if k not in ('supervisor', 'worker')}
        # §58 model chains: which layer's chain or card each role runs (job_chain.effective; a chain voids a lower
        # layer's card and the other way round). The picks happen in job_start.
        eff = job_chain.effective(spec, base)
        out.update(supervisor_profile=eff['supervisor_profile'], worker_profiles=eff['worker_profiles'],
                   chains={'supervisor': eff['supervisor'], 'worker': eff['worker']}, chain_source=eff['source'])
        cards = []
        if (fields or {}).get('roster'):
            names, _, profiles = roster.load_entries(fields['roster'])
            out.update(workers=len(names), worker_profiles=[])
            cards = sorted(set(profiles.values()))
        wps = out['worker_profiles']
        if wps is None:
            wps = out['worker_profiles'] = []
        if not isinstance(wps, list) or not all(isinstance(x, str) for x in wps):
            raise ValueError('worker_profiles must be a list of agent card ids')
        if 'worker_profiles' in spec and 'workers' not in spec and wps:
            out['workers'] = len(wps)
        workers = out['workers']
        if type(workers) is not int or workers < 1:
            raise ValueError('workers must be a positive integer')
        if wps and len(wps) != workers:
            raise ValueError('worker_profiles names ' + str(len(wps)) + ' cards for ' + str(workers) + ' workers')
        if type(out['hours']) not in (int, float) or not 0 < out['hours'] < 1000:
            raise ValueError('hours must be a positive number')
        cap = subfleet.limits(self.cfg, job=True)['child_workers_max']  # §58: no default cap for a job
        if cap is not None and workers > cap:
            raise ValueError('child_workers_max ' + str(cap) + ': the job asks for ' + str(workers) + ' workers')
        sup = out['supervisor_profile']
        if sup is not None and not isinstance(sup, str):
            raise ValueError('supervisor_profile must be an agent card id')
        if sup or wps or cards:
            try:
                registry = AgentRegistry(self.cfg.get('agent_registry') or None)
            except (AgentRegistryError, OSError) as exc:
                raise ValueError('cannot read agent cards: ' + str(exc))
            for card_id in ([sup] if sup else []) + wps + cards:
                try:
                    card = registry.get(card_id)
                except KeyError:
                    raise ValueError('unknown agent card ' + card_id)
                if card_id == sup and card.cli != 'claude':
                    raise ValueError('supervisor_profile ' + sup + ' runs ' + card.cli + ': only an official Claude '
                                     'Code session can become a sub-fleet supervisor')
        return out

    def job_pi_gate(self, cli, model):
        """§51 / §58 model chains: why this fleet cannot run a pi sub-fleet worker (pi binary, --allow-unsandboxed-pi),
        else None. Passed to job_chain as ``refused`` at submit and at start."""
        return subfleet.named_pi_problem(self.cfg, {'worker_cli': 'pi'}) if cli == 'pi' else None

    def job_root_busy(self):
        """§19 "Task root": a root task fleet (v2) whose own task runs takes a `task` as a job."""
        return (not self.service and self.v2 is not None and not self.cfg.get('parent_config')
                and self.task().get('status') == 'running')

    def job_submit(self, spec):
        """§58: register a job and return its id at once. The job never goes to the supervisor's routing queue:
        job_tick gives it a slot and a sub-fleet. Every check that does not depend on a free slot runs here, so a
        job that can never start is refused now instead of waiting forever."""
        if not isinstance(spec, dict) or not isinstance(spec.get('goal'), str) or not spec['goal'].strip():
            raise ValueError('a job needs a goal')
        extra = sorted(set(spec) - set(JOB_KEYS))
        if extra:
            raise ValueError('a job spec takes only ' + ', '.join(JOB_KEYS) + ' (not ' + ', '.join(extra) + ')')
        if not self.v2 or self.cfg.get('parent_config'):
            raise ValueError('jobs run only on a root fleet of transport v2')
        if not self.service and not self.job_root_busy():
            raise ValueError('this task fleet runs no task of its own now; it takes jobs only while its task runs. '
                             'Nothing was submitted. Start a fleet of its own (`fleetmux start --no-join`), or stop '
                             'this one')
        problem = subfleet.backend_problem(self.cfg)
        if problem:
            raise ValueError(problem)
        if self.cfg.get('adopted_supervisor_hook_delivery') is not True:
            raise ValueError('no worker of this run can become a sub-fleet supervisor (the run predates '
                             'adopted_supervisor_hook_delivery); start a new run for jobs')
        if not any(agent_cli(self.runtime_cfg(w), w) == 'claude' for w in self.active_workers()):
            raise ValueError('no worker of this fleet runs the official Claude Code CLI: only such a worker can '
                             'become the supervisor of a job sub-fleet')
        fields = self.job_spawn_fields(spec)
        rows = None
        if fields.get('roster'):  # the raw rows: job_chain refuses pi_model / pi_thinking rows (§58 model chains)
            import yaml
            rows = (yaml.safe_load(Path(fields['roster']).read_bytes()) or {}).get('workers') or []
        problem = job_chain.submit_problem(spec, rows, refused=self.job_pi_gate)
        if problem:
            raise ValueError(problem)
        settings = self.job_settings(spec, fields)
        if 'artifacts' in spec:
            validate_artifacts(spec['artifacts'])
        if spec.get('tree') is not None and not (isinstance(spec['tree'], str) and os.path.isabs(spec['tree'])
                                                 and os.path.isfile(spec['tree'])):
            raise ValueError('tree must be the absolute path of a tree JSON file (docs/design.md §62)')
        by = decompose.seed_by(spec['by']) if spec.get('by') is not None else None  # §62: who wrote the tree
        budget = brake.validate_budget(spec.get('budget'))
        life = spec.get('lifecycle')
        if life is not None and (not lifecycle.known(life) or lifecycle.base_mode(life) != 'task'):
            raise ValueError('lifecycle must be a task lifecycle (' + ', '.join(
                m for m in lifecycle.MODE_NAMES if lifecycle.base_mode(m) == 'task') + ') or null')
        if life == 'dev_task' and spec.get('tree') is None and decompose.enabled():  # §62: a dev job brings its tree
            raise ValueError(decompose.tree_required('job'))
        jid = JOB_PREFIX + uuid.uuid4().hex[:10]
        goal, now = spec['goal'].strip(), time.time()
        req_spec = {'id': jid, 'payload': {'goal': goal}, 'instruction': 'Job: ' + goal.splitlines()[0][:200],
                    'reply': {'required': ['reply']}}
        self.r.hset(REQ + jid, mapping={'spec': json.dumps(req_spec, ensure_ascii=False), 'status': 'job_queued',
                                        'created': awake.now(), 'attempts': 0, 'worker': '', 'job': 1})
        jobs = self.job_state()
        jobs[jid] = {'goal': goal, 'settings': settings, 'artifacts': spec.get('artifacts'), 'budget': budget,
                     'lifecycle': life, 'state': 'queued', 'slot': '', 'task': '', 'run': '', 'waiting': '',
                     'tries': 0, 'created': now, 'spawn': fields, 'hot_slot': '', 'tree': spec.get('tree'),
                     'by': by}
        self.save_jobs(jobs)
        head = ('[job ' + jid + '] accepted: the program runs it in its own task-mode sub-fleet on a free worker ('
                + str(settings['workers']) + ' sub-fleet workers, ' + str(settings['hours']) + ' h). Goal: '
                + goal.splitlines()[0][:300])
        if self.service:
            self.queue('supervisor', head + '\nThis is not a request for you: do not dispatch, answer or skip ' + jid
                       + ' (a decision that does is invalid). Its result is published under ' + jid + ' when the '
                       'sub-fleet ends. Reply with the usual service decision JSON: status RUNNING, dispatch [], '
                       'skip [].', request_id=jid, kind='followup')
        else:  # §19 "Task root": a job beside this fleet's own task
            self.queue('supervisor', head + '\nThis is not part of your task: do not dispatch to the worker that '
                       'supervises it, use its M<n> or withdraw it (a decision that does is invalid). Its result is '
                       'published under ' + jid + ' when the sub-fleet ends. While a job is open your COMPLETED is '
                       'held: the task closes when the last job ends. Continue the current task using the AGENTS.md '
                       'decision JSON.')
        self.log('job.submitted', job=jid, workers=settings['workers'], hours=settings['hours'],
                 **{k: v for k, v in fields.items() if k in ('name', 'roster', 'roles')})
        return jid

    def job_spawn_args(self, jid):
        """The sub-fleet request fields of job ``jid`` (escalation_apply): its hours, lifecycle, artifacts, budget,
        and ``job`` (its id, name, roster, roles, deliver: subfleet.spawn_child)."""
        job = self.job_state().get(jid) or {}
        out = {'hours': (job.get('settings') or {}).get('hours') or subfleet.limits(self.cfg)['split_budget_h'],
               'lifecycle': job.get('lifecycle'),
               'job': dict(job.get('spawn') or {}, id=jid, submitted=job.get('created'), **(job.get('chain') or {}))}
        if job.get('resume_run'):  # section 58 "Restart": the same sub-fleet, handed to a fresh slot (readopt_child)
            out['job'].update(resume_run=job['resume_run'], suspended_at=job.get('suspended_at'))
        if job.get('artifacts') is not None:
            out['artifacts'] = job['artifacts']
        if job.get('budget'):
            out['budget'] = job['budget']
        return out

    def job_slot_busy(self, slot, jid=''):
        """Why ``slot`` cannot take job ``jid`` now apart from subfleet.check, else ''."""
        if applet_lease.taken(self, slot):
            return 'leased by an applet'
        named = named_subfleet.reserved(self, slot)  # roster subfleets: or attached; also while that sub-fleet is down
        if named:
            kind = 'attached' if named_subfleet.reserved(self, slot, attached_only=True) == named else 'named'
            return 'supervisor slot of ' + kind + ' sub-fleet ' + named
        other = next((j for j, x in self.job_state().items() if x.get('hot_slot') == slot and j != jid), '')
        if other:
            return 'added for job ' + other
        held = next((k for k, v in self.escalation_state().items() if v.get('slot') == slot
                     and v.get('kind', 'split') == 'split' and v.get('status') in ('approved', 'rotating', 'delegated')), '')
        if held:
            return 'held by the split of ' + held
        mine = next((rid for rid in self.r.lrange(REQQ, 0, -1)
                     if self.r.hmget(REQ + rid, 'status', 'worker') == ['dispatched', slot]), '')
        if mine:
            return 'working on request ' + mine
        if self.inbox_depth(slot):
            return 'has queued work'
        return ''

    def job_tick(self):
        """§58, daemon tick, outside transactions: queued jobs (oldest first) take a free slot each."""
        if not self.v2:
            return
        if not self.service and not self.job_task_root_tick():
            return
        if self.paused() or self.stopping:  # section 58 "Restart": nothing starts while the root stops
            return
        jobs = self.job_state()
        for jid in sorted((j for j, x in jobs.items() if x.get('state') in ('queued', 'suspended')),
                          key=lambda j: jobs[j]['created']):
            self.job_start(jid)

    def job_task_root_tick(self):
        """§19 "Task root": a task root's own task ended: the jobs that have no sub-fleet yet fail (a running one
        is stopped and collected with the task, and fails there). Its COMPLETED was held for the jobs: the last one
        ended, so the task completes now (lifecycle_close then closes the run as usual). True: jobs may start."""
        if self.cfg.get('parent_config'):
            return False
        jobs = self.job_state()
        task = self.task()
        if task.get('status') != 'running':
            for jid in [j for j, x in jobs.items() if x.get('state') in ('queued', 'starting')]:
                job = jobs.pop(jid)
                esc = self.escalation_state()
                if job.get('task') and esc.pop(job['task'], None) is not None:
                    self.save_escalations(esc)
                self.job_hot_drop(job)
                self.save_jobs(jobs)
                why = 'the task of this fleet ended (' + str(task.get('status') or 'none') + ') before the job started'
                if self.r.hgetall(REQ + jid):
                    self.job_publish(jid, 'failed', worker='fleet', error=why,
                                     result={'job': jid, 'outcome': 'not_started', 'reason': why})
                self.log('job.failed', job=jid, reason=why)
            return False
        if task.get('jobs_hold') and not jobs and not self.unsettled_workers():  # own work out again: wait for it
            self.log('task.jobs_released', task=task.get('id', ''))
            self.finish('completed', task.get('jobs_hold'))
            return False
        return True

    def job_start(self, jid):
        """Give queued job ``jid`` an idle worker slot that no lease, split or request holds, and approve the
        sub-fleet on it with the §44 machinery (no proposal: the sub-fleet's supervisor plans the pieces). No such
        slot: the job stays queued and the reason shows in status. True when a slot was taken."""
        jobs = self.job_state()
        job = jobs.get(jid)
        if not job or job.get('state') not in ('queued', 'suspended'):
            return False
        if not self.r.hgetall(REQ + jid):  # published meanwhile: nothing left to run
            self.job_hot_drop(jobs.pop(jid, None))
            self.save_jobs(jobs)
            return False
        # section 58 "Restart": never two sub-fleets for one job: one still running (or its daemon alive) is waited for, a
        # suspended one is the only one handed on, anything else fails closed
        kids = subfleet.job_children(self.run_dir, jid)
        busy = [d for d in kids if subfleet.record(d).get('state') in subfleet.ACTIVE or daemon_running(d)]
        if busy:
            why = 'its sub-fleet ' + busy[0].name + ' is still running; waiting for it to end'
            if job.get('waiting') != why:
                self.job_update(jid, waiting=why)
            return False
        resume_run = ''
        if job['state'] == 'suspended':
            sus = [d for d in kids if subfleet.record(d).get('state') == 'suspended']
            problem = ('' if len(sus) == 1 and str(sus[0]) == str(job.get('run')) else
                       'not safe to continue: ' + str(len(sus)) + ' suspended sub-fleets for this job')
            if not problem:
                try:
                    ccfg = load(sus[0] / 'config.json')
                    if not Path(ccfg['redis_runtime_dir']).is_dir():
                        problem = 'its sub-fleet ' + sus[0].name + ' lost its Redis directory'
                except (OSError, ValueError, KeyError):
                    problem = 'its sub-fleet ' + sus[0].name + ' has no readable config.json'
            if problem:
                self.job_fail(jid, 'not_carried', 'cannot resume after the restart: ' + problem, notify=True)
                return False
            resume_run = str(sus[0])
        restarted = bool(job.get('restarted') or job.get('carried_from') or resume_run)
        problem = self.job_carry_problem(job) if restarted else ''  # brief goal 5: only across a root restart
        if problem:
            self.job_fail(jid, 'not_carried', 'cannot ' + ('resume' if resume_run else 'start') + ' after the root '
                          'restarted: ' + problem, notify=True)
            return False
        st = job['settings']
        profiles = {'worker-' + str(i + 1): p for i, p in enumerate(st['worker_profiles'])}
        if resume_run:
            key_ = job['task']
        else:
            taken = []  # a number with a sub-fleet directory (a failed spawn too) is never given to a job again
            key_ = self.next_item_id(taken)
            while subfleet.attempts_used(self.run_dir, key_):
                taken.append({'id': key_})
                key_ = self.next_item_id(taken)
        spawn = dict(job.get('spawn') or {}, id=jid, submitted=job.get('created'))
        if resume_run:
            spawn.update(resume_run=resume_run, suspended_at=job.get('suspended_at'))
        full = self.job_tree_full(job)
        if full:
            if job.get('waiting') != full:
                self.job_update(jid, waiting=full)
                self.log('job.waiting', job=jid, reason=full)
            return False
        # §58 model chains: the sub-fleet workers run the worker chain's pick, written into the child config; no
        # usable entry now (walls, quota) = the job waits with the reason, never a default model
        over, traces, nope = self.job_worker_pick(job, spawn)
        if over is None:
            if job.get('waiting') != nope:
                self.job_update(jid, waiting=nope)
                self.log('job.waiting', job=jid, reason=nope[:500])
            return False
        chain = {}
        if st.get('chains') is not None:
            chain = {'job_chain': st['chains'], 'model_chain': {'supervisor': None, 'workers': traces}}
            if over:
                chain['worker_overrides'] = over
        spawn.update(chain)
        why, slot = [], None
        hot = job.get('hot_slot') or ''
        for w in sorted(self.active_workers(), key=lambda w: w != hot):  # its own added worker first
            reason = (self.job_slot_busy(w, jid)
                      or subfleet.check(self, w, key_, st['workers'], ready=True, profiles=profiles, job=spawn))
            if not reason:  # §58 model chains: the supervisor card, or the supervisor chain's pick for this slot
                sup_fields, sup_trace, reason = self.job_supervisor_pick(w, st)
            if reason:
                why.append(self.label(w) + ': ' + reason)
                continue
            slot = w
            break
        if slot is None:
            waiting = '; '.join(why) or 'no active worker'
            added = self.job_hot_add(jid, job, key_, profiles, spawn)
            if added:
                waiting = added + '; ' + waiting
            if job.get('waiting') != waiting:
                self.job_update(jid, waiting=waiting)
                self.log('job.waiting', job=jid, reason=waiting[:500])
            return False
        if chain:
            chain['model_chain']['supervisor'] = sup_trace
        if hot and slot != hot:  # another worker came free first: the one added for this job goes again
            self.job_hot_drop(job)
            self.job_update(jid, hot_slot='')
        goal = job['goal']
        self.r.hset(REQ + jid, mapping={'status': 'dispatched', 'worker': slot})
        items_ = json.loads(self.r.get(ITEMS) or '{}')
        items_[key_] = {'to': slot, 'request': jid, 'instruction': goal}
        self.r.set(ITEMS, json.dumps(items_, ensure_ascii=False))
        stale = json.loads(self.r.get(SPLIT_STALE) or '{}')
        if stale.pop(key_, None) is not None:  # the number is in use again
            self.r.set(SPLIT_STALE, json.dumps(stale, ensure_ascii=False))
        esc = self.escalation_state()
        esc[key_] = {'slot': slot, 'reason': 'job ' + jid, 'proposal': [], 'workers': st['workers'],
                     'instruction': goal, 'status': 'escalated', 'request': jid, 'job': jid, 'message_id': '',
                     'asked_at': time.time()}
        self.save_escalations(esc)
        self.job_update(jid, state='starting', slot=slot, task=key_, waiting='', chain=chain, resume_run=resume_run)
        answer = dict({'task': key_, 'decision': 'approve', 'note': '', 'workers': st['workers']}, **sup_fields)
        if st['worker_profiles']:
            answer['worker_profiles'] = list(st['worker_profiles'])
        self.escalation_apply({}, None, key_, esc[key_], answer)
        esc = self.escalation_state()
        if esc.get(key_, {}).get('status', 'escalated') == 'escalated':  # refused by the program after all
            rec = esc.pop(key_, None) or {'slot': slot, 'request': jid, 'job': jid}
            self.split_drop(esc, key_, rec, 'the program refused the sub-fleet on ' + self.label(slot))
            self.save_escalations(esc)
            return False
        self.log('job.started', job=jid, slot=slot, task=key_, status=esc[key_]['status'])
        return True

    def job_worker_pick(self, job, spawn):
        """§58 model chains: (overrides, {slot: trace}, reason) of the sub-fleet workers of ``job``. Slots with a card
        (a roster row's profile, worker_profiles) keep it; the others run job_chain.pick_worker over the job's worker
        chain, with this run's walls, the section 51 pi gate and the model quota of those slots. overrides None = no
        usable entry now (``reason``). A job queued before model chains (no ``chains``) runs as before: ({}, {}, '')."""
        st = job['settings']
        chains = st.get('chains')
        if chains is None:
            return {}, {}, ''
        names, cards = subfleet.job_roster(spawn)
        if not names:
            names = ['worker-' + str(i + 1) for i in range(st['workers'])]
            cards = {'worker-' + str(i + 1): p for i, p in enumerate(st.get('worker_profiles') or [])}
        cards = {n: c for n, c in (cards or {}).items() if c}
        registry = AgentRegistry(self.cfg.get('agent_registry') or None)
        traces = {}
        for name, card_id in cards.items():
            card = registry.get(card_id)
            traces[name] = job_chain.trace({'entry': {'cli': card.cli, 'model': card.model}, 'profile': card_id,
                                            'skipped': []}, 'card')
        free = [n for n in names if n not in cards]
        if not free:
            return {}, traces, ''
        if not chains.get('worker'):
            return None, traces, 'no worker chain for ' + ', '.join(free)

        def refused(cli, model):
            over = {'worker_cli': cli, ('pi_model' if cli == 'pi' else 'worker_model'): model}
            return self.job_pi_gate(cli, model) or model_usage.default_problem(
                subfleet.job_worker_cfg(self.cfg, {'worker_overrides': over}), free, registry)
        pick = job_chain.pick_worker(chains['worker'], 'claude', self.walled(), refused=refused,
                                     card_slots=bool(cards))
        if pick['entry'] is None:
            return None, traces, pick['reason']
        source = (st.get('chain_source') or {}).get('worker') or 'discovery'
        traces.update({n: job_chain.trace(pick, source) for n in free})
        return pick['overrides'], traces, ''

    def job_model_card(self, cli, model, registry):
        """The model card of (cli, model) (its max_concurrent caps the model), or None."""
        return next((m for m in registry.models if m.cli == cli and m.model == model), None)

    def job_supervisor_pick(self, slot, st):
        """§58 model chains: (approve fields, trace, reason) for ``slot`` as the supervisor of a job's sub-fleet.
        A supervisor card (spec or job_subfleet): that card, as before. Else job_chain.pick_supervisor over the job's
        supervisor chain, with this run's walls and the quota of each model for this slot (Fleet.card_gate): its
        card, or {supervisor_model} for a model with no card. The slot already runs the pick: no fields, adopted in
        place (D15). ``reason`` non-empty: this slot cannot supervise the job now."""
        registry = AgentRegistry(self.cfg.get('agent_registry') or None)
        card_id = st.get('supervisor_profile')
        if card_id:
            why = self.split_supervisor_launch(slot, card_id)[1] or ''
            try:
                model = registry.get(card_id).model
            except KeyError:
                model = None
            return ({'supervisor_profile': card_id}, job_chain.trace(
                {'entry': {'cli': 'claude', 'model': model}, 'profile': card_id, 'skipped': []}, 'card'), why)
        chain = (st.get('chains') or {}).get('supervisor')
        if not chain:  # a job queued before model chains: the slot keeps its own model, as before
            return {}, None, ''
        return self.supervisor_chain_pick(slot, chain, (st.get('chain_source') or {}).get('supervisor'), registry)

    def supervisor_chain_pick(self, slot, chain=None, source=None, registry=None):
        """(approve fields, trace, reason) for ``slot`` as the supervisor of a sub-fleet whose model nobody named:
        the first usable Claude entry of ``chain`` (a job's supervisor chain) or, with ``chain`` None, of
        discovery.supervisor (job_chain.subfleet_supervisor: a §44 split, user 2026-11-22 D27). Skips walls
        (Fleet.walled) and models at their quota for this slot (Fleet.card_gate). Fields: {} when the slot already runs
        the pick (adopted in place, no rotation), else the pick's card (supervisor_profile) or, without a card,
        {supervisor_model}. ``reason`` non-empty: no usable entry, or the pick's launch is refused (D28)."""
        registry = registry or AgentRegistry(self.cfg.get('agent_registry') or None)

        def refused(cli, model):
            card = self.job_model_card(cli, model, registry)
            gate = self.card_gate(slot, card, registry) if card else None
            return gate[1] if gate else None
        if chain is None:
            pick = job_chain.subfleet_supervisor(self.walled(), registry=registry, refused=refused)
            chain, source = pick['chain'], pick['source']
        else:
            pick = job_chain.pick_supervisor(chain, self.walled(), registry=registry, refused=refused)
        if pick['entry'] is None:
            return {}, None, pick['reason']
        trace = job_chain.trace(pick, source or 'discovery')
        cfg, model = self.runtime_cfg(slot), pick['entry']['model']
        if job_chain.runs_pick(pick, chain, agent_cli(cfg, slot), claude_model(cfg, slot)):
            return {}, trace, ''
        fields = {'supervisor_profile': pick['profile']} if pick['profile'] else {'supervisor_model': model}
        why = self.split_supervisor_launch(slot, pick['profile'], None if pick['profile'] else model)[1]
        return fields, trace, why or ''

    def job_hot_add(self, jid, job, task_id='', profiles=None, spawn=None):
        """§58: no worker can take queued job ``jid``: add one worker row to the root roster (the applet supply
        writer, applet_ops.write_worker_row); the roster reload (§16) starts the slot and the next job_start makes it
        the job's sub-fleet supervisor. One added worker per job; none while a reclaimed worker can be woken
        (lifecycle_hold wakes one per queued job), without a roster file, at ``limits.job_hot_add_max``, or when a
        refusal that does not depend on the slot (limits, cards, the supervisor card gate) would stop a new worker
        too. An added slot that ends dead is dropped. Returns the line for ``waiting`` ('' when nothing was done)."""
        from fleetmux import applet_ops
        hot = job.get('hot_slot') or ''
        if hot:
            if hot in self.workers and self.agent(hot).get('state') == 'dead':  # its spawn failed: do not wait on it
                self.job_hot_drop(job)
                self.job_update(jid, hot_slot='', hot_failed='added worker ' + hot + ' did not start')
                return 'added worker ' + hot + ' did not start'
            if hot in self.workers:
                return 'added worker ' + hot + ' for this job'
            refused = self.r.hget(ROSTER, 'rejected') or ''
            if refused and hot not in self.desired:  # the reload refused the file with the row: take the row out
                self.job_hot_drop(job)
                self.job_update(jid, hot_slot='', hot_failed=refused[:300])
                self.log('job.hot_add_refused', job=jid, slot=hot, reason=refused[:300])
                return 'the roster reload refused added worker ' + hot + ': ' + refused[:300]
            return 'added worker ' + hot + ' for this job; waiting for the roster reload to start it'
        if job.get('hot_failed'):
            return 'adding a worker failed earlier: ' + job['hot_failed']
        if any(self.agent(w).get('state') == 'reclaimed' and not named_subfleet.reserved(self, w)
               for w in self.active_workers()):
            return ''  # lifecycle_hold.service_tick wakes one for it (never a sub-fleet's supervisor slot)
        lim = subfleet.limits(self.cfg, job=True)
        if self.job_tree_full(job, lim, added=1):
            return ''  # a tree-wide cap: another worker would not start the job
        paths = applet_ops.roster_paths(self)
        if not paths:
            return 'no worker added: this run has no roster file'
        st = job.get('settings') or {}
        like = next((w for w in self.active_workers() if agent_cli(self.runtime_cfg(w), w) == 'claude'), None)
        if like and task_id:  # limits and cards only (ready=False): what a new Claude worker would meet as well
            same = (subfleet.check(self, like, task_id, st.get('workers', 1), ready=False, profiles=profiles,
                                   job=spawn)
                    or self.job_supervisor_pick(like, st)[2])
            if same:
                return 'no worker added: ' + str(same)
        cap = lim.get('job_hot_add_max')
        count = len([j for j in self.job_state().values() if j.get('hot_slot')])
        if cap is not None and count >= cap:
            return 'no worker added: job_hot_add_max ' + str(cap) + ' reached (' + str(count) + ' added)'
        name = applet_ops.free_name(self, JOB_HOT_BASE)
        try:
            for path in paths:
                applet_ops.write_worker_row(path, name, None)
        except (OSError, ValueError) as exc:
            for path in paths:
                try:
                    applet_ops.remove_worker_row(path, name)
                except (OSError, ValueError):
                    pass
            self.job_update(jid, hot_failed=str(exc)[:300])
            return 'no worker added: ' + str(exc)[:300]
        self.r.hset(applet_ops.USED, mapping={name: jid})  # never given out again in this run
        self.job_update(jid, hot_slot=name)
        self.log('job.worker_added', job=jid, slot=name, count=count + 1, cap=cap)
        return 'added worker ' + name + ' for this job; waiting for the roster reload to start it'

    def job_tree_full(self, job, lim=None, added=0):
        """§58: the tree-wide cap (roster / config ``limits:``) that job ``job`` would cross now, counting the jobs
        whose sub-fleet is approved but not spawned yet (state ``starting``), else ''. Checked before a slot is
        taken, so capped jobs start in submit order instead of racing in the spawn queue."""
        lim = lim or subfleet.limits(self.cfg, job=True)
        pending = [j for j in self.job_state().values() if j.get('state') == 'starting']
        if lim['max_children'] is not None:
            live = len(subfleet.active_children(self.run_dir))
            if live + len(pending) >= lim['max_children']:
                return ('max_children ' + str(lim['max_children']) + ': ' + str(live) + ' sub-fleets run, '
                        + str(len(pending)) + ' starting')
        if lim['max_agents_total'] is not None:
            total = (subfleet.tree_agents(self.run_dir) + added + (job.get('settings') or {}).get('workers', 1)
                     + sum((j.get('settings') or {}).get('workers', 0) for j in pending))
            if total > lim['max_agents_total']:
                return 'max_agents_total ' + str(lim['max_agents_total']) + ': this job would make ' + str(total)
        return ''

    def job_hot_drop(self, job):
        """Take the worker row added for ``job`` out of the roster again: the roster reload retires the slot once it
        is idle (§16). Called when the job ends, fails or is gone."""
        from fleetmux import applet_ops
        hot = (job or {}).get('hot_slot')
        if not hot:
            return
        for path in applet_ops.roster_paths(self):
            try:
                applet_ops.remove_worker_row(path, hot)
            except (OSError, ValueError) as exc:
                self.log('job.worker_remove_failed', slot=hot, path=str(path), error=str(exc)[:200])
        self.log('job.worker_removed', slot=hot)

    def job_release(self, jid, why, count=True):
        """A job whose sub-fleet did not start goes back to the queue (its split record and M<n> row are already
        gone: split_drop). After JOB_TRIES such returns it fails with the last reason. ``count`` False: a return
        caused by a root stop or resume (section 58 "Restart"), which is no failed start. A suspended job's sub-fleet that
        could not be handed on goes back to ``suspended``."""
        jobs = self.job_state()
        job = jobs.get(jid)
        if job is None:
            return
        if not self.r.hgetall(REQ + jid):
            self.job_hot_drop(jobs.pop(jid))
            self.save_jobs(jobs)
            return
        tries = int(job.get('tries') or 0) + (1 if count else 0)
        final = subfleet.ROSTER_REJECTED in why  # §58 D18: the child would refuse its roster: fail now, no retry
        if tries >= JOB_TRIES or final:
            self.job_hot_drop(jobs.pop(jid))
            self.save_jobs(jobs)
            error = why if final else 'job sub-fleet could not start ' + str(tries) + ' times; last: ' + why
            self.job_publish(jid, 'failed', worker='fleet', error=error,
                             result={'job': jid, 'outcome': 'not_started', 'reason': why})
            self.log('job.failed', job=jid, reason=why)
            return
        self.r.hset(REQ + jid, mapping={'status': 'job_queued', 'worker': ''})
        if job.get('resume_run'):  # its suspended sub-fleet stays: handed on at the next try
            job.update(state='suspended', slot='', run=job['resume_run'], resume_run='', waiting=why, tries=tries)
        else:
            job.update(state='queued', slot='', task='', run='', waiting=why, tries=tries)
        self.save_jobs(jobs)
        self.log('job.requeued', job=jid, tries=tries, reason=why)

    def job_finished(self, jid, task_id, slot, child_run, outcome, reason, replace=False):
        """§58: the sub-fleet of job ``jid`` was collected: its outcome becomes the job's one result (ok /
        blocked / failed, with the child's reason, run and workspace paths and artifacts). Returns the line the
        collect notice to the supervisor carries. ``replace``: a gate recheck publishes the job again."""
        jobs = self.job_state()
        job = jobs.pop(jid, None)
        self.job_hot_drop(job)
        self.save_jobs(jobs)
        child = subfleet.child_result(child_run) or {}
        status = 'ok' if outcome == 'completed' else 'blocked' if outcome == 'blocked' else 'failed'
        fields = {'reply': reason or outcome, 'job': jid, 'subfleet': task_id, 'outcome': outcome, 'reason': reason,
                  'run': str(child_run), 'workspace': str(Path(child_run) / 'workspace'),
                  'artifacts': child.get('artifacts') or []}
        if child.get('delivery'):  # §58: the child's lifecycle gate record (dev_task: sha, queue entry, tests)
            fields['delivery'] = child['delivery']
        if isinstance(child.get('decompose'), dict):  # §62: the submitter gets the review (seed incl.) and the tune
            fields['decompose'] = child['decompose']
        chain = child.get('model_chain') or run_model_chain(child_run) or ((job or {}).get('chain') or {}).get(
            'model_chain')
        if chain:  # §58 (D17): each slot's cli:model, the entries skipped before it and the switches
            fields['model_chain'] = chain
        extra = {} if status == 'ok' else {'error': 'job sub-fleet ' + task_id + ' ended ' + outcome
                                           + (': ' + reason if reason else '')}
        self.job_publish(jid, status, worker=self.label(slot), result=fields, replace=replace, **extra)
        self.log('job.done', job=jid, task=task_id, outcome=outcome, status=status)
        return (' Job ' + jid + ' ended ' + status + '; its result is published under ' + jid
                + '. Do not dispatch, answer or skip it.')

    def job_guard(self, decision):
        """§58: a service decision may not dispatch, answer or skip a job, answer its sub-fleet's split, or
        dispatch to the slot that supervises it (ValueError: an invalid decision). A task root's decision (§19
        "Task root") also may not use a job's M<n> as a dispatch id or withdraw a job's slot."""
        jobs = self.job_state()
        if not jobs:
            return
        for field in ('dispatch', 'answer', 'skip'):
            for item in decision.get(field) or []:
                rid = item.get('request_id')
                if rid in jobs:
                    raise ValueError(field + ' of ' + rid + ': ' + rid + ' is a job, run by the program in its own '
                                     'sub-fleet; do not dispatch, answer or skip it')
        for item in decision.get('dispatch') or []:
            held = self.job_holding(self.resolve(item.get('to') or ''))
            if held:
                raise ValueError('dispatch to ' + item['to'] + ': it supervises the sub-fleet of job ' + held
                                 + '; route to another worker')
        tasks = {job.get('task'): jid for jid, job in jobs.items() if job.get('task')}
        for item in decision.get('dispatch') or []:  # task mode: a dispatch id names a plain task M<n>
            if item.get('id') in tasks:
                raise ValueError('dispatch id ' + item['id'] + ' is the sub-fleet of job ' + tasks[item['id']]
                                 + '; give your own work another id')
        for name in decision.get('withdraw') or []:
            held = self.job_holding(self.resolve(name))
            if held:
                raise ValueError('withdraw ' + name + ': it supervises the sub-fleet of job ' + held + ', which is '
                                 'not your task\'s work; leave it out')
        for item in decision.get('escalations') or []:
            if item.get('task') in tasks:
                raise ValueError('escalations: ' + item['task'] + ' is the sub-fleet of job ' + tasks[item['task']]
                                 + ' and is a job, run by the program; there is nothing to answer')

    def escalation_finished(self, rec, ok):
        """A delegated task came back (subfleet.collect). Returns extra lines for the supervisor."""
        task_id = rec['task_id']
        esc = self.escalation_state()
        if esc.pop(task_id, None) is None:
            return ''  # an operator spawn, not an escalation
        self.save_escalations(esc)
        state = self.contract_state()
        row = state['tasks'].get(task_id) if state is not None else None
        if row is None:
            return ''
        failures = [] if ok else ['sub-fleet failed (attempt ' + str(rec.get('attempt', 1)) + '): '
                                  + str(rec.get('reason') or rec.get('outcome'))]
        entries = [contracts.current(state, i) for i in row.get('provides') or []]
        if ok:
            for i, e in zip(row.get('provides') or [], entries):
                failures += ([i + ' is not registered'] if e is None else contracts.file_failures(e, self.workspace()))
        notes = []
        if not failures:
            for e in entries:
                if e['status'] != 'ready':
                    e['status'] = 'ready'
                    self.contract_notify(state, e['consumers'], '[contract] ready ' + contracts.ref(e)
                                         + ': switch your input from ./stubs/ to ' + e['path'] + ' and re-run.')
                    notes.append('[contract] ready ' + contracts.ref(e))
            row.update(status='done', reason='sub-fleet outputs verified')
            for held in contracts.releasable(state):
                held['status'] = 'running'
                held['attempts'] = held.get('attempts', 0) + 1
                self.contract_queue(state, held['to'], held['message'], held['id'])
                self.contract_log('task.released', task=held['id'])
        else:
            row.update(status='failed', reason='; '.join(failures))
        self.contract_log('task.' + row['status'], task=task_id, by='sub-fleet', reason=row['reason'])
        notes.append(contracts.table_line(state))
        self.save_contract(state)
        return '\n' + '\n'.join(notes)

    # ------------------------------------------------------------ sub-fleet interfaces: notify the top, veto (§20)
    def notify_parent(self, state, changes):
        """A sub-fleet's interfaces take effect at once; the top supervisor is told and may veto."""
        if not changes or not self.cfg.get('parent_config'):
            return
        folder = Path(load(self.cfg['parent_config'])['run']) / 'subfleet-notices'
        folder.mkdir(parents=True, exist_ok=True)
        items = [{'ref': k, 'change': kind, 'path': str(self.workspace() / state['interfaces'][k]['path']),
                  'producer': state['interfaces'][k]['producer']} for k, kind in changes if k in state['interfaces']]
        save(folder / (uuid.uuid4().hex[:12] + '.json'),
             {'origin_run': str(self.run_dir), 'task': self.cfg.get('task_id'), 'interfaces': items})

    def held_notice(self, path):
        """A sub-fleet's held watchdog (docs/design.md §24) could not recover a slot: tell this fleet's supervisor (a
        notice, nothing to rule on) and, at a managed root, the tg / parent channels."""
        try:
            note = load(path)
        except (OSError, ValueError):
            note = None
        path.unlink(missing_ok=True)
        if not note:
            return
        text = ('[sub-fleet held] sub-fleet ' + str(note.get('task') or '') + ' (' + str(note.get('origin_run') or '')
                + '): its ' + str(note.get('label') or note.get('slot') or 'slot') + ' is held: ' + str(note.get('text') or '')
                + '. Its daemon could not recover it by itself. Look at that sub-fleet (status / its panes); take it back '
                'or stop it if it cannot go on.')
        text = heal.tell(self, text, send=False)  # section 67: the user channels get it without a quoted CLI wall line
        self.log('subfleet.held', task=note.get('task'), slot=note.get('slot'), reason=note.get('reason'))
        if self.running():  # §67: a service parent is told too, with the heal line to copy
            heal.held_line(self, note)
        if lifecycle_close.managed(self):
            from fleetmux import lifecycle
            try:
                policy = lifecycle.get_mode(self.cfg['lifecycle'])
            except ValueError:
                policy = {}
            try:
                lifecycle_close.notify(self, 'slot_held', '[fleet ' + lifecycle_close.fleet_name(self) + '] ' + text, policy)
            except Exception as exc:  # a notice never takes the daemon down
                self.log('subfleet.held_notify_failed', level='error', error=str(exc)[:300])
        elif not self.cfg.get('parent_config'):  # §24: a root without a lifecycle: its channels, else counted
            try:
                notify_route.unmanaged_send(self, 'slot_held', text)
            except OSError as exc:
                self.log('notify.count_failed', level='error', error=str(exc)[:300])

    def relay_notices(self):
        """Parent tick: pass a sub-fleet notice up the tree; the top fleet tells its supervisor."""
        folder = self.run_dir / 'subfleet-notices'
        for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
            if path.name.startswith('held-'):
                self.held_notice(path)  # §24 held watchdog: the nearest parent's supervisor, not the top only
                continue
            if path.name.startswith('heal-'):
                heal.child_note(self, path)  # §67: held / dead / walled / switch failed, with the line to copy
                continue
            if path.name.startswith(baseline_check.NOTICE):  # switch baseline: nearest parent, then up to the user
                baseline_check.relay(self, path)
                continue
            if self.cfg.get('parent_config'):
                up = Path(load(self.cfg['parent_config'])['run']) / 'subfleet-notices'
                up.mkdir(parents=True, exist_ok=True)
                os.replace(path, up / path.name)
                continue
            try:
                note = load(path)
            except (OSError, ValueError):
                note = None
            path.unlink(missing_ok=True)
            if note and note.get('kind') == 'tree':  # §62: a sub-fleet's opening summary, for the user
                self.log('tree.summary_relayed', task=note.get('task'), status=self.tree_notify(
                    '[sub-fleet ' + str(note.get('task')) + '] ' + str(note.get('text') or '')))
                continue
            if not note or not self.running():
                continue
            if note.get('kind') == 'report':
                self.report_intake(note)
                continue
            state = self.contract_state()
            if state is not None:
                state.setdefault('child_interfaces', {}).update({i['ref']: note['origin_run'] for i in note['interfaces']})
                self.save_contract(state)
            self.log('subfleet.interfaces', task=note.get('task'), interfaces=[i['ref'] for i in note['interfaces']])
            self.queue('supervisor', '[sub-fleet interfaces] sub-fleet ' + str(note.get('task')) + ' registered: '
                       + '; '.join(i['ref'] + ' (' + i['change'] + ', ' + i['path'] + ')' for i in note['interfaces'])
                       + '\nThey are in effect already. To withdraw one, add to your next JSON "interfaces": [{"id": '
                       '"<id>", "version": <n>, "decision": "veto", "note": "<why>"}]. Reply with the JSON format '
                       'from AGENTS.md.')

    def apply_vetoes(self):
        """Sub-fleet tick: the top supervisor vetoed one of our interfaces: withdraw it (deprecated)."""
        folder = self.run_dir / 'subfleet-vetoes'
        for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
            try:
                veto = load(path)
            except (OSError, ValueError):
                veto = None
            path.unlink(missing_ok=True)
            if veto and veto.get('kind') == 'report':
                self.report_ruled(veto)
                continue
            if veto and veto.get('kind') == blocked_scope.PENDING_KIND:
                self.report_pending(veto)
                continue
            state = self.contract_state() if veto and self.running() else None
            e = state['interfaces'].get(veto['ref']) if state else None
            if not e or e['status'] == 'deprecated':
                continue
            e.update(status='deprecated', deprecated_by='veto by the top supervisor')
            self.contract_log('vetoed', by='top supervisor', iface=veto['ref'], note=veto.get('note', ''))
            self.contract_notify(state, [e['producer']] + e['consumers'], '[contract] ' + veto['ref']
                                 + ' was vetoed by the top supervisor: ' + (veto.get('note') or '-') + '. Stop using it.')
            self.queue('supervisor', '[contract] the top supervisor vetoed ' + veto['ref'] + ': ' + (veto.get('note') or '-')
                       + '\nIt is withdrawn (deprecated). Re-plan the tasks that provide or consume it (declare a '
                       'replacement under a new id). Reply with the JSON format from AGENTS.md.')
            self.save_contract(state)

    # ------------------------------------------------------------ sub-fleet boundaries: report up, top ruling (§46)
    def report_up_guard(self, decision):
        if decision.get('report_up') and not self.cfg.get('parent_config'):
            raise ValueError('report_up is only for a sub-fleet supervisor; you are the top supervisor: rule on it '
                             'yourself, or reply BLOCKED for what only the user can decide')

    def report_up(self, items):
        """Sub-fleet supervisor: what it may not grant itself goes to the top supervisor (one notice per item)."""
        if not items:
            return
        folder = Path(load(self.cfg['parent_config'])['run']) / 'subfleet-notices'
        folder.mkdir(parents=True, exist_ok=True)
        pending = json.loads(self.r.get(REPORTS_UP) or '{}')
        for item in items:
            key_ = REPORT_KEY + str(self.cfg.get('task_id')) + ':' + uuid.uuid4().hex[:6]
            note = {'kind': 'report', 'id': key_, 'origin_run': str(self.run_dir), 'task': self.cfg.get('task_id'),
                    'supervisor': (self.cfg.get('adopt') or {}).get('label') or SUPERVISOR_LABEL,
                    'what': item['what'].strip(), 'why': item['why'].strip(), 'who': item.get('who', '').strip()}
            if item.get('paths'):
                note['paths'] = item['paths']
            save(folder / (uuid.uuid4().hex[:12] + '.json'), note)
            # what was asked, so a ruling can only grant inside it (report_ruled)
            pending[key_] = {'what': note['what'], 'who': note['who'], 'paths': item.get('paths') or []}
            self.log('report.up', report=key_, who=item.get('who', ''), what=item['what'][:300], paths=item.get('paths') or [])
        self.r.set(REPORTS_UP, json.dumps(pending, ensure_ascii=False))
        self.report_clock(pending)

    def report_clock(self, pending):
        """§46: the task clock stops while a report_up waits for the top ruling and runs again when the last
        ruling arrives (the deadline moves by the time waited; ``report_wait_s`` sums it for status)."""
        t = self.task()
        since, now = t.get('report_wait_at'), awake.now()  # awake time, like the deadline it moves
        if pending and not since and t.get('status') == 'running' and t.get('deadline'):
            self.r.hset(TASK, mapping={'report_wait_at': now})
            self.log('task.clock_paused', reason='report_up awaiting the top ruling', reports=len(pending))
        elif not pending and since:
            waited = max(0.0, now - float(since))
            self.r.hset(TASK, mapping={'report_wait_at': '', 'deadline': float(t.get('deadline') or 0) + waited,
                                       'report_wait_s': round(float(t.get('report_wait_s') or 0) + waited, 3)})
            self.log('task.clock_resumed', waited_s=round(waited, 1))

    def report_intake(self, note):
        """Top fleet: a sub-fleet report becomes an open escalation its supervisor must rule on (never auto-approved)."""
        key_ = note['id']
        paths = note.get('paths') or []
        if blocked_scope.resend_held(self, note):  # §24: the same request already waits for the user
            return
        mid = self.queue('supervisor', self.report_notice(key_, note))
        esc = self.escalation_state()
        esc[key_] = {'kind': 'report', 'origin_run': note['origin_run'], 'task': note.get('task'),
                     'supervisor': note.get('supervisor'), 'what': note['what'], 'why': note['why'],
                     'who': note.get('who', ''), 'paths': paths, 'status': 'escalated', 'asked_at': time.time(),
                     'message_id': mid or '', 'fingerprint': blocked_scope.fingerprint(note)}
        self.save_escalations(esc)
        self.log('report.asked', report=key_, task=note.get('task'), what=note['what'][:300])

    def report_reask(self, key_, rec):
        """escalation_tick: an open report whose notice was cancelled while still queued (start --resume
        reconciles the queues) never reached the supervisor: ask again. ``rec`` is changed in place."""
        mid = rec.get('message_id') or ''
        if (rec.get('status') != 'escalated' or not self.v2 or not mid
                or not self.v2.bus.r.hget(self.v2.bus.msg_key(mid), 'cancel_reason')):
            return False
        rec['message_id'] = self.queue('supervisor', self.report_notice(key_, rec)) or ''
        self.log('report.reasked', report=key_, cancelled=mid)
        return True

    @staticmethod
    def report_notice(key_, note):
        """The [sub-fleet report] text for report ``key_`` (a relayed note, or its open escalation record)."""
        paths = note.get('paths') or []
        return ('[sub-fleet report] sub-fleet ' + str(note.get('task')) + ' (' + str(note.get('supervisor')) + '): '
                + note['what'] + ' — ' + note['why'] + (' (worker: ' + note['who'] + ')' if note.get('who') else '')
                + ('\nPaths ' + note['who'] + ' asks to write: ' + ', '.join(paths) + '. An approve makes the program '
                   'grant exactly these to that worker until the sub-fleet ends; to grant less, add "paths": [...] with '
                   'some of them or paths inside them (never more).' if paths else '')
                + '\nA sub-fleet supervisor may not grant itself anything that leaves its workspace; it holds that '
                'piece until you rule. Answer in your next JSON: "escalations": [{"task": "' + key_ + '", "decision": '
                '"approve" | "reject", "note": "..."}]. approve only what the user\'s task clearly allows (say in note '
                'how far it goes); a delete / move, a push, credentials or keys, or anything only the user may decide: '
                'reject, and reply BLOCKED for the user if the task cannot go on without it. Reply with the JSON format '
                'from AGENTS.md.')

    def report_ruling(self, task_id, rec, answer):
        """Top fleet: the ruling goes back down to the origin sub-fleet (its next tick, apply_vetoes)."""
        esc = self.escalation_state()
        esc.pop(task_id, None)
        self.save_escalations(esc)
        folder = Path(rec['origin_run']) / 'subfleet-vetoes'
        folder.mkdir(parents=True, exist_ok=True)
        by = answer.get('by') or 'supervisor'  # §24: 'user' for `fleetmux answer`
        for ref in [task_id] + list(rec.get('aliases') or []):  # a re-sent copy waits in the sub-fleet too
            ruling = {'kind': 'report', 'ref': ref, 'decision': answer['decision'],
                      'note': answer.get('note', '').strip(), 'what': rec['what']}
            if by != 'supervisor':  # the ledger keeps who ruled; a supervisor ruling file stays as it was
                ruling['by'] = by
            if answer['decision'] == 'approve' and rec.get('paths'):  # narrowed by the answer, or all that was asked
                ruling.update(who=rec['who'], paths=answer.get('paths') or rec['paths'])
            save(folder / (uuid.uuid4().hex[:12] + '.json'), ruling)
        blocked_scope.closed(self, task_id, rec, answer['decision'], by)
        self.log('report.ruled', report=task_id, decision=answer['decision'], by=by, note=answer.get('note', '')[:300])

    def report_ruled(self, ruling):
        """Origin sub-fleet: the top supervisor's ruling reaches this fleet's supervisor."""
        pending = json.loads(self.r.get(REPORTS_UP) or '{}')
        asked = pending.pop(ruling.get('ref'), None)
        self.r.set(REPORTS_UP, json.dumps(pending, ensure_ascii=False))
        self.report_clock(pending)  # before the [top ruling] delivery takes the task deadline
        if not self.running():
            return
        self.log('report.ruling', report=ruling.get('ref'), decision=ruling.get('decision'))
        granted = ''
        if ruling.get('decision') == 'approve' and isinstance(asked, dict) and asked.get('paths'):
            # Only inside what this fleet reported, to the worker it named: a ruling never widens it.
            paths = [os.path.normpath(x) for x in ruling.get('paths') or asked['paths'] if isinstance(x, str) and os.path.isabs(x)]
            granted = self.grant(asked.get('who') or '', str(ruling['ref']), [x for x in paths if path_within(x, asked['paths'])])
        self.queue('supervisor', '[top ruling] ' + str(ruling.get('ref')) + ' ' + str(ruling.get('decision'))
                   + (' (by the user)' if ruling.get('by') == 'user' else '') + ': ' + (ruling.get('note') or '-') + '\n(on what you reported up: ' + str(ruling.get('what')) + ')'
                   + (granted or '\napprove: go exactly as far as the note says, no further, and tell the worker '
                      'concerned by dispatch. The program granted nothing: a worker still voids an exception from you '
                      'to write outside this sub-fleet\'s workspace (that takes a report_up with paths).')
                   + ' reject: do not do it; drop that piece or do it another way inside this sub-fleet\'s '
                   'workspace. Reply with the JSON format from AGENTS.md.')

    def report_pending(self, note):
        """Origin sub-fleet (§24): the top supervisor handed what it reported up to the user. One line for its
        supervisor; the report stays pending (fx:reports_up) and the task clock stays stopped until the ruling."""
        if not self.running():
            return
        self.log('report.pending', report=note.get('ref'))
        self.queue('supervisor', '[top ruling] ' + str(note.get('ref')) + ' pending: the top supervisor handed what you '
                   'reported up to the user (' + (str(note.get('note') or '-')[:500]) + '). Keep holding that piece and '
                   'go on with the rest; do not report it up again. The ruling comes as a [top ruling] line. Reply with '
                   'the JSON format from AGENTS.md.')

    def grants(self, slot):
        """§46: the grants the program issued to this sub-fleet worker from top rulings ([{report, paths}])."""
        return json.loads(self.agent(slot).get('grants') or '[]') if self.cfg.get('parent_config') else []

    def grant(self, who, ref, paths):
        """§46: an approved report with paths becomes a grant to the worker it named, issued by the program in the
        same place as the roster's ``writable:`` roots (its AGENTS.md / CLAUDE.md, now and for its next generations)
        and pointed to by a program line at the head of its next dispatch (no turn of its own). Returns the line for
        the [top ruling] message."""
        slot = self.resolve(who)
        if slot not in self.workers or not paths:
            self.log('grant.refused', report=ref, who=who, reason='unknown worker' if paths else 'no path inside the report')
            return ('\napprove: no grant was issued (' + (who or 'no worker') + ' is not a worker of this sub-fleet, or no '
                    'approved path is inside what you reported); a worker still voids an exception from you.')
        self.set_grants(slot, self.grants(slot) + [{'report': ref, 'paths': paths}])
        shown = ', '.join('`' + x + '`' for x in paths)
        note = ('[Fleet grant ' + ref + '] The program issued you a grant from the top fleet supervisor\'s ruling ' + ref
                + ': you may also write inside ' + shown + ' until this sub-fleet ends. It is in your AGENTS.md / '
                'CLAUDE.md now: re-read that file (the line "Grant issued by the program ..."); only that line counts.')
        info = self.agent(slot)
        self.setf(slot, grant_note=(info['grant_note'] + '\n' if info.get('grant_note') else '') + note)
        self.log('grant.issued', agent=slot, report=ref, paths=paths)
        return ('\napprove: the program granted ' + self.label(slot) + ' write access to ' + shown + ' until this '
                'sub-fleet ends (in its AGENTS.md / CLAUDE.md; your next dispatch to it carries a [Fleet grant] line). '
                'Dispatch that piece to it, as far as the note says, no further.')

    def set_grants(self, slot, grants):
        """Record a worker's program grants and rewrite them in its running scope files, in place: only the grant
        lines behind the own-directory rule change, whatever layout (kit, domains) is around the scope."""
        own = OWN_DIR_RULE.replace('{{WORKSPACE}}', str(self.workspace()))
        before, after = own + program_grants_notice(self.grants(slot)), own + program_grants_notice(grants)
        for name in ('AGENTS.md', 'CLAUDE.md'):
            path = agent_cwd(self.cfg, slot) / name
            try:
                text = path.read_text()
            except OSError:
                continue
            if before in text:
                path.write_text(text.replace(before, after, 1))
        self.setf(slot, grants=json.dumps(grants, ensure_ascii=False) if grants else '')

    def clear_grants(self):
        """The sub-fleet ends: every program grant ends with it (status, scope files, the pending pointer)."""
        for slot in self.workers:
            if self.grants(slot) or self.agent(slot).get('grant_note'):
                self.set_grants(slot, [])
                self.setf(slot, grant_note='')
                self.log('grant.cleared', agent=slot)

    def take_grant_note(self, slot):
        """The program's [Fleet grant] line(s) for the head of this worker's next dispatch, once."""
        note = self.agent(slot).get('grant_note') or ''
        if note:
            self.setf(slot, grant_note='')
        return note + '\n' if note else ''

    def pi_metadata(self, name, ev):
        fields = {k: ev[k] for k in ('thread', 'transcript', 'ctx_used', 'ctx_window')
                  if ev.get(k) is not None}
        if fields:
            self.setf(name, **fields)

    def on_turn(self, ev):
        if self.v2:
            raise ValueError('v2 accepts bound structured settlement events only')
        name, info = ev.get('agent'), self.agent(ev.get('agent', ''))
        if not info or ev.get('generation') != int(info.get('generation', 0)):
            self.log('turn.stale', agent=name, generation=ev.get('generation'))
            return
        if info.get('state') == 'dead' or (is_pi(self.runtime_cfg(name), name) and info.get('state') not in ('starting', 'busy', 'handoff')):
            self.log('turn.duplicate', agent=name, msg_id=ev.get('msg_id'))
            return
        if info.get('state') == 'starting':
            if info.get('launch_ready', '1') != '1':
                self.log('turn.launch_held', agent=name, generation=ev.get('generation'))
                return
            if ev.get('failed') or ev.get('aborted') or ev.get('text', '').strip() != 'READY':
                record = self.startup_wall(name, ev.get('text', ''))
                if record:
                    self.brake_notify(record)
                    return
                if self.startup_unusable(name, ev.get('text', '') + '\n' + str(ev.get('error') or '')):
                    return
                self.block_profile(name, 'startup did not return READY; check its terminal', dead=True)
                return
        if (is_claude(self.runtime_cfg(name), name) or is_pi(self.runtime_cfg(name), name)) and info.get('state') in ('busy', 'handoff', 'idle'):
            # A Claude/Pi turn counts only if it answers the message delivered last, once. (The spawn /
            # takeover prompt is a CLI argument without a msg id: that turn is accepted in 'starting'.)
            if info.get('state') == 'idle' or ev.get('msg_id') != info.get('msg_id') or info.get('msg_done') == info.get('msg_id'):
                self.log('turn.duplicate', agent=name, msg_id=ev.get('msg_id'), current=info.get('msg_id'))
                if name != 'supervisor' and is_claude(self.runtime_cfg(name), name) and ev.get('text', '').strip():
                    peer.unsolicited(self, name, ev['text'][-8000:])  # e.g. woken by a peer SendMessage
                return
            self.setf(name, msg_done=ev.get('msg_id'))
        if codex_queue.is_codex(self.runtime_cfg(name), name) and info.get('state') in ('busy', 'handoff', 'starting'):
            # A queued delivery settles only on the turn the rollout binds to it (a manual or repeated turn does not).
            rec = codex_queue.record(self.agent(name))
            if rec['cq_delivery'] and rec['cq_state'] not in ('', 'abandoned'):
                ok, why = codex_queue.bound(rec, ev.get('thread') or '', ev.get('turn') or '', self.sessions,
                                           ev.get('generation'))
                if not ok or rec['cq_turn'] == (ev.get('turn') or ''):
                    self.log('turn.unbound', agent=name, delivery=rec['cq_delivery'], turn=ev.get('turn'),
                             reason=why or 'turn already settled')
                    return
                if not codex_queue.mark_bound(self.r, name, ev['turn'], rec['cq_token']):
                    self.log('turn.unbound', agent=name, delivery=rec['cq_delivery'], turn=ev.get('turn'),
                             reason='delivery abandoned or replaced while binding')
                    return
        if is_pi(self.runtime_cfg(name), name):
            self.pi_metadata(name, ev)
            failed = ev.get('failed') or ev.get('aborted')
            if failed and info.get('state') == 'handoff':
                self.finish_rotation(name, timed_out=True)
                return
            if failed:
                ev = dict(ev, text='')  # never publish a partial/error response as a successful result
        if ev.get('thread'):
            self.setf(name, thread=ev['thread'])
        if ev.get('transcript'):
            self.setf(name, transcript=ev['transcript'])
        if info.get('state') == 'handoff':
            self.finish_rotation(name)
            return
        was = info.get('state')
        self.setf(name, state='idle', stalls=0, idle_confirmed=1)
        if was == 'starting':  # READY after spawn / takeover
            self.setf(name, runtime_ready=1, current_profile=info.get('runtime_profile', ''))
            if info.get('runtime_profile'):
                self.setf(name, profile_state='ready', profile_reason='runtime returned READY; queued assignment awaits revalidation')
            self.log('ready', agent=name, generation=info.get('generation'))
            return
        text = ev.get('text', '')
        if self.running() and len(text.strip()) <= brake.TURN_WALL_MAX:
            record = self.on_wall(name, text, 'turn')
            if record:  # a wall message is not a report: keep the message for after resume (once per message)
                if info.get('last_msg') and info.get('wall_requeued') != (ev.get('msg_id') or info.get('msg_id') or '-'):
                    self.r.lpush(key('inbox', name), info['last_msg'])
                    self.setf(name, wall_requeued=ev.get('msg_id') or info.get('msg_id') or '-')
                self.brake_notify(record)
                return
        if not text.strip() and info.get('last_msg'):
            # Empty turn: the model backend dropped the request (seen with a local server
            # under memory pressure). Re-deliver the same message after a backoff instead of
            # spending a supervisor turn on it.
            tries = int(info.get('empty_tries', 0)) + 1
            self.log('turn.empty', agent=name, tries=tries)
            if tries <= 3:
                self.r.lpush(key('inbox', name), info['last_msg'])
                self.setf(name, empty_tries=tries, retry_at=awake.deadline(30 * tries))
                return
        self.setf(name, empty_tries=0)
        self.r.rpush(key('replies', name), text[-4000:])
        self.r.ltrim(key('replies', name), -5, -1)
        if not self.running():
            self.log('turn.outside_task', agent=name)
            return
        if self.service:
            if name == 'supervisor':
                self.on_service_supervisor(text, about=info.get('last_msg'))
            else:
                self.on_service_worker(name, info, text)
            return
        if name == 'supervisor':
            self.on_supervisor(text, about=info.get('last_msg'))
        else:
            prev = info.get('prev_label')
            self.report(name, text[-8000:], prev)
            if prev:
                self.setf(name, prev_label='')

    def report(self, name, text, prev=None):
        tag = '[' + self.label(name) + ' report' + (' (took over from ' + prev + ')' if prev else '') + ']'
        item = self.report_item(name)  # before contract_report consumes the owner of this report
        self.tree_reported(name, item, text)  # §62: no-op while the switch is off
        extra = self.contract_report(name, text)
        if extra is None:  # the contract gate ended the task
            return
        fields = contracts.report_fields(text) if self.contract_state() is None else {}
        if fields.get('escalate') is not None:  # plain task: no task table, same validation
            _, _, note, reply = self.escalate_intake(name, fields, None)
            if reply:
                self.queue(name, reply)
            extra += note
        blocked = report_blocked(text) if not self.service and name in self.workers else ''
        key_, note = self.blocked_intake(name, item, blocked) if blocked else (None, '')
        mid = self.queue('supervisor', tag + '\n' + text + extra + note
                         + '\n\nWorker status now (state, queue, context, last task):\n' + self.worker_board()
                         + self.task_waiting_line()
                         + '\n\nReply with the JSON format from AGENTS.md.')
        if key_ and mid:  # the supervisor's reply to this very message must answer it (on_supervisor)
            esc = self.escalation_state()
            if key_ in esc:
                esc[key_]['message_id'] = mid
                self.save_escalations(esc)

    def others_active(self):
        # Contract tasks: P0 / P1 replies legitimately dispatch nothing (spec §3.0), so the
        # "no worker is busy: RUNNING must dispatch" rule does not apply in those phases.
        if self.contract_phase() in ('interfaces', 'skeleton'):
            return True
        def busy(info):
            # A Claude worker held for its first advisor sheet with nothing queued is not working (P1-01).
            if info.get('advice_hold') == '1' and info.get('state') == 'starting':
                return False
            return info.get('state') != 'idle'
        # A delivery still queued for the supervisor itself (typically another worker's report) guarantees
        # another supervisor turn, so "RUNNING, waiting" cannot stall the task. So does a ruling still due
        # on a report_up (§46): the [top ruling] line comes as a delivery.
        if json.loads(self.r.get(REPORTS_UP) or '{}'):
            return True
        if self.job_state():  # §19 "Task root": an open job ends with a notice to the supervisor
            return True
        return bool(self.inbox_depth('supervisor')) or any(
            busy(self.agent(w)) or self.inbox_depth(w) for w in self.workers)

    def unsettled_reason(self, w):
        """Why worker ``w`` still has dispatched work out, or '' when everything it got has settled."""
        state = self.agent(w).get('state')
        if state in ('busy', 'held', 'delegated'):
            return {'busy': 'working on a delivery', 'held': 'delivery held, not settled',
                    'delegated': 'sub-fleet running'}[state]
        return 'delivery queued' if self.inbox_depth(w) else ''

    def unsettled_workers(self):
        return [w for w in self.workers if self.unsettled_reason(w)]

    def on_supervisor(self, text, invalid=None, about=None, about_id=None):
        """`invalid`: the v2 validator's own rejection; the correction must name it, not a parse error.
        `about`: the text of the message this reply answers (§24: a root's BLOCKED on a sub-fleet notice)."""
        t = self.task()
        try:
            if invalid:
                raise ValueError(invalid)
            decision = parse_decision(text, self.dispatch_targets(), self.others_active(), self.tree_context())
            self.report_up_guard(decision)
            self.job_guard(decision)  # §19 "Task root": a job is the program's, as in a service fleet (§58)
            state = self.contract_state()
            if state is not None and self.decompose_on():
                self.tree_contract_fill(decision)  # §62: the task table's parts come from the tree
            if state is not None:
                state, rows, held, changes, skeleton = self.contract_decision(state, decision)
                rows = {r['id']: r for r in rows + held}
            command_guard(t, decision)  # after the shape checks: a malformed cmd_exit keeps its own message
            # Validate the whole dispatch before queuing any part of it. Rejections
            # use the existing supervisor correction/three-strike blocked path.
            prepared = []
            for item in decision['dispatch'] if decision['status'] == 'RUNNING' else []:
                slot = self.resolve(item['to'])
                if item.get('fresh') and self.fresh_refusal(slot):
                    raise ValueError(self.fresh_refusal(slot))
                instruction = item['instruction'].strip()
                prepared.append((slot, instruction, self.prepare_worker_profile(slot, instruction)))
            escalations = self.escalation_decision(decision, state) if decision['status'] == 'RUNNING' else []
            consults = self.consult_decision(decision, state) if decision['status'] == 'RUNNING' else []
            self.escalations_missed(decision)
        except (ValueError, TypeError, KeyError) as exc:
            # P0 gap lists never count toward the three-strike limit (user 10/13): the supervisor
            # keeps fixing interfaces until all checks pass; the task `hours` budget is the bound.
            # Same for the first reply that leaves a [dialog] / [blocked] question open (plan F' 8.3 / 8.4).
            p0 = str(exc).startswith('P0') or isinstance(exc, NoStrike)
            bad = int(self.r.hget(TASK, 'bad_replies') or 0) if p0 else self.r.hincrby(TASK, 'bad_replies', 1)
            if not p0 and bad >= 3:
                self.finish('blocked', 'supervisor gave no valid decision JSON three times in a row: ' + str(exc))
            else:
                # Ahead of reports already queued: delivered behind them it arrives stale, about a reply the
                # supervisor has since replaced (2026-10-28 fleetmux run 2, the third strike).
                hint = (' A RUNNING reply with no dispatch may instead declare "waiting": {"for": "...", '
                        '"check": "...", "for_hours": 2} while the next step depends on external evidence.'
                        if str(exc).startswith('no worker is busy') else '')
                self.queue('supervisor', 'Your reply is not a valid decision JSON (' + str(exc) + ').' + hint
                           + ' Resend one JSON object in the AGENTS.md format, nothing else.', front=True)
            return
        artifacts = json.loads(t.get('artifacts') or '[]')
        added = [a for a in decision.get('artifacts', []) if a not in artifacts]
        if t.get('artifacts_frozen') == '1':
            if added:  # layer 1 of the contract belongs to the caller: never widened or narrowed here
                self.log('contract.append_ignored', task=t.get('id', ''), dropped=added)
        else:
            artifacts += added
        self.brake_count('rounds')
        self.r.hset(TASK, mapping={'bad_replies': 0, 'completion': decision['completion'],
                                   'reason': decision.get('reason', ''),
                                   'artifacts': json.dumps(artifacts, ensure_ascii=False)})
        if decision['status'] == 'RUNNING' and decision['dispatch'] and t.get('jobs_hold'):
            self.r.hset(TASK, mapping={'jobs_hold': ''})  # §19 "Task root": new own work, the held COMPLETED lapses
        self.apply_waiting(decision)  # explicit dependency wait (M15): persisted here, cleared by any later reply
        self.advisor_decision(decision)  # before the dispatches below: `advisor: "on"` advises them already
        tree_new = self.tree_decision(decision)  # §62: the tree this reply handed in (None: unchanged / switch off)
        if decision['status'] == 'BLOCKED':
            # §24: a root's BLOCKED on a notice about one of its own sub-fleets / jobs stops only that one
            if not blocked_scope.absorb(self, about, decision.get('reason', ''), about_id=about_id):
                self.finish('blocked', decision.get('reason', 'needs a human'))
            return
        if decision['status'] == 'COMPLETED':
            withdrawn = {self.resolve(w) for w in decision['withdraw']}
            # §19 "Task root": a slot that supervises a job's sub-fleet is the job's, not this task's work (the
            # hold below waits for it); every other unsettled slot bounces as before.
            pending = [w for w in self.unsettled_workers() if w not in withdrawn and not self.job_holding(w)]
            if pending:
                # Never end a task over work still out: every dispatched delivery settles first, or the
                # supervisor withdraws it explicitly (10/13: COMPLETED while workers still ran).
                names = ', '.join(self.label(w) + ' (' + self.unsettled_reason(w) + ')' for w in pending)
                self.log('complete.refused', workers=pending)
                bounces = self.r.hincrby(TASK, 'bounces', 1)
                if bounces >= 3:
                    self.finish('blocked', 'supervisor claimed completion three times while workers had unsettled '
                                'deliveries: ' + names)
                else:
                    self.queue('supervisor', 'COMPLETED refused: ' + names + ' still have unsettled deliveries. Wait '
                               'for their reports (RUNNING with an empty dispatch), or reply COMPLETED again with '
                               '"withdraw": [' + ', '.join('"' + self.label(w) + '"' for w in pending)
                               + '] to drop that work explicitly.')
                return
            if withdrawn:
                self.log('complete.withdrawn', workers=sorted(withdrawn))
            extended = state is not None or t.get('artifacts_frozen') == '1'
            failures = (check_artifacts(artifacts, self.workspace(), extended, extended, caller_commands(t))
                        if artifacts else ['no artifacts were declared, nothing to verify'])
            failures += self.contract_gate(state) if state is not None else []
            if not failures:  # brief launch-demo §1.2: bookkeeping only, after the judgement
                lifecycle_close.acceptance_verdict(self, 'task_artifacts', t.get('id', ''), True,
                                                   bounce=int(t.get('bounces') or 0))
                self.tree_tune(decision)  # §62: a closing tune applies only once the COMPLETED passed verification
            if not failures and self.job_state():
                # §19 "Task root": own work done, jobs still open: held, not bounced (no strike). The task stays
                # running (its job sub-fleets run on) and completes when the last job ends (job_task_root_tick).
                reason = decision.get('reason', 'artifacts verified')
                first = not t.get('jobs_hold')
                self.r.hset(TASK, mapping={'jobs_hold': reason})
                if first:
                    open_ = sorted(self.job_state())
                    self.log('complete.held_for_jobs', jobs=open_)
                    self.queue('supervisor', 'COMPLETED recorded: your own work passed verification. ' + str(len(open_))
                               + ' job(s) still run in their own sub-fleets (' + ', '.join(open_) + '); this task '
                               'closes by itself when the last one ends. Nothing to dispatch for them: reply RUNNING '
                               'with an empty dispatch to any job notice, or dispatch new work if you have some.')
                return
            if not failures:
                self.finish('completed', decision.get('reason', 'artifacts verified'))
                return
            bounces = self.r.hincrby(TASK, 'bounces', 1)
            lifecycle_close.acceptance_verdict(self, 'task_artifacts', t.get('id', ''), False, '; '.join(failures),
                                               bounce=bounces)
            if bounces >= 3:
                self.finish('blocked', 'supervisor claimed completion three times without passing verification: '
                            + '; '.join(failures))
            else:
                self.queue('supervisor', 'Independent verification failed, the task cannot end: ' + '; '.join(failures)
                           + '\nDispatch corrective instructions to the workers (RUNNING + dispatch).')
            return
        if state is not None:
            self.contract_apply(t, state, changes, skeleton)
        tree = self.tree_state() if self.decompose_on() else None  # §62
        for item, (slot, instruction, application) in zip(decision['dispatch'], prepared):
            if state is None and not item.get('id'):  # §44: every plain dispatch gets an id, so any task can split
                item['id'] = self.next_item_id(decision['dispatch'])
            # Metadata describes the next instruction, not the last appended one.
            # A busy worker records intent only; delivery re-evaluates at idle.
            # §46: a program grant is pointed to by the program's own line, ahead of the supervisor's text
            advisory = self.take_grant_note(slot) + profile_runtime.profile_advisory(slot, application)
            if self.advisor_online():
                # Only supervisor dispatches are advised (never [contract] notices or report corrections).
                # Registered before the depends_on hold: a held row is advised when the daemon releases it.
                self.r.rpush(ADVICE_PENDING + slot, json.dumps({'instruction': instruction, 'note': item.get('advise_note', ''),
                                                                'task': item.get('id', '')}, ensure_ascii=False))
            if state is not None:
                row = rows[item['id']]
                row['message'] = advisory + self.contract_message(t.get('id', ''), row)
                if row['status'] == 'pending':  # held until its depends_on are done
                    self.contract_log('task.held', task=row['id'], waits=row['depends_on'])
                    continue
                row['attempts'] = row.get('attempts', 0) + 1
            if not self.inbox_depth(slot):
                self.record_worker_profile(slot, application)
            self.mark_dispatch(slot, item.get('fresh'), item['instruction'])
            if state is not None:
                self.contract_queue(state, slot, row['message'], row['id'])
                continue
            if tree is not None:  # §62: what the block owns, uses and how it is accepted, from the tree
                instruction += decompose.brief(tree, item['id'])
            if item.get('id'):  # plain task: an id makes the item escalatable (§20)
                items = json.loads(self.r.get(ITEMS) or '{}')
                items[item['id']] = dict({'to': slot, 'instruction': instruction},
                                         **({'leaf': True} if item.get('leaf') else {}))
                self.r.set(ITEMS, json.dumps(items, ensure_ascii=False))
            self.queue(slot, advisory + '[Supervisor instruction ' + t.get('id', '') + '] '
                       + ('[' + item['id'] + '] ' if item.get('id') else '') + instruction
                       + '\nShared output directory: ' + str(self.workspace()) + ' (write only inside your own '
                       'subdirectory ' + slot + '/). Reply in the AGENTS.md format when done.')
        esc = self.escalation_state()
        redispatched = [x['id'] for x in decision['dispatch'] if esc.get(x.get('id'), {}).get('status') == 'escalated']
        # A new instruction to (or a consult on) a worker that reported Blocked answers that too (plan F' 8.4).
        redispatched += [BLOCKED_KEY + slot for slot in [s for s, _, _ in prepared]
                         + [self.resolve(c['worker']) for c in decision['consult']]
                         if esc.get(BLOCKED_KEY + slot, {}).get('status') == 'escalated']
        if redispatched:  # the supervisor answered the split by dispatching the task again
            self.save_escalations({k: v for k, v in esc.items() if k not in redispatched})
        # Split answers first: an approved split supersedes a [blocked] ruling on its task in the same reply.
        for task_id, rec, answer in sorted(escalations, key=lambda e: e[1].get('kind', 'split') != 'split'):
            self.tree_return(task_id, rec, answer)  # §62: a cause-tagged answer is a return of its block
            self.escalation_apply(t, state, task_id, rec, answer)
        self.report_up(decision['report_up'])
        if decision.get('reconcile'):  # section 60
            restart_state.decide(self, decision['reconcile'])
        if decision.get('heal'):  # §67: run by heal.tick, outside this transaction
            heal.accept(self, decision['heal'])
        self.consult_apply(consults)
        if state is not None:
            self.save_contract(state)
        if tree is not None and decision['dispatch']:
            self.tree_dispatched(decision['dispatch'])
        self.log('dispatch', count=len(decision['dispatch']), completion=decision['completion'],
                 **self.tree_audit(decision))
        if tree_new is not None:  # §62: what can go now; a reply that dispatched nothing gets it as its next turn
            note = decompose.accepted_message(self.tree_state())
            if self.others_active():
                self.r.rpush(decompose.TREE_NOTES, note)
            else:
                self.queue('supervisor', note + '\nReply with the JSON format from AGENTS.md.')

    # ---- declared dependency waiting (M15): metadata + one gate + one bounded reminder, no fourth status ----
    def apply_waiting(self, decision):
        """Persist `decision['waiting']` or clear it. A wait never changes the task status, the `hours` deadline or
        any inbox: it only records what the supervisor is waiting for and when it will be reminded (M15). The whole
        declaration lives in the task's `waiting` field (one JSON object, `''` = none), so it is visible in
        `status` / the control view as it stands and needs no field deletion."""
        t = self.task()
        declared = decision.get('waiting')
        if not declared:
            if t.get('waiting'):
                self.r.hset(TASK, mapping={'waiting': ''})
                self.log('task.waiting_cleared')
            return
        now = awake.now()  # until: awake time, like the budget (§35); since: Unix time, shown only
        until = now + float(declared['for_hours']) * 3600
        deadline = task_deadline(t, now)
        if deadline:
            until = min(until, deadline)  # never outlive the budget: waiting does not pause it
        previous = task_waiting(t)
        record = dict(declared, since=previous.get('since') or time.time(), until=until,
                      renewals=int(previous.get('renewals') or 0) + (1 if previous else 0), nudged=False)
        self.r.hset(TASK, mapping={'waiting': json.dumps(record, ensure_ascii=False)})
        self.log('task.waiting_renewed' if previous else 'task.waiting', until=until,
                 renewals=record['renewals'])

    def waiting_tick(self):
        """One reminder when a declared wait reaches its horizon (M15): never dispatches, never blocks, never
        renews, never touches a worker. The supervisor decides in its next turn; the task keeps `running`."""
        if not self.running():
            return
        w = task_waiting(self.task())
        if not w or w.get('nudged'):
            return
        if not w.get('until') or not awake.passed(w['until']):
            return
        if self.inbox_depth('supervisor') or self.agent('supervisor').get('state') != 'idle':
            return  # a turn is already coming: it sees the waiting line instead of a second reminder
        w['nudged'] = True
        self.r.hset(TASK, mapping={'waiting': json.dumps(w, ensure_ascii=False)})
        self.queue('supervisor', 'waiting horizon reached: ' + str(w.get('for') or '(not recorded)')
                   + ' — dispatch the next step, renew the wait with a new "waiting" (new for_hours), or reply '
                   'BLOCKED with the human action needed. Check: ' + str(w.get('check') or '(not recorded)'))
        self.log('task.waiting_expired', until=w.get('until'))

    def task_waiting_line(self):
        """One line for the supervisor's next-turn context while a wait is declared (``''`` when there is none, so an
        ordinary report message keeps its exact previous text)."""
        w = task_waiting(self.task())
        if not w:
            return ''
        stamp = lambda ts: time.strftime('%Y-%m-%d %H:%M', time.localtime(float(ts))) if ts else '?'
        until = float(w['until']) - awake.now() + time.time() if w.get('until') else None  # awake -> clock time
        return ('\nTask waiting since ' + stamp(w.get('since')) + ' until ' + stamp(until) + ' for: '
                + str(w.get('for') or '') + ' (check: ' + str(w.get('check') or '') + ')'
                + (' [horizon reached, reminder sent]' if w.get('nudged') else ''))

    def submit(self, spec):
        if not isinstance(spec, dict) or not isinstance(spec.get('goal'), str) or not spec['goal'].strip():
            raise ValueError('a task needs a goal')
        if self.service:
            raise ValueError('this fleet runs in service mode: submit requests, not tasks')
        if self.running():
            raise ValueError('a task is already running; stop it or wait for it to finish')
        if self.v2 and self.agent('supervisor').get('state') == 'held':
            # The last task's end held the supervisor's in-flight delivery (RuntimeV2.finish). A Pi supervisor has
            # no hold exit (design §24 ⑦-4), so a task accepted now would never be delivered: say so instead.
            mid = self.r.hget(self.v2.bus.lease_key('supervisor'), 'message_id')
            why = (self.r.hget(self.v2.bus.msg_key(mid), 'uncertain_reason') if mid else '') or 'execution unconfirmed'
            raise ValueError('the supervisor is held (' + why[:300] + '); this run cannot take a new task until '
                             'that delivery is released. Start a new run for it')
        auto_agents = spec.get('auto_agents', False)
        if type(auto_agents) is not bool:
            raise ValueError('auto_agents must be a boolean')
        profile_runtime.validate_live_setting(spec.get('apply_profiles_live', False),
                                              allowed=self.cfg['allow_profile_live_switch'])
        if spec.get('apply_profiles_live') and not auto_agents:
            raise ValueError('apply_profiles_live requires auto_agents and profile assignments')
        use_contracts = spec.get('contracts', False)
        if type(use_contracts) is not bool:
            raise ValueError('contracts must be a boolean')
        skeleton_worker = spec.get('skeleton_worker', (self.active_workers() or [None])[0])
        if use_contracts and skeleton_worker not in self.active_workers():
            raise ValueError('skeleton_worker must be one of ' + ', '.join(self.active_workers()))
        frozen = validate_contract(spec.get('contract'))
        if frozen and not spec.get('artifacts'):
            raise ValueError('contract.artifacts_frozen needs the caller\'s non-empty artifacts list')
        if 'artifacts' in spec:
            # Auto-planned task specs may explicitly say there are no pre-declared artifacts.
            # Keep the legacy non-auto validation unchanged.
            if not (auto_agents and spec['artifacts'] == []):
                validate_artifacts(spec['artifacts'], extended=use_contracts or frozen)
        budget = brake.validate_budget(spec.get('budget'))
        hours = spec.get('hours', 16)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours < 1000:
            raise ValueError('hours must be a positive number')
        tree_on, seed = decompose.enabled(), None  # §62: the switch is read once per task
        if (tree_on and spec.get('tree') is None and self.cfg.get('lifecycle') == 'dev_task'
                and not self.cfg.get('parent_config')):  # §62: a new dev task brings its tree; sub-fleets get a seed
            raise ValueError(decompose.tree_required('task'))
        if tree_on:
            root, prefix = self.tree_root()
            seed = (decompose.seed_from(spec['tree'], root, spec['goal'], prefix, by=spec.get('by'))
                    if spec.get('tree') is not None
                    else decompose.new_tree(root, spec['goal']))
        life_spec = {}
        # A sub-fleet's deliverable is checked by its parent; a job's sub-fleet (§58) runs its own lifecycle gate.
        if not self.cfg.get('parent_config') or self.cfg.get('job'):
            if self.cfg.get('lifecycle') == 'dev_task':
                life_spec['deliver'] = lifecycle_deliver.validate_deliver(spec.get('deliver'))
        registry_path = validate_agent_registry_path(spec.get('agent_registry'))
        dispatch_plan = profile_assignments = None
        if auto_agents:
            registry = AgentRegistry(registry_path)
            # Official Claude workers: prefer claude cards so a live switch never leaves the CLI
            # (v2 Codex supervisor + Claude workers rejects Pi/Codex workers). Pi/Codex fleets unchanged.
            # Card hosts filter only when the operator named this machine (config 'host');
            # max_concurrent counts the models the slots run now (a slot that already runs one is reused).
            usage = self.model_usage(registry)
            plan = build_dispatch_plan(spec['goal'], registry,
                                       'claude' if self.cfg.get('worker_cli') == 'claude' else None,
                                       host=self.cfg.get('host') or None,
                                       occupancy=usage.for_planning(self.active_workers()))
            dispatch_plan = plan.to_dict()
            profile_assignments = assign_profiles(plan, self.active_workers(), registry,
                                                  running={s: m for s, (_, m) in usage.slots.items()})
        task_id = 'task-' + uuid.uuid4().hex[:10]
        self.r.delete(TASK)
        fields = {'id': task_id, 'goal': spec['goal'], 'status': 'running', 'completion': 0,
                  'lifecycle_spec': json.dumps(life_spec, ensure_ascii=False),
                  'reason': '', 'deadline': awake.deadline(hours * 3600), 'budget_s': hours * 3600, 'bad_replies': 0,
                  'bounces': 0, 'artifacts': json.dumps(spec.get('artifacts', []), ensure_ascii=False),
                  'caller_artifacts': json.dumps(spec.get('artifacts', []), ensure_ascii=False),
                  'apply_profiles_live': json.dumps(spec.get('apply_profiles_live', False))}
        if use_contracts:
            fields['contracts'] = 1
        if frozen:
            fields['artifacts_frozen'] = 1
        if tree_on:
            fields['decompose'] = 1
        if dispatch_plan is not None:
            fields.update(
                auto_agents=1,
                agent_registry=str(registry.directory),
                dispatch_plan=json.dumps(dispatch_plan, ensure_ascii=False, separators=(',', ':')),
                profile_assignments=json.dumps(
                    profile_assignments, ensure_ascii=False, separators=(',', ':')),
            )
        self.r.hset(TASK, mapping=fields)
        self.brake_start(budget)
        prompt = ('[New task ' + task_id + '] Goal: ' + spec['goal']
                  + '\nWorkers you can dispatch to: ' + ', '.join(self.labels())
                  + '\nShared output directory: ' + str(self.workspace())
                  + ' (artifact paths are relative to it, e.g. worker-1/report.md)'
                  + '\nArtifacts pre-declared by the operator: '
                  + json.dumps(spec.get('artifacts', []), ensure_ascii=False))
        if tree_on:
            seeded = [n['id'] for n in decompose.level_nodes(seed)]
            prompt += ('\nDecomposition is on for this task: your first reply hands in the tree only ("tree": [...], '
                       '"dispatch": []); see "Decomposition tree" in AGENTS.md. The program keeps it in '
                       + str(self.workspace() / 'tree.json') + '.'
                       + (' The task came with a tree: ' + ', '.join(seeded) + ' are in it already; complete or change '
                          'them in your first reply.' if seeded else ''))
        if frozen:
            prompt += ('\nThese artifacts are frozen by the caller: they are the acceptance contract. Artifacts you '
                       'add in a decision are ignored and audited; to change them, reply BLOCKED and say why.')
        if budget:
            prompt += '\nBudget card (the daemon pauses the fleet at any line): ' + json.dumps(budget, ensure_ascii=False)
        if dispatch_plan is not None:
            prompt += ('\nAuto-agent dispatch plan (profile ids are recommendations, not worker names): '
                       + fields['dispatch_plan']
                       + '\nProfile assignments to stable worker slots: ' + fields['profile_assignments']
                       + '\nUse each assignment worker value for dispatch.to; never use agent_id/profile as a worker name.'
                       + '\nInclude "Recommended profile: <agent_id>" in each worker instruction so the profile stays '
                       'assigned while the worker slot remains authoritative.'
                       + '\nInclude "Subtask: <subtask_id>" to bind the instruction to its assignment. '
                       'Only the daemon may apply profiles; workers must not switch their own runtime.'
                       + (' Live profile switching is enabled for this task.' if spec.get('apply_profiles_live')
                          else ' Profile application is advisory metadata only.')
                       + '\nDo not dispatch dependent subtasks until every id in depends_on has finished.')
        if use_contracts:
            self.contracts_text = None
            self.save_contract(contracts.new_state(skeleton_worker))
            for name in ['supervisor'] + self.workers:  # never the advisor: it sees only consultations
                cwd = agent_cwd(self.cfg, name)
                cwd.mkdir(parents=True, exist_ok=True)
                (cwd / 'CONTRACTS.md').write_text(self.contract_doc('supervisor' if name == 'supervisor' else 'worker', name))
            prompt += ('\nInterface contracts are ON for this task: read CONTRACTS.md in your current directory now '
                       'and follow it. Your first reply is phase "interfaces": a full plan (M0…Mn) and every '
                       'interface with its sample, and an empty dispatch. The P1 skeleton worker is '
                       + skeleton_worker + '.')
        elif self.r.get(CONTRACTS) is not None:
            self.r.delete(CONTRACTS)  # an earlier contract task's state never leaks into this one
        for stale in (ESCALATIONS, ITEMS):
            if self.r.get(stale) is not None:
                self.r.delete(stale)
        for stale in (decompose.TREE, decompose.TREE_SUMMARY):  # §62: a new task, a new tree
            if self.r.get(stale) is not None:
                self.r.delete(stale)
        for stale in (decompose.TREE_NOTES, decompose.TUNED_KEY):
            if self.r.llen(stale):
                self.r.delete(stale)
        self.tree_text = None
        if tree_on:
            self.save_tree(seed)
        for w in self.workers:  # an earlier task's unadvised dispatches never match this one
            if self.r.llen(ADVICE_PENDING + w):
                self.r.delete(ADVICE_PENDING + w)
        if self.r.llen(RESCUE):  # an earlier task's consults and rescue leftovers never act in this one
            self.r.delete(RESCUE)
        rescue = json.loads(self.r.get(RESCUE_PENDING) or 'null')
        if rescue:
            self.r.delete(RESCUE_PENDING)
            self.advisor_abandon(rescue['ticket'], 'a new task started')
        for w in self.workers:
            info = self.agent(w)
            self.drop_advice_wait(w)
            self.drop_advice_pending(w, 'a new task started')
            left = {k: '' for k in RESCUE_FIELDS + ('advice_domains',) if info.get(k)}
            if left:
                self.setf(w, **left)
        self.queue('supervisor', prompt + '\nReply with the JSON format from AGENTS.md.')
        (self.run_dir / 'result.json').unlink(missing_ok=True)  # a new task: this run is not finished
        self.log('task.submitted', task=task_id)
        return task_id

    def finish(self, status, reason):
        if self.cfg.get('parent_config'):  # §46: program grants last for this sub-fleet run only
            self.clear_grants()
        if self.v2:
            return self.v2.finish(status, reason)
        if self.service:  # a resident fleet never ends: hold everything for a human instead
            self.pause(status + ': ' + reason)  # v1 only: no cause (the §24 pause review is v2-only)
            return
        self.r.hset(TASK, mapping={'status': status, 'reason': reason})
        self.log('task.' + status, reason=reason)
        for name in self.names:
            self.r.delete(key('inbox', name))
            codex_queue.clear(self, name, 'task ' + status)
            pane = self.agent(name).get('pane')
            if pane and self.agent(name).get('state') == 'busy':
                # An aborted turn fires no notify, so mark idle here; tabs stay open.
                self.mgr.send_key(pane, 'Escape')
                self.setf(name, state='idle', idle_confirmed=0)

    # -- service mode
    def request(self, spec):
        if not self.service:
            raise ValueError('requests need a fleet started with --mode service')
        if named_subfleet.is_op(spec):  # §29: own branch, never REQQ / validate_request / request_timeout
            return named_subfleet.request(self, spec)
        if applet_lease.is_op(spec):  # applet-interface.md 3.2: own branch, like the sub-fleet ops
            return applet_lease.request(self, spec)
        validate_request(spec, self.dispatch_targets())
        if (refused := applet_lease.screen(self, spec)):  # a leased `to`: slot_leased / the lease's own checks
            return refused
        rid = spec['id']
        held = self.job_holding(self.resolve(spec['to'])) if spec.get('to') else ''
        if held:
            raise ValueError(spec['to'] + ' is the supervisor of the sub-fleet of job ' + held + '; send the request '
                             'to another worker or without "to"')
        if (self.r.hgetall(REQ + rid) or self.r.get(RESULT + rid)
                or (self.v2 and self.r.get(self.v2.prefix + ':public:' + runtime_v2.digest(rid)))):
            raise ValueError('duplicate request id: ' + rid)
        if rid.endswith('.done') and self.split_owns(rid[:-len('.done')]):
            raise ValueError('duplicate request id: ' + rid + ' (reserved for the follow-up result of a split)')
        self.r.hset(REQ + rid, mapping={'spec': json.dumps(spec, ensure_ascii=False), 'status': 'routing',
                                        'created': awake.now(), 'attempts': 0, 'worker': ''})  # awake seconds (§35)
        self.r.rpush(REQQ, rid)
        self.route(rid, spec)
        self.log('request', id=rid, to=spec.get('to') or 'supervisor')
        return rid

    def route(self, rid, spec):
        if spec.get('to'):
            self.assign(rid, self.resolve(spec['to']), spec.get('instruction'))
        else:
            self.queue('supervisor', '[Request ' + rid + '] A new request needs routing.'
                       + ('\nNote from the sender: ' + spec['instruction'] if spec.get('instruction') else '')
                       + '\nWorkers: ' + ', '.join(self.labels())
                       + '\nWorker status now (state, queue, current request, role):\n' + self.worker_board()
                       + '\nPayload (JSON):\n'
                       + json.dumps(spec['payload'], ensure_ascii=False, indent=1)
                       + '\nReply with the service decision JSON from AGENTS.md.')

    def worker_board(self):
        """One line per dispatchable worker for a service routing message (docs/design.md §32):
        the supervisor picks an idle worker by role from this, not by guessing."""
        current = {}
        for rid in self.r.lrange(REQQ, 0, -1):
            req = self.r.hgetall(REQ + rid)
            if req.get('status') == 'dispatched' and req.get('worker'):
                current.setdefault(req['worker'], []).append(rid)
        # §44: an approved / rotating split holds its slot (escalation_decision refuses a dispatch to it); a
        # delegated one already shows as the slot's state
        splits = {rec['slot']: (k, rec['status']) for k, rec in self.escalation_state().items()
                  if rec.get('kind', 'split') == 'split' and rec.get('status') in ('approved', 'rotating')}
        roles = self.cfg.get('worker_roles') or {}
        lines = []
        for w in self.active_workers():
            state, depth = self.agent(w).get('state') or 'unknown', self.inbox_depth(w)
            line = '- ' + self.label(w) + ': ' + state + (', ' + str(depth) + ' queued' if depth else '')
            if w in splits:
                k, status = splits[w]
                line += (', split ' + k + ' ' + status + ': it is becoming sub-fleet ' + k + "'s supervisor, do not "
                         'route to it until [Sub-fleet ' + k + ' completed]')
            if current.get(w):
                line += ', has request ' + ', '.join(current[w])
            role = (Path(roles[w]) if w in roles else
                    Path(self.cfg['roles']).expanduser() / (w + '.md') if self.cfg.get('roles') else None)
            if role and role.is_file():
                first = next((x.strip('# ').strip() for x in role.read_text().splitlines() if x.strip()), '')
                line += ', role: ' + first[:120]
            info = self.agent(w)
            if int(info.get('delivered') or 0):  # §37: what a reuse would carry along (a new session carries nothing)
                try:
                    used, window = self.usage(w)
                except ValueError:
                    used, window = int(info.get('ctx_used') or 0), int(info.get('ctx_window') or 0)
                if used and window:
                    line += ', context used ' + str(round(100 * used / window)) + '% (' + str(round(used / 1000)) + 'k tokens)'
            task = self.last_task(info)
            if task:
                line += ', last task: ' + task
            if self.fresh_always(w):  # §45
                line += ', fresh: always'
            lines.append(line)
        return '\n'.join(lines)

    def last_task(self, info):
        """First line (<= 80 chars) of the last message delivered to this generation, '' when none (§37)."""
        text = info.get('last_msg') or ''
        if self.v2 and info.get('msg_id'):
            body = self.v2.bus.message(info['msg_id']).get('body') or {}
            text = '' if body.get('purpose') == 'handoff' else body.get('text') or ''
        text = SUPERVISOR_INSTRUCTION_HEAD.sub('', text)
        return next((' '.join(x.split()) for x in text.splitlines() if x.strip()), '')[:80]

    def mark_dispatch(self, slot, fresh, instruction=None):
        """§37, as a validated dispatch to ``slot`` is queued: ``fresh`` marks the slot for a clean generation at
        its delivery (none for a new or §28-cleared session); without it, one [Hint] line when the slot is
        already over fresh_hint_ratio of its window. §45: an always slot only records an explicit true / false
        for this one dispatch (``deliver`` decides; no mark, no hint)."""
        if self.fresh_always(slot):
            if fresh is not None and (instruction or '').strip():
                self.r.rpush(FRESH_INTENT + slot, json.dumps([instruction.strip(), bool(fresh)]))
                self.r.ltrim(FRESH_INTENT + slot, -32, -1)
            return
        info = self.agent(slot)
        if not int(info.get('delivered') or 0) or (self.v2 and self.v2.clear_enabled(slot)):
            if fresh:
                self.log('dispatch.fresh_skipped', agent=slot, reason='session has no earlier work')
            return
        if fresh:
            self.setf(slot, fresh_pending=1)
            return
        if info.get('fresh_pending') == '1':
            return  # a reset is already due before this one
        try:
            ratio = fleet_config.fresh_hint_ratio()
            used, window = self.usage(slot)
        except ValueError as exc:
            self.log('dispatch.hint_failed', agent=slot, error=str(exc)[:300])
            return
        if not window or used <= ratio * window:
            return
        hint = ('[Hint] ' + self.label(slot) + ' was at ' + str(round(100 * used / window)) + '% context when it got '
                'this dispatch; if the work is unrelated to its last task, add "fresh": true next time.')
        if hint not in self.r.lrange(FRESH_HINTS, 0, -1):  # two dispatches to one worker: one line
            self.r.rpush(FRESH_HINTS, hint)

    def fresh_refusal(self, slot):
        """Why a `"fresh": true` dispatch to ``slot`` is refused (docs/design.md §37), '' when it is taken."""
        if self.fresh_always(slot):
            return ''  # §45: an always worker is reset when this dispatch's turn comes, never refused
        info = self.agent(slot)
        if not int(info.get('delivered') or 0) or (self.v2 and self.v2.clear_enabled(slot)):
            return ''  # nothing to drop: no reset, plain delivery
        if info.get('state') != 'idle':
            why = 'it is ' + (info.get('state') or 'not ready')
        elif self.inbox_depth(slot):
            why = 'it has ' + str(self.inbox_depth(slot)) + ' message(s) queued'
        elif self.v2 and self.r.hget(self.v2.bus.lease_key(slot), 'current_delivery'):
            why = 'it has a delivery in flight'
        elif info.get('clear_required') == '1':
            why = 'its context is being cleared'
        elif subfleet.adopted(self.cfg, slot, info.get('generation')):
            why = 'its session belongs to the parent fleet'
        elif slot in self.profile_swaps():
            why = 'its profile is being switched'
        else:
            return ''
        return ('"fresh": true to ' + self.label(slot) + ' refused: ' + why + '. A fresh session only replaces an '
                'idle worker with nothing queued; send it to an idle worker, or drop "fresh" to queue behind its work')

    def fresh_always(self, slot):
        """§45: the roster row says `fresh: always` (read per call, so a hot reload applies to the next dispatch)."""
        return slot in self.workers and (self.cfg.get('worker_fresh') or {}).get(slot) == 'always'

    def fresh_intent(self, slot, text, pop=False):
        """§45: the supervisor's explicit `fresh` (True / False) for the dispatch ``text``, None when it wrote none."""
        for raw in self.r.lrange(FRESH_INTENT + slot, 0, -1):
            instruction, value = json.loads(raw)
            if instruction in text:
                if pop:
                    self.r.lrem(FRESH_INTENT + slot, 1, raw)
                return value
        return None

    def is_dispatch(self, text, msg):
        """§45: ``text`` (the next message for a worker) is a supervisor dispatch / service request, not a notice."""
        if self.service:
            head = REQ_HEAD.match(text)
            rid = msg.get('request_id') if self.v2 else head.group(1) if head else ''
            return bool(rid and self.r.exists(REQ + rid))
        return bool(SUPERVISOR_INSTRUCTION_HEAD.search(text))

    def assign(self, rid, slot, instruction=None):
        spec = json.loads(self.r.hget(REQ + rid, 'spec'))
        self.r.hset(REQ + rid, mapping={'status': 'dispatched', 'worker': slot})
        # §46: behind the [Request <id>] header the transport binds, the program's [Fleet grant] line if any
        self.queue(slot, self.request_message(rid, spec, instruction, slot=slot,
                                              prefix=applet_lease.frame(self, spec, slot) + self.take_grant_note(slot)))

    def request_message(self, rid, spec, instruction=None, prefix='', slot=None):
        reply = spec['reply']
        keys = ', '.join(reply['required'])
        if reply.get('allowed') is not None:
            keys += ' (allowed keys: ' + ', '.join(reply['allowed']) + ')'
        # Only Pi has the fleet_result tool; official CLIs (Codex/Claude TUIs) keep the
        # service_worker.md wording and are mapped by runtime_v2.tui_candidates.
        if self.v2 and not (slot and runtime_v2.tui_slot(self.runtime_cfg(slot), slot)):
            return ('[Request ' + rid + '] ' + prefix + (instruction or 'Handle this request as your role describes.').strip()
                    + '\nPayload (JSON):\n' + json.dumps(spec['payload'], ensure_ascii=False, indent=1)
                    + '\nUse fleet_result with a business JSON object with keys: ' + keys + '.')
        return ('[Request ' + rid + '] ' + prefix + (instruction or 'Handle this request as your role describes.').strip()
                + '\nPayload (JSON):\n' + json.dumps(spec['payload'], ensure_ascii=False, indent=1)
                + '\nYour final reply must be exactly one JSON object with the keys: ' + keys
                + '. No text outside the JSON object.')

    def publish(self, rid, status, replace=False, **fields):
        """``replace`` (v2): publish again over an earlier result of ``rid`` (a delivery gate recheck)."""
        if self.v2:
            return self.v2.publish(rid, status, replace=replace, **fields)
        req = self.r.hgetall(REQ + rid)
        now = time.time()
        rec = dict(id=rid, status=status, at=now, elapsed_s=round(awake.since(req.get('created') or awake.now()), 1),
                   **fields)
        raw = json.dumps(rec, ensure_ascii=False)
        self.r.set(RESULT + rid, raw, ex=RESULT_TTL)
        self.r.rpush(RESULTS, raw)
        self.r.ltrim(RESULTS, -1000, -1)
        self.r.delete(REQ + rid)
        self.r.lrem(REQQ, 0, rid)
        self.log('result', id=rid, status=status)
        if req.get('operator') == '1':
            self.queue('supervisor', '[Operator result ' + rid + '] ' + raw
                       + '\nThis request is finished. Report the worker result to the operator in the reason '
                       'field of the usual service decision JSON: status RUNNING, dispatch [], skip []. '
                       'Do not dispatch it again or invent a new request ID.')

    def on_service_worker(self, name, info, text):
        rid = info.get('request')
        self.setf(name, request='')
        if not rid:
            self.log('turn.no_request', agent=name)
            return
        req = self.r.hgetall(REQ + rid)
        if req.get('status') != 'dispatched':
            self.log('result.late', id=rid, agent=name, raw=text[-4000:])  # timed out or already answered
            return
        spec = json.loads(req['spec'])
        try:
            result = parse_reply(text, spec['reply'])
        except ValueError as exc:
            if self.r.hincrby(REQ + rid, 'attempts', 1) < 2:
                self.r.lpush(key('inbox', name), self.request_message(
                    rid, spec, 'Resend your answer as the JSON object only.',
                    prefix='Your last reply was rejected (' + str(exc) + '). '))
                return
            self.publish(rid, 'invalid', worker=self.label(name), error=str(exc), raw=text[-2000:])
            return
        self.publish(rid, 'ok', worker=self.label(name), result=result)

    def on_service_supervisor(self, text, invalid=None, about=None, about_id=None):
        try:
            if invalid:
                raise ValueError(invalid)
            decision = parse_service_decision(text, self.dispatch_targets())
            self.report_up_guard(decision)
            self.job_guard(decision)  # §58: a job is the program's; the supervisor never routes or answers it
            for item in decision['dispatch']:
                if item.get('fresh') and self.fresh_refusal(self.resolve(item['to'])):
                    raise ValueError(self.fresh_refusal(self.resolve(item['to'])))
                if 'split' in item and not self.cfg.get('supervisor_split'):
                    raise ValueError('dispatch.split: this fleet does not let the supervisor start a sub-fleet '
                                     '(supervisor_split is off); dispatch without split')
            applet_lease.check_decision(self, decision)  # leased slots, applet answers (applets.md section 4)
            answers = self.service_answers(decision)
            escalations = self.escalation_decision(decision, None) if decision['status'] == 'RUNNING' else []
            self.escalations_missed(decision)
        except (ValueError, TypeError, KeyError) as exc:
            # The first reply that leaves a [dialog] / [blocked] open is sent back without a strike (plan F').
            bad = (int(self.r.hget(SERVICE, 'bad_replies') or 0) if isinstance(exc, NoStrike)
                   else self.r.hincrby(SERVICE, 'bad_replies', 1))
            if bad >= 3:
                self.r.hset(SERVICE, mapping={'bad_replies': 0})
                self.pause('supervisor gave no valid decision JSON three times in a row: ' + str(exc),
                           kind='supervisor_fault', slot='supervisor',
                           generation=int(self.agent('supervisor').get('generation') or 0))
            else:
                self.queue('supervisor', 'Your reply is not a valid service decision JSON (' + str(exc)
                           + '). Resend one JSON object in the AGENTS.md format, nothing else.', front=True)
            return
        self.r.hset(SERVICE, mapping={'bad_replies': 0, 'reason': decision.get('reason', '')})
        self.brake_count('rounds')
        for item in decision['dispatch']:
            if self.r.hget(REQ + item['request_id'], 'status') != 'routing':
                self.log('dispatch.unknown_request', id=item['request_id'])
                continue
            if item.get('split'):
                self.supervisor_split(item['request_id'], self.resolve(item['to']), item)
                continue
            self.mark_dispatch(self.resolve(item['to']), item.get('fresh'), item['instruction'])
            self.assign(item['request_id'], self.resolve(item['to']), item['instruction'])
        for item in decision['skip']:
            if self.r.hget(REQ + item['request_id'], 'status') == 'routing':
                self.publish(item['request_id'], 'skipped', worker='supervisor', reason=item['reason'])
        for rid, result in answers:
            if self.r.hget(REQ + rid, 'status') == 'routing':
                self.publish(rid, 'ok', worker='supervisor', result=result)
        for task_id, rec, answer in escalations:
            self.escalation_apply({}, None, task_id, rec, answer)
        self.report_up(decision['report_up'])
        if decision.get('subfleet'):
            named_subfleet.decide(self, decision['subfleet'])
        if decision.get('applet'):
            applet_lease.decide(self, decision['applet'])
        if decision.get('reconcile'):  # section 60
            restart_state.decide(self, decision['reconcile'])
        if decision.get('heal'):  # §67: run by heal.tick, outside this transaction
            heal.accept(self, decision['heal'])
        if decision['status'] == 'BLOCKED' and not blocked_scope.absorb(self, about, decision.get('reason') or '',
                                                                         about_id=about_id):
            # §24: a root's BLOCKED on a notice about one of its own sub-fleets / jobs stops only that one;
            # otherwise a BLOCKED on a stuck / held notice (switch off, or a fleet with a parent) pauses as kind subfleet
            child = blocked_scope.notice_run(self, about)
            self.pause(decision.get('reason') or 'supervisor needs a human',
                       **({'kind': 'subfleet', 'subfleet_run': child} if child else {'kind': 'supervisor_blocked'}))

    def supervisor_split(self, rid, slot, item):
        """§44「监工直接开子 fleet」(user 2026-11-10, switch ``supervisor_split``): the supervisor judged request ``rid``
        too big for one worker and gave the split itself. The request is dispatched to ``slot`` without a delivery,
        the split goes through the worker path (service_split_intake: M<n>, limits, refusals) without the [escalate]
        question, and is approved at once (escalation_apply). A refusal by the program hands the request to that
        worker as a normal dispatch (it answers it itself) and tells the supervisor."""
        split = item['split']
        self.r.hset(REQ + rid, mapping={'status': 'dispatched', 'worker': slot})
        fields = {'escalate': split['reason'].strip(), 'workers': split.get('workers', len(split['proposal'])),
                  'proposal': [{k: v for k, v in x.items() if k in ('instruction', 'output', 'kind')}
                               for x in split['proposal']]}
        task_id = self.service_split_intake(slot, rid, fields, instruction=item['instruction'], quiet=True)
        self.log('split.by_supervisor', agent=slot, request_id=rid, task=task_id or '', accepted=bool(task_id))
        if not task_id:
            return
        answer = {'task': task_id, 'decision': 'approve', 'note': str(split.get('note') or '')}
        answer.update({k: split[k] for k in ('workers', 'supervisor_profile', 'worker_profiles') if k in split})
        self.escalation_apply({}, None, task_id, self.escalation_state()[task_id], answer)

    def service_answers(self, decision):
        """The supervisor's own answers in a service decision, as (request id, result) pairs. Each result must
        pass parse_reply against that request's own spec['reply'], else the whole decision is invalid
        (ValueError). A request that is not routing any more (unknown, dispatched, finished) is logged and
        left out, as a dispatch to one is."""
        answers = []
        for item in decision['answer']:
            rid = item['request_id']
            req = self.r.hgetall(REQ + rid)
            if req.get('status') != 'routing':
                self.log('answer.unknown_request', id=rid)
                continue
            try:
                result = parse_reply(json.dumps(item['result'], ensure_ascii=False), json.loads(req['spec'])['reply'])
            except ValueError as exc:
                raise ValueError('answer to ' + rid + ' does not fit its reply schema: ' + str(exc))
            answers.append((rid, result))
        return answers

    def expire_requests(self, now):
        for rid in self.r.lrange(REQQ, 0, -1):
            req = self.r.hgetall(REQ + rid)
            if not req:
                self.r.lrem(REQQ, 0, rid)
                continue
            limit = request_limit(json.loads(req['spec']), self.cfg)
            if limit is not None and midturn.effective_age(req, now) >= limit:
                self.publish(rid, 'timeout', worker=req.get('worker') or 'supervisor',
                             error='no valid answer within ' + str(int(limit)) + ' s (' + req.get('status', '') + ')')

    # -- delivery + rotation
    def pump(self):
        if self.paused():
            # nothing is delivered and nothing rotates; requests keep queuing (and may time out). §67: a heal notice
            # to the supervisor passes a pause a slot failure or a wall owns, nothing else does.
            heal.pump_paused(self)
            return
        if not self.service:
            self.rescue_tick()  # advisor rescue (§3.2): outside any v2 transaction, like the pre-dispatch advice
        cap = int(self.cfg.get('max_busy') or 0)
        card = (self.brake_budget() or {}).get('max_busy')
        if card:  # the budget card can only tighten --max-busy
            cap = min(cap, card) if cap else card
        busy = sum(self.agent(w).get('state') in ('busy', 'handoff', 'held') or bool(applet_lease.taken(self, w))
                   for w in self.workers)  # a leased slot is busy for its whole lease
        for name in self.names:
            info = self.agent(name)
            if name in self.workers and cap and busy >= cap and not applet_lease.taken(self, name):
                continue  # e.g. a local model server that rejects concurrent large requests
            if (info.get('advice_hold') == '1' and info.get('state') == 'starting'
                    and info.get('launch_ready') == '0' and (self.inbox_depth(name) or not self.advisor_online())):
                if self.advisor_online():
                    self.release_hold(name)  # first sheet decides model / effort / domains
                else:
                    self.release_default(name)  # advisor went offline: never stay held
                continue
            if info.get('state') == 'idle' and self.inbox_depth(name) and session_env.hold_delivery(self, name, info):
                continue  # applet route port closed (distributed-development.md): queue kept, delivered once it answers
            if (info.get('state') == 'idle' and self.inbox_depth(name)
                    and awake.passed(info.get('retry_at') or 0)):
                self.deliver(name)
                if name in self.workers and self.agent(name).get('state') in ('busy', 'handoff'):
                    busy += 1
            elif info.get('state') == 'idle' and not self.inbox_depth(name) and (name == 'supervisor' or name in self.workers):
                self.idle_rotate(name)
                if name in self.workers and self.agent(name).get('state') == 'handoff':
                    busy += 1

    def idle_rotate(self, name):
        """Rotate an idle agent over its ratio as soon as its turn settles, not at its next delivery
        (docs/design.md §32): a session that ends a long turn past the ratio and then gets nothing
        would otherwise keep its full context until the CLI's own compaction. Checked once per
        (generation, delivered), with the same guards as ``deliver``."""
        info = self.agent(name)
        if info.get('fresh_pending') == '1' and info.get('stopfail_fresh') == '1':
            # §24 service: a session that ended in a model refusal never takes more work; rotate it now.
            self.fresh_rotation(name, *self.usage(name), source='stopfail')
            return
        if info.get('fresh_pending') == '1' and info.get('result_fresh') == '1':
            # §51 fresh_after_result: a published result, not a cancelled dispatch; rotate once settled on the bus
            if not self.v2 or runtime_v2.tui_slot(self.runtime_cfg(name), name) or info.get('bridge_available') == '1':
                self.fresh_rotation(name, *self.usage(name), source='after_result')
            return
        if info.get('fresh_pending') == '1':
            self.setf(name, fresh_pending=0)  # §37: its fresh dispatch was cancelled before delivery
        if self.fresh_always(name):
            self.r.delete(FRESH_INTENT + name)  # §45: nothing queued, so no recorded true / false is still due
        mark = str(info.get('generation') or 0) + ':' + str(info.get('delivered') or 0)
        if int(info.get('delivered') or 0) == 0 or info.get('idle_rotate_mark') == mark:
            return  # a fresh session (READY only) is never rotated here; rotate.impossible stays deliver's
        if name != 'supervisor' and name in self.profile_swaps():
            return
        if self.v2 and not runtime_v2.tui_slot(self.runtime_cfg(name), name) and info.get('bridge_available') != '1':
            return  # not settled on the bus yet: look again next pump
        self.setf(name, idle_rotate_mark=mark)
        used, window = self.usage(name)
        ratio = float(self.agent(name).get('handoff_ratio') or fleet_config.handoff_ratio(self.cfg))
        if subfleet.adopted(self.cfg, name, info.get('generation')) or not window or used < ratio * window:
            return
        if agent_cli(self.runtime_cfg(name), name) == 'codex' and active_children(info.get('thread'), self.sessions):
            return  # deliver() defers it again at the next message
        self.start_rotation(name, used, window)

    def split_settings(self, name):
        """§44 long task split settings of a worker: its roster row > roster `split:` > built-in. Published
        to the agent hash (status shows them like handoff_source) whenever they change."""
        values, source = midturn.resolve_split(self.split_table, self.worker_split.get(name))
        fields = dict({'split_' + k: v for k, v in values.items()}, split_source=source)
        info = self.agent(name)
        if any(info.get(k) != str(v) for k, v in fields.items()):
            self.setf(name, **fields)
        return values

    def midturn_settings(self, name):
        cfg = self.runtime_cfg(name)
        cli = agent_cli(cfg, name)
        return midturn.resolve_settings(cli, str(cfg.get(MODEL_KEYS[cli]) or ''), self.cfg,
                                        self.handoff_table)[0]

    def midturn_guard(self, name, info):
        """Same ownership/profile/clear boundaries as normal rotation; dialogs are hands-off."""
        owns = getattr(self.mgr, 'owns', None)
        return (name not in self.names or name == ADVISOR or self.paused()
                or subfleet.adopted(self.cfg, name, info.get('generation'))
                or info.get('state') in ('delegated', 'clearing') or info.get('clear_required') == '1'
                or info.get('prompt_wait') or info.get('profile_state') == 'switching'
                or name in self.profile_swaps() or not info.get('pane')
                or owns is not None and not owns(info['pane']))

    def midturn_targets(self):
        """Only the main loop touches Redis; the file thread receives plain snapshots."""
        targets = {}
        for name in self.names:
            info = self.agent(name)
            if name == ADVISOR or info.get('state') != 'busy':
                continue
            lease = self.r.hgetall(self.v2.bus.lease_key(name))
            if lease.get('status') != 'received' or not lease.get('message_id'):
                continue
            msg = self.v2.bus.message(lease['message_id'])
            if msg['body'].get('purpose') == 'handoff':
                continue
            cfg = self.runtime_cfg(name)
            cli = agent_cli(cfg, name)
            override = self.handoff_limits(name)[0]
            targets[name] = dict(cli=cli, generation=int(info.get('generation') or 0),
                message_id=lease['message_id'], thread=info.get('thread') or '',
                path=info.get('transcript') if cli == 'claude' else '',
                directory=str(self.run_dir / 'pi-sessions' / name / ('g' + str(info.get('generation') or 0)))
                    if cli == 'pi' else str(self.sessions),
                window=override or int(info.get('ctx_window') or (self.cfg['window'] if cli == 'codex' else 0)),
                override=override)
            since = self.split_due(name, info)
            if since is not None:
                targets[name]['split_since'] = since
        return targets

    # -- long task split (§44): look at a long busy turn, nudge a flowing one to propose a split
    def split_due(self, name, info):
        """Start of the look-back window when this busy worker's split check is due, else None."""
        if self.service or name not in self.workers or self.task().get('status') != 'running':
            return None
        if self.decompose_on():  # §62: a block is never asked to split; an overrun goes to the supervisor instead
            return None
        settings = self.split_settings(name)
        now = time.time()
        if (not settings['check_minutes'] or now - float(info.get('busy_since') or now) < settings['check_minutes'] * 60
                or now < float(info.get('split_recheck_at') or 0)
                or info.get('midturn_typed_g') == str(info.get('generation') or 0)  # a §34 handoff is under way
                or self.midturn_guard(name, info)):
            return None
        return now - settings['window_minutes'] * 60

    def item_leaf(self, task_id):
        """§44 leaf: the supervisor dispatched this task with "leaf": true, a block one worker does whole.
        §62: while the task's tree switch is on, the program's judgement on the tree decides instead."""
        if self.decompose_on():
            return (decompose.by_id(self.tree_state() or {'nodes': []}).get(task_id) or {}).get('leaf') is True
        state = self.contract_state()
        rows = state['tasks'] if state is not None else json.loads(self.r.get(ITEMS) or '{}')
        return (rows.get(task_id) or {}).get('leaf') is True

    def split_apply(self, name, target, result):
        """Main loop: the probe's look at a due worker. Flowing + splittable + not nudged for this task yet
        -> register the one-line nudge (the PostToolUse hook hands it over) and tell the supervisor."""
        settings = self.split_settings(name)
        self.setf(name, split_recheck_at=time.time() + settings['check_minutes'] * 60)
        info, got = self.agent(name), result.get('activity')
        # The task of the delivery running now (its `[M<n>]` head), not the last one queued to this slot.
        head = re.search(r'\[(' + contracts.M_RE + r')\]', self.v2.bus.message(result['message_id'])['body'].get('text') or '')
        task = head.group(1) if head else self.report_item(name) or ''
        minutes = int((time.time() - float(info.get('busy_since') or time.time())) // 60)
        if got is None:
            self.setf(name, split_last='unknown: ' + result['cli'] + ' has no readable tool-call record')
            self.log('split.unknown', agent=name, cli=result['cli'])
            return
        flowing = got['calls'] >= settings['min_calls'] and got['distinct'] >= settings['min_distinct']
        last = ('calls=' + str(got['calls']) + ' distinct=' + str(got['distinct']) + ' files=' + str(got['files'])
                + ' in ' + str(settings['window_minutes']) + 'm: ' + ('flowing' if flowing else 'slow'))
        self.log('split.checked', agent=name, task=task, minutes=minutes, flowing=flowing, **got)
        if not flowing:
            return self.setf(name, split_last=last)
        key_ = task or result['message_id']
        refused = ('already nudged for ' + key_ if info.get('split_nudged') == key_ else
                   'no task id' if not task else
                   task + ' is a leaf (the supervisor marked it not worth splitting)' if self.item_leaf(task) else
                   'a split of ' + task + ' is already open' if task in self.escalation_state() else
                   subfleet.check(self, name, task, 1, ready=False))
        if not refused and not is_claude(self.runtime_cfg(name), name):
            refused = 'only a Claude Code worker takes the nudge (PostToolUse hook)'  # check() refuses others first
        if refused:
            return self.setf(name, split_last=last + '; not splittable: ' + refused)
        sid = 'fx-' + uuid.uuid4().hex[:16]
        prompt = midturn.SPLIT_PROMPT.format(split_id=sid, task=task, minutes=minutes, calls=got['calls'],
                                             distinct=got['distinct'], window=settings['window_minutes'])
        self.setf(name, split_last=last + '; nudged', split_nudged=key_, split_id=sid, split_prompt=prompt,
                  split_g=result['generation'], split_message=result['message_id'], split_send_pending=1)
        self.log('split.nudged', agent=name, task=task, split_id=sid, minutes=minutes, **got)
        self.queue('supervisor', '[long-running] ' + self.label(name) + ' 已跑 ' + str(minutes) + ' 分钟，最近 '
                   + str(settings['window_minutes']) + ' 分钟 ' + str(got['calls']) + ' 次工具调用、改了 '
                   + str(got['files']) + ' 个文件\nThe fleet asked it to stop at a safe point and send a split '
                   'proposal for ' + task + ' ([escalate] follows). Nothing to do until then; when it comes, an '
                   'approve may pick the models (supervisor_profile / worker_profiles); without supervisor_profile '
                   'the program picks the supervisor model from the supervisor chain. '
                   'Reply with the JSON format from AGENTS.md.')

    def midturn_apply(self):
        """Apply file results and type prompts on the daemon thread, outside event transactions."""
        if not self.v2:
            return
        targets = self.midturn_targets()
        self.midturn_probe.update(targets)
        for name, result in self.midturn_probe.take().items():
            target = targets.get(name)
            if not target or any(target[k] != result[k] for k in ('generation', 'message_id')):
                continue
            if 'split_since' in target and 'activity' in result:
                self.split_apply(name, target, result)
            info = self.agent(name)
            if result['used'] is None:
                if result['cli'] in ('opencode', 'dsh') and info.get('midturn_no_usage_g') != str(result['generation']):
                    self.v2.transaction('midturn-no-usage-' + uuid.uuid4().hex,
                        lambda name=name, result=result: self.v2.midturn_no_usage(name, result['generation']))
                continue
            used, window = result['used'], result['window']
            settings = self.midturn_settings(name)
            self.setf(name, ctx_used=used, ctx_window=window, midturn_ratio=settings['midturn_ratio'],
                      midturn_line=settings['midturn_ratio'] * window)
            # control's audit renderer includes status on its event line; keep the live numbers there
            # without changing its schema or the separate control_view module (outside M1's file scope).
            self.v2.audit('midturn.usage', slot=name,
                          status='ctx=' + str(used) + '/' + str(window) + ' mid=' + str(int(settings['midturn_ratio'] * window)))
            if self.midturn_guard(name, info) or info.get('midturn_typed_g') == str(result['generation']):
                continue
            msg = self.v2.bus.message(result['message_id'])
            splits = self.v2.midturn_splits(msg)
            action = midturn.decide(used, window, midturn_splits=splits, **settings)
            if action == 'blocked':
                self.v2.transaction('midturn-limit-' + result['message_id'],
                    lambda name=name, msg=msg: self.v2.midturn_blocked(name, msg))
            elif action == 'steer':
                self.midturn_order(name, msg, used, window, settings)
        self.midturn_timeouts()

    def midturn_order(self, name, msg, used, window, settings):
        now = time.time()
        if msg['deadline_at'] <= int(now * 1000):
            return
        if self.service:
            req = self.r.hgetall(REQ + msg['request_id'])
            limit = request_limit(json.loads(req['spec']), self.cfg) if req else None
            if limit is not None and midturn.effective_age(req, awake.now()) >= limit:
                return
        hid = 'fx-' + uuid.uuid4().hex[:16]
        draft = agent_cwd(self.cfg, name) / '.handoff' / ('draft-' + hid + '.md')
        # Single-line paste even for a run directory containing control characters.
        if any(c in str(draft) for c in '\n\r\t'):
            self.v2.audit('midturn.type_failed', slot=name, level='error', error='handoff path contains a control character')
            return
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(handoff.partial_template())
        prompt = midturn.MIDTURN_PROMPT.format(midturn_id=hid, used=used, window=window, path=draft)
        def order():
            fields = midturn.pause_message(msg, int(now * 1000), midturn_retry=settings['midturn_retry'],
                                           profile_ready_timeout=float(self.cfg['profile_ready_timeout']))
            fields = {k: fields[k] for k in ('deadline_at', 'deadline_remaining_ms', 'clock_paused_at')}
            fields.update(midturn_id=hid, midturn_at=now, midturn_used=used)
            self.v2.context['authorization'] = {
                self.v2.bus.msg_key(msg['message_id']): {'status': 'received', 'generation': str(msg['generation']),
                    'delivery_id': msg['delivery_id'], 'session_id': msg['session_id']},
                self.v2.bus.lease_key(name): {'status': 'received', 'message_id': msg['message_id'],
                    'current_delivery': msg['delivery_id'], 'generation': str(msg['generation'])}}
            self.r.hset(self.v2.bus.msg_key(msg['message_id']), mapping=fields)
            self.setf(name, midturn_id=hid, midturn_draft=draft, midturn_mtime=draft.stat().st_mtime_ns,
                      midturn_typed_g=msg['generation'], midturn_deadline=now + settings['midturn_retry'],
                      midturn_tries=1, midturn_prompt=prompt, midturn_send_pending=1, midturn_type_attempts=0,
                      midturn_request=msg['request_id'], midturn_message=msg['message_id'],
                      midturn_instruction=msg.get('midturn_instruction') or msg['body']['text'], midturn_wait_at='')
            self.v2.midturn_pause_request(msg['request_id'], awake.now())
            self.v2.audit('midturn.ordered', slot=name, message_id=msg['message_id'], detail=fields)
            self.v2.midturn_card(name, self.label(name) + ' 上下文 ' + str(used) + '/' + str(window) + '，已发中途交接指令')
        self.v2.transaction('midturn-order-' + hid, order)

    def midturn_type(self, name):
        info = self.agent(name)
        if is_claude(self.runtime_cfg(name), name):
            return  # delivered by the PostToolUse hook (claude_posttool), never typed into the pane
        prompt = midturn.registered_prompt(info, info['generation'])
        if not prompt or self.midturn_guard(name, info):
            return
        attempts = int(info.get('midturn_type_attempts') or 0) + 1
        # Record before typing: a crash never silently replays a possibly accepted paste.
        self.setf(name, midturn_type_attempts=attempts, midturn_send_pending=0)
        try:
            ok = self.mgr.send_prompt_to_idle(info['pane'], prompt)
        except Exception as exc:
            ok = False
            self.v2.audit('midturn.type_failed', slot=name, level='error', error=str(exc))
        if not ok:
            self.setf(name, midturn_send_pending=int(attempts < 2))
            self.v2.audit('midturn.type_failed', slot=name, level='error', detail={'attempt': attempts})

    def midturn_timeouts(self):
        now = time.time()
        for name in self.names:
            info = self.agent(name)
            if not info.get('midturn_id') or info.get('state') != 'busy':
                continue
            lease = self.r.hgetall(self.v2.bus.lease_key(name))
            if lease.get('status') != 'received' or lease.get('message_id') != info.get('midturn_message'):
                continue
            if info.get('midturn_wait_at'):
                elapsed = max(0, now - float(info['midturn_wait_at']))
                def unfreeze():
                    self.setf(name, midturn_wait_at=now if info.get('prompt_wait') else '',
                              midturn_deadline=float(info['midturn_deadline']) + elapsed)
                    key_ = self.v2.bus.msg_key(lease['message_id'])
                    self.r.hset(key_, mapping={'deadline_at': int(self.r.hget(key_, 'deadline_at')) + round(elapsed * 1000)})
                # Extend throughout the wait, so bridge lease renewals also see a live message deadline.
                self.v2.transaction('midturn-wait-' + uuid.uuid4().hex, unfreeze)
                info = self.agent(name)
            if info.get('prompt_wait'):
                if not info.get('midturn_wait_at'):
                    self.setf(name, midturn_wait_at=now)
                continue  # dialog protection precedes even a paused-fleet timeout
            if now >= float(info['midturn_deadline']):
                if self.midturn_guard(name, info):
                    self.v2.transaction('midturn-held-' + uuid.uuid4().hex,
                        lambda name=name: self.v2.hold(name, 'midturn timeout; rotation guarded'))
                    continue
                if int(info.get('midturn_tries') or 1) < 2:
                    retry = self.midturn_settings(name)['midturn_retry']
                    self.setf(name, midturn_tries=2, midturn_deadline=now + retry,
                              midturn_send_pending=1, midturn_type_attempts=0)
                    self.v2.audit('midturn.retry', slot=name)
                else:
                    try:
                        screen = self.mgr.capture_output(info['pane']) or ''
                    except Exception:
                        screen = ''
                    self.v2.watchdog.rotate(name, 'midturn timeout', screen, reason='midturn')
                    continue
            if self.agent(name).get('midturn_send_pending') == '1':
                self.midturn_type(name)

    def handoff_limits(self, name):
        """(window or None = measured, ratio, source) for this agent's current runtime model."""
        cfg = self.runtime_cfg(name)
        cli = agent_cli(cfg, name)
        model = cfg.get(MODEL_KEYS[cli]) or ''
        limits = handoff_limits(cli, str(model), self.cfg, self.handoff_table, self.worker_handoff.get(name),
                                card=self.card_handoff_ratio(name))
        self.setf(name, handoff_window=limits[0] or 'measured', handoff_ratio=limits[1], handoff_source=limits[2])
        return limits

    def usage(self, name):
        override = self.handoff_limits(name)[0]
        if is_pi(self.runtime_cfg(name), name) or is_dsh(self.runtime_cfg(name), name):  # bridge-reported
            info = self.agent(name)
            return int(info.get('ctx_used') or 0), override or int(info.get('ctx_window') or self.cfg['window'])
        if is_opencode(self.runtime_cfg(name), name):
            # Bridge-reported usage (assistant tokens); the window is the configured one (free tier 200K),
            # never a model's self-reported size and never a Codex rollout.
            info = self.agent(name)
            return int(info.get('ctx_used') or 0), override or int(self.cfg.get('opencode_window') or OPENCODE_WINDOW)
        if is_claude(self.runtime_cfg(name), name):
            path = self.agent(name).get('transcript')
            if not path:
                return 0, 0
            used, window = claude_transcript_usage(path, override)
            if used is None:
                return int(self.agent(name).get('ctx_used') or 0), window
            self.setf(name, ctx_used=used, ctx_window=window)
            return used, window
        if not is_codex(self.runtime_cfg(name), name):
            raise ValueError('no usage reader for ' + agent_cli(self.runtime_cfg(name), name))
        thread = self.agent(name).get('thread')
        paths = rollouts(thread, self.sessions) if thread else []
        if not paths:
            return 0, 0
        used, window = read_usage(str(paths[0]), int(self.cfg['window']))
        window = override or window
        if used is None:
            return int(self.agent(name).get('ctx_used') or 0), window
        self.setf(name, ctx_used=used, ctx_window=window)
        return used, window

    def model_usage(self, registry=None):
        """Which models each worker slot holds (model_usage: one slot counts once per model; a tab
        still alive keeps its model, also an old generation's or a dead slot's)."""
        registry = registry or AgentRegistry(self.task().get('agent_registry') or None)

        def alive(pane):
            try:
                return self.mgr.get_pane_status(pane) is not None
            except Exception:
                return True  # unknown: keep holding rather than free a ticket early
        return model_usage.Usage.from_agents({w: self.agent(w) for w in self.workers}, self.cfg, registry,
                                             alive=alive)

    def card_gate(self, name, card, registry):
        """Before ``name`` starts a generation on ``card`` (None = the fleet default runtime):
        ('block', why) if this machine may never run it, ('wait', why) while its model is at
        max_concurrent on other slots, else None. An unmapped default is not capped."""
        if card is None:
            card = model_usage.default_card(self.cfg, registry)
            if card is None:
                return None
        problem = model_usage.host_problem(card, self.cfg.get('host') or None)
        if problem:
            return 'block', problem
        problem = self.model_usage(registry).admit(name, card)
        return ('wait', problem) if problem else None

    def card_handoff_ratio(self, name):
        """(profile id, ratio) from the slot's running profile card (own or from its model card), or
        None. One layer of handoff_limits (design.md §22, below the roster / config tables)."""
        profile = self.agent(name).get('runtime_profile')
        if not profile:
            return None
        try:
            card = AgentRegistry(self.task().get('agent_registry') or None).get(profile)
        except (AgentRegistryError, KeyError, OSError):
            return None
        return (profile, card.handoff_ratio) if card is not None and card.handoff_ratio is not None else None

    def handoff_ratio(self, name):
        """The slot's profile card ratio, else the fleet's last layer (a typed --handoff-ratio, else
        fleet.yaml#handoff). Card layer only; the full resolution incl. roster / config tables is handoff_limits."""
        card = Fleet.card_handoff_ratio(self, name)
        return card[1] if card else fleet_config.handoff_ratio(self.cfg)

    def deliver(self, name):
        info = self.agent(name)
        if info.get('state') != 'idle':
            return
        pending_ids = self.v2.bus.pending(name, 1) if self.v2 else []
        next_msg = self.v2.bus.message(pending_ids[0]) if pending_ids else {}
        continuing = next_msg.get('kind') == 'continuation'
        if not continuing and name != 'supervisor' and info.get('roster_swap_deferred') != '1' and name in self.profile_swaps():
            return  # the next generation (new profile) takes this queue once READY (a deferred swap keeps working)
        if self.v2 and not runtime_v2.tui_slot(self.runtime_cfg(name), name) and info.get('bridge_available') != '1':
            return
        pending = self.v2.pending_text(name) if self.v2 else self.r.lrange(key('inbox', name), 0, 0)
        if not pending:
            return
        text = pending[0]
        if name in self.workers and not self.service:
            head = SUPERVISOR_INSTRUCTION_HEAD.search(text)
            if self.v2:
                mid = self.v2.bus.pending(name, 1)[0]
                msg = self.v2.bus.message(mid)
                # v2 stores the task binding as metadata; original profile validator
                # still receives the business instruction at the idle boundary.
                if msg['request_id'] != self.task().get('id'):
                    self.finish('blocked', 'stale task instruction for ' + name)
                    return
                head = SUPERVISOR_INSTRUCTION_HEAD.search('[Supervisor instruction ' + msg['request_id'] + '] ' + text)
                text = '[Supervisor instruction ' + msg['request_id'] + '] ' + text
            if head and not continuing:
                if head.group(1) != self.task().get('id'):
                    self.finish('blocked', 'stale task instruction for ' + name)
                    return
                try:
                    # Strip only the daemon-generated advisory; reauthorize the
                    # original instruction against this task after queueing/READY/
                    # handoff. Queue and callback wire formats stay unchanged.
                    application = self.prepare_worker_profile(name, text[head.end():])
                except profile_runtime.ProfileRuntimeError as exc:
                    self.finish('blocked', 'profile validation before delivery to ' + name + ': ' + str(exc))
                    return
                self.record_worker_profile(name, application)
                if application.state in ('switching', 'applied-live'):
                    try:
                        registry = AgentRegistry(self.task().get('agent_registry') or None)
                        launch = profile_launch.resolve_launch(registry.get(application.desired_profile), registry, self.cfg)
                        if application.state == 'switching':
                            gate = self.card_gate(name, registry.get(application.desired_profile), registry)
                            if gate and gate[0] == 'block':
                                raise ValueError(gate[1])
                            if gate:  # keep the message queued; retried every tick until a slot frees up
                                if info.get('capacity_wait') != gate[1]:
                                    self.log('profile.capacity_wait', agent=name,
                                             profile=application.desired_profile, reason=gate[1])
                                self.setf(name, profile_state='pending', profile_reason=gate[1], capacity_wait=gate[1])
                                return
                            if info.get('capacity_wait'):
                                self.setf(name, capacity_wait='')
                            self.switch_profile(name, launch)
                            return  # never pop/deliver until this generation returns READY
                        if profile_launch.encode_launch(launch) != info.get('runtime_launch'):
                            raise ValueError('profile launch definition changed since spawn; submit a new task after repair')
                    except (OSError, ValueError, RuntimeError, KeyError, SystemExit, subprocess.SubprocessError) as exc:
                        self.block_profile(name, str(exc))
                        return
                text = profile_runtime.profile_advisory(name, application) + text[head.start():]
                if (self.advisor_online() and application.state not in ('switching', 'applied-live')
                        and not self.advise_before_delivery(name, text)):
                    return  # respawning / blocked: never pop/deliver until the new generation returns READY
        # Validate profile intent before even the existing context rotation path.
        # Leave the original message queued across handoff; READY triggers another
        # idle-boundary validation. This daemon remains the sole inbox consumer.
        used, window = self.usage(name)
        if (continuing and int(info.get('delivered') or 0) == 0 and window
                and used >= self.midturn_settings(name)['midturn_ratio'] * window):
            self.v2.transaction('midturn-impossible-' + next_msg['message_id'],
                                lambda: self.v2.midturn_blocked(name, next_msg, impossible=True))
            return
        if self.agent(name).get('fresh_pending') == '1' and not continuing:
            # §37: a `fresh` dispatch waits here for a clean generation; a session that has done nothing since
            # (new after a restart / advisor or profile respawn) or an adopted pane just takes it.
            if not int(info.get('delivered') or 0) or subfleet.adopted(self.cfg, name, info.get('generation')):
                self.setf(name, fresh_pending=0)
                self.log('dispatch.fresh_skipped', agent=name, reason='session has no earlier work or is adopted')
            elif agent_cli(self.runtime_cfg(name), name) == 'codex' and active_children(info.get('thread'), self.sessions):
                self.log('rotate.deferred', agent=name, reason='native subagents still running')
                return
            else:
                self.fresh_rotation(name, used, window)
                return
        elif not continuing and self.fresh_always(name) and self.is_dispatch(text, next_msg):
            # §45 roster `fresh: always`: a clean generation before every dispatch once this one has delivered work.
            explicit = self.fresh_intent(name, text)
            mark = str(info.get('generation') or 0) + ':' + str(info.get('delivered') or 0)
            skip = ('clear_after_result' if self.v2 and self.v2.clear_enabled(name)
                    else 'explicit_false' if explicit is False
                    else 'delivered_0' if not int(info.get('delivered') or 0)
                    else 'fresh_failed' if info.get('fresh_always_failed') == mark else '')
            wait = ('' if skip else 'adopted' if subfleet.adopted(self.cfg, name, info.get('generation'))
                    else 'clearing' if info.get('clear_required') == '1'
                    else 'native subagents still running' if agent_cli(self.runtime_cfg(name), name) == 'codex'
                    and active_children(info.get('thread'), self.sessions) else '')
            if wait:  # §37's guards postpone the reset; the dispatch never goes to this session
                if info.get('fresh_deferred') != mark + ':' + wait:
                    self.setf(name, fresh_deferred=mark + ':' + wait)
                    self.log('dispatch.fresh_deferred', agent=name, reason=wait)
                return
            self.fresh_intent(name, text, pop=True)
            if skip:
                self.log('dispatch.fresh_skipped', agent=name, reason=skip,
                         source='always' if explicit is None else 'explicit')
            else:
                if self.fresh_rotation(name, used, window, source='explicit' if explicit else 'always') is False:
                    self.setf(name, fresh_always_failed=mark)  # after the restore: this dispatch goes to the old session
                return
        ratio = float(self.agent(name).get('handoff_ratio') or fleet_config.handoff_ratio(self.cfg))  # usage()
        if subfleet.adopted(self.cfg, name, info.get('generation')):
            window = 0  # an adopted pane goes back to its parent fleet: never replace it with a successor
        if window and used >= ratio * window:
            if int(info.get('delivered') or 0) == 0:
                # A fresh session (only its READY turn so far) is already over the ratio: its
                # baseline (system prompt + AGENTS.md) exceeds the budget, so rotating would spawn
                # successors forever. Deliver anyway and say so once per generation.
                self.log('rotate.impossible', agent=name, used=used, window=window,
                         ratio=ratio, reason='fresh session already over ratio; raise handoff_ratio (roster) or --handoff-ratio')
            elif agent_cli(self.runtime_cfg(name), name) == 'codex' and active_children(info.get('thread'), self.sessions):
                # Hold the message: never feed a near-full session while its subagents run.
                self.log('rotate.deferred', agent=name, reason='native subagents still running')
                return
            else:
                self.start_rotation(name, used, window)
                return
        if self.agent(name).get('advice_hotload'):  # set by a consultation made while online
            text = self.apply_hotload(name, text)
        if self.v2:
            return self.v2.deliver(name)
        self.r.lpop(key('inbox', name))
        if self.service:
            head = REQ_HEAD.match(text)
            if head and not self.r.hgetall(REQ + head.group(1)):
                self.log('deliver.dropped', agent=name, id=head.group(1), reason='request already finished')
                return
            if name != 'supervisor':
                self.setf(name, request=head.group(1) if head else '')
        self.setf(name, state='busy', busy_since=time.time(), delivered=int(info.get('delivered') or 0) + 1)
        self.brake_count('calls')
        if not self.send(name, self.agent(name)['pane'], text):
            self.r.lpush(key('inbox', name), text)
            self.finish('blocked', 'delivery failed: ' + name)
            return
        self.setf(name, last_msg=text)
        self.log('deliver', agent=name, text=text[:120])

    # -- advisor (roster advisor: on; advisor plan §3.1, pre-dispatch path)
    def floor_domains(self):
        """start --worker-domains: domains every Claude worker gets, advised or not (the advisor only adds)."""
        return list(self.cfg.get('worker_domains') or [])

    def drop_floor(self, raw):
        """A sheet's domains without the pinned ones: a card whitelist never rejects a sheet for echoing them."""
        floor = self.floor_domains()
        if floor and isinstance(raw, dict) and isinstance(raw.get('domains'), list):
            return dict(raw, domains=[d for d in raw['domains'] if d not in floor])
        return raw

    def install_default_kit(self, cwd, scope):
        """The default (non-advised) layout: the old kit copy, plus the --worker-domains links and their rules
        when the kit is a domain kit (a domain kit has no .claude/: without a floor only mcp.json is copied).
        installed_domains stays '' (default layout)."""
        install_kit(self.cfg.get('worker_kit'), cwd)
        install_imported_mcp(self.cfg, cwd)  # roster `import_mcp`, after the kit copy
        floor = self.floor_domains()
        if not floor or not self.cfg.get('worker_kit'):
            return
        try:
            kit = self.kit or kitmod.Kit(self.cfg['worker_kit'])
        except (OSError, ValueError) as exc:
            self.log('kit.floor_unavailable', level='error', domains=floor, error=str(exc)[:500])
            return
        floor = [d for d in floor if d in kit.domains]
        kit.link_domains(cwd, floor)
        (cwd / 'CLAUDE.md').write_text(kit.claude_md(scope, floor))

    def install_advised_kit(self, name, cwd, scope, record=True):
        """Advisor runs: Claude CLAUDE.md = role scope + project core + this generation's domain rules;
        cwd/.claude/{skills,agents} cleared, then the domains linked; full kit mcp.json. ``record``: write
        installed_domains (the slot's launch record); False for a held generation that has not started."""
        info = self.agent(name)
        domains = json.loads(info.get('advice_domains') or info.get('installed_domains') or '[]')
        domains = list(dict.fromkeys(self.floor_domains() + domains))  # --worker-domains: the advisor only adds
        if self.kit is None and self.cfg.get('worker_kit'):  # off at daemon start: an inherited layout still needs it
            try:
                self.kit = kitmod.Kit(self.cfg['worker_kit'])
            except (OSError, ValueError) as exc:
                self.log('advice.kit_unavailable', level='error', agent=name, domains=domains, error=str(exc)[:500])
        if self.kit:
            domains = [d for d in domains if d in self.kit.domains]
            self.kit.install(cwd, domains)
            (cwd / 'CLAUDE.md').write_text(self.kit.claude_md(scope, domains))
        else:
            kitmod.Kit.clear(cwd)
            domains = []
            (cwd / 'CLAUDE.md').write_text(scope)
        install_imported_mcp(self.cfg, cwd)  # roster `import_mcp`, after the kit's mcp.json
        if record:
            self.setf(name, installed_domains=json.dumps(domains, ensure_ascii=False), advice_domains='')

    def advice_identity(self, name, text):
        if self.v2:
            mids = self.v2.bus.pending(name, 1)
            return mids[0] if mids else ''
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def take_pending_advice(self, name, text):
        """The supervisor dispatch this queued text carries, or None (notices / corrections)."""
        for raw in self.r.lrange(ADVICE_PENDING + name, 0, -1):
            entry = json.loads(raw)
            if entry['instruction'] and entry['instruction'] in text:
                self.r.lrem(ADVICE_PENDING + name, 1, raw)
                return entry
        return None

    def advice_context(self, name, cli, cfg):
        """What one pre-dispatch consultation of ``name`` is checked against (re-read per consultation and,
        for a resident one, again when its answer is read)."""
        try:
            registry, registry_error = AgentRegistry(), None
        except (OSError, ValueError) as exc:  # a broken card file: treat as advisor unavailable (§8), never exit
            registry, registry_error = (), 'agent registry unreadable: ' + str(exc)[:400]
        cards = advisor.launch_cards(registry, cli)
        model, effort = advisor.runtime_model(cfg, cli), advisor.runtime_effort(cfg, cli)
        current = advisor.fingerprint(cli, model, effort)
        models = list(dict.fromkeys(([model] if model else []) + list(cards)))
        whitelist = (cards[model].kit_domains if model in cards else None)
        matrix, matrix_error = advisor.load_matrix()
        catalog = self.kit.domains_catalog() if self.kit and cli == 'claude' else []

        def check(raw):
            raw = self.drop_floor(raw)
            target = raw.get('model')
            wl = cards[target].kit_domains if target in cards else whitelist
            return advisor.validate(raw, cli=cli, models=models, current=current,
                                    catalog=[c['name'] for c in catalog], whitelist=wl, matrix=matrix, cards=cards)
        return dict(registry=registry, registry_error=registry_error, cards=cards, model=model, effort=effort,
                    current=current, models=models, matrix=matrix, matrix_error=matrix_error, catalog=catalog,
                    check=check)

    def advise(self, name, entry, cli, cfg, kind='dispatch', identity=''):
        """One consultation: (sheet, current, cards, registry) with the validated or fallback sheet, or None
        while the resident advisor has not answered (the caller keeps its message queued and asks again on a
        later pump through advice_poll; design §27). The one-shot runner answers at once (one retry).
        ``kind`` is reserved for the mid-task rescue path (phase 2: consult / respawn / reassign)."""
        ctx = self.advice_context(name, cli, cfg)
        info = self.agent(name)
        installed = json.loads(info.get('installed_domains') or '[]')
        preset = json.loads(info.get('advice_preset') or '{}')
        if (kind == 'dispatch' and entry.get('task') and preset.get('task') == entry['task']
                and preset.get('run_task') == self.task().get('id')):
            # The supervisor re-dispatches a task the advisor reassigned here: that sheet holds (no second consult).
            self.setf(name, advice_preset='')
            self.log('advice.preset', agent=name, ticket=preset['sheet']['ticket'], task=entry['task'])
            sheet = {k: v for k, v in preset['sheet'].items() if k not in ('action', 'to', 'handoff_hint')}
            return sheet, ctx['current'], ctx['cards'], ctx['registry']
        payload = advisor.build_input(
            kind=kind, goal=self.task().get('goal', ''), instruction=entry['instruction'], advise_note=entry.get('note', ''),
            slot={'name': name, 'cli': cli, 'generation': int(info.get('generation') or 0), 'model': ctx['model'],
                  'effort': ctx['effort'], 'installed_domains': installed,
                  'pinned_domains': self.floor_domains() if cli == 'claude' else []},
            models=ctx['models'], matrix=ctx['matrix'], catalog=ctx['catalog'])
        view = json.dumps({k: payload[k] for k in ('goal', 'instruction', 'advise_note', 'slot')},
                          ensure_ascii=False)[:4000]
        started = time.time()
        if self.advisor_resident() and not ctx['registry_error']:
            ticket = self.new_ticket()
            if self.advisor_request(ticket, name, kind, payload, view):
                self.setf(name, advice_pending=json.dumps({'ticket': ticket, 'msg': identity, 'kind': kind},
                                                          ensure_ascii=False))
                return None
            sheet, envelope, errors = None, None, ['advisor tab could not be started']
        else:
            runner = self.advisor_runner or (lambda data: advisor.run_claude(data, cfg=self.cfg, run_dir=self.run_dir))
            if ctx['registry_error']:
                sheet, envelope, errors = None, None, [ctx['registry_error']]
            else:
                sheet, envelope, errors = advisor.consult(payload, runner, ctx['check'])
            ticket = self.new_ticket()
        return self.advice_finish(name, kind, ticket, sheet, envelope, errors, ctx, view, started)

    def advice_finish(self, name, kind, ticket, sheet, envelope, errors, ctx, view, started):
        """Close a pre-dispatch consultation: the fallback sheet when there is no valid one (§8), the audit hash
        fx:advice:<ticket>, the supervisor note, the slot's advice_last."""
        info = self.agent(name)
        model, effort, models = ctx['model'], ctx['effort'], ctx['models']
        unavailable = sheet is None
        if unavailable:
            last = json.loads(info.get('advice_last') or '{}')
            if last.get('task') == self.task().get('id') and last.get('sheet', {}).get('model') in models:
                sheet = dict(last['sheet'], reason='advisor unavailable: previous sheet of this task')
            else:  # the slot's running runtime, no domains: never forces a respawn (§8 reading, see report)
                sheet = {'model': model, 'effort': effort, 'domains': [], 'reason': 'advisor unavailable: running runtime, domains []'}
        sheet['ticket'] = ticket
        audit = {'slot': name, 'task': self.task().get('id', ''), 'kind': kind, 'input': view,
                 'output': json.dumps(sheet, ensure_ascii=False), 'errors': json.dumps(errors, ensure_ascii=False),
                 'elapsed_s': round(time.time() - started, 3), 'status': 'unavailable' if unavailable else 'ok',
                 'at': time.time()}
        audit.update(advisor.usage_fields(envelope))
        if ctx['matrix_error']:
            audit['matrix_error'] = ctx['matrix_error'][:500]
        self.r.hset(ADVICE + ticket, mapping=audit)
        self.advice_recent(ticket)
        if unavailable:
            self.log('advice.unavailable', agent=name, ticket=ticket, errors=errors)
            self.r.rpush(ADVICE_NOTES, '[advisor unavailable] 按缺省派出 ' + self.label(name) + '（' + sheet['reason'] + '）')
        if sheet.get('capability_gap'):
            self.log('capability.gap', agent=name, ticket=ticket, capability=sheet.get('capability'), model=sheet['model'])
        self.setf(name, advice_last=json.dumps({'task': self.task().get('id', ''), 'sheet': sheet}, ensure_ascii=False))
        return sheet, ctx['current'], ctx['cards'], ctx['registry']

    def advice_poll(self, name, pending):
        """A pre-dispatch consultation out with the resident advisor: None while it is still out, else what
        advise() returns for it (the validated sheet, or the fallback after one correction / the time limit)."""
        cfg, ctx = self.runtime_cfg(name), {}

        def context():  # read the cards / matrix only once there is an answer to check (not on every pump)
            if not ctx:
                ctx.update(self.advice_context(name, agent_cli(cfg, name), cfg))
            return ctx
        state, sheet, errors = self.advisor_poll(pending['ticket'], lambda raw: context()['check'](raw))
        if state == 'wait':
            return None
        self.setf(name, advice_pending='')
        t = self.r.hgetall(ADVICE + pending['ticket'])
        return self.advice_finish(name, pending.get('kind') or 'dispatch', pending['ticket'], sheet, None, errors,
                                  context(), t.get('input', ''), float(t.get('started') or time.time()))

    def advice_pending_for(self, name, identity):
        """The resident consultation this queued message waits for, or None. A pending consultation of another
        message (that message left the queue) is abandoned."""
        pending = json.loads(self.agent(name).get('advice_pending') or 'null')
        if pending and pending.get('msg') != identity:
            self.drop_advice_pending(name, 'the advised message left the queue')
            return None
        return pending

    def drop_advice_pending(self, name, why):
        pending = json.loads(self.agent(name).get('advice_pending') or 'null')
        if pending:
            self.setf(name, advice_pending='')
            self.advisor_abandon(pending['ticket'], why)

    # -- resident advisor tab (docs/design.md §27): consultations are queued to it and read back later
    def new_ticket(self):
        seq = self.r.hincrby(ADVICE_SEQ, 'n', 1)
        return 'adv-' + str(self.cfg.get('token') or self.run_dir.name[-8:]) + '-' + str(seq)

    def advice_recent(self, ticket):
        """Tickets the advisor's handoff skeleton lists (sheets and reasons only, never worker text)."""
        if self.advisor_resident():
            self.r.rpush(ADVICE_RECENT, ticket)
            self.r.ltrim(ADVICE_RECENT, -10, -1)

    def advisor_send(self, ticket, request, error=''):
        """Queue one consultation (or its correction) on the advisor's bus inbox; returns the message id."""
        head = ('[Advisor correction ' + ticket + '] The daemon refused your last sheet: ' + ' '.join(error.split())[:500]
                + '\nReply again with one corrected JSON sheet for this input.' if error
                else '[Advisor consultation ' + ticket + ']')
        return self.v2.queue(ADVISOR, head + '\n' + request, request_id=ticket, kind='advisor')

    def advisor_request(self, ticket, slot, kind, payload, view):
        """Queue a consultation for the resident advisor; its audit hash fx:advice:<ticket> is its state.
        False when the tab cannot take it (it could not be started): the caller falls back at once (§8)."""
        info = self.agent(ADVISOR)
        if not self.advisor_enabled() or info.get('spawn_failed') == '1':
            return False
        now, state = time.time(), info.get('state') or 'not started'
        ahead = self.inbox_depth(ADVISOR) + (state == 'busy')
        request = json.dumps(payload, ensure_ascii=False)
        mid = self.advisor_send(ticket, request)
        self.r.hset(ADVICE + ticket, mapping={
            'slot': slot, 'task': self.task().get('id', ''), 'kind': kind, 'input': view, 'request': request,
            'status': 'pending', 'mid': mid, 'attempt': 1, 'phase': 'queued', 'deadline': awake.deadline(ADVICE_WAIT),
            'started': now, 'errors': '[]', 'at': now})
        self.log('advice.queued', agent=slot, ticket=ticket, path=kind, advisor=state, ahead=ahead)
        if state != 'idle' or ahead:  # merge seam M1: say once why the supervisor hears nothing for a while
            why = ('顾问未就绪（' + state + '）' if state not in ('idle', 'busy') else '前面还有 ' + str(ahead) + ' 条会诊')
            self.r.rpush(ADVICE_NOTES, '[advisor] waiting: ' + self.label(slot) + ' 的 ' + kind + ' 会诊排队中，' + why
                         + '；最多等 ' + str(ADVICE_WAIT) + ' 秒，超时按缺省')
        return True

    def advisor_poll(self, ticket, check, request=None):
        """('wait' | 'ok' | 'unavailable', sheet or None, errors) of a resident consultation. An unusable answer
        gets one correction; a second one, the time limit, or a failed / cancelled advisor turn: unavailable.
        ``request``: the input as it is now (rescue: idle slots may have changed), sent with the correction."""
        t = self.r.hgetall(ADVICE + ticket)
        errors = json.loads(t.get('errors') or '[]')
        now = time.time()
        if t.get('status') == 'answered':
            try:
                return 'ok', check(advisor.sheet_text({'result': t.get('raw') or ''})), errors
            except (advisor.AdviceError, ValueError, KeyError, TypeError) as exc:
                errors.append(str(exc)[:500])
            attempt = int(t.get('attempt') or 1)
            if attempt >= advisor.ATTEMPTS or not self.advisor_enabled():
                return 'unavailable', None, errors
            request = request or t.get('request') or ''
            mid = self.advisor_send(ticket, request, error=errors[-1])
            self.r.hset(ADVICE + ticket, mapping={'status': 'pending', 'mid': mid, 'attempt': attempt + 1, 'request': request,
                                                  'phase': 'queued', 'deadline': awake.deadline(ADVICE_WAIT), 'raw': '',
                                                  'errors': json.dumps(errors, ensure_ascii=False)})
            self.log('advice.correction', ticket=ticket, attempt=attempt + 1, error=errors[-1])
            return 'wait', None, errors
        if t.get('status') != 'pending':  # failed (a wall), abandoned, or already closed
            return 'unavailable', None, errors
        msg = self.v2.bus.message(t['mid']) if t.get('mid') else {}
        status = msg.get('status')
        if status in ('uncertain', 'expired') or (status == 'committed' and msg.get('outcome') != 'completed'):
            # The advisor turn failed, was held (fault, closed tab) or cancelled (restart / resume): §8 at once.
            errors.append('advisor turn ' + (msg.get('outcome') or status) + ': '
                          + str(msg.get('uncertain_reason') or msg.get('cancel_reason') or '')[:300])
            return 'unavailable', None, errors
        if self.agent(ADVISOR).get('spawn_failed') == '1':
            errors.append('advisor tab could not be started')
            return 'unavailable', None, errors
        if status in ('offered', 'received') and t.get('phase') == 'queued':
            # The advisor has it now: the time limit starts again for the answer itself.
            self.r.hset(ADVICE + ticket, mapping={'phase': 'answering', 'deadline': awake.deadline(ADVICE_WAIT)})
            return 'wait', None, errors
        if awake.passed(t.get('deadline') or 0):  # awake time (§35)
            errors.append('advisor gave no sheet within ' + str(ADVICE_WAIT) + ' s ('
                          + ('waiting for the advisor' if t.get('phase') == 'queued' else 'answering') + ')')
            if status == 'queued':
                self.advisor_cancel(t['mid'], 'consultation timed out')
            return 'unavailable', None, errors
        return 'wait', None, errors

    def advisor_cancel(self, mid, why):
        if self.v2 and self.r.hget(self.v2.bus.msg_key(mid), 'status') == 'queued':
            self.v2.cancel_queued(ADVISOR, {'message_id': mid}, why)

    def advisor_abandon(self, ticket, why):
        """Nobody waits for this consultation any more: closed as unavailable, its queued message withdrawn."""
        t = self.r.hgetall(ADVICE + ticket)
        if t.get('status') not in ('pending', 'answered'):
            return
        errors = json.loads(t.get('errors') or '[]') + [why]
        self.r.hset(ADVICE + ticket, mapping={'status': 'unavailable', 'effect': 'abandoned',
                                              'errors': json.dumps(errors, ensure_ascii=False)})
        if t.get('mid'):
            self.advisor_cancel(t['mid'], why)
        self.log('advice.abandoned', ticket=ticket, reason=why)

    def advisor_settled(self, ticket, mid, text):
        """The advisor's turn for one consultation ended (RuntimeV2.settle, inside its transaction): keep the raw
        answer for the waiting slot to validate. Never a report, never task state (§27)."""
        t = self.r.hgetall(ADVICE + ticket)
        if t.get('status') != 'pending' or t.get('mid') != mid:
            self.log('advice.late', ticket=ticket, message_id=mid)  # timed out / abandoned meanwhile: ignored
            return
        if len(text.strip()) <= brake.TURN_WALL_MAX and brake.classify_wall(text):
            # A usage wall instead of a sheet: this consultation falls back now, the tab restarts in place.
            errors = json.loads(t.get('errors') or '[]') + ['advisor hit a usage wall: ' + ' '.join(text.split())[:200]]
            self.r.hset(ADVICE + ticket, mapping={'status': 'failed', 'errors': json.dumps(errors, ensure_ascii=False)})
            self.setf(ADVISOR, advisor_fault='usage wall: ' + ' '.join(text.split())[:200])
            return
        self.r.hset(ADVICE + ticket, mapping={'status': 'answered', 'raw': text[-20000:]})

    def advisor_skeleton(self, info, t):
        """The advisor's handoff note (§27): recent sheets and their reasons from fx:advice:*, no worker text."""
        done = []
        for ticket in self.r.lrange(ADVICE_RECENT, 0, -1):
            a = self.r.hgetall(ADVICE + ticket)
            if a:
                done.append(ticket + ' ' + a.get('kind', '') + ' ' + a.get('slot', '') + ' [' + a.get('status', '')
                            + ']: ' + ' '.join((a.get('output') or '').split())[:400])
        return handoff.build_skeleton(
            info['handoff_id'], SESSION, 'fleet advisor: answer each consultation with one configuration sheet '
            '(advisor.md); the lowest model / effort that is enough, the smallest set of domains',
            done_lines=done, inprogress='none (handoff happens at an idle boundary)',
            next_lines=['read this note, reply READY, then answer each consultation the daemon sends on its own input'],
            pending='none', task=t.get('id', ''), hop=int(info['generation']) + 1,
            key_files=[str(self.run_dir / 'advisor.md')],
            risks=['earlier sheets are history, not input: never copy one without checking the matrix again',
                   'zero memory: this note carries no worker conversation, only the daemon\'s audit of sheets'])

    def advisor_start(self):
        """The first generation of the advisor tab (run start, or the switch turned on), in the supervisor's
        pane. A failed spawn falls back every consultation at once (§8) and is retried by advisor_tick."""
        info = self.agent(ADVISOR)
        generation = int(info['generation']) + 1 if info.get('generation') else 0
        self.setf(ADVISOR, label='advisor' if generation == 0 else slot_base(ADVISOR) + '.' + str(generation),
                  generation=generation, handoff_id='', restart_at=awake.deadline(ADVISOR_RESTART_GAP))
        self.advisor_slot = True
        self.names = self.slot_names()
        try:
            pane = self.spawn(ADVISOR, 'You are the fleet advisor ' + self.label(ADVISOR) + '. There is no '
                              'consultation yet: your final reply must be exactly READY.')
        except (Exception, SystemExit) as exc:
            self.setf(ADVISOR, state='dead', spawn_failed=1, launch_ready=0)
            self.log('advisor.spawn_failed', level='error', error=str(exc)[:500])
            return
        self.place_advisor(pane)
        self.refresh_history()
        self.log('advisor.started', generation=generation, pane=pane)

    def place_advisor(self, pane):
        """The advisor's tab joins the supervisor's pane (after the supervisor tab): the fixed explicit window."""
        sup = self.agent('supervisor').get('pane')
        if not (sup and self.mgr.tab_into(pane, sup, before=False)):
            self.log('advisor.placement_failed', level='warning', pane=pane, supervisor=sup or '')

    def advisor_tick(self):
        """Resident advisor tab lifecycle, outside any v2 transaction (§27): start it when the switch turns on,
        retire it once its current consultation is done when the switch turns off, restart it in place after
        a fault (watchdog hold, closed tab, wall, startup timeout / failure). Never pauses or blocks the fleet."""
        if not self.advisor_resident():
            return
        on, info = self.advisor_enabled(), self.agent(ADVISOR)
        if not self.advisor_slot:
            if on:
                self.advisor_start()
            return
        busy = self.r.hget(self.v2.bus.lease_key(ADVISOR), 'status') in ('offered', 'received')
        if not on:
            if not busy and info.get('state') not in ('busy', 'handoff'):
                self.advisor_retire()
            return
        failed = info.get('state') in ('dead', 'held') or info.get('advisor_fault')
        if failed and not self.paused() and awake.passed(info.get('restart_at') or 0):
            self.advisor_restart(info.get('advisor_fault') or ('startup failed' if info.get('state') == 'dead'
                                                                else 'held'))

    def advisor_retire(self):
        info = self.agent(ADVISOR)
        for mid in self.v2.bus.pending(ADVISOR):  # their tickets were abandoned when the switch went off
            self.v2.cancel_queued(ADVISOR, {'message_id': mid}, 'advisor turned off')
        if info.get('pane') and not self.v2.retire(ADVISOR, 'advisor turned off'):
            self.log('advisor.retire_failed', level='error', pane=info['pane'])
            return
        history.record(self.run_dir, slot=ADVISOR, label=self.label(ADVISOR), generation=int(info.get('generation') or 0),
                       handoff='', snapshot='', source='retired', timed_out=False)
        self.setf(ADVISOR, state='retired', pane='', launch_ready=0, runtime_ready=0)
        self.advisor_slot = False
        self.names = self.slot_names()
        self.refresh_history()
        self.log('advisor.retired')

    def advisor_restart(self, reason, source='advisor-restart'):
        """Next generation of the advisor tab next to the supervisor: after a fault, for a §22 handoff
        (``source='handoff'``) and on start --resume. Every failure here is an advisor fault (design §27 §2c-6):
        nothing raises into the daemon, nothing blocks the task, pauses the fleet or holds another slot. On
        failure the queued consultations are withdrawn (their slots fall back to the default on their next
        pump) and advisor_tick tries the next generation again after ADVISOR_RESTART_GAP."""
        info = self.agent(ADVISOR)
        generation, old, prev = int(info.get('generation') or 0), info.get('pane'), self.label(ADVISOR)
        self.setf(ADVISOR, restart_at=awake.deadline(ADVISOR_RESTART_GAP), advisor_fault='', handoff_id='')
        try:
            alive = bool(old) and self.mgr.get_pane_status(old) is not None
            hid = ('handoff-' if source == 'handoff' else 'restart-') + uuid.uuid4().hex[:8]
            note = self.run_dir / 'handoffs' / (ADVISOR + '-g' + str(generation) + '-' + hid + '.md')
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(self.skeleton(ADVISOR, dict(info, handoff_id=hid)))
            snapshot = ''
            if alive:  # retire first: two advisor sessions never run at once
                snapshot = str(note.with_suffix('.terminal.txt'))
                self.mgr.capture_to(old, snapshot)
                if not self.mgr.kill_pane(old) or self.mgr.get_pane_status(old) is not None:
                    return self.advisor_failed('held', reason, 'old advisor tab ' + prev + ' did not close')
            self.v2.confirm_retired(ADVISOR)  # its in-flight consultation is cancelled: that slot falls back (§8)
            history.record(self.run_dir, slot=ADVISOR, label=prev, generation=generation, handoff=str(note),
                           snapshot=snapshot, source=source, timed_out=source != 'handoff')
            label = slot_base(ADVISOR) + '.' + str(generation + 1)
            self.setf(ADVISOR, generation=generation + 1, label=label)
            why = ('took over at its context threshold' if source == 'handoff' else
                   'the advisor tab ' + prev + ' failed: ' + ' '.join(reason.split())[:200])
            pane = self.spawn(ADVISOR, '[Advisor takeover] You are ' + label + ' (' + why + '). Read the handoff note '
                              + str(note) + '. In this turn only read the note and make your final reply exactly READY.')
        except (Exception, SystemExit) as exc:
            return self.advisor_failed('dead', reason, str(exc))
        self.place_advisor(pane)
        try:
            self.refresh_history()
        except Exception as exc:  # the page is cosmetic: never an advisor failure
            self.log('advisor.history_failed', level='warning', error=str(exc)[:300])
        self.log('advisor.restarted', prev=prev, label=label, reason=reason, source=source)

    def advisor_failed(self, state, reason, error):
        """The next advisor generation could not be started: dead (no tab) or held (old tab still there).
        Queued consultations fall back now; advisor_tick retries after the gap."""
        fields = dict(state=state, launch_ready=0, runtime_ready=0, advisor_fault=('restart failed: ' + error)[:300])
        if state == 'dead':
            fields['spawn_failed'] = 1
        self.setf(ADVISOR, **fields)
        try:
            for mid in self.v2.bus.pending(ADVISOR):
                self.v2.cancel_queued(ADVISOR, {'message_id': mid}, 'advisor restart failed')
        except Exception as exc:
            self.log('advisor.cancel_failed', level='error', error=str(exc)[:300])
        self.log('advisor.restart_failed', level='error', reason=reason, error=error[:500])

    def advice_launch(self, cli, sheet, cards, registry):
        card = cards[sheet['model']]
        extras = {'effort': sheet['effort']} if cli == 'claude' and sheet.get('effort') else None
        return profile_launch.resolve_launch(card, registry, self.cfg, extras=extras)

    def record_advice(self, name, sheet, effect):
        shown = sheet['model'] + '/' + (sheet.get('effort') or '-') + '/' + (','.join(sheet['domains']) or '-')
        self.setf(name, advice_ticket=sheet['ticket'], advice_reason=sheet['reason'], advice_display=shown)
        self.r.hset(ADVICE + sheet['ticket'], mapping={'effect': effect})
        self.log('advice', agent=name, ticket=sheet['ticket'], model=sheet['model'], effort=sheet.get('effort', ''),
                 domains=sheet['domains'], effect=effect)
        self.r.rpush(ADVICE_NOTES, advisor.notice(self.label(name), sheet, effect))

    def advice_waiting(self, name, identity):
        """The sheet a slot holds while its model is at max_concurrent (this queued message only), or None."""
        waiting = json.loads(self.agent(name).get('advice_wait') or '{}')
        return waiting.get('sheet') if identity and waiting.get('msg') == identity else None

    def advice_gate(self, name, identity, sheet, card, registry):
        """Fleet.card_gate before an advisor sheet starts a generation (same meaning as deliver()'s
        Phase 5 switch): host mismatch raises (the caller blocks the task, no silent swap); a full
        model keeps the message queued, profile_state=pending, audits profile.capacity_wait once per
        reason and returns True (retried every tick). None = may go on."""
        gate = self.card_gate(name, card, registry)
        if gate and gate[0] == 'block':
            raise ValueError(gate[1])
        info = self.agent(name)
        if gate:
            if info.get('capacity_wait') != gate[1]:
                self.log('profile.capacity_wait', agent=name, profile=card.id if card else '', reason=gate[1])
            self.setf(name, profile_state='pending', profile_reason=gate[1], capacity_wait=gate[1],
                      advice_wait=json.dumps({'msg': identity, 'sheet': sheet}, ensure_ascii=False))
            return True
        if info.get('capacity_wait') or info.get('advice_wait'):
            self.setf(name, capacity_wait='', advice_wait='')
        return None

    def advice_switch(self, name, identity, sheet, launch, card, registry):
        """Respawn for an advisor sheet once card_gate admits it; False = never deliver now.

        The sheet becomes the slot's launch record (runtime_launch via switch_profile, advice_domains for
        the successor's kit) only when a successor really starts. A full model, or a codex slot whose
        native subagents still run (switch_profile defers without spawning), keeps the sheet in
        advice_wait for this message: retried every tick, never re-advised, never delivered to the old
        runtime as if advised."""
        if self.advice_gate(name, identity, sheet, card, registry):
            return False
        self.setf(name, advice_domains=json.dumps(sheet['domains'], ensure_ascii=False))
        try:
            self.switch_profile(name, launch)
        except BaseException:
            self.setf(name, advice_domains='')  # nothing launched with it: never staged for another spawn
            raise
        if self.agent(name).get('state') == 'starting':
            self.record_advice(name, sheet, 'respawn')
        else:
            self.setf(name, advice_domains='', advice_wait=json.dumps({'msg': identity, 'sheet': sheet}, ensure_ascii=False))
        return False

    def drop_advice_wait(self, name):
        """The advisor went off while this slot's sheet waited (model ticket / codex subagents): nothing was
        launched with it, so the slot keeps its record and the queued message goes to its running runtime."""
        if self.agent(name).get('advice_wait'):
            self.setf(name, advice_wait='', capacity_wait='', profile_state='', profile_reason='')
            self.log('advice.wait_dropped', agent=name)

    def advise_before_delivery(self, name, text):
        """True = go on delivering (no change, or a hot-load queued); False = respawning / blocked."""
        identity = self.advice_identity(name, text)
        info = self.agent(name)
        waiting = self.advice_waiting(name, identity)
        if waiting is not None:  # a decided respawn waiting for its model ticket: gate again, never re-advise
            cli = agent_cli(self.runtime_cfg(name), name)
            try:
                registry = AgentRegistry()
                cards = advisor.launch_cards(registry, cli)
                return self.advice_switch(name, identity, waiting, self.advice_launch(cli, waiting, cards, registry),
                                          cards[waiting['model']], registry)
            except (OSError, ValueError, RuntimeError, KeyError, SystemExit, subprocess.SubprocessError) as exc:
                self.block_profile(name, str(exc))
                return False
        cfg = self.runtime_cfg(name)
        cli = agent_cli(cfg, name)
        pending = self.advice_pending_for(name, identity)
        if pending is not None:  # out with the resident advisor (§27): the message stays queued meanwhile
            result = self.advice_poll(name, pending)
        else:
            if identity and info.get('advice_msg') == identity:
                return True  # already advised (e.g. delivered again after the respawn's READY)
            entry = self.take_pending_advice(name, text)
            if entry is None:
                return True
            self.setf(name, advice_msg=identity)
            result = self.advise(name, entry, cli, cfg, identity=identity)
        if result is None:
            return False
        sheet, current, cards, registry = result
        installed = json.loads(info.get('installed_domains') or '[]')
        target = advisor.fingerprint(cli, sheet['model'], sheet.get('effort'))
        effect = advisor.decide(current, target, installed, sheet['domains'])
        try:
            if effect == 'respawn':
                launch = self.advice_launch(cli, sheet, cards, registry)
                # Phase 5 path behind the model card gate; blocks the task itself on failure
                return self.advice_switch(name, identity, sheet, launch, cards[sheet['model']], registry)
            frozen = info.get('runtime_launch')
            if frozen and sheet['model'] in cards and json.loads(frozen).get('profile_id') == cards[sheet['model']].id:
                drift = profile_runtime.launch_drift(frozen, self.advice_launch(cli, sheet, cards, registry))
                if drift:
                    raise ValueError('advisor launch ' + drift + ' changed since spawn; submit a new task after repair')
        except (OSError, ValueError, RuntimeError, KeyError, SystemExit, subprocess.SubprocessError) as exc:
            self.block_profile(name, str(exc))
            return False
        if effect == 'hotload':
            new = [d for d in sheet['domains'] if d not in installed]
            self.setf(name, advice_hotload=json.dumps(new, ensure_ascii=False))
        self.record_advice(name, sheet, effect)
        return True

    def apply_hotload(self, name, text):
        """Link the new domains' skills / agents and put their rules in front of this instruction."""
        new = json.loads(self.agent(name).get('advice_hotload') or '[]')
        installed = json.loads(self.agent(name).get('installed_domains') or '[]')
        linked = self.kit.link_domains(agent_cwd(self.cfg, name), new) if self.kit else {'skills': [], 'agents': []}
        prefix = ((self.kit.rules_text(new).lstrip() if self.kit else '')
                  + '\n[本任务领域：' + ', '.join(new) + '；已链入 skill：' + (', '.join(linked['skills']) or '无')
                  + '；agent：' + (', '.join(linked['agents']) or '无') + ']\n\n')
        text = prefix + text
        if self.v2:  # the TUI receives the bus body, so rewrite it before the offer
            from fleetmux.message_bus import _json
            mid = self.v2.bus.pending(name, 1)[0]
            body = self.v2.bus.message(mid)['body']
            body['text'] = prefix + body['text']
            self.r.hset(self.v2.bus.msg_key(mid), mapping={'body': _json(body)})
        self.setf(name, advice_hotload='', installed_domains=json.dumps(installed + new, ensure_ascii=False))
        self.log('advice.hotload', agent=name, domains=new, linked=linked)
        return text

    def release_hold(self, name):
        """First dispatch to a held Claude worker: write its launch from the sheet, install, open the gate."""
        if self.v2:
            mid = self.v2.bus.pending(name, 1)[0]
            text = self.v2.bus.message(mid)['body']['text']
        else:
            text = self.r.lrange(key('inbox', name), 0, 0)[0]
        identity = self.advice_identity(name, text)
        sheet = self.advice_waiting(name, identity)
        try:
            if sheet is None:
                pending = self.advice_pending_for(name, identity)
                if pending is not None:
                    result = self.advice_poll(name, pending)
                else:
                    entry = self.take_pending_advice(name, text) or {'instruction': text[:2000], 'note': ''}
                    result = self.advise(name, entry, 'claude', self.runtime_cfg(name), identity=identity)
                    self.setf(name, advice_msg=identity)
                if result is None:
                    return  # held until the resident advisor answers, at most ADVICE_WAIT (§27)
                if self.advisor_resident() and self.r.hget(ADVICE + result[0]['ticket'], 'status') == 'unavailable':
                    # The advisor tab could not start or did not answer in time (180 s): the user's rule is the
                    # plain default launch; the [advisor unavailable] note is already queued (advice_finish).
                    self.release_default(name)
                    return
                sheet, current, cards, registry = result
            else:  # waited for a model ticket with this sheet: gate again, never re-advise
                registry = AgentRegistry()
                cards = advisor.launch_cards(registry, 'claude')
            launch = self.advice_launch('claude', sheet, cards, registry) if sheet['model'] in cards else {}
            # An unreadable registry (advisor unavailable) has no cards to gate; the fallback never switches.
            if (not isinstance(registry, tuple)
                    and self.advice_gate(name, identity, sheet, cards[sheet['model']] if launch else None, registry)):
                return  # model full: keep holding the first generation until a ticket frees up
            # Admitted: only now does the sheet become this slot's launch record (staged domains + launch).
            self.setf(name, advice_domains=json.dumps(sheet['domains'], ensure_ascii=False))
            generation = int(self.agent(name).get('generation') or 0)
            if launch:
                self.setf(name, runtime_launch=profile_launch.encode_launch(launch), runtime_profile=launch['profile_id'],
                          profile_state='switching')
            cfg = self.runtime_cfg(name)
            save(self.run_dir / 'runtime' / (name + '-g' + str(generation) + '.json'), cfg)
            cwd = agent_cwd(self.cfg, name)
            scope = self.role_scope(name, cfg, launch)
            (cwd / 'AGENTS.md').write_text(scope)
            self.install_advised_kit(name, cwd, scope)
        except (OSError, ValueError, KeyError) as exc:
            self.setf(name, advice_hold=0)
            self.block_profile(name, 'advisor release failed: ' + str(exc), dead=True)
            return
        self.record_advice(name, sheet, 'launch')  # the held first generation starts with this sheet
        fields = dict(advice_hold=0, launch_ready=1)
        if self.v2:
            fields['startup_deadline'] = awake.deadline(self.cfg['profile_ready_timeout'])
        if launch:
            fields['profile_ready_deadline'] = awake.deadline(self.cfg['profile_ready_timeout'])
        self.setf(name, **fields)
        self.log('advice.released', agent=name, ticket=sheet['ticket'])

    def release_default(self, name):
        """The advisor went offline while this Claude slot waited for its first sheet: open its gate with
        the slot defaults, laid out like a spawn without the advisor (snapshot, CLAUDE.md, kit copy)."""
        generation = int(self.agent(name).get('generation') or 0)
        launch = json.loads(self.agent(name).get('runtime_launch') or '{}')
        cfg = self.runtime_cfg(name)
        cfg.pop('advisor', None)
        self.drop_advice_wait(name)
        # It launches with the slot defaults, and that is its record (installed_domains '' = default layout;
        # runtime_launch as it was). Only a held first generation gets here, so no running record is cleared.
        self.setf(name, installed_domains='', advice_domains='')
        try:
            save(self.run_dir / 'runtime' / (name + '-g' + str(generation) + '.json'), cfg)
            cwd = agent_cwd(self.cfg, name)
            scope = self.role_scope(name, cfg, launch)
            (cwd / 'AGENTS.md').write_text(scope)
            kitmod.Kit.clear(cwd)
            (cwd / 'CLAUDE.md').write_text(scope)
            self.install_default_kit(cwd, scope)
        except (OSError, ValueError) as exc:
            self.setf(name, advice_hold=0)
            self.block_profile(name, 'release without advisor failed: ' + str(exc), dead=True)
            return
        fields = dict(advice_hold=0, launch_ready=1)
        if self.v2:
            fields['startup_deadline'] = awake.deadline(self.cfg['profile_ready_timeout'])
        self.setf(name, **fields)
        self.log('advice.released_default', agent=name)

    # -- advisor mid-task rescue (advisor plan §3.2 / §4.2: consult -> respawn | reassign | give_up)
    def consult_decision(self, decision, state):
        """Validate the supervisor's consult items against the task table (no side effects)."""
        dispatched = {self.resolve(x['to']) for x in decision['dispatch']}
        ids = {x.get('id') for x in decision['dispatch']}
        waiting = {json.loads(x)['task'] for x in self.r.lrange(RESCUE, 0, -1)}
        out = []
        for item in decision['consult']:
            slot, task_id = self.resolve(item['worker']), item['task']
            owner, instruction, status = self.escalation_item(task_id, state)
            if owner != slot or not instruction or status in ('escalated', 'delegated'):
                raise ValueError('consult: ' + task_id + ' is not a task of ' + self.label(slot)
                                 + ' (consult names the dispatch id that worker last ran)')
            if slot in dispatched or task_id in ids:
                raise ValueError('consult: ' + task_id + ' / ' + self.label(slot) + ' is also dispatched in this '
                                 'reply; consult first, then act on the advisor\'s answer')
            if task_id in waiting:
                raise ValueError('consult: ' + task_id + ' is already with the advisor; wait for its answer')
            out.append({'slot': slot, 'task': task_id, 'finding': ' '.join(item['finding'].split())[:2000],
                        'want': ' '.join(item.get('want', '').split())[:1000]})
        return out

    def consult_apply(self, consults):
        for c in consults:  # offline: advisor_decision already answered with the one [advisor off] note
            if self.advisor_online():
                self.r.rpush(RESCUE, json.dumps(dict(c, run_task=self.task().get('id', '')), ensure_ascii=False))
                self.log('advice.consult', agent=c['slot'], task=c['task'])
            else:
                self.log('advice.consult_off', agent=c['slot'], task=c['task'])

    def rescue_tick(self):
        """Ready notes of reassigned slots; then one consult request whose worker is idle."""
        if not self.running():
            return
        for w in self.workers:
            info = self.agent(w)
            if info.get('rescue_note') and info.get('state') == 'idle':
                self.setf(w, rescue_note='')
                self.queue('supervisor', info['rescue_note'])
        pending = json.loads(self.r.get(RESCUE_PENDING) or 'null')
        if pending:  # the one rescue out with the resident advisor (§27): read its answer, nothing new meanwhile
            if pending['req'].get('run_task') != self.task().get('id'):
                self.r.delete(RESCUE_PENDING)
                self.advisor_abandon(pending['ticket'], 'left over from an earlier task')
            elif (self.r.hget(ADVICE + pending['ticket'], 'status') != 'pending'
                    or self.advisor_poll(pending['ticket'], None)[0] != 'wait'):
                self.rescue(pending['req'], pending)  # answered or over: rebuild its check and act
            return
        for raw in self.r.lrange(RESCUE, 0, -1):
            req = json.loads(raw)
            if req.get('run_task') != self.task().get('id'):
                self.r.lrem(RESCUE, 1, raw)  # left over from an earlier task
                continue
            if self.agent(req['slot']).get('state') != 'idle' or self.inbox_depth(req['slot']):
                continue  # judged at the worker's next idle boundary
            self.r.lrem(RESCUE, 1, raw)
            if not self.advisor_online():  # accepted while online, turned off before the worker went idle
                self.queue('supervisor', self.consult_off_note([req]) + '\nReply with the JSON format from AGENTS.md.')
            else:
                self.rescue(req)
            return  # at most one consultation per pump

    def advice_options(self, name, registry):
        cfg = self.runtime_cfg(name)
        cli = agent_cli(cfg, name)
        cards = advisor.launch_cards(registry, cli)
        model, effort = advisor.runtime_model(cfg, cli), advisor.runtime_effort(cfg, cli)
        info = self.agent(name)
        return {'name': name, 'cli': cli, 'cards': cards, 'model': model, 'effort': effort,
                'current': advisor.fingerprint(cli, model, effort),
                'models': list(dict.fromkeys(([model] if model else []) + list(cards))),
                'generation': int(info.get('generation') or 0),
                'installed_domains': json.loads(info.get('installed_domains') or '[]')}

    def rescue_candidates(self, name):
        """Idle slots a reassign may go to (a v2 Claude slot still held for its first sheet counts)."""
        out = []
        for w in self.active_workers():
            info = self.agent(w)
            held = (info.get('advice_hold') == '1' and info.get('state') == 'starting'
                    and info.get('launch_ready') == '0')
            if (w != name and (info.get('state') == 'idle' or held) and not self.inbox_depth(w)
                    and not info.get('rescue_note') and not info.get('rescue_relay')):
                out.append(w)
        return out

    def rescue(self, req, pending=None):
        """One rescue consultation. Unavailable advisor = no action, the supervisor is told (§8). With the
        resident advisor it is queued (RESCUE_PENDING) and acted on once answered, at the worker's idle
        boundary (``pending``: that consultation, read again on each pump)."""
        name, task_id, t = req['slot'], req['task'], self.task()
        state = self.contract_state()
        instruction = self.escalation_item(task_id, state)[1] or ''
        relay_key = t.get('id', '') + '|' + task_id + '|' + name
        relays = int(self.r.hget(RESCUE_RELAYS, relay_key) or 0)
        try:
            registry, registry_error = AgentRegistry(), None
        except (OSError, ValueError) as exc:
            registry, registry_error = (), 'agent registry unreadable: ' + str(exc)[:400]
        candidates = self.rescue_candidates(name)
        opts = {w: self.advice_options(w, registry) for w in [name] + candidates}
        actions = advisor.rescue_actions(relays, candidates)
        matrix, matrix_error = advisor.load_matrix()
        catalog = self.kit.domains_catalog() if self.kit else []
        names = [c['name'] for c in catalog]

        def view(o, models=False):
            out = {k: o[k] for k in ('name', 'cli', 'generation', 'model', 'effort', 'installed_domains')}
            out['pinned_domains'] = self.floor_domains() if o['cli'] == 'claude' else []
            return dict(out, models=o['models']) if models else out
        last = json.loads(self.agent(name).get('advice_last') or '{}')
        payload = advisor.build_rescue_input(
            goal=t.get('goal', ''), task=task_id, instruction=instruction, finding=req['finding'], want=req['want'],
            slot=view(opts[name], True), previous_sheet=last.get('sheet') if last.get('task') == t.get('id') else None,
            relays=relays, actions=actions, candidates=[view(opts[c], True) for c in candidates],
            matrix=matrix, catalog=catalog)

        def check_slot(target, raw):
            raw = self.drop_floor(raw)
            o = opts[target or name]
            wl = (o['cards'][raw.get('model')].kit_domains if raw.get('model') in o['cards']
                  else o['cards'][o['model']].kit_domains if o['model'] in o['cards'] else None)
            return advisor.validate(raw, cli=o['cli'], models=o['models'], current=o['current'],
                                    catalog=names if o['cli'] == 'claude' else [], whitelist=wl, matrix=matrix,
                                    cards=o['cards'])

        def check(raw):
            return advisor.validate_rescue(raw, actions=actions, check_slot=check_slot, candidates=candidates)
        shown = json.dumps({k: payload[k] for k in ('goal', 'task', 'instruction', 'finding', 'want', 'slot',
                                                    'relays', 'actions')}, ensure_ascii=False)[:4000]
        started, envelope = time.time(), None
        if pending is not None:
            if (self.r.hget(ADVICE + pending['ticket'], 'status') == 'answered'
                    and (self.agent(name).get('state') != 'idle' or self.inbox_depth(name))):
                return  # answered: acted on at the worker's next idle boundary
            # The correction carries the input as it is now: a reassign target that went busy meanwhile is
            # refused against the current idle slots and the advisor sees those (review astra r1 P2-01).
            state_, sheet, errors = self.advisor_poll(pending['ticket'], check, json.dumps(payload, ensure_ascii=False))
            if state_ == 'wait':
                return
            self.r.delete(RESCUE_PENDING)
            ticket = pending['ticket']
            started = float(self.r.hget(ADVICE + ticket, 'started') or started)
        elif self.advisor_resident() and not registry_error:
            ticket = self.new_ticket()
            if self.advisor_request(ticket, name, 'rescue', payload, shown):
                self.r.set(RESCUE_PENDING, json.dumps({'ticket': ticket, 'req': req}, ensure_ascii=False))
                return
            sheet, errors = None, ['advisor tab could not be started']
        else:
            runner = self.advisor_runner or (lambda data: advisor.run_claude(data, cfg=self.cfg, run_dir=self.run_dir))
            if registry_error:
                sheet, errors = None, [registry_error]
            else:
                sheet, envelope, errors = advisor.consult(payload, runner, check)
            ticket = self.new_ticket()
        audit = {'slot': name, 'task': t.get('id', ''), 'kind': 'rescue', 'item': task_id,
                 'input': shown,
                 'output': json.dumps(sheet, ensure_ascii=False), 'errors': json.dumps(errors, ensure_ascii=False),
                 'elapsed_s': round(time.time() - started, 3), 'status': 'unavailable' if sheet is None else 'ok',
                 'action': (sheet or {}).get('action', 'none'), 'at': time.time()}
        audit.update(advisor.usage_fields(envelope))
        if matrix_error:
            audit['matrix_error'] = matrix_error[:500]
        self.r.hset(ADVICE + ticket, mapping=audit)
        self.advice_recent(ticket)
        who = task_id + ' (' + self.label(name) + ')'
        if sheet is None:
            self.log('advice.unavailable', agent=name, ticket=ticket, path='rescue', errors=errors)
            self.queue('supervisor', '[advisor unavailable] consult ' + who + '：顾问不可用，未做任何动作（'
                       + (errors[-1] if errors else '')[:300] + '）。Decide yourself: dispatch a correction, or BLOCKED.'
                       '\nReply with the JSON format from AGENTS.md.')
            return
        sheet['ticket'] = ticket
        self.log('advice.rescue', agent=name, ticket=ticket, task=task_id, action=sheet['action'])
        if sheet.get('capability_gap'):
            self.log('capability.gap', agent=name, ticket=ticket, capability=sheet.get('capability'), model=sheet['model'])
        if sheet['action'] == 'give_up':
            self.queue('supervisor', '[advisor] give_up ' + task_id + ': ' + sheet['reason']
                       + '\nNo configuration of ' + self.label(name) + ' or another slot is expected to finish '
                       + task_id + ': reply BLOCKED with this reason.\nReply with the JSON format from AGENTS.md.')
            return
        target = sheet.get('to', name)
        self.setf(target, advice_last=json.dumps({'task': t.get('id', ''), 'sheet': sheet}, ensure_ascii=False))
        try:
            if sheet['action'] == 'respawn':
                if self.rescue_respawn(name, task_id, sheet, opts[name], registry, state):
                    self.r.hincrby(RESCUE_RELAYS, relay_key, 1)
            else:
                self.rescue_reassign(name, task_id, sheet, opts[target], registry, state)
        except (OSError, ValueError, RuntimeError, KeyError, SystemExit, subprocess.SubprocessError) as exc:
            self.block_profile(target, 'advisor ' + sheet['action'] + ' failed: ' + str(exc))

    def rescue_message(self, task_id, state, slot):
        """The original instruction of a task, in the same envelope the supervisor's dispatch had."""
        t = self.task()
        if state is not None:
            row = state['tasks'][task_id]
            return row.get('message') or self.contract_message(t.get('id', ''), row)
        instruction = self.escalation_item(task_id, None)[1]
        return ('[Supervisor instruction ' + t.get('id', '') + '] [' + task_id + '] ' + instruction
                + '\nShared output directory: ' + str(self.workspace()) + ' (write only inside your own '
                'subdirectory ' + slot + '/). Reply in the AGENTS.md format when done.')

    def rescue_respawn(self, name, task_id, sheet, opt, registry, state):
        """Same slot, next generation: the old one writes the three-section handoff, the successor starts
        with the new sheet (finish_rotation), reads the note, returns READY, then gets the task again."""
        launch = None
        if advisor.fingerprint(opt['cli'], sheet['model'], sheet.get('effort')) != opt['current']:
            launch = self.advice_launch(opt['cli'], sheet, opt['cards'], registry)
            gate = self.card_gate(name, opt['cards'][sheet['model']], registry)  # same gate as pre-dispatch (P2-9)
            if gate and gate[0] == 'block':
                raise ValueError(gate[1])
            if gate:  # model full: nothing done, the relay is not counted; the supervisor decides
                self.r.hset(ADVICE + sheet['ticket'], mapping={'effect': 'capacity_wait'})
                self.log('profile.capacity_wait', agent=name, profile=launch['profile_id'], reason=gate[1], path='rescue')
                self.queue('supervisor', '[advisor] respawn ' + task_id + ' (' + self.label(name) + ') 未执行：'
                           + gate[1] + '。Decide yourself: dispatch a correction, consult again later, or BLOCKED.'
                           '\nReply with the JSON format from AGENTS.md.')
                return False
            self.preflight_profile(launch)  # Phase 5: dependencies first, never a fallback CLI
        text = (self.rescue_message(task_id, state, name) + '\n[advisor] 上一代已做：见交接单；本代领域：'
                + (', '.join(sheet['domains']) or '无') + '。')
        if state is not None:
            row = state['tasks'][task_id]
            row.update(status='running', attempts=row.get('attempts', 0) + 1, reason='advisor respawn')
            self.contract_queue(state, name, text, task_id)  # queued behind the handoff, delivered after READY
            self.save_contract(state)
        else:
            self.queue(name, text)
        self.setf(name, advice_domains=json.dumps(sheet['domains'], ensure_ascii=False),
                  rescue_hint=sheet.get('handoff_hint', ''), rescue_relay=1,
                  rescue_launch=profile_launch.encode_launch(launch) if launch else '')
        self.record_advice(name, sheet, 'respawn')
        used, window = self.usage(name)
        self.start_rotation(name, used, window, prompt=handoff.RESCUE_PROMPT)
        return True

    def rescue_reassign(self, name, task_id, sheet, opt, registry, state):
        """Take the task back from the stuck worker and start the target slot per the sheet; the
        supervisor re-dispatches (dispatch stays with the supervisor). The old worker gets no handoff."""
        to, t = sheet['to'], self.task()
        if state is not None:
            row = state['tasks'][task_id]
            row.update(status='running', attempts=row.get('attempts', 0) + 1,
                       reason='reassigned by the advisor: ' + self.label(name) + ' -> ' + self.label(to)
                       + '; waits for the supervisor to dispatch it again')
            self.contract_log('task.reassigned', task=task_id, by='advisor', frm=name, to=to)
            self.save_contract(state)
        else:
            items = json.loads(self.r.get(ITEMS) or '{}')
            items.setdefault(task_id, {})['to'] = ''
            self.r.set(ITEMS, json.dumps(items, ensure_ascii=False))
        shown = sheet['model'] + ('/' + sheet['effort'] if sheet.get('effort') else '')

        def note():  # after a switch the slot's label is the new generation's
            return ('[advisor] ' + task_id + ' → ' + self.label(to) + ' (' + shown + ', domains: ['
                    + ', '.join(sheet['domains']) + '])：reassign，' + self.label(name) + ' 不再负责 ' + task_id
                    + '；请重新 dispatch ' + task_id + ' 给 ' + self.label(to) + '（' + sheet['reason'] + '）'
                    + '\nReply with the JSON format from AGENTS.md.')
        # The re-dispatch of this task to `to` uses this sheet instead of a second consultation.
        self.setf(to, advice_preset=json.dumps({'task': task_id, 'run_task': t.get('id', ''), 'sheet': sheet},
                                               ensure_ascii=False))
        info = self.agent(to)
        held = info.get('advice_hold') == '1' and info.get('state') == 'starting'
        if not held and advisor.fingerprint(opt['cli'], sheet['model'], sheet.get('effort')) != opt['current']:
            launch = self.advice_launch(opt['cli'], sheet, opt['cards'], registry)
            gate = self.card_gate(to, opt['cards'][sheet['model']], registry)  # same gate as pre-dispatch (P2-9)
            if gate and gate[0] == 'block':
                raise ValueError(gate[1])
            if gate is None:
                self.setf(to, advice_domains=json.dumps(sheet['domains'], ensure_ascii=False))
                try:
                    self.switch_profile(to, launch)  # Phase 5 idle switch; the note goes out after its READY
                except BaseException:
                    self.setf(to, advice_domains='')
                    raise
                if self.agent(to).get('state') == 'starting':
                    self.record_advice(to, sheet, 'respawn')
                    self.setf(to, rescue_note=note())
                    return
                self.setf(to, advice_domains='')  # deferred (codex subagents): nothing launched with it
            # Model full or switch deferred: the preset carries the sheet; the re-dispatch applies it through
            # advice_gate / advice_switch like any pre-dispatch sheet. Tell the supervisor now.
            if self.task().get('status') == 'running':
                self.queue('supervisor', note())
            return
        self.r.hset(ADVICE + sheet['ticket'], mapping={'effect': 'reassign'})
        self.queue('supervisor', note())  # held slot: released by the re-dispatch with this sheet

    def send(self, name, pane, text):
        """Codex accepts verbatim paste; Claude and Pi use a file pointer with a delivery id."""
        if self.v2:
            raise ValueError('v2 deliveries use Redis authorization; file/paste fallback forbidden')
        if codex_queue.is_codex(self.runtime_cfg(name), name):
            # The TUI takes it from its own queue; on_turn settles only the turn the rollout binds to it.
            try:
                codex_queue.deliver(self, name, codex_queue.new_delivery_id('v1'), text)
            except codex_queue.QueueError:
                return False  # never falls back to typing
            return True
        # Claude's Stop hook reads the pointer from its transcript; Pi's extension captures it
        # from before_agent_start. Both bind each turn end to exactly one daemon delivery.
        box = agent_cwd(self.cfg, name) / '.inbox'
        box.mkdir(exist_ok=True)
        mid = 'msg-' + time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
        msg = box / (mid + '.md')
        msg.write_text(text)
        self.setf(name, msg_id=mid)
        first = ' '.join(text.split('\n', 1)[0].split())[:200]
        pointer = first + ' -- the full message is in ' + str(msg) + ' . Read that whole file first and follow it.'
        if claude_hook.enabled(self.runtime_cfg(name), name):
            # The Stop hook hands the pointer over; on_turn binds by the msg id in its feedback row.
            generation = int(self.agent(name).get('generation') or 0)
            k = claude_hook.keys('fx', name, generation)
            if not claude_hook.post(self.r, k, {'kind': 'pointer', 'msg_id': mid, 'generation': generation,
                                                 'text': pointer}):
                self.log('hook.unarmed', agent=name, msg_id=mid,
                         reason='no Stop hook waiting; the next turn end of this session picks it up')
            return True
        if is_claude(self.runtime_cfg(name), name):  # §48: never typed; validate_config refuses this config first
            self.log('send.refused', agent=name, msg_id=mid, reason='Claude agent without a Stop hook; '
                     + runtime_v2.NO_TYPING)
            return False
        return self.mgr.send_prompt_to_idle(pane, pointer)

    def start_rotation(self, name, used, window, prompt=None):
        info = self.agent(name)
        if info.get('state') == 'clearing' or info.get('clear_required') == '1':
            return
        if name == ADVISOR:
            # §22 handoff of the advisor (§27): it has only Read and cannot write a draft, so no handoff turn;
            # the daemon's skeleton (recent sheets) is its note. Failures stay advisor faults (advisor_restart).
            self.log('rotate.start', agent=name, used=used, window=window, generation=info.get('generation'))
            return self.advisor_restart('context ' + str(used) + '/' + str(window), source='handoff')
        hid = uuid.uuid4().hex[:10]
        draft = agent_cwd(self.cfg, name) / '.handoff' / ('draft-' + hid + '.md')
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(handoff.partial_template())
        self.setf(name, state='handoff', handoff_id=hid, handoff_draft=draft,
                  handoff_mtime=draft.stat().st_mtime_ns,
                  handoff_deadline=time.time() + fleet_config.handoff_timeout(self.cfg),
                  handoff_prompt=prompt or '')  # offered again if the session had no hook (offer_handoff_again)
        self.log('rotate.start', agent=name, used=used, window=window, generation=info.get('generation'))
        prompt = ((prompt or handoff.DUMP_PROMPT).format(path=draft, used=used, window=window)
                  + ' This is a same-task handoff: do not open windows, do not continue the task itself.')
        if self.v2:
            self.v2.offer_handoff(name, prompt)
            return
        if not self.send(name, info['pane'], prompt):
            self.finish_rotation(name, timed_out=True)

    def skeleton(self, name, info):
        t = self.task()
        if name == ADVISOR:
            return self.advisor_skeleton(info, t)
        replies = self.r.lrange(key('replies', name), 0, -1)
        pending = self.v2.pending_text(name) if self.v2 else self.r.lrange(key('inbox', name), 0, 0)
        back = json.loads(info.get('subfleet_return') or '{}')  # §29: the slot comes back from a named sub-fleet
        facts, open_ids = subfleet.return_lines(back) if back else ([], 'none')
        return handoff.build_skeleton(
            info['handoff_id'], SESSION, t.get('goal') or (
                ('resident service: route each request to a worker' if name == 'supervisor'
                 else 'resident service: answer each request as role ' + name) if self.service
                else '(no task yet, standing by)'),
            done_lines=facts + ['recent reply: ' + ' '.join(x.split())[:600] for x in replies],
            inprogress=(handoff.midturn_progress(info.get('midturn_message', ''), info['midturn_instruction'])
                        if info.get('midturn_instruction') else 'none (handoff happens at an idle boundary)'),
            next_lines=['read this note, reply READY, then wait for the daemon to deliver the next message'
                        + (' (pending: ' + ' '.join(pending[0].split())[:300] + ')' if pending else '')],
            pending=open_ids, task=t.get('id', ''), hop=int(info['generation']) + 1,
            key_files=[str(self.workspace()), str(agent_cwd(self.cfg, name) / 'AGENTS.md')],
            risks=['role ' + name + ' and task ' + t.get('id', 'none') + ' are unchanged',
                   'anything the model claims as done must be checked against the artifacts'])

    def finish_rotation(self, name, timed_out=False):
        codex_queue.clear(self, name, 'rotation')  # its token can never settle (Codex's queue is not edited)
        back = self.agent(name).get('subfleet_return')
        if self.v2 and timed_out and not (back and not self.v2.handoff_taken(name)):
            # (§29 return: a handoff the returned session never took ran nothing; retiring it needs no hold)
            # An unfinished handoff can still run tools. Retire it reliably before
            # using a skeleton or advancing generation; Escape alone proves nothing.
            self.v2.transaction('handoff-timeout-' + uuid.uuid4().hex,
                                lambda: self.v2.hold(name, 'handoff deadline expired'))
        info = self.agent(name)
        generation = int(info['generation'])
        base = self.skeleton(name, info)
        draft = Path(info['handoff_draft'])
        out = self.run_dir / 'handoffs' / (name + '-g' + str(generation) + '-' + info['handoff_id'] + '.md')
        out.parent.mkdir(parents=True, exist_ok=True)
        source = 'skeleton'
        if not timed_out and draft.exists() and draft.stat().st_mtime_ns > int(info['handoff_mtime']):
            out.write_text(handoff.merge_partial(base, draft.read_text()))
            source = 'model'
            if not handoff.validate(str(out))[0]:
                source = 'skeleton'
        if source == 'skeleton':
            out.write_text(base)  # never fail a rotation because the model did not write
        if info.get('rescue_hint'):  # advisor rescue: its handoff_hint goes after the merge, never dropped
            out.write_text(handoff.add_next_step(out.read_text(), info['rescue_hint']))
        old = info['pane']
        snapshot = out.with_suffix('.terminal.txt')
        self.mgr.capture_to(old, str(snapshot))
        prev = self.label(name)
        label = slot_base(name) + '.' + str(generation + 1)  # supervisor-1 -> supervisor-1.1, worker-1 -> worker-1.1
        self.setf(name, generation=generation + 1, handoff_id='', label=label)
        if name in self.workers:
            self.setf(name, prev_label=prev)
            self.r.hset(NAMES, mapping={label: name})  # old names keep routing to this slot
        history.record(self.run_dir, slot=name, label=prev, generation=generation, handoff=str(out),
                       snapshot=str(snapshot), source=source, timed_out=timed_out)
        if info.get('rescue_launch'):  # advisor rescue respawn: the successor starts with the new sheet's runtime
            self.setf(name, runtime_launch=info['rescue_launch'], rescue_launch='')
        if info.get('split_launch'):  # §44: the supervisor's pick (or, after the sub-fleet, the slot's own model)
            self.setf(name, runtime_launch=json.loads(info['split_launch'])['launch'], split_launch='')
        if back:  # §29: the successor is told where its predecessor was, not "same task"
            self.setf(name, subfleet_return='')
            prompt = subfleet.return_prompt(json.loads(back)['name'], str(out)).format(label=label)
        else:
            prompt = ('[Same-task takeover] You are ' + label + ' (taking over from ' + prev
                      + '). Read the handoff note ' + str(out) + ' and continue from it. In this turn only read '
                      'the note, do no task work, and make your final reply exactly READY.')
        new = self.spawn(name, prompt, launch_held=bool(self.v2))
        placed = self.mgr.tab_into(new, old)  # the successor takes the retiring session's place on the page
        if (self.v2 and not placed) or not self.mgr.kill_pane(old) or (self.v2 and self.mgr.get_pane_status(old) is not None):
            if self.v2:
                self.setf(name, state='dead', launch_ready=0, retired_pane=old,
                          retired_launch=info.get('runtime_launch') or '')  # the old pane's launch (before rescue_launch)
            if self.v2:
                self.v2.transaction('midturn-rotate-failed-' + uuid.uuid4().hex, lambda: self.v2.midturn_clear(name))
            self.finish('blocked', 'could not close the old session ' + prev + '; two sessions hold slot ' + name)
            return
        if self.v2:
            self.v2.confirm_retired(name)
            self.v2.transaction('midturn-rotated-' + uuid.uuid4().hex, lambda: self.v2.midturn_clear(name, resume=False))
            self.setf(name, launch_ready=1, retired_pane='', retired_launch='')
        self.refresh_history()
        self.log('rotate.done', agent=name, label=label, prev=prev, generation=generation + 1,
                 handoff=str(out), source=source, timed_out=timed_out)
        if info.get('rescue_relay'):
            self.setf(name, rescue_relay='', rescue_hint='')
            self.r.rpush(ADVICE_NOTES, '[advisor] respawn ' + prev + ' → ' + label + ' done')

    def fresh_rotation(self, name, used, window, source='explicit'):
        """§37: the next generation of an idle slot for a `fresh` dispatch, in place, in finish_rotation's order but
        with no handoff turn, no note and no takeover text; the queued instruction is delivered after READY.
        A successor that cannot start (or take the tab) is removed and the old session keeps the slot."""
        info = self.agent(name)
        if info.get('state') == 'clearing' or info.get('clear_required') == '1':
            return  # start_rotation's guard: never while §28 clears the session
        self.setf(name, fresh_pending=0, stopfail_fresh=0, result_fresh=0)
        codex_queue.clear(self, name, 'fresh')
        generation, old, prev = int(info.get('generation') or 0), info['pane'], self.label(name)
        snapshot = self.run_dir / 'handoffs' / (name + '-g' + str(generation) + '-fresh.terminal.txt')
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        self.mgr.capture_to(old, str(snapshot))
        label = slot_base(name) + '.' + str(generation + 1)
        self.setf(name, generation=generation + 1, handoff_id='', label=label)
        if name in self.workers:
            self.r.hset(NAMES, mapping={label: name})
        new, why = None, ''
        try:
            new = self.spawn(name, 'You are fleet agent ' + label + '. Read AGENTS.md in the current directory first. '
                             'Your next instruction arrives after this turn: your final reply must be exactly READY.',
                             launch_held=bool(self.v2))
            if self.v2 and not self.mgr.tab_into(new, old):
                why = 'the new session could not take the old tab'
        except RuntimeError as exc:
            new, why = self.agent(name).get('pane'), str(exc)[:300]
        if why:
            if new and new != old:
                self.mgr.kill_pane(new)
            self.setf(name, **dict(info, fresh_pending=0, stopfail_fresh=0, result_fresh=0))
            self.log('dispatch.fresh_failed', level='error', agent=name, reason=why)
            self.r.rpush(FRESH_HINTS, '[Fresh failed] ' + prev + ': a clean session did not start (' + why + '); '
                         'your instruction went to the existing session ' + prev + '.')
            return False
        if not self.v2:
            self.mgr.tab_into(new, old)
        if not self.mgr.kill_pane(old) or (self.v2 and self.mgr.get_pane_status(old) is not None):
            if self.v2:
                self.setf(name, state='dead', launch_ready=0, retired_pane=old,
                          retired_launch=info.get('runtime_launch') or '')
            self.finish('blocked', 'could not close the old session ' + prev + '; two sessions hold slot ' + name)
            return
        if self.v2:
            self.v2.confirm_retired(name)
            self.setf(name, launch_ready=1, retired_pane='', retired_launch='')
        history.record(self.run_dir, slot=name, label=prev, generation=generation, handoff='',
                       snapshot=str(snapshot), source='fresh', timed_out=False)
        self.refresh_history()
        self.log('dispatch.fresh', agent=name, label=label, prev=prev, from_generation=generation,
                 generation=generation + 1, dropped_tokens=used, window=window, source=source,
                 transcript=info.get('transcript') or info.get('thread') or '')
        return True

    def refresh_history(self):
        history.write_page(self.run_dir, self.history_slots(), self.history_links())
        pane = self.r.hget(SERVICE, 'history_pane')
        if pane and getattr(self.mgr, 'supports_browser', True):
            self.mgr.reload_browser(pane)

    def live_pane(self, pane):
        return bool(pane) and self.mgr.get_pane_status(pane) is not None

    def arrange_page(self):
        """Lay the page out (supervisor, workers, history pane) and record the history pane in Redis.

        Only live workers take part (docs/design.md section 17 "Windows"): a worker that is delegated, retired or
        dead, or whose pane is no longer on the server, is left out, so one stale pane id never makes the
        backend's ``arrange`` fail as a whole. Without a live supervisor pane nothing is arranged (None)."""
        sup = self.agent('supervisor').get('pane')
        if not self.live_pane(sup):
            self.history_pane = None
            self.r.hset(SERVICE, mapping={'history_pane': ''})
            return None
        workers = []
        for w in self.workers:
            info = self.agent(w)
            if info.get('state') not in ('delegated', 'retired', 'dead') and self.live_pane(info.get('pane')):
                workers.append(info['pane'])
        self.history_pane = self.mgr.arrange(sup, workers, history.page_url(self.run_dir))
        self.r.hset(SERVICE, mapping={'history_pane': self.history_pane or ''})
        return self.history_pane

    def history_lost(self):
        """True when this backend shows a history pane and the recorded one is empty or gone."""
        return (getattr(self.mgr, 'supports_browser', True)
                and not self.live_pane(self.r.hget(SERVICE, 'history_pane')))

    HISTORY_POLL = 30  # seconds between two checks of the history pane (as the liveness check)

    def history_tick(self, now=None):
        """Daemon fallback (docs/design.md section 2): a lost history pane is rebuilt once with the same rule as
        ``arrange_page`` and the control tab docked behind it. "Once" is per loss and per daemon process: a
        failed rebuild writes one audit record and is not tried again until a live history pane was seen
        (or the daemon starts again); a success re-arms it. Only two consecutive checks (one HISTORY_POLL apart)
        that both see the pane lost count as a loss, so a single failed backend read never lays the page out."""
        now = time.time() if now is None else now
        if now < getattr(self, 'history_next', 0):
            return
        self.history_next = now + self.HISTORY_POLL
        if not self.history_lost():
            self.history_tried = self.history_suspect = False
            return
        if not getattr(self, 'history_suspect', False):
            self.history_suspect = True  # one lost reading may be a failed backend read: confirm it next check
            return
        if getattr(self, 'history_tried', False) or getattr(self, 'stopping', False):
            return
        if not self.service and self.task().get('status', 'running') != 'running':
            return  # a finished run: its page is closed on purpose (lifecycle_close)
        if not self.live_pane(self.agent('supervisor').get('pane')):
            return  # nothing to lay the page out around yet; not an attempt
        lost = self.r.hget(SERVICE, 'history_pane') or ''
        self.history_tried = True
        pane = self.arrange_page()
        if pane and self.live_pane(pane):
            self.mgr.dock_control(pane)
            self.refresh_history()
            self.history_tried = False
            self.log('history.rebuilt', lost=lost, pane=pane)
        else:
            self.log('history.rebuild_failed', level='error', lost=lost,
                     error=getattr(self.mgr, 'last_error', '') or 'arrange returned no history pane')

    def announce_history_page(self):
        """A backend with no browser surface (``supports_browser`` false, design §4.3) does not show
        ``<run>/history.html``: print its path in the control pane and open it once with the system opener
        when there is a desktop (macOS ``open`` / Linux ``xdg-open``; otherwise the path is all you get)."""
        if getattr(self.mgr, 'supports_browser', True):
            return
        page = history.page_path(self.run_dir)
        try:
            self.log('history.page', page=str(page))
        except Exception:
            pass  # logging must never break the layout
        print('[fleet] history page: ' + str(page) + ' (backend ' + str(self.cfg.get('backend') or 'cmux')
              + ' has no embedded browser)', flush=True)
        opener = shutil.which('open' if sys.platform == 'darwin' else 'xdg-open')
        if not opener:
            return
        try:
            subprocess.Popen([opener, str(page)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            pass  # no desktop session: the printed path is still there

    # -- hot roster (start --roster, or <run>/roster.yaml written by the advisor switch)
    def roster_tick(self):
        path = roster_file(self.cfg)
        if not self.cfg.get('roster') and not path.exists():
            return
        self.night_tick(path)
        if self.cfg.get('job'):
            self.job_roster_refused()  # §58 D18: the task arrived after the refused first read
        if path != self.roster_followed:  # night on/off: follow the other file from now on
            self.roster_followed = path
            self.roster_stamp = self.roster_digest = None
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != self.roster_stamp:
            self.roster_stamp = stamp
            self.reload_roster(path)
        for name in [w for w in self.workers if w not in self.desired]:
            info = self.agent(name)
            # Retire only between turns: no queued work and (v2) no delivery in flight.
            if (info.get('state') in ('idle', 'dead') and not self.inbox_depth(name)
                    and not (self.v2 and self.r.hget(self.v2.bus.lease_key(name), 'current_delivery'))):
                self.retire_worker(name)
        self.profile_swap_tick()

    def job_roster_refused(self):
        """§58 D18: a job sub-fleet's first roster read was refused: its task fails (now, or once it arrives)."""
        if not self.cfg.get('job'):
            return
        why = self.r.hget(ROSTER, 'job_refused')
        if why and self.task().get('status') == 'running':
            self.r.hset(ROSTER, mapping={'job_refused': ''})
            self.finish('failed', why)

    def reload_roster(self, path):
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raw, problem = b'', 'cannot read roster: ' + str(exc)
        else:
            problem = None
        digest = hashlib.sha256(raw).hexdigest() + (problem or '')
        if digest == self.roster_digest:
            return  # touched, not changed
        self.roster_digest = digest
        try:
            if problem:
                raise ValueError(problem)
            workers, roles, profiles = roster.parse_entries(raw, Path(path).parent)
            table, per_worker = roster.handoff(raw)
            split_table, split_workers = roster.split(raw)  # §44, validated with the rest above
            fresh = roster.fresh(raw)  # §45
            subfleets = roster.subfleets(raw, Path(path).parent)  # §29 (exempt from the used-name rule)
            if roster.has_channels(raw) and (not self.service or self.cfg.get('parent_config')
                                             or self.cfg.get('transport_version', 1) != 2):
                raise ValueError(roster.SERVICE_ONLY)  # §53: a task fleet / sub-fleet never gets channels
            new_channels = roster.channels(raw)  # [] = none
            new_applets = roster.applets(raw, Path(path).parent)  # applet-interface.md section 1
            from fleetmux import relay_applet  # a roster row may not take a built-in applet's name
            if relay_applet.reserved(new_applets):
                raise ValueError('\n'.join(relay_applet.reserved(new_applets)))
            if new_applets and not self.service:
                raise ValueError('applets: only a service fleet (a root service fleet or a named sub-fleet) hosts applets')
            if subfleets and subfleet.backend_problem(self.cfg):  # §33.4: none on WezTerm; keep the old roster
                raise ValueError(subfleet.backend_problem(self.cfg))
            # A slot name used earlier in this run still has Redis state and a workspace.
            used = [n + ': name was already used in this run (agent state or workspace exists); pick a new name'
                    for n in workers if n not in self.workers
                    and (self.r.exists(key('agent', n)) or (self.workspace() / n).exists())]
            if used:
                raise ValueError('\n'.join(used))
            # An added slot (or a newly declared / retargeted sub-fleet's workers) ends with -<number> (§3);
            # the run's own names, old style or not, are never re-checked here.
            problems = roster.row_errors(workers, keep=self.workers) + roster.subfleet_suffix_errors(
                subfleets, self.cfg.get('subfleets'))
            if problems:
                raise ValueError('\n'.join(problems))
            # Added rows without a profile run the fleet default: its model card caps them with the
            # live slots (C). Rows with a profile are counted by night.resolve_launches (B).
            pi_models, pi_thinkings = roster.pi_settings(raw)
            adds = [n for n in workers if n not in self.workers and not profiles.get(n)]
            if adds:
                problem = self.default_capacity_problem(adds, dict(self.cfg, pi_models=dict(
                    {k: v for k, v in (self.cfg.get('pi_models') or {}).items() if k not in workers}, **pi_models)))
                if problem:
                    raise ValueError(problem)
            night.resolve_launches(self.cfg, profiles, workers)  # unknown card, refused pair, card limits: reject all
            problem = roster.pi_cli_problem(pi_models, pi_thinkings, self.cfg.get('worker_cli'))
            unset = [n for n in adds if self.cfg.get('worker_cli') == 'pi' and not pi_models.get(n)
                     and not self.cfg.get('pi_model')]
            if problem or unset:
                raise ValueError(problem or 'no pi model for ' + ', '.join(unset) + ': give the row a pi_model '
                                 '(the run has no --pi-model fallback)')
        except ValueError as exc:
            self.log('roster.rejected', level='error', path=str(path), errors=str(exc).split('\n'),
                     kept=self.desired)
            if self.cfg.get('job') and not self.r.hget(ROSTER, 'accepted'):
                # §58 D18: a job sub-fleet whose first roster read is refused ends failed; its supervisor gets no
                # [Roster rejected] line and nothing runs on a default model (later hot edits: as below)
                self.r.hset(ROSTER, mapping={'job_refused': subfleet.ROSTER_REJECTED + str(exc).replace('\n', '; ')})
                self.job_roster_refused()
                return
            self.r.hset(ROSTER, mapping={'rejected': str(exc)})  # once per rejected content (digest above)
            return
        self.r.hset(ROSTER, mapping={'rejected': '', 'accepted': 1})  # fixed before the supervisor heard: say nothing
        before, old_roles = list(self.desired), dict(self.cfg.get('worker_roles') or {})
        adds = [n for n in workers if n not in self.workers]
        results = {n: 'retire cancelled' for n in workers if n in self.workers and n not in self.desired}
        results.update({n: 'retiring when idle' for n in self.desired if n not in workers})
        results.update({n: 'role changed; applies at its next generation' for n in workers
                        if n in self.workers and roles.get(n) != old_roles.get(n)})
        # Handoff thresholds (§22): next delivery check uses them; retiring workers keep theirs.
        per_worker = dict({w: v for w, v in self.worker_handoff.items() if w in self.workers and w not in workers},
                          **per_worker)
        changed = {n: {'before': self.worker_handoff.get(n) or {}, 'after': per_worker.get(n) or {}}
                   for n in sorted(set(per_worker) | set(self.worker_handoff))
                   if per_worker.get(n) != self.worker_handoff.get(n)}
        if table != self.handoff_table:
            changed['table'] = {'before': self.handoff_table, 'after': table}
        self.handoff_table, self.worker_handoff = table, per_worker
        if (split_table, split_workers) != (self.split_table, self.worker_split):
            changed['split'] = {'before': {'table': self.split_table, 'workers': self.worker_split},
                                'after': {'table': split_table, 'workers': split_workers}}
        self.split_table, self.worker_split = split_table, split_workers
        old_fresh = dict(self.cfg.get('worker_fresh') or {})  # §45: the next delivery reads the new value
        results.update({n: 'fresh ' + (fresh.get(n) or 'off') + '; applies to its next dispatch' for n in workers
                        if n in self.workers and n not in results and fresh.get(n) != old_fresh.get(n)})
        self.cfg['worker_fresh'] = fresh
        # max_busy (11-09): read by every pump(); the roster value wins, a removed key restores --max-busy.
        cap = roster.max_busy(raw)
        cap = self.max_busy_start if cap is None else cap
        if cap != (self.cfg.get('max_busy') or 0):
            changed['max_busy'] = {'before': self.cfg.get('max_busy') or 0, 'after': cap}
            self.cfg['max_busy'] = cap
        old_profiles = dict(self.cfg.get('worker_profiles') or {})
        results.update({n: 'profile ' + (profiles.get(n) or 'run default') + '; next generation once idle'
                        for n in workers if n in self.workers and profiles.get(n) != old_profiles.get(n)})
        self.cfg['worker_profiles'] = profiles
        # Own pi model / thinking per row (the supervisor's entry comes from start and stays).
        old_pi = (dict(self.cfg.get('pi_models') or {}), dict(self.cfg.get('pi_thinkings') or {}))
        keep = [w for w in self.workers if w not in workers] + ['supervisor']  # retiring slots keep theirs
        self.cfg['pi_models'] = dict({k: v for k, v in old_pi[0].items() if k in keep}, **pi_models)
        self.cfg['pi_thinkings'] = dict({k: v for k, v in old_pi[1].items() if k in keep}, **pi_thinkings)
        results.update({n: 'pi_model ' + (self.cfg['pi_models'].get(n) or 'run default') + ' thinking '
                        + (self.cfg['pi_thinkings'].get(n) or 'run default') + '; next generation once idle'
                        for n in workers if n in self.workers and n not in results
                        and (old_pi[0].get(n), old_pi[1].get(n)) != (pi_models.get(n), pi_thinkings.get(n))})
        pi_changed = old_pi != (self.cfg['pi_models'], self.cfg['pi_thinkings'])
        self.desired = workers
        # Retiring workers keep their role until gone (a handoff successor may still finish work).
        roles = dict({w: old_roles[w] for w in self.workers if w not in workers and w in old_roles}, **roles)
        self.cfg['worker_roles'] = roles
        if adds:
            self.workers = self.workers + adds
            self.names = self.slot_names()
        # §29: a hot-added supervisor_slot must be known before it spawns (tmux slot + no advisor hold read
        # cfg['subfleets']); written after the adds, it started as a plain held tab and its sub-fleet waited (10-14).
        self.tab_specs = roster.tabs(raw, Path(path).parent)  # opened once by tabs.open_all (§31)
        self.applet_specs = new_applets  # started / watched / stopped by applet_host.tick
        self.builtin_applets()  # relay.enabled, read again on every roster reload
        subs_changed = subfleets != (self.cfg.get('subfleets') or [])
        if subs_changed:
            self.cfg['subfleets'] = subfleets
        old_channels = self.cfg.get('channels') or []
        channels_changed = new_channels != old_channels
        if channels_changed:  # §53: the actor (P1f) reads cfg['channels']; no key at all when none
            if new_channels:
                self.cfg['channels'] = new_channels
            else:
                self.cfg.pop('channels', None)
            changed['channels'] = {'before': [e['type'] + ':' + e['account'] for e in old_channels],
                                   'after': [e['type'] + ':' + e['account'] for e in new_channels]}
        routes_changed = session_env.update_config(self.cfg, new_applets, subfleets)  # root: union of the tree
        if (adds or roles != old_roles or profiles != old_profiles or pi_changed or subs_changed
                or channels_changed or routes_changed or fresh != old_fresh):
            # Hooks and bridges check membership in the config file: write it before spawning.
            self.persist_roster()
        # Before the adds: a worker added together with `advisor: on` is spawned advised (held for its sheet).
        self.set_advisor(roster.advisor(raw), 'user')  # no-op when equal (e.g. the supervisor's own write)
        for name in adds:
            results[name] = self.add_worker(name)
        if adds or any(n not in workers for n in before) or any(n not in before for n in workers):
            self.r.hset(ROSTER, mapping={'notify': 1})
        if results or changed:
            self.log('roster.changed', before=before, after=self.desired, results=results, handoff=changed)
        for name in self.names:  # status shows the effective values without waiting for a delivery
            if self.agent(name):
                self.handoff_limits(name)
                if name in self.workers:
                    self.split_settings(name)
        if subfleets or named_subfleet.declared(self):
            named_subfleet.declare(self, subfleets)

    def default_capacity_problem(self, adds, cfg=None):
        """None if new default slots ``adds`` fit the default model's card next to the live slots
        (``cfg``: the config with the new roster's per-slot pi models; default self.cfg)."""
        cfg = cfg or self.cfg
        try:
            registry = AgentRegistry(self.task().get('agent_registry') or None)
        except (AgentRegistryError, OSError) as exc:
            return 'cannot read agent cards to check the default model: ' + str(exc)
        keys = {model_usage.default_model(pi_adapter.for_agent(cfg, n), registry) for n in adds} - {None}
        if not keys:
            return None
        usage = self.model_usage(registry)
        held = {slot for key in keys for slot in usage.holders(key)}
        return model_usage.default_problem(cfg, sorted(held | set(adds)), registry)

    def persist_roster(self):
        self.cfg['workers'][:] = self.workers  # in place: the control view holds this list
        fields = {'workers': list(self.workers), 'worker_roles': self.cfg.get('worker_roles') or {},
                  'worker_profiles': self.cfg.get('worker_profiles') or {},
                  'pi_models': self.cfg.get('pi_models') or {}, 'pi_thinkings': self.cfg.get('pi_thinkings') or {},
                  'subfleets': self.cfg.get('subfleets') or [], 'worker_fresh': self.cfg.get('worker_fresh') or {}}

        def merged(stored):  # §53: `channels` written when the roster has some, removed when it has none
            out = dict(stored, **fields)
            if self.cfg.get('channels'):
                out['channels'] = self.cfg['channels']
            else:
                out.pop('channels', None)
            return session_env.carry(out, self.cfg)
        save(self.path, merged(load(self.path)))
        try:
            latest = load(LATEST)
            if Path(latest.get('config') or '').resolve() == self.path:
                save(LATEST, merged(latest))
        except (OSError, ValueError):
            pass  # latest.json belongs to another run or is missing: nothing to update

    def add_worker(self, name):
        self.init_worker(name)
        try:
            self.apply_roster_launch(name)
            pane = self.spawn_ready(name)
        except (Exception, SystemExit) as exc:
            self.setf(name, state='dead', launch_ready=0, runtime_ready=0)
            return 'spawn failed: ' + str(exc)
        peer = next((p for p in (self.agent(w).get('pane') for w in self.workers if w != name)
                     if p and self.mgr.get_pane_status(p) is not None), None)
        placed = (self.mgr.tab_into(pane, peer, before=False) if peer
                  else self.mgr.place_right_of(pane, self.agent('supervisor').get('pane')))
        self.refresh_history()
        return 'added' if placed else 'added; could not move it into the worker column'

    def retire_worker(self, name):
        applet_lease.slot_retired(self, name)  # applets.md section 4: the lease is revoked first
        info, label = self.agent(name), self.label(name)
        generation, pane = int(info.get('generation') or 0), info.get('pane')
        snapshot = ''
        if pane and self.mgr.get_pane_status(pane) is not None:
            snapshot = self.run_dir / 'handoffs' / (name + '-g' + str(generation) + '-retired.terminal.txt')
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            self.mgr.capture_to(pane, str(snapshot))
        if not pane:
            ok = True  # never spawned: nothing to close
        elif self.v2:
            ok = self.v2.retire(name, 'removed from roster')  # holds the slot if the close is unconfirmed
        else:
            ok = self.mgr.kill_pane(pane) and self.mgr.get_pane_status(pane) is None
        if not ok:
            self.log('roster.retire_failed', agent=name, pane=pane)
            return
        history.record(self.run_dir, slot=name, label=label, generation=generation, handoff='',
                       snapshot=str(snapshot), source='retired', timed_out=False)
        self.setf(name, state='retired', launch_ready=0, runtime_ready=0)
        self.workers = [w for w in self.workers if w != name]
        self.names = self.slot_names()
        (self.cfg.get('worker_roles') or {}).pop(name, None)
        self.persist_roster()  # its workspace stays: declared artifact paths must still verify
        self.refresh_history()
        self.log('roster.retired', agent=name, label=label, workers=self.workers)

    # -- roster profiles / night switch (docs/design.md §25)
    def apply_roster_launch(self, name):
        """A slot that the roster gives a profile starts (or is added) running that card."""
        profile = (self.cfg.get('worker_profiles') or {}).get(name)
        self.setf(name, roster_pi=self.roster_pi(name))
        if profile:
            launch = night.resolve_launches(self.cfg, {name: profile})[name]
            self.setf(name, runtime_launch=profile_launch.encode_launch(launch), roster_profile=profile)

    def roster_pi(self, name):
        """This slot's own pi model / thinking as the roster wants it (compared to roster_pi in Redis)."""
        return json.dumps([(self.cfg.get('pi_models') or {}).get(name) or '',
                           (self.cfg.get('pi_thinkings') or {}).get(name) or ''])

    def roster_card_gate(self, name, profile):
        """Feature C's per-slot card gate (Fleet.card_gate) before a roster swap onto ``profile``:
        None, ('wait', why) while the model is at max_concurrent elsewhere, ('block', why). Without C: None."""
        if not hasattr(self, 'card_gate'):
            return None
        registry = AgentRegistry(self.cfg.get('agent_registry') or None)
        return self.card_gate(name, registry.get(profile), registry)

    def profile_swaps(self):
        """Active slots whose roster profile differs from the one their generation runs."""
        wanted = self.cfg.get('worker_profiles') or {}
        # roster_pi missing = a slot started before per-agent pi models: never swapped for that alone
        return [n for n in self.active_workers() if (self.agent(n).get('roster_profile') or '') != (wanted.get(n) or '')
                or self.agent(n).get('roster_pi') not in (None, self.roster_pi(n))]

    def night_tick(self, path):
        """Record a night on/off transition (audit + brake card placeholder for the budget code)."""
        on = bool(self.cfg.get('roster_night')) and night.read_state(self.cfg['run'])['on']
        prev = self.r.hget(night.NIGHT, 'on')
        if prev == str(int(on)):
            return
        if prev is not None or on:
            self.log('night.' + ('on' if on else 'off'), roster=str(path), brake_card='night' if on else 'day',
                     supervisor='unchanged')
        self.r.hset(night.NIGHT, mapping={'on': int(on), 'brake_card': 'night' if on else 'day', 'since': time.time()})

    def profile_swap_tick(self):
        pending = self.profile_swaps()
        for name in self.active_workers():
            if name not in pending and (self.agent(name).get('roster_swap_deferred') == '1'
                                        or self.agent(name).get('roster_capacity_wait')):
                # roster changed back: a later deferral / capacity wait is audited again
                self.setf(name, roster_swap_deferred=0, roster_capacity_wait='')
        live = False
        if pending and not self.service:
            try:
                live = profile_runtime.task_live_setting(self.task(), self.cfg['allow_profile_live_switch'])
            except profile_runtime.ProfileRuntimeError:
                live = True
        for name in pending:
            info = self.agent(name)
            if live:  # a live task assignment owns this slot's runtime until the task ends
                if info.get('roster_swap_deferred') != '1':
                    self.setf(name, roster_swap_deferred=1)
                    self.log('roster.profile_deferred', agent=name, reason='task apply_profiles_live owns the slot runtime')
                continue
            if (info.get('state') != 'idle' or info.get('idle_confirmed') == '0'
                    or (self.v2 and self.r.hget(self.v2.bus.lease_key(name), 'current_delivery'))):
                continue  # in-flight turns finish first
            if self.swap_profile(name) is False:
                return  # blocked: the task is finished as blocked, leave the other slots alone
        if self.r.hget(night.NIGHT, 'brief_due') == '1' and not self.profile_swaps() and not any(
                self.agent(w).get('state') == 'starting' for w in self.active_workers()):
            self.r.hset(night.NIGHT, mapping={'brief_due': 0})
            self.breakpoint_brief()

    def swap_profile(self, name):
        """Same slot, next generation, the roster's profile; like a handoff, the queue stays on the slot."""
        profile = (self.cfg.get('worker_profiles') or {}).get(name) or ''
        info = self.agent(name)
        try:
            launch = night.resolve_launches(self.cfg, {name: profile})[name] if profile else None
            gate = self.roster_card_gate(name, profile) if launch else None
            if gate and gate[0] == 'wait':
                if info.get('roster_capacity_wait') != gate[1]:  # audit each new reason once
                    self.setf(name, roster_capacity_wait=gate[1])
                    self.log('roster.capacity_wait', agent=name, profile=profile, reason=gate[1])
                return None  # queue stays held on this slot; retried next tick (no old-card work meanwhile)
            if gate:
                raise ValueError(gate[1])  # host mismatch never clears: block, never a silent swap
            if launch:
                self.preflight_profile(launch, cli_change_ok=True)
        except (OSError, ValueError, KeyError, SystemExit, subprocess.SubprocessError) as exc:
            return self.block_profile(name, 'roster profile ' + (profile or 'run default') + ': ' + str(exc)) or False
        generation, old, prev = int(info['generation']), info.get('pane'), self.label(name)
        if not old or self.mgr.get_pane_status(old) is None:
            return self.block_profile(name, 'roster profile switch: the old runtime tab is missing', dead=True) or False
        handoff_id = uuid.uuid4().hex[:8]
        note = self.run_dir / 'handoffs' / (name + '-g' + str(generation) + '-' + handoff_id + '-profile.md')
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(self.skeleton(name, dict(info, handoff_id=handoff_id)))
        snapshot = note.with_suffix('.terminal.txt')
        self.mgr.capture_to(old, str(snapshot))
        history.record(self.run_dir, slot=name, label=prev, generation=generation, handoff=str(note),
                       snapshot=str(snapshot), source='roster-profile', timed_out=False)
        label = name + '.' + str(generation + 1)
        # Fence old callbacks before the successor exists (same order as an idle profile switch).
        self.setf(name, generation=generation + 1, label=label, prev_label=prev, handoff_id='',
                  runtime_launch=profile_launch.encode_launch(launch) if launch else '', runtime_profile='',
                  roster_profile=profile, roster_pi=self.roster_pi(name), roster_swap_deferred=0, roster_capacity_wait='', launch_ready=0,
                  retired_pane=old)
        self.r.hset(NAMES, mapping={label: name})
        self.r.hset(night.NIGHT, mapping={'brief_due': 1})
        try:
            new = self.spawn(name, '[Same-task takeover] You are ' + label + ' (taking over from ' + prev
                             + ' on profile ' + (profile or 'run default') + '). Read the handoff note ' + str(note)
                             + ' and continue from it. In this turn only read the note, do no task work, and make '
                             'your final reply exactly READY.', launch_held=True)
            if not self.mgr.tab_into(new, old):
                raise RuntimeError('could not place the successor in the worker pane')
            if not self.mgr.kill_pane(old) or self.mgr.get_pane_status(old) is not None:
                raise RuntimeError('could not confirm retirement of the old runtime ' + prev)
        except (Exception, SystemExit) as exc:
            self.setf(name, launch_ready=0, state='dead')
            return self.block_profile(name, 'roster profile switch failed: ' + str(exc), dead=True) or False
        if self.v2:
            self.v2.confirm_retired(name)
        self.setf(name, launch_ready=1, retired_pane='')
        self.refresh_history()
        self.log('roster.profile_switched', agent=name, label=label, prev=prev, generation=generation + 1,
                 profile=profile or 'run default', cli=agent_cli(self.runtime_cfg(name), name), handoff=str(note),
                 **({'pi_model': self.runtime_cfg(name).get('pi_model') or ''} if is_pi(self.runtime_cfg(name), name) else {}))
        return True

    def breakpoint_brief(self):
        """After a profile/night swap: tell the supervisor where the task stands (resume's summary)."""
        t = self.task()
        if self.service or t.get('status') != 'running':
            return self.log('night.brief_skipped', reason='no running task')
        wanted = self.cfg.get('worker_profiles') or {}
        slots, kept = [], []
        for w in self.active_workers():
            slots.append('- ' + self.label(w) + ': ' + (wanted.get(w) or 'run default') + ' ('
                         + agent_cli(self.runtime_cfg(w), w) + ')')
            pending = self.v2.pending_text(w) if self.v2 else self.r.lrange(key('inbox', w), 0, 0)
            if pending:
                kept.append('- ' + self.label(w) + ' (' + str(self.inbox_depth(w)) + ' queued): '
                            + ' '.join(pending[0].split())[:200])
        night_on = self.r.hget(night.NIGHT, 'on') == '1'
        self.queue('supervisor', '[' + ('Night' if night_on else 'Day') + ' switch ' + t['id'] + '] Workers moved to '
                   'their ' + ('night' if night_on else 'day') + ' profiles at an idle boundary: same names, new '
                   'generations that only read a handoff note. Work they finished stays finished.'
                   + '\nGoal: ' + t.get('goal', '')
                   + '\nWorkers now:\n' + '\n'.join(slots)
                   + '\nWorkers you can dispatch to: ' + ', '.join(self.labels())
                   + '\nShared output directory: ' + str(self.workspace())
                   + (self.v2.progress(t) if self.v2 else '')
                   + '\nQueued instructions kept on their slot (the new generation gets them; do NOT dispatch them again):\n'
                   + ('\n'.join(kept) or '- none')
                   + '\nDispatch only work that is still missing. Reply with the JSON format from AGENTS.md.')
        self.log('night.brief', workers=self.labels(), kept=len(kept))

    # -- stall watchdog
    def last_activity(self, info):
        """Newest of: when we handed the agent its message, its rollout file's mtime, and ``act_floor`` (the end of
        a daemon gap, RuntimeV2.daemon_gap: the daemon's absence is not the agent's stillness, §24)."""
        last = max(float(info.get('busy_since') or 0), float(info.get('act_floor') or 0))
        if info.get('transcript'):  # claude worker: main transcript + its subagent transcripts
            main = Path(info['transcript'])
            for path in [main] + list(main.with_suffix('').glob('**/*.jsonl')):
                try:
                    last = max(last, path.stat().st_mtime)
                except OSError:
                    pass
            return last
        thread = info.get('thread')
        paths = rollouts(thread, self.sessions) if thread else []
        if paths:
            last = max(last, paths[0].stat().st_mtime)
        return last

    def check_stalls(self, now):
        limit = float(self.cfg['stall_timeout'])
        if limit <= 0:
            return
        for name in self.names:
            info = self.agent(name)
            if info.get('state') not in ('busy', 'starting'):
                continue
            last = self.last_activity(info)
            if not last or now - last < limit:
                continue
            self.on_stall(name, info, int(now - last))

    def on_stall(self, name, info, idle_s):
        screen = self.capture_screen(name, info) if self.running() else ''
        if self.v2:
            def stalled():
                self.v2.hold(name, 'no activity for ' + str(idle_s) + ' s; stop unconfirmed', public_status='stalled')
                return self.on_wall(name, screen, 'screen')
            record = self.v2.transaction('stall-' + uuid.uuid4().hex, stalled)
            if record:
                self.brake_notify(record)
            return None
        record = self.on_wall(name, screen, 'screen')
        if record:
            self.brake_notify(record)
        stalls = int(info.get('stalls', 0)) + 1
        self.log('agent.stalled', agent=name, state=info.get('state'), idle_s=idle_s, stalls=stalls)
        self.mgr.send_key(info['pane'], 'Escape')  # abort the turn; an aborted turn fires no notify
        self.setf(name, state='idle', idle_confirmed=0, stalls=stalls, busy_since=time.time())
        queued = False  # a Codex item that never started is still in the TUI's queue (no official withdraw)
        if codex_queue.unsettled(info):
            rec = codex_queue.record(info)
            if codex_queue.token_turn(rec['cq_thread'], rec['cq_token'], self.sessions):
                codex_queue.clear(self, name, 'stalled turn aborted')  # it left the queue: a retry is not a duplicate
            else:
                queued = True
        if info.get('state') == 'starting':
            self.setf(name, state='dead', runtime_ready=0, current_profile='',
                      profile_state='blocked', profile_reason='startup timed out before READY')
            if self.running():
                self.finish('blocked', name + ' never became ready (' + str(idle_s) + ' s without activity)')
            return
        if not self.running():
            return
        minutes = str(idle_s // 60)
        if self.service and name != 'supervisor':
            rid = info.get('request')
            self.setf(name, request='')
            if rid and self.r.hget(REQ + rid, 'status') == 'dispatched':
                self.publish(rid, 'stalled', worker=self.label(name),
                             error='no activity for ' + minutes + ' min, the turn was aborted')
            return
        if name == 'supervisor':
            if stalls >= 2 or not info.get('last_msg'):
                self.finish('blocked', 'supervisor stalled twice (' + minutes + ' min without activity)')
            elif queued:
                # Its queued item has not started and still runs once the TUI is free: wait for it (its own
                # turn settles it) instead of queueing the same message a second time.
                self.setf(name, state='busy')
                self.log('queue.kept', agent=name, delivery=codex_queue.record(info)['cq_delivery'])
            else:
                self.r.lpush(key('inbox', name), info['last_msg'])  # one retry of the same message
        else:
            self.report(name, 'STALLED: no activity for ' + minutes + ' min, its turn was aborted. The worker is '
                        'idle again. Dispatch a smaller, concrete instruction, or reply BLOCKED if a human is needed.')

    # -- brake (fleetmux/brake.py): budget card per pool + wall table
    def quota_reading(self):
        """The pool readings of every brake reader: the hot quota block (fleetmux/fleet_superpowers_hot.py)."""
        from fleetmux import fleet_superpowers_hot
        return fleet_superpowers_hot.quota(self.r)

    def quota_source(self):
        """The treasurer's quota.json the daemon ingests: config ``quota_file``, else the default location."""
        from fleetmux import fleet_superpowers
        path = self.cfg.get('quota_file')
        return Path(path).expanduser() if path else fleet_superpowers.old_location('quota')

    def brake_budget(self):
        return json.loads(self.r.hget(BRAKE, 'budget') or 'null')

    def brake_start(self, budget):
        """A new card: counters restart (task submit; service: first tick)."""
        if self.paused() and (self.r.hget(SERVICE, 'reason') or '').startswith('brake: '):
            self.resume()  # a brake pause belongs to the card it tripped, not to the next task
        base = brake.pool_reading(self.quota_reading(), (budget or {}).get('pool'))
        self.r.delete(BRAKE)
        self.r.hset(BRAKE, mapping={'budget': json.dumps(budget), 'calls': 0, 'rounds': 0, 'started': time.time(),
                                    'usd_base': '' if base is None else base, 'tripped': ''})

    def brake_count(self, counter):
        self.r.hincrby(BRAKE, counter, 1)

    def brake_tick(self, now):
        """Pause at any budget line. Returns the trip record (notify after the transaction) or None."""
        if self.service and self.cfg.get('budget') and not self.r.hget(BRAKE, 'budget'):
            self.brake_start(brake.validate_budget(self.cfg['budget']))
        state = self.r.hgetall(BRAKE)
        budget = json.loads(state.get('budget') or 'null')
        if not budget or not self.running() or self.paused():
            return None
        quota = self.quota_reading()
        if brake.pool_meter(budget.get('pool')) == 'usd_equiv' and state.get('usd_base') in (None, ''):
            base = brake.pool_reading(quota, budget['pool'], now)
            if base is not None:  # the first known reading is the baseline: spend before it is not ours
                self.r.hset(BRAKE, mapping={'usd_base': base})
                state['usd_base'] = base
        found = brake.trips(self.brake_card(budget), brake.counters(state, quota, now))
        if found:
            return self.brake_trip('budget', 'budget line reached: ' + brake.describe(found), lines=found)
        return None

    def brake_card(self, budget):
        """The card as evaluated (default usd_equiv cash cap, day or night per B's fx:night)."""
        return brake.effective(budget, self.r.hget('fx:night', 'brake_card') == 'night')

    def brake_trip(self, kind, reason, **detail):
        record = dict(kind=kind, reason=reason, at=time.time(), task=self.task().get('id', ''), **detail)
        self.r.hset(BRAKE, mapping={'tripped': json.dumps(record, ensure_ascii=False)})
        if not self.paused():
            self.pause('brake: ' + reason, kind='budget' if kind == 'budget' else 'wall', brake_kind=kind,
                       slot=detail.get('agent'))
            # The pause this brake owns (the first trip's reason and agent): later trips overwrite `tripped`.
            self.r.hset(BRAKE, mapping={'paused_by': json.dumps({'reason': 'brake: ' + reason, 'kind': kind,
                                                                 'agent': detail.get('agent', '')})})
        self.log('brake.tripped', **{('brake' if k == 'kind' else k): v for k, v in record.items() if k != 'at'})
        return record

    def brake_notify(self, record):
        """Side effects of a trip, outside any v2 transaction: <run>/brake.json and the operator's notify argv.
        §67: a sub-fleet tells its parent's supervisor about a wall pause (the parent heals it)."""
        if record.get('kind') == 'wall' and record.get('agent') and not record.get('advisor'):
            heal.report_up(self, 'walled', record['agent'], record.get('reason') or '')
        state = self.r.hgetall(BRAKE)
        card = json.loads(state.get('budget') or 'null')
        save(self.run_dir / 'brake.json', dict(record, budget=card, effective_budget=self.brake_card(card),
             counters=brake.counters(state, self.quota_reading())))
        argv = self.cfg.get('brake_notify')
        if argv:
            try:
                subprocess.Popen(list(argv) + ['[fleetmux brake] ' + record['reason']], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                self.log('brake.notify_failed', error=str(exc))

    def brake_resume(self, raises):
        """Operator resume: refused while a budget line is still reached (resume --raise LIMIT=VALUE)."""
        if raises is not None and (not isinstance(raises, list) or not all(isinstance(x, str) for x in raises)):
            raise ValueError('raise must be a list of LIMIT=VALUE strings')
        state = self.r.hgetall(BRAKE)
        budget = json.loads(state.get('budget') or 'null')
        tripped = json.loads(state.get('tripped') or 'null')
        if raises:
            if not budget:
                raise ValueError('--raise needs a budget card on the current task / service')
            budget = brake.parse_raise(raises, budget)
            if budget.get('pool') != json.loads(state['budget']).get('pool'):
                raise ValueError('--raise cannot change the pool')
        if budget:  # any resume, whatever paused the run (a later wall trip or a hold can overwrite the record)
            found = brake.trips(self.brake_card(budget), brake.counters(state, self.quota_reading()))
            if found:
                raise ValueError('budget still over the line (' + brake.describe(found)
                                 + '); resume with --raise LIMIT=<higher value>')
        if raises:
            self.r.hset(BRAKE, mapping={'budget': json.dumps(budget)})
            self.log('brake.raised', budget=budget)
        if tripped:
            self.r.hset(BRAKE, mapping={'tripped': ''})
        self.r.hset(BRAKE, mapping={'paused_by': ''})

    def on_wall(self, name, text, source, wall=None):
        """A wall on a turn text or a stalled screen: decide (next model on the role's chain / stop), record, pause.
        ``wall``: already classified by the caller (the screen gate, a StopFailure's CLI-owned fields: the only
        sources of the safeguards class, design section 23); else ``text`` is classified as a turn text."""
        wall = wall or brake.classify_wall(text)
        if not wall:
            return None
        action = self.wall_decide(name, self.runtime_cfg(name), wall)
        self.setf(name, wall=json.dumps(dict(wall, **action), ensure_ascii=False))
        if action['action'] == 'stop':
            self.setf(name, wall_downgrade='', wall_switch='')  # a stop wins over an earlier pending switch
        else:
            pending = json.loads(self.agent(name).get('wall_downgrade') or 'null')
            if not pending or (pending.get('to_cli'), pending.get('to')) != (action['to_cli'], action['to']):
                self.setf(name, wall_downgrade=json.dumps(dict(action, key=self.wall_key(name, action['to_cli']),
                                                               from_key=self.wall_key(name, action['cli']),
                                                               source=source, at=uuid.uuid4().hex[:12],
                                                               kind=wall.get('kind') or wall['scope']),
                                                          ensure_ascii=False))
        if name == 'supervisor' and action['action'] == 'stop' and self.cfg.get('model_chain'):
            self.chain_switched(name, action['cli'] + ':' + (action.get('from') or ''), None,
                                job_chain.SUPERVISOR_WALL)  # §58 D8: a job's adopted supervisor only stops
        what = ('next on the chain: ' + action['cli'] + ':' + action['from'] + ' -> ' + action['to_cli'] + ':'
                + action['to'] if action['action'] == 'switch' else action['advice'])
        if action.get('skipped'):
            what += ' (skipped ' + ', '.join(action['skipped']) + ')'
        # section 67: the reason names the wall and never quotes the CLI's line (it goes up to parents and onto panes);
        # the raw line stays in the record's `wall` (brake.json, audit)
        phrase = brake.wall_phrase(self.label(name), wall.get('kind') or wall['scope'], action['cli'],
                                   action.get('from'), brake.wall_resets(wall.get('line') or ''))
        return self.brake_trip('wall', phrase + '; ' + what, agent=name, source=source, wall=wall, decision=action)

    # -- wall chain (user 2026-11-11): 「所以对于supervisor，撞墙前优先级依次是Astra, fable, opus sol deepseek flash,
    #    对于worker 撞墙前依次是opus, sol,sonnet,terra,deepseek flash」 -- fleet.yaml#discovery, docs/design.md §23
    def model_chain(self):
        """§58: this job sub-fleet's model_chain (config.json at start, <run>/model_chain.json once a switch was
        recorded), or {}."""
        try:
            return load(self.run_dir / MODEL_CHAIN_FILE)
        except (OSError, ValueError):
            return copy.deepcopy(self.cfg.get('model_chain') or {})

    def chain_switched(self, slot, frm, to, reason):
        """§58 (brief 2.7, D8): add one switch (or a supervisor stop, ``to`` None) to the slot's trace, once per
        change, and keep the whole record in <run>/model_chain.json."""
        chain = self.model_chain()
        entry = chain.get('supervisor') if slot == 'supervisor' else (chain.get('workers') or {}).get(slot)
        if entry is None:
            return
        last = (entry.get('switched') or [{}])[-1]
        if (last.get('from'), last.get('to'), last.get('reason')) == (frm, to, reason):
            return
        entry.setdefault('switched', []).append({'from': frm, 'to': to, 'reason': reason, 'at': time.time()})
        save(self.run_dir / MODEL_CHAIN_FILE, chain)
        self.log('job.model_switched', agent=slot, frm=frm, to=to, reason=reason)

    def wall_chained(self, name, kind=None):
        """Walls of workers and of a main fleet's own supervisor walk the role's chain; a sub-fleet's supervisor
        (an adopted pane of the parent) and the advisor (§27) keep their old handling. A safeguards wall (design
        section 23) also moves a sub-fleet supervisor this fleet owns; an adopted pane (generation 0) never gets a
        successor from the child (see the context handoff in deliver: window = 0 for an adopted pane), so it stops."""
        if name in self.workers:
            return True
        if name != 'supervisor':
            return False
        if not self.cfg.get('adopt') and not self.cfg.get('parent_config'):
            return True
        return False  # §67: a sub-fleet's supervisor (any generation, any wall kind) is reopened by its parent only

    def subfleet_supervisor(self, name):
        return name == 'supervisor' and bool(self.cfg.get('adopt') or self.cfg.get('parent_config'))

    def wall_key(self, name, cli):
        """The launch override key that names this slot's model on ``cli``."""
        return (brake.SUPERVISOR_MODEL_KEY if name == 'supervisor' else brake.MODEL_KEY).get(cli, '')

    def slot_model(self, name, cfg):
        key = self.wall_key(name, agent_cli(cfg, name))
        return (cfg.get(key) if key else '') or ''

    def wall_chains_load(self):
        """Resolve both wall chains once -- fleet.yaml#discovery order over this fleet home's discovered.yaml (or the
        model cards plus the run's own models), the discovery supervisor pi thinking, and the pair table's rows for
        this mode -- into Redis WALL_CHAINS (user 2026-11-11: the config read and changed most is loaded into Redis).
        Every daemon start / resume rewrites it; while the run is up walls read only the key. A failed read stores
        empty chains with the error (walls then stop, as before the chain)."""
        extra = []
        for slot in ['supervisor'] + self.active_workers():
            cfg = self.runtime_cfg(slot)
            extra.append((agent_cli(cfg, slot), self.slot_model(slot, cfg)))
        mode = self.cfg.get('mode') or 'task'
        try:
            disc = fleet_config.block('discovery') or {}
            own = self.cfg.get('job_chain') or {}  # §58 (D9): a job sub-fleet walks its own chains, not discovery's
            disc = dict(disc, **{role: own[role] for role in ('supervisor', 'worker') if own.get(role)})
            sup, sup_patterns = discover.chain('supervisor', extra, disc)
            wrk, wrk_patterns = discover.chain('worker', extra, disc)
            pairs = {k[0] + '|' + k[1]: ('adapter missing' if row['adapter_missing'] else row['status'])
                     for k, row in cli_pairs.load_pairs().rows.items() if k[2] == mode}
            data = {'supervisor': sup, 'worker': wrk, 'patterns': {'supervisor': sup_patterns, 'worker': wrk_patterns},
                    'supervisor_pi_thinking': disc.get('supervisor_pi_thinking') or 'high',
                    'worker_thinking': disc.get('worker_thinking') or 'high', 'mode': mode, 'pairs': pairs,
                    'safeguards_prefer': brake.wall_table()['safeguards_prefer']}
        except Exception as exc:  # may run inside the wall's v2 transaction (lazy load): never lose the pause
            self.log('brake.chain_unreadable', error=repr(exc)[:300])
            data = {'supervisor': [], 'worker': [], 'patterns': {}, 'mode': mode, 'pairs': {}, 'error': repr(exc)[:300]}
        data['at'] = time.time()
        self.r.set(WALL_CHAINS, json.dumps(data, ensure_ascii=False))
        return data

    def wall_chains(self):
        """The chains in Redis; a run started before they were loaded at start gets them resolved now, once."""
        raw = self.r.get(WALL_CHAINS)
        return json.loads(raw) if raw else self.wall_chains_load()

    def wall_chain(self, name):
        """([(cli, model)], patterns) of this slot's role, from Redis WALL_CHAINS."""
        role = 'supervisor' if name == 'supervisor' else 'worker'
        data = self.wall_chains()
        return [tuple(x) for x in data.get(role) or []], list((data.get('patterns') or {}).get(role) or [])

    def walled(self):
        return [json.loads(v) for v in self.r.hgetall(WALLS).values()]

    def wall_refused(self, name, cli):
        """Why the pair table refuses ``cli`` for this slot now, or None (its rows for this mode, loaded with the
        chains). Only a verified row without missing adapters passes: a worker pairs with the supervisor's current
        CLI, the supervisor with every active worker's current CLI."""
        if cli != 'claude' and self.subfleet_supervisor(name):
            return job_chain.NOT_CLAUDE.format(cli=cli)  # design section 23: pass one then falls through to pass two
        data = self.wall_chains()
        mode = data.get('mode') or 'task'
        if name == 'supervisor':
            combos = sorted({(cli, agent_cli(self.runtime_cfg(w), w)) for w in self.active_workers()})
        else:
            combos = [(agent_cli(self.runtime_cfg('supervisor'), 'supervisor'), cli)]
        for sup, wrk in combos:
            status = (data.get('pairs') or {}).get(sup + '|' + wrk)
            if status != 'verified':
                return sup + ' supervisor + ' + wrk + ' workers (' + mode + ') is ' + (status or 'not in the pair table')
        return None

    def wall_decide(self, name, cfg, wall=None, kind=None):
        """brake.wall_action for this slot: the role's chain below its running (cli, model) -- from the chain head
        when that model is not on the chain -- with this run's wall records and the pair table. ``wall``: record
        the running model as walled first (an account wall marks its whole account). ``kind`` (or wall['kind'])
        safeguards (design section 23): the WHOLE chain except the flagged entry, walls.safeguards_prefer entries
        first (chain order), then the others (chain order)."""
        kind = kind or (wall or {}).get('kind')
        safeguards = kind == brake.SAFEGUARDS
        cli, model = agent_cli(cfg, name), self.slot_model(name, cfg)
        if not self.wall_chained(name, kind):
            if wall and safeguards:  # recorded on its chain entry like any safeguards wall (this run's later picks)
                entries, patterns = self.wall_chain(name)
                at = discover.place(entries, patterns, cli, model or (claude_model(cfg, name) if cli == 'claude' else ''))
                self.wall_mark(name, entries[at] if at is not None else (cli, model), wall['scope'], kind)
            why = ('adopted sub-fleet supervisor (the parent owns its pane): no automatic switch'
                   if subfleet.adopted(self.cfg, name, self.agent(name).get('generation'))
                   else 'sub-fleet supervisor: no automatic switch')
            return {'action': 'stop', 'cli': cli, 'from': model,
                    'advice': (('safeguards refusal on ' + cli + ':' + str(model) + ': ' + why + '; the parent fleet '
                                'decides; the flagged message stays queued') if safeguards
                               else why + '; the parent fleet is told and reopens it (heal)')}
        entries, patterns = self.wall_chain(name)
        at = discover.place(entries, patterns, cli, model or (claude_model(cfg, name) if cli == 'claude' else ''))
        here = entries[at] if at is not None else (cli, model)
        if wall:
            self.wall_mark(name, here, wall['scope'], kind)
        prefer = None
        if safeguards:
            below = [e for e in entries if e != here]
            patterns_ = self.wall_chains().get('safeguards_prefer')
            patterns_ = brake.wall_table()['safeguards_prefer'] if patterns_ is None else patterns_
            prefer = lambda c, m: brake.preferred(m, patterns_)  # noqa: E731
        else:
            below = entries[at + 1:] if at is not None else entries
        action = brake.wall_action(cli, model, below, self.walled(), lambda c, m: self.wall_refused(name, c), prefer)
        if safeguards and action['action'] == 'stop':
            action['advice'] = ('safeguards refusal on ' + cli + ':' + str(model) + ': no usable model left on the whole '
                                'chain (fleet.yaml#discovery); the flagged message stays queued; restart the slot by '
                                'hand on another model')
        return action

    def wall_mark(self, name, here, scope, reason=None):
        field = here[0] + ':' + (here[1] or '')
        old = json.loads(self.r.hget(WALLS, field) or '{}')
        if old.get('scope') == 'account':
            scope = 'account'  # a later pool wall never narrows an account wall
        rec = {'cli': here[0], 'model': here[1], 'scope': scope, 'account': brake.account(*here), 'at': time.time(),
               'agent': name}
        if reason == brake.SAFEGUARDS or old.get('reason'):
            rec['reason'] = reason if reason == brake.SAFEGUARDS else old['reason']  # audit: why it is skipped
        self.r.hset(WALLS, mapping={field: json.dumps(rec, ensure_ascii=False)})

    def notify_later(self, record):
        """A trip recorded inside a v2 event transaction: brake.json / notify argv run on the next tick."""
        if record:
            self.pending_notify.append(record)

    def startup_wall(self, name, text):
        """A new generation answered its startup prompt with a wall instead of READY: walled (not dead),
        fleet paused resumable. A model-pool wall is downgraded by wall_tick; any other wall is retried
        (same launch) once the operator resumes."""
        if len((text or '').strip()) > brake.TURN_WALL_MAX:
            return None
        record = self.on_wall(name, text, 'startup')
        if record:
            self.setf(name, state='walled', runtime_ready=0, bridge_available=0)
        return record

    def startup_unusable(self, name, text):
        """A generation a wall switch started whose CLI could not log in (user 2026-11-27; 11-26: pi's "OAuth refresh
        failed ... invalid_grant" blocked two sub-fleets): its account is marked on this run's wall records and the
        pending switch moves on to the next usable model of the chain, like a refused switch (wall_target); the
        slot waits walled for wall_tick. False: not that case, or nothing usable is left (the caller blocks)."""
        info = self.agent(name)
        req = json.loads(info.get('wall_downgrade') or 'null')
        if not req or info.get('wall_switch') != req.get('at') or not runtime_v2.AUTH_ERROR.search(text or ''):
            return False
        here = (req['to_cli'], req['to'])
        self.wall_mark(name, here, 'account')
        self.log('brake.startup_unusable', agent=name, model=here[0] + ':' + here[1], error=(text or '').strip()[:300])
        action = self.wall_decide(name, self.runtime_cfg(name), kind=req.get('kind'))
        wall = json.loads(info.get('wall') or '{}')
        if action['action'] != 'switch':
            self.setf(name, wall_downgrade='', wall_switch='', wall=json.dumps(dict(wall, **action), ensure_ascii=False))
            self.log('brake.switch_exhausted', agent=name, skipped=action['skipped'])
            return False
        req = dict(action, key=self.wall_key(name, action['to_cli']), from_key=self.wall_key(name, action['cli']),
                   source=req.get('source'), at=uuid.uuid4().hex[:12], kind=req.get('kind'))
        self.setf(name, state='walled', runtime_ready=0, bridge_available=0, wall_switch='',
                  wall_downgrade=json.dumps(req, ensure_ascii=False), wall=json.dumps(dict(wall, **action), ensure_ascii=False))
        return True

    def wall_tick(self):
        """Wall side effects (spawn / kill), outside any v2 transaction. At an idle boundary run the pending chain
        switch as the slot's next generation (wall_target); once that generation is READY lift the wall's own
        pause; retry a startup-walled slot after the operator resumed."""
        for name in self.active_workers() + ['supervisor']:
            info = self.agent(name)
            req = json.loads(info.get('wall_downgrade') or 'null')
            if name == 'supervisor' and not self.wall_chained(name, (req or {}).get('kind')):
                continue
            if req and info.get('wall_switch') == req['at']:
                if info.get('state') == 'idle' and info.get('runtime_ready') == '1':
                    self.wall_switched(name, req)
                continue
            in_flight = self.v2 and self.r.hget(self.v2.bus.lease_key(name), 'current_delivery')
            current = self.runtime_cfg(name)
            if req and (agent_cli(current, name) != req['cli']
                        or (current.get(req['from_key']) or '') != (req['from'] or '')):
                # The slot already runs something else (B's roster / night swap, a live profile): never undo it.
                self.setf(name, wall_downgrade='', wall_switch='')
                self.log('brake.downgrade_superseded', agent=name, wanted=req, cli=agent_cli(current, name),
                         model=self.slot_model(name, current))
                req = None
            if req and info.get('state') in ('idle', 'walled') and not in_flight:
                launch, req = self.wall_target(name, json.loads(info.get('runtime_launch') or '{}'), req, current)
                if req:
                    self.setf(name, wall_switch=req['at'])
                    self.wall_rotate(name, launch, 'next model on the chain after a usage wall: ' + req['cli'] + ':'
                                     + str(req['from']) + ' -> ' + req['to_cli'] + ':' + req['to'])
            elif info.get('state') == 'walled' and not self.paused():
                self.wall_rotate(name, json.loads(info.get('runtime_launch') or 'null'), 'retry after the wall pause was resumed')

    STARTUP_RETRY_BACKOFF = (30, 60, 120, 300)  # seconds before retry k+1; the last value repeats
    STARTUP_RETRY_MAX = 24  # failures with the network up; then the old stop (blocked, run paused)
    # Public anycast resolvers on 443, tried in order; any TCP connect = online. Two regions so one blocked
    # route does not read as offline.
    NETWORK_PROBES = (('1.1.1.1', 443), ('223.5.5.5', 443), ('8.8.8.8', 443))

    def network_up(self, timeout=2.0):
        """Any probe answers. An offline answer is reused for 10 s: callers ask every tick while they wait."""
        import socket
        if time.time() < getattr(self, 'offline_until', 0):
            return False
        for host, port in self.NETWORK_PROBES:
            try:
                socket.create_connection((host, port), timeout=timeout).close()
                return True
            except OSError:
                continue
        self.offline_until = time.time() + 10
        return False

    def schedule_startup_retry(self, name, info):
        """A startup that failed on the network (runtime_v2.startup_offline): the slot stays dead, the run keeps
        going, and startup_retry_tick starts its next generation after a backoff. None once the attempts are
        used up."""
        attempt = int(info.get('startup_retries') or 0) + 1
        if attempt > self.STARTUP_RETRY_MAX:
            return None
        wait = self.STARTUP_RETRY_BACKOFF[min(attempt, len(self.STARTUP_RETRY_BACKOFF)) - 1]
        self.setf(name, startup_retries=attempt, startup_retry_at=awake.deadline(wait))
        return attempt, wait

    def startup_retry_tick(self):
        """Start the next generation of a slot whose startup failed on the network, once its backoff is due and
        the network answers. While it does not (lid closed, the daemon ticking in a dark wake) the retry waits:
        no attempt spent, no new generation. The backoff is awake time (§35)."""
        now = awake.now()
        online = None
        for name in ['supervisor'] + self.active_workers():
            info = self.agent(name)
            due = float(info.get('startup_retry_at') or 0)
            if not due or now < due or info.get('state') != 'dead' or self.paused():
                continue
            if self.subfleet_supervisor(name):  # §67: never a new generation of a sub-fleet supervisor here
                self.setf(name, startup_retry_at=0)
                heal.report_up(self, 'dead', name, 'its start failed on the network')
                continue
            if online is None:
                online = self.network_up()
            if not online:
                self.setf(name, startup_retry_at=now + self.STARTUP_RETRY_BACKOFF[0])
                continue
            self.setf(name, startup_retry_at=0)
            self.log('startup.retry', agent=name, attempt=info.get('startup_retries'))
            self.wall_rotate(name, json.loads(info.get('runtime_launch') or 'null'),
                             'retry after a network error at startup')

    def wall_target(self, name, launch, req, current):
        """(launch, req) of the pending switch. A CLI change passes the profile preflight first (binary, pi opt-in /
        home, the actor's own pair check); a refused model is recorded (scope refused) and the next one on the
        chain is taken. (None, None): nothing usable left -- the slot stays walled and the fleet paused (stop)."""
        cli_key = 'supervisor_cli' if name == 'supervisor' else 'worker_cli'
        while True:
            overrides = dict(launch.get('overrides') or {}, **{cli_key: req['to_cli'], req['key']: req['to']})
            if name == 'supervisor' and req['to_cli'] == 'pi':  # the pi supervisor's thinking as start gives it
                overrides['pi_thinking'] = ((self.cfg.get('pi_thinkings') or {}).get('supervisor')
                                            or self.wall_chains().get('supervisor_pi_thinking') or 'high')
            elif req['to_cli'] == 'pi':  # a worker moved to pi: the worker default, not the run's old pi level
                overrides['pi_thinking'] = self.wall_chains().get('worker_thinking') or 'high'
            new = {'profile_id': launch.get('profile_id') or 'wall-switch', 'role_text': launch.get('role_text', ''),
                   'overrides': overrides}
            if req['to_cli'] == agent_cli(current, name):
                return new, req
            try:
                self.preflight_profile(new, cli_change_ok=True, slot=name)
                return new, req
            except (OSError, ValueError, KeyError, SystemExit, subprocess.SubprocessError) as exc:
                problem = str(exc)
            self.wall_mark(name, (req['to_cli'], req['to']), 'refused')
            self.log('brake.switch_refused', agent=name, to=req['to_cli'] + ':' + req['to'], reason=problem[:300])
            action = self.wall_decide(name, current, kind=req.get('kind'))
            if action['action'] != 'switch':
                wall = json.loads(self.agent(name).get('wall') or '{}')
                self.setf(name, wall_downgrade='', wall_switch='', wall=json.dumps(
                    dict(wall, **action), ensure_ascii=False))
                self.log('brake.switch_exhausted', agent=name, skipped=action['skipped'])
                heal.report_up(self, 'switch_failed', name, 'no usable model left on the chain ('
                               + ', '.join(action.get('skipped') or []) + ')')
                return None, None
            req = dict(action, key=self.wall_key(name, action['to_cli']), from_key=req['from_key'],
                       source=req.get('source'), at=uuid.uuid4().hex[:12], kind=req.get('kind'))
            wall = json.loads(self.agent(name).get('wall') or '{}')  # the slot shows the target it now takes
            self.setf(name, wall_downgrade=json.dumps(req, ensure_ascii=False),
                      wall=json.dumps(dict(wall, **action), ensure_ascii=False))

    def wall_switched(self, name, req):
        self.setf(name, wall_downgrade='', wall_switch='')
        if self.cfg.get('model_chain'):  # §58: a job sub-fleet records every switch down its chain
            wall = json.loads(self.agent(name).get('wall') or '{}')
            self.chain_switched(name, req['cli'] + ':' + (req.get('from') or ''), req['to_cli'] + ':' + req['to'],
                                (wall.get('kind') or wall.get('scope') or 'usage') + ' wall')
        owner = json.loads(self.r.hget(BRAKE, 'paused_by') or 'null') or {}
        ours = (owner.get('kind') == 'wall' and owner.get('agent') == name
                and self.r.hget(SERVICE, 'reason') == owner.get('reason'))
        budget = self.brake_budget()
        over = budget and brake.trips(self.brake_card(budget), brake.counters(
            self.r.hgetall(BRAKE), self.quota_reading()))
        self.log('brake.downgraded', agent=name, label=self.label(name), cli=req['to_cli'], to=req['to'],
                 from_cli=req['cli'], from_model=req['from'], resumed=bool(self.paused() and ours and not over))
        if self.paused() and ours and not over:
            # Only the wall's own pause: an operator pause, an F hold or a budget line keeps the fleet paused.
            self.r.hset(BRAKE, mapping={'tripped': '', 'paused_by': ''})
            self.resume()

    def wall_rotate(self, name, launch, why):
        """Same slot, next generation (optionally another launch), queue kept on the slot. Like B's
        roster profile swap: skeleton handoff, saved screen, held launch, tab into place, old tab killed,
        v2 delivery confirmed retired. Failure blocks the task like any profile switch failure."""
        info = self.agent(name)
        generation, old, prev = int(info['generation']), info.get('pane'), self.label(name)
        if not old or self.mgr.get_pane_status(old) is None:
            return self.block_profile(name, why + ': the old runtime tab is missing', dead=True) or False
        handoff_id = uuid.uuid4().hex[:8]
        note = self.run_dir / 'handoffs' / (name + '-g' + str(generation) + '-' + handoff_id + '-wall.md')
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(self.skeleton(name, dict(info, handoff_id=handoff_id)))
        snapshot = note.with_suffix('.terminal.txt')
        self.mgr.capture_to(old, str(snapshot))
        history.record(self.run_dir, slot=name, label=prev, generation=generation, handoff=str(note),
                       snapshot=str(snapshot), source='wall', timed_out=False)
        label = slot_base(name) + '.' + str(generation + 1)  # supervisor-1.1, worker-1.1
        self.setf(name, generation=generation + 1, label=label, prev_label=prev, handoff_id='',
                  runtime_launch=profile_launch.encode_launch(launch) if launch else '', runtime_profile='',
                  launch_ready=0, runtime_ready=0, retired_pane=old)
        if name in self.workers:
            self.r.hset(NAMES, mapping={label: name})
        if self.v2:  # a switch to another CLI: queued text still names the old CLI's reply channel
            self.v2.transaction('wall-reword-' + uuid.uuid4().hex, lambda: self.v2.reword_pending(name))
        try:
            new = self.spawn(name, '[Same-task takeover] You are ' + label + ' (taking over from ' + prev + ': ' + why
                             + '). Read the handoff note ' + str(note) + ' and continue from it. In this turn only '
                             'read the note, do no task work, and make your final reply exactly READY.', launch_held=True)
            if not self.mgr.tab_into(new, old):
                raise RuntimeError('could not place the successor in the worker pane')
            if not self.mgr.kill_pane(old) or self.mgr.get_pane_status(old) is not None:
                raise RuntimeError('could not confirm retirement of the old runtime ' + prev)
        except (Exception, SystemExit) as exc:
            self.setf(name, launch_ready=0, state='dead')
            heal.report_up(self, 'switch_failed', name, why + ' failed: ' + str(exc)[:200])
            return self.block_profile(name, why + ' failed: ' + str(exc), dead=True) or False
        if self.v2:
            self.v2.confirm_retired(name)
        self.setf(name, launch_ready=1, retired_pane='')
        self.refresh_history()
        self.log('brake.wall_rotated', agent=name, label=label, prev=prev, generation=generation + 1, why=why,
                 cli=agent_cli(self.runtime_cfg(name), name), handoff=str(note))
        return True

    def wall_gate(self, name, screen, source='watch'):
        """E before F: the watchdog calls this on every screen it captures, BEFORE the prompt whitelist,
        the unknown-dialog hold and the stall ladder. A wall on the bottom of the screen trips the brake
        (pause, record, notify) and returns the trip record: the caller then presses no key, holds nothing
        and leaves the slot alone this tick (a wall menu is never a dialog to answer). None: no wall."""
        wall = brake.screen_wall(screen)
        if not wall:
            return None
        if name != ADVISOR and not self.wall_new(name, screen, wall):
            return None  # §67: the line was on the screen before the last resume: old text, no wall
        if name == ADVISOR:
            # §27: the advisor's wall never pauses the fleet. Its consultation falls back to the default
            # (the held message) and advisor_tick restarts the tab in place.
            if self.agent(name).get('state') != 'held':
                self.v2.transaction('advisor-wall-' + uuid.uuid4().hex,
                                    lambda: self.v2.hold(name, 'usage wall on the advisor tab'))
            return {'kind': 'wall', 'agent': name, 'advisor': True}
        tripped = json.loads(self.r.hget(BRAKE, 'tripped') or 'null')
        if self.paused() and tripped and tripped.get('kind') == 'wall' and tripped.get('agent') == name:
            return tripped  # already braked on this agent's wall: hands off, no second notify
        tail = '\n'.join((screen or '').rstrip().splitlines()[-brake.GATE_TAIL:])
        if self.v2:
            record = self.v2.transaction('brake-wall-' + uuid.uuid4().hex,
                                         lambda: self.on_wall(name, tail, source, wall=wall))
        else:
            record = self.on_wall(name, tail, source, wall=wall)
        if record:
            self.brake_notify(record)
        return record

    def stalled_screens(self, now):
        """Screens of busy agents past stall_timeout (only while a task / service is running)."""
        limit = float(self.cfg['stall_timeout'])
        if limit <= 0 or not self.running() or self.paused():
            return {}
        return {name: self.capture_screen(name, info) for name, info in ((n, self.agent(n)) for n in self.names if n != ADVISOR)
                if info.get('state') == 'busy' and now - self.last_activity(info) >= limit}

    def capture_screen(self, name, info):
        """The tail of an agent's screen for wall detection ('' if it cannot be read)."""
        pane = info.get('pane')
        if not pane:
            return ''
        path = self.run_dir / 'brake' / (name + '-screen.txt')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.mgr.capture_to(pane, str(path))
            return '\n'.join(path.read_text(errors='replace').splitlines()[-60:])
        except (OSError, AttributeError, RuntimeError, subprocess.SubprocessError):
            return ''

    def write_result(self):
        """Task terminal: write <run>/result.json once per task (start --resume refuses a finished run)."""
        t = {} if self.service else self.task()
        if t.get('status', 'running') == 'running':
            return
        try:
            if load(self.run_dir / 'result.json').get('task_id') == t.get('id', ''):
                return
        except (OSError, ValueError):
            pass
        state = self.contract_state()
        summary = None if state is None else {
            'phase': state['phase'], 'interfaces': {k: e['status'] for k, e in sorted(state['interfaces'].items())},
            'tasks': {m: x['status'] for m, x in sorted(state['tasks'].items())},
            'pending_user_decisions': [dict(d, interface=k) for k, e in sorted(state['interfaces'].items())
                                       for d in e.get('decisions') or [] if d.get('status') == 'pending_user']}
        save_result(self.run_dir, t, contract_summary=summary, decompose_summary=self.tree_final())

    def channel_tick(self):
        """The fleet's one channel actor process (P1f, contracts §8): started for a main service fleet on v2 with
        ``channels:``, restarted with backoff / on a stale heartbeat / on a roster change; nothing without channels.
        Injectable: ``channel_spawn(argv)``, ``channel_clock``."""
        from fleetmux.channels import supervise
        supervise.fleet_tick(self)

    def channel_stop_all(self, drain_s=5):
        """Bounded stop drain of the channel actor (P1f, ruling M10): at most ``drain_s`` plus the 5 s + 5 s stop
        waits; returns at once without an actor of this daemon. Injectable: ``channel_sleep``, ``channel_clock``."""
        from fleetmux.channels import supervise
        return supervise.stop_all(self, drain_s)

    def security_tick(self):
        """Security stages of the daemon tick (contracts §8 Z9): each isolated, one SecurityTickError at the end.
        No policy: every stage returns at once."""
        from fleetmux.channels import auditlog, stop
        stop.run_stages(self, (('stop', stop.daemon_tick), ('audit', auditlog.daemon_pump)))

    def security_keeps_pause(self):
        """#17: start --resume keeps a /stop pause (policy present and a /stop pending or the pause is a /stop's)."""
        from fleetmux.channels import stop
        return stop.keep_pause_on_resume(self)

    def superpowers_refresh(self):
        """A root fleet daemon keeps this machine's machines entry fresh (fleet_superpowers_refresh.daemon_tick:
        at start, then every hot.refresh_seconds, in a background thread, one daemon per machine at a time)."""
        if getattr(self, 'r', None) is None or self.cfg.get('parent_config'):  # no bus: not a running daemon
            return
        from fleetmux import fleet_superpowers_refresh
        if not hasattr(self, 'superpowers_refresh_state'):
            self.superpowers_refresh_state = {}
        done = fleet_superpowers_refresh.daemon_tick(self.superpowers_refresh_state)
        if done.startswith('failed: '):
            self.log('tick.error', part='superpowers_refresh', error=done[len('failed: '):])

    def builtin_applets(self):
        """The built-in applets join the roster's applets (relay_applet: the relay when relay.enabled, on a root
        v2 service fleet only). Read at daemon start and resume and at every roster reload: a change of the switch
        takes effect at the next daemon restart or roster reload."""
        from fleetmux import relay_applet
        current = getattr(self, 'applet_specs', None)
        try:
            self.applet_specs = relay_applet.compose(current, getattr(self, 'cfg', None),
                                                     getattr(self, 'workers', None))
        except ValueError as exc:  # e.g. no worker row for the applet lease policy: said once per cause
            self.applet_specs = [e for e in current or [] if e.get('name') not in relay_applet.NAMES]
            if getattr(self, 'builtin_applet_error', None) != str(exc):
                self.builtin_applet_error = str(exc)
                self.log('applet.builtin_error', level='error', error=str(exc)[:300])
            return
        self.builtin_applet_error = None

    def superpowers_flush(self):
        """quota.json into the hot quota block when it changed, then the dirty hot blocks back to the file."""
        if getattr(self, 'r', None) is None:  # no bus yet: nothing was loaded
            return
        from fleetmux import fleet_superpowers_hot
        fleet_superpowers_hot.ingest_quota(self.r, self.quota_source())
        fleet_superpowers_hot.flush(self.r)

    def tick(self):
        # Each part is guarded: under v2 this runs in the daemon loop, and one failing part (a bad
        # roster, a broken sub-fleet record, a full disk) must not kill the daemon. Bus errors stay fatal.
        if getattr(self, 'r', None) is not None:  # §35: fold the sleep since the last tick into the awake clock, publish
            awake.tick(self.r)
        if self.v2:
            self.v2.gap_tick()  # a daemon gap (lid-closed sleep) is known before any part judges a wait (§35)
        for part, run in (('roster', self.roster_tick),  # the only place the roster changes
                          ('advisor', self.advisor_tick),  # resident advisor tab: start / retire / restart (§27)
                          ('lifecycle', lambda: lifecycle_close.tick(self)),  # mode policy (done gate, hold, timeouts, idle); before result.json
                          ('baseline', lambda: baseline_check.tick(self)),  # switch baseline: restore hot, report the rest
                          ('result', self.write_result), ('contracts', self.sync_contract_files),
                          ('tree', self.sync_tree_files),  # §62: tree.json follows fx:tree
                          ('tree_summary', self.tree_summary_tick),  # §62: the opening summary, once
                          ('decompose_params', self.decompose_flush),  # §62: write-back, root fleets only
                          ('tree_overrun', self.tree_overrun_tick),  # §62: overrun on the chain -> supervisor
                          ('subfleet', lambda: subfleet.tick(self)),  # side effects, outside any v2 transaction
                          ('history', self.history_tick),  # a lost history pane is rebuilt once (design section 2)
                          ('tabs', lambda: tabs.open_all(self)),  # companion tabs (§31): once per daemon start
                          ('tab_watch', lambda: tabs.watch(self)),  # §31 restart: on-exit + `tab restart`
                          ('fleetmux', lambda: fleetmux_sidebar.tick(self)),  # fleetmux sidebar cards (kv sidebar)
                          ('gui_keep', lambda: gui_keep.tick(self)),  # root only: keep the `fleetmux gui` window open
                          ('jobs', self.job_tick),  # §58: queued jobs take a free slot (outside transactions)
                          ('escalations', self.escalation_tick), ('waiting', self.waiting_tick),
                          ('notices', self.relay_notices), ('vetoes', self.apply_vetoes),
                          ('channels', self.channel_tick),  # the channel actor process (P1f): spawn / watch / restart
                          ('security', self.security_tick),  # P8m /stop (then P8g audit mirror); no policy: no-op
                          ('applets', lambda: applet_host.tick(self)),  # applet processes (applets.md section 2)
                          ('applet_views', lambda: applet_view.tick(self)),  # one tab per applet in the daemon area
                          ('applet_leases', lambda: applet_lease.tick(self)),  # leases (applets.md section 3)
                          ('superpowers', self.superpowers_flush),  # quota.json in, hot inventory blocks back to the file
                          ('superpowers_refresh', self.superpowers_refresh),  # root only: machines.<self>
                          ('restart', lambda: restart_state.tick(self)),  # section 60: boot reconcile, ledger, timer snapshot
                          ('release_drift', lambda: release_drift.tick(self))):  # §56: root only, onto the installed release
            try:
                run()
            except BusError:
                raise
            except Exception as exc:
                self.log('tick.error', part=part, error=repr(exc))
        try:  # a parent/child page link was clicked: switch to that fleet's workspace (never fatal)
            subfleet.follow_jump(self)
        except Exception as exc:
            self.log('history.jump_error', error=str(exc))
        if self.v2:
            # v2 holds a stalled slot inside RuntimeV2.tick (Fleet.on_stall is v1 only): read the
            # screens of slots about to be held first, so a wall pauses the fleet instead.
            screens = {n: brake.own_text(x) for n, x in self.stalled_screens(time.time()).items()
                       if not (brake.screen_wall(x) and not self.wall_new(n, x, brake.screen_wall(x)))}
            # section 67: rows that show a fleet delivery are not the CLI's own (own_text drops them)
            record = self.v2.transaction('brake-' + uuid.uuid4().hex, lambda: next(
                (r for r in [self.on_wall(n, text, 'screen') for n, text in screens.items()] if r), None)
                or self.brake_tick(time.time()))
        else:
            record = self.brake_tick(time.time())
        for done in [r for r in self.pending_notify + [record] if r]:
            self.brake_notify(done)
        self.pending_notify = []
        self.wall_tick()  # spawn / kill: outside any v2 transaction
        self.startup_retry_tick()  # same
        if self.v2:
            return self.v2.tick()
        now = time.time()
        for name in self.names:
            info = self.agent(name)
            if (info.get('state') == 'starting' and info.get('runtime_profile')
                    and awake.passed(info.get('profile_ready_deadline', '0'))):
                self.block_profile(name, 'profile runtime timed out before READY', dead=True)
                continue
            if info.get('state') == 'handoff' and now >= float(info.get('handoff_deadline', 0)):
                self.finish_rotation(name, timed_out=True)
        t = self.task()
        if t.get('status') == 'running' and awake.now() >= task_deadline(t):  # awake time (§35)
            self.finish('budget_exhausted', 'time budget exhausted')
        if self.service:
            self.expire_requests(awake.now())  # the request clock is awake seconds (§35)
        self.check_stalls(now)
        if now >= self.next_liveness:
            self.next_liveness = now + 30
            for name in self.names:
                pane = self.agent(name).get('pane')
                if pane and self.mgr.get_pane_status(pane) is None:
                    self.setf(name, state='dead', runtime_ready=0, current_profile='',
                              profile_state='blocked', profile_reason='runtime tab was closed')
                    if self.running():
                        self.finish('blocked', name + ' tab was closed')

    def capture_terminal_theme(self):
        """Ask the control tab's terminal for its background/foreground once (design §39). The daemon runs
        in that tab's pane, so the answer is the colour the bash panel itself uses; a terminal that does
        not answer just leaves no <run>/terminal_theme.json and the history page keeps its own colours.
        Never raises and never blocks longer than the probe's own timeout."""
        theme = terminal_theme.capture(self.run_dir)
        if theme:
            self.log('theme.captured', bg=theme.get('bg'), fg=theme.get('fg'))
        else:
            self.log('theme.unavailable')
        return theme

    def run(self):
        # Before start_all(): the first write_page must already see <run>/terminal_theme.json. A resume
        # re-spawns the control window and calls run() again, so a theme switch is picked up next start.
        self.capture_terminal_theme()
        if not self.mgr.get_or_create_session() and run_backend(self.cfg) != 'cmux':
            # No session, no fleet: stop with the backend's reason (Redis and the run stay for --resume).
            # cmux is left as it was: its session is a `cmux ping`, and a daemon running inside a cmux tab
            # has always carried on past a missed ping (workspaces are created on the first spawn).
            why = getattr(self.mgr, 'last_error', '') or 'no reason given'
            self.log('backend.session_failed', backend=run_backend(self.cfg), reason=why)
            raise SystemExit('backend ' + run_backend(self.cfg) + ' could not create its session: ' + why)
        awake.tick(self.r)  # §35: go on from the fleet's published awake clock before anything is stamped
        sandbox.startup_audits(self)  # P8o / M172: one security.sandbox_off per degraded slot (no policy: no-op)
        self.wall_chains_load()  # start and resume alike: walls read the chains from Redis only
        self.builtin_applets()  # relay.enabled: the built-in relay applet, also without a roster file
        try:  # §62: a running task's tree lost with a Redis that keeps no AOF comes back from tree.json
            self.tree_restore()
        except Exception as exc:
            self.log('tick.error', part='tree_restore', error=repr(exc))
        try:  # §62: the decompose parameters into this fleet's Redis; never fatal
            self.decompose_load()
        except Exception as exc:
            self.log('tick.error', part='decompose_load', error=repr(exc))
        try:  # fleet-superpowers hot blocks (models, quota) into this fleet's Redis; never fatal
            from fleetmux import fleet_superpowers_hot
            fleet_superpowers_hot.load(self.r)
            fleet_superpowers_hot.ingest_quota(self.r, self.quota_source())
        except Exception as exc:
            self.log('tick.error', part='superpowers_load', error=repr(exc))
        if self.v2:
            return self.v2.run()
        self.start_all()
        while not self.stopping:
            self.r.set('fx:daemon:beat', str(time.time()))  # liveness for operators: a hung loop stops beating
            item = self.r.blpop(EVENTS, timeout=2)
            if item:
                try:
                    self.handle(json.loads(item[1]))
                except ValueError as exc:
                    self.log('event.bad', error=str(exc))
            self.pump()
            self.tick()


# ------------------------------------------------------------ operator CLI
def save_result(run, task, status=None, reason=None, contract_summary=None, decompose_summary=None):
    """<run>/result.json (atomic): the run is finished. Artifacts are re-checked on disk here."""
    workspace = Path(run) / 'workspace'
    artifacts = json.loads(task.get('artifacts') or '[]')
    extended = extended_checks(task)
    result = {
        'status': status or task.get('status', ''), 'reason': task.get('reason', '') if reason is None else reason,
        'task_id': task.get('id', ''), 'goal': task.get('goal', ''), 'at': time.time(),
        'artifacts': [dict(a, problems=check_artifacts([a], workspace, extended)) for a in artifacts]}
    if contract_summary:
        result['contracts'] = contract_summary
    if decompose_summary:  # §62: the final review table and the tune applied in this fleet
        result['decompose'] = decompose_summary
    try:
        cfg = load(Path(run) / 'config.json')
    except (OSError, ValueError):
        cfg = {}
    result.update(mode=cfg.get('lifecycle'), base_mode=cfg.get('mode', 'task'))  # mode = lifecycle (None: legacy run)
    chain = run_model_chain(run, cfg)
    if chain:  # §58 (D17): a job sub-fleet's models, skipped entries and wall switches
        result['model_chain'] = chain
    save(Path(run) / 'result.json', result)


def run_model_chain(run, cfg=None):
    """§58: the model_chain of a job sub-fleet run (<run>/model_chain.json, else its config.json), or None."""
    for path in (Path(run) / MODEL_CHAIN_FILE, Path(run) / 'config.json'):
        try:
            data = load(path) if path.name == MODEL_CHAIN_FILE or cfg is None else cfg
        except (OSError, ValueError):
            continue
        chain = data if path.name == MODEL_CHAIN_FILE else data.get('model_chain')
        if chain:
            return chain
    return None


def start_redis(cfg, tries=30):
    """Start the run's private Redis on its pinned socket and directory. v2 has AOF on, so a
    restart with the same --dir reloads everything written up to the last fsync (about 1 s)."""
    redis_dir = Path(cfg['redis_runtime_dir'])
    if cfg.get('run'):
        save(redis_dir / REDIS_OWNER, {'run': str(cfg['run'])})  # lets reap_orphan_redis find the run later
    subprocess.run([cfg['redis_server'], '--port', '0', '--unixsocket', cfg['redis_socket'],
                    '--unixsocketperm', '700', '--dir', str(redis_dir), '--dbfilename', 'fleet.rdb',
                    '--appendonly', 'yes' if cfg.get('transport_version') == 2 else 'no',
                    '--appendfsync', 'everysec', '--save', '60 1', '--daemonize', 'yes',
                    '--pidfile', str(redis_dir / 'redis.pid'), '--logfile', str(redis_dir / 'redis.log')], check=True)
    for _ in range(tries):
        try:
            return connect(cfg)
        except Exception:
            time.sleep(.1)
    raise SystemExit('private Redis did not become ready')


REDIS_OWNER = 'owner.json'
ORPHAN_REDIS_GRACE = 120  # seconds between start_redis and the daemon taking its lock


def _own_processes():
    """(pid, command line) of this user's processes; [] when ps fails."""
    try:
        out = subprocess.run(['ps', '-xww', '-o', 'pid=,command='], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(' ')
        if pid.isdigit():
            rows.append((int(pid), command))
    return rows


def _run_alive(run, procs):
    """A daemon holds the run's lock, or any live process (actor, hook, child fleet) names the run."""
    if (Path(run) / 'daemon.lock').exists() and daemon_running(run):
        return True
    return any(str(run) in command for pid, command in procs if 'redis-server' not in command)


def reap_orphan_redis(keep=None, grace=ORPHAN_REDIS_GRACE, root=None):
    """Shut down private Redis instances whose run was abandoned without `stop` (v2 keeps Redis
    up when a daemon dies, for --resume). Lossless: SHUTDOWN saves, and --resume restarts Redis
    from the same directory. Only instances with an owner record are touched; a sub-fleet's Redis
    is left to its parent while any ancestor run is alive. `root` limits the sweep to one runtime
    root (tests). Returns the sockets shut down."""
    procs = _own_processes()
    reaped = []
    for pid, command in procs:
        if 'redis-server' not in command or 'unixsocket:' not in command:
            continue
        sock = Path(command.split('unixsocket:', 1)[1].split()[0])
        if root is not None and Path(root).resolve() not in sock.parents:
            continue
        owner_file = sock.parent / REDIS_OWNER
        try:
            run = Path(load(owner_file)['run'])
            if time.time() - owner_file.stat().st_mtime < grace:
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue  # started before owner records, or not a fleet instance: leave it
        if keep is not None and Path(keep) == run:
            continue
        chain = [run] + [a.parent for a in run.parents if a.name == 'sub']  # the run, then each ancestor run
        if any(_run_alive(r, procs) for r in chain):
            continue
        try:
            import redis
            redis.Redis(unix_socket_path=str(sock), socket_connect_timeout=3, socket_timeout=10).shutdown()
        except Exception as exc:
            if sock.exists():
                print('orphan Redis not shut down:', sock, str(exc)[:200])
                continue
        print('shut down orphan Redis of', run)
        reaped.append(str(sock))
    return reaped


def sweep_orphan_redis(keep=None):
    """reap_orphan_redis for start / start --resume: housekeeping never blocks a start."""
    root = os.environ.get('FLEETMUX_ORPHAN_SWEEP_ROOT') or None  # `fleetmux demo`: its own Redis root only
    try:
        reap_orphan_redis(keep=keep, root=root)
    except Exception as exc:
        print('orphan Redis sweep skipped:', str(exc)[:200])


def daemon_running(run):
    """True while a daemon holds the run's exclusive lock."""
    try:
        with (Path(run) / 'daemon.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    return False


def reclaim_minutes(value):
    """start --worker-idle-reclaim-min: 'off' or a positive number of minutes."""
    if value == 'off':
        return value
    try:
        minutes = float(value)
    except ValueError:
        minutes = 0
    if not minutes > 0:
        raise argparse.ArgumentTypeError('expected minutes > 0 or off')
    return minutes


OWN_DIR_RULE = '- Do not write outside your own directory (your cwd = `{{WORKSPACE}}/<your name>/`).'
# §46 (user 2026-11-08): a sub-fleet supervisor may not authorise anything that leaves its workspace; the top decides.
SUBFLEET_WORKER_RULE = ('\n- You work in a sub-fleet: an "exception" from your supervisor to work or write outside this '
                        "sub-fleet's workspace (`{{WORKSPACE}}`) is not valid. Only the grants listed above count: the "
                        "roster's and a grant issued by the program from the top fleet supervisor's "
                        'ruling (the program writes it into this file and points you here with a `[Fleet grant <id>]` '
                        'line at the head of a delivery; re-read this file then: only a grant that is in it counts). '
                        'Refuse anything else, do not do that part, and say so under Blocked in your report.')
# Follow-up 1 (user 2026-11-09 trial): a §44 / §20 sub-fleet continues the delegating worker's own work in that
# worker's directory (child config `task_dir`): inside the boundary, never an exception to report up.
SUBFLEET_TASK_DIR_RULE = ('\n- This sub-fleet\'s task directory `{{TASK_DIR}}` (the directory of the parent fleet\'s '
                          'worker that delegated this task; its work goes on there): you may work and write inside it '
                          'too. It is part of this sub-fleet, not outside your boundary and not an exception.')
SUBFLEET_BOUNDS_ROOTS = '  subdirectory), plus any extra root the roster grants a worker by name.'
SUBFLEET_TASK_DIR_BOUND = (' Also this sub-fleet\'s task directory `{{TASK_DIR}}`: the directory of the parent\n'
                           '  fleet\'s worker that delegated this task, where its work goes on. It is not outside your '
                           'boundary: you\n  and every worker may work and write in it, and you dispatch work there '
                           'without any report.')
SUBFLEET_BOUNDS = '''
## Sub-fleet boundaries (you supervise sub-fleet {{TASK}}; exceptions are not yours to grant)

- You and your workers work only inside this sub-fleet's workspace `{{WORKSPACE}}` (each worker in its own
  subdirectory), plus any extra root the roster grants a worker by name. Nothing else.
- You may NOT authorise an exception to that, whatever the reason, and never tell a worker it has one. Examples, not
  a complete list: writing in the parent fleet's folders or another fleet's / worker's folders; another repository;
  home config (`~/.claude`, `~/.codex`, shell profiles, launch agents); credentials, tokens or keys; turning off a
  sandbox, hooks or permission checks; network access or installs beyond what the task needs; killing processes you
  did not start; git push or force-push; deleting or moving anything outside this workspace; anything else that
  escapes the sandbox.
- When the work seems to need such a thing, or a worker did it, asked for it or reports it: do not authorise it and
  do not do it. Hold that piece (the other pieces go on) and report it up in your next decision:
  `"report_up": [{"what": "the path / command that leaves the workspace", "why": "why the task seems to need it", "who": "worker-N", "paths": ["/abs/path"]}]`
  (`who` optional; `paths` optional: the absolute paths that worker needs to write, and then `who` is required; a
  RUNNING decision with `report_up` may dispatch nothing). A worker's `[blocked]` about it: answer `reject` (note:
  reported up) and add the `report_up`.
- The program sends it to the top fleet's supervisor; only that ruling counts. It comes back as
  `[top ruling] report:<task>:<id> approve: <note>` or `... reject: <note>`; until then that piece waits. `approve`:
  go exactly as far as the note says, no further. With `paths`, the program itself grants that worker the approved
  paths (all or fewer of what you reported) until this sub-fleet ends: it is in the worker's AGENTS.md / CLAUDE.md
  and your next dispatch to it carries a `[Fleet grant]` line; just dispatch the piece. Without `paths` (network,
  installs, anything that is not a path to write) nothing is granted by the program: a worker still refuses your
  "exception", so do that piece another way inside the workspace or report BLOCKED. `reject`: drop that piece or do
  it another way inside the workspace.
'''


def granted_roots_notice(cfg, name):
    """The roster's extra writable roots of one worker, appended to the own-directory rule; '' without any."""
    path = cfg.get('roster')
    try:
        roots = roster.writable_roots(Path(path).read_bytes()).get(name) if path else None
    except (OSError, ValueError, TypeError, KeyError):
        roots = None
    if not roots:
        return ''
    return ('\n- Exception granted by the roster for you only: you may also write inside '
            + ', '.join('`' + r + '`' for r in roots)
            + ' (a `host:path` root means that path on that host over ssh). Everything else outside your own'
              ' directory stays forbidden.')


def program_grants_notice(grants):
    """§46: the grants the program issued to one sub-fleet worker from top rulings (``fx:agent:<slot>`` ``grants``),
    appended to the own-directory rule like the roster's; '' without any."""
    return ''.join('\n- Grant issued by the program from the top fleet supervisor\'s ruling ' + g['report']
                   + ', for you only and until this sub-fleet ends: you may also write inside '
                   + ', '.join('`' + p + '`' for p in g['paths']) + '. Everything else outside your own directory '
                   'stays forbidden.' for g in grants)


def command_base(cfg):
    """The argv prefix of this run's own fleetmux: its pinned locations in front (``env ...``), then its python and
    this fleet.py, so a command pasted as-is in any terminal acts on this run."""
    env = [k + '=' + str(cfg[c]) for k, c in (('FLEETMUX_HOME', 'fleet_home'), ('FLEETMUX_STATE', 'cmux_state_dir'),
                                              ('FLEETMUX_CONFIG', 'private_config'), ('CMUX_BIN', 'cmux_bin'))
           if cfg.get(c) and cfg[c] != fleet_config.NONE]
    return (['env'] + env if env else []) + [cfg['python'], str(Path(__file__).resolve())]


def daemon_down_section(cfg, watched):
    """§36: the supervisor's AGENTS.md section with this run's exact restart / status / log commands (the run's
    pinned locations in front, so a command pasted as-is in any pane acts on this run). ``watched``: a pi supervisor,
    whose fleet extension checks the heartbeat itself (Claude / Codex supervisors have no such loop)."""
    base = command_base(cfg)
    run, config = str(Path(cfg['run'])), str(Path(cfg['run']) / 'config.json')
    restart = shlex.join(base + ['start', '--allow-development-v2', '--resume', run])
    status_cmd = shlex.join(base + ['status', '--config', config])
    logs = shlex.join(base + ['logs', '--config', config, '--json', '--limit', '100000']) + ' | tail -n 30'
    # P6d (brief v2 §2.2): with channels (and not a sub-fleet), one channel step between PROGRESS.md and the
    # restart, the later steps renumbered; without channels the text below is byte-for-byte what it was.
    notify = ''
    if cfg.get('channels') and not cfg.get('parent_config'):
        from fleetmux import channels
        send = shlex.join(base + ['channel', 'send', '--config', config, '--event', 'daemon_down', '--text',
                                  channels.fleet_label(cfg) + ' daemon down: heartbeat stale, restarting it now'])
        notify = ('4. Tell the user once through the run\'s channels: `' + send + '`. Run it once only: if it fails, '
                  'do not retry it and go on to the next step; a failed message never blocks the restart.\n')
    n = 5 if notify else 4
    return ('\n## Daemon down (the one fleet command you may run)\n\n'
            'The fleet daemon delivers every message to you and beats about once a second. '
            + ('Your session checks that heartbeat on its own and starts a turn with a `[Daemon watchdog]` message '
               '(the facts from the logs) when it is stale and no daemon holds this run. On that message, or when '
               'the user says the daemon is down:\n\n'
               if watched else 'Nothing in your session checks that heartbeat. When the user says the daemon is down:\n\n')
            + '1. Check: `' + logs + '` (the last audit records and their times) and `' + status_cmd + '` '
            '(`daemon.heartbeat_age_s`: seconds since the last heartbeat).\n'
            '2. If the heartbeat is fresh again (under 10 s), the daemon is back: stop here.\n'
            '3. Append one line to PROGRESS.md in your directory: the time, how long the daemon was down, the last '
            'event, and that you are restarting it. Do this first: the restarted daemon replaces this session with '
            'a new supervisor generation within seconds and briefs it.\n'
            + notify
            + str(n) + '. Run once: `' + restart + '`. A live daemon makes it print "nothing to resume"; that is fine.\n'
            + str(n + 1) + '. Verify with the status command that `daemon.heartbeat_age_s` is small again.\n\n'
            'Never run the restart command a second time for the same outage, never start a daemon any other way, '
            'never stop or kill a process, tab or Redis. If the command fails (for example a sandbox refuses it), '
            'do not retry: say in your reply that the daemon is down and give the command and its error for the '
            'human. This section is the only exception to "do not call the fleet CLI".\n')


class NotContinuable(SystemExit):
    """`start --resume`: a run with open jobs that can never run again here (this release refuses its config, its
    Redis directory is gone or its Redis does not load). `start` takes its open jobs over into a new run
    (section 58 "Restart"); a plain SystemExit (a bad flag, the backend not answering, ...) is fixed and retried instead."""


def refused(run, text):
    """NotContinuable when ``run`` holds open jobs (they go to a new run), else the plain SystemExit as before."""
    return NotContinuable(text) if open_job_rows(run) else SystemExit(text)


def open_job_rows(run):
    """section 58 "Restart": the open jobs <run>/jobs.json records ({} when none, or unreadable)."""
    try:
        rows = load(Path(run) / JOBS_FILE)
    except (OSError, ValueError):
        return {}
    return rows if isinstance(rows, dict) else {}


def write_job_mirror(cfg, run_dir, jobs):
    """section 58 "Restart": write <run>/jobs.json = ``jobs`` and name the run in <fleet home>/job-runs/ while it holds
    open jobs (the body of Fleet.job_mirror; `fleetmux upgrade` writes the same files from its snapshot taken before
    the fleet stops). Raises OSError."""
    save(Path(run_dir) / JOBS_FILE, jobs)
    job_index(cfg, Path(run_dir), bool(jobs))


def job_index(cfg, run, open_):
    """<fleet home>/job-runs/<run dir name>.json while ``run`` holds open jobs: `start` finds the run through it even
    when latest.json names another run or a stub, or the run lives under --runs-dir / --project-dir."""
    home = (cfg or {}).get('fleet_home')
    if not home:
        return
    path = Path(home) / JOB_INDEX / (Path(run).name + '.json')
    if open_:
        if not path.exists():
            save(path, {'run': str(run), 'config': str(Path(run) / 'config.json')})
    else:
        path.unlink(missing_ok=True)


def continue_supervisor(cfg, args):
    """section 60: the supervisor settings a continued run takes (its root supervisor is never reattached: resume_run
    starts its next generation from this config) and the typed start options it does not apply, as text. A different
    supervisor CLI is not applied (its delivery channel is fixed for the run): stop --final, then start."""
    from fleetmux import join
    try:
        typed = join.given(args)
    except Exception:  # noqa: BLE001 - an args object without the start parser's fields
        return []
    skipped = []
    cli = typed.get('supervisor_cli')
    now_cli = cfg.get('supervisor_cli') or 'codex'
    if cli and cli != now_cli:
        skipped.append('--supervisor-cli ' + str(cli) + ' (this run\'s supervisor runs ' + now_cli + '; a CLI change '
                       'needs a new run: stop --final, then start; stop --final cancels the open jobs, so to keep '
                       'them wait until they are done, then change the CLI)')
    else:
        uses = SUPERVISOR_CONTINUE_BY_CLI.get(now_cli, ())
        applied = []
        for k in SUPERVISOR_CONTINUE:
            if k not in typed or typed[k] == cfg.get(k):
                continue
            if k not in uses:  # this run's supervisor CLI does not read it: never printed as applied
                skipped.append('--' + k.replace('_', '-') + ' ' + str(typed[k]) + ' (this run\'s supervisor runs '
                               + now_cli + ', which does not use it)')
                continue
            print('start: the continued run\'s next supervisor generation runs --' + k.replace('_', '-') + ' '
                  + str(typed[k]) + ' (was ' + str(cfg.get(k)) + ')')
            cfg[k] = typed[k]
            if k in ('supervisor_pi_model', 'supervisor_pi_thinking'):  # what a pi supervisor's launch reads
                table = 'pi_models' if k == 'supervisor_pi_model' else 'pi_thinkings'
                cfg[table] = dict(cfg.get(table) or {}, supervisor=typed[k])
            applied.append(k)
        if applied:  # the daemon drops the supervisor's old launch record once (a wall / fallback card would win)
            cfg['supervisor_continue'] = {'id': uuid.uuid4().hex[:12], 'keys': applied}
    skipped += sorted('--' + k.replace('_', '-') for k in typed
                      if k not in CONTINUE_APPLIED and k not in ('supervisor_cli', 'resume', 'config'))
    if skipped and not getattr(args, 'not_applied_printed', False):
        print('start: not applied to the continued run (listed in its reconcile ledger): ' + ', '.join(skipped))
    elif skipped and skipped[0].startswith('--supervisor-cli'):  # the short list above names it without the why
        print('start: not applied to the continued run (listed in its reconcile ledger): ' + skipped[0])
    return skipped


def continue_problem(run):
    """'' when ``run`` is a root service run of transport v2 (continued by `start`); why it cannot be continued
    (its open jobs go to a new run); None when it is no root service run (a task root's jobs follow design section 19)."""
    try:
        cfg = load(Path(run) / 'config.json')
    except (OSError, ValueError):
        return 'no readable config.json'
    if not isinstance(cfg, dict) or cfg.get('parent_config') or (cfg.get('mode') or 'task') != 'service':
        return None
    if cfg.get('transport_version') != 2:
        return 'only Redis v2 runs can be resumed'
    return ''


def carry_candidates(home):
    """Runs of fleet home ``home`` with open jobs and no live daemon, newest first: the run latest.json names, the
    home index (job-runs/), then <home>/runs/* (runs from before the index)."""
    home = Path(home)
    found = []
    try:
        latest = load(home / 'latest.json')
        if isinstance(latest, dict) and latest.get('run') and 'join' not in latest:
            found.append(Path(latest['run']))
    except (OSError, ValueError):
        pass
    index = home / JOB_INDEX
    for p in sorted(index.glob('*.json')) if index.is_dir() else []:
        try:
            found.append(Path(load(p)['run']))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    runs = home / 'runs'
    if runs.is_dir():
        found += sorted(p for p in runs.iterdir() if p.is_dir())
    out, seen = [], set()
    for run in found:
        key = str(run.expanduser().resolve())
        if key in seen or not run.is_dir() or not open_job_rows(run):
            continue
        seen.add(key)
        if (run / 'daemon.lock').is_file() and daemon_running(run):
            continue
        out.append(run)
    return sorted(out, key=lambda r: r.name, reverse=True)


def continue_home(args):
    """section 58 "Restart", `start` / `start --resume auto`: continue the newest run of this fleet home that was stopped (or
    died) with open jobs. Runs that cannot be continued go to ``args.carry_from`` (the new run takes their open jobs
    over). A retryable refusal exits non-zero with the jobs left in place. True when a run was continued."""
    tried = {c['run'] for c in args.carry_from}
    for run in carry_candidates(HOME):
        if str(run) in tried:
            continue
        problem = continue_problem(run)
        if problem is None:
            continue
        if problem:
            args.carry_from.append({'run': str(run), 'reason': 'cannot resume ' + str(run) + ': ' + problem})
            continue
        from fleetmux import join
        count = len(open_job_rows(run))
        skipped = sorted('--' + k.replace('_', '-') for k in join.given(args) if k not in CONTINUE_APPLIED)
        print('start: continuing ' + str(run) + ' (stopped with ' + str(count) + ' open job(s)); not applied: '
              + (', '.join(skipped) if skipped else 'none') + ' (listed in its reconcile ledger)')
        args.not_applied_printed = True
        try:
            if resume(run, args):
                return True
        except NotContinuable as exc:
            print('start: ' + str(exc.code) + '; its open jobs go to a new run')
            args.carry_from.append({'run': str(run), 'reason': str(exc.code)})
            continue
        except SystemExit:
            sys.stderr.write('start: ' + str(count) + ' open job(s) of ' + str(run) + ' stay queued there; fix this '
                             'and run start again\n')
            raise
    return False


def resume_target(value):
    """The run to reattach for start --resume, or None: 'auto' with nothing unfinished starts a new run."""
    auto = value == 'auto'
    try:
        run = Path(load(LATEST)['run']) if auto else Path(value).expanduser().resolve()
    except (OSError, ValueError, KeyError, TypeError):
        print('--resume auto: no latest run; starting a new run')
        return None
    if run.name == 'config.json':
        run = run.parent
    problem = None
    if (run / 'result.json').exists() and not resumable(run) and not (open_job_rows(run)
                                                                      and continue_problem(run) == ''):
        problem = 'run already finished (result.json exists)'
    else:
        try:
            if load(run / 'config.json').get('transport_version') != 2:
                problem = 'only Redis v2 runs can be resumed'
        except (OSError, ValueError):
            problem = 'no readable config.json'
    if problem and not auto:
        raise SystemExit('cannot resume ' + str(run) + ': ' + problem)
    if problem:
        print('--resume auto: ' + str(run) + ': ' + problem + '; starting a new run')
        return None
    return run


def resumable(run):
    """True when the run was stopped by a lifecycle hold timeout (stop_keep_resumable)."""
    try:
        return load(Path(run) / 'result.json').get('resumable') is True
    except (OSError, ValueError):
        return False


def run_result(run):
    """The run's result.json as a dict, {} when it is missing or unreadable (never raises)."""
    try:
        value = load(Path(run) / 'result.json')
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def result_state(run):
    """``(present, value)`` for the run's result.json: ``present`` False = no file at all; ``value`` None = the
    file exists but cannot be read or parsed, or is not a JSON object. An unreadable file is never reported as an
    empty result (A42-LINUX-RESTORE-FAILURE-04 P-04-CLASSIFY)."""
    path = Path(run) / 'result.json'
    try:
        text = path.read_text()
    except FileNotFoundError:
        return False, {}
    except OSError:
        return True, None
    try:
        value = json.loads(text)
    except ValueError:
        return True, None
    return True, (value if isinstance(value, dict) else None)


def clear_resumable_result(run, task):
    """Drop a sidebar-closed run's result.json: the task never left ``running``, so --resume reopens it.

    Only the fleetmux sidebar closes a run without finishing its task (``request_close(resumable=True)``
    does not call finish): the stop tail still writes result.json, which would make a later stop and every
    ``start --resume`` read the run as finished. Everything else keeps the file.
    """
    if task.get('status', 'running') != 'running' or not resumable(run):
        return False
    try:
        (Path(run) / 'result.json').unlink()
    except OSError:  # no result.json: nothing to clear
        return False
    return True


SECURITY_FILE = 'security.yaml'  # brief v2.2 §8.1: channels.config_dir()/security.yaml, never in the roster


def _security_file(path, stall_window):
    """Read and parse one security.yaml -> (policy, absolute source). ValueError names the path and the keys,
    never a value; a file that exists but cannot be read is refused, never taken as "no file"."""
    from fleetmux.channels import security as channel_security
    try:
        src = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError('security policy ' + str(path) + ' rejected: path not usable (' + type(exc).__name__ + ')')
    where = 'security policy ' + str(src) + ' rejected'
    if src.is_dir():
        raise ValueError(where + ': a directory, not a file')
    try:
        text = src.read_text(encoding='utf-8')
    except FileNotFoundError:
        raise ValueError(where + ': no such file') from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(where + ': not readable (' + type(exc).__name__ + ')') from None
    policy, errors = channel_security.parse_security(text, stall_window=stall_window)
    if errors:
        raise ValueError(where + ':\n' + '\n'.join(errors))
    return policy, str(src)


def resolve_security(args, *, stall_window):
    """start: the run's security policy -> (policy | None, absolute source | None). --no-security never looks
    at the config dir; --security PATH wins; else channels.config_dir()/security.yaml when it exists (an empty
    file = every default). ValueError on a bad / unreadable file (core-2 P8-start-plan §2, D3 / D4)."""
    if getattr(args, 'no_security', False):
        return None, None
    if getattr(args, 'security', None):
        return _security_file(args.security, stall_window)
    from fleetmux import channels
    default = channels.config_dir() / SECURITY_FILE
    try:
        os.stat(default)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise ValueError('security policy ' + str(default) + ' rejected: not readable (' + type(exc).__name__
                         + ')') from None
    return _security_file(default, stall_window)


def _pinned_security(value, stall_window):
    """Validate a policy already pinned in a config (legacy run without a source, a sub-fleet) -> the policy.
    Empty or not a mapping is refused: the key says the run had security on, so it is never read as "off"."""
    import yaml
    from fleetmux.channels import security as channel_security
    if not isinstance(value, dict) or not value:
        raise ValueError('security: the pinned policy is empty or not a mapping; fix it, or resume with '
                         '--security PATH or --no-security')
    policy, errors = channel_security.parse_security(yaml.safe_dump(value), stall_window=stall_window)
    if errors:
        raise ValueError('security: the pinned policy is invalid:\n' + '\n'.join(errors))
    return policy


def resume_security(cfg, args):
    """start --resume: settle cfg['security'] / cfg['security_source'] in place before anything is spawned
    (core-2 P8-start-plan §2 / §3). ValueError -> the caller refuses the resume with config.json untouched.
    security_taint is never touched here (contracts §8 O3 / X1); a run that carries it refuses --no-security (M164)."""
    on, off = getattr(args, 'security', None), getattr(args, 'no_security', False)
    stall_window = cfg.get('stall_window') or watchdog.DEFAULTS['stall_window']
    if cfg.get('parent_config'):  # a sub-fleet follows its parent: no local override, no re-read, validated
        if on or off:
            raise ValueError('security: a sub-fleet follows its parent fleet\'s security policy; '
                             '--security / --no-security are not accepted here')
        if 'security' in cfg:
            cfg['security'] = _pinned_security(cfg['security'], stall_window)
        return
    if off:
        from fleetmux.channels import strangers
        if strangers.TAINT_KEY in cfg:  # M164: outside content reached this run; the mark keeps its guard on
            raise ValueError('security: this run carries ' + strangers.TAINT_KEY + ' (outside content reached it); '
                             '--no-security would drop its guard and is refused. Resume without the flag, or with '
                             '--security PATH')
        if 'security' in cfg or 'security_source' in cfg:
            cfg.pop('security', None)
            cfg.pop('security_source', None)
            print('security: off for this run (--no-security)')
        return
    if on:
        cfg['security'], cfg['security_source'] = _security_file(on, stall_window)
        return
    if 'security' not in cfg:  # D2: a run without security never discovers a default file on resume
        return
    if cfg.get('security_source'):
        try:
            cfg['security'], cfg['security_source'] = _security_file(cfg['security_source'], stall_window)
        except ValueError as exc:
            raise ValueError(str(exc) + '\nfix the file, or resume with --security PATH or --no-security') from None
        return
    cfg['security'] = _pinned_security(cfg['security'], stall_window)  # legacy: no source key, none written
    print('security: no source recorded, keeping the pinned policy')


def canary_start(cfg):
    """P8h (brief v2.2 §8.2 C5; core-2 P8-start-plan §1 C1), right after P8p pinned cfg['security']: with
    canary.enabled on, write a fresh <run>/<canary.path> (a new value every run) and return the value to SET in
    fx:channel:canary once the run's Redis is up. No policy / off / a sub-fleet -> None: nothing read or written.
    A refused plant -> SystemExit before Redis is started (the text names the run, never the value)."""
    from fleetmux.channels import canary as channel_canary
    policy = cfg.get('security')
    if cfg.get('parent_config') or channel_canary._off(policy):
        return None
    try:
        return channel_canary.read_valid(channel_canary.plant(Path(cfg['run']), policy))
    except (OSError, ValueError) as exc:
        raise SystemExit('cannot start ' + str(cfg['run']) + ': security: canary not planted: '
                         + type(exc).__name__ + (': ' + str(exc) if isinstance(exc, ValueError) else '')) from None


def canary_resume(run, prev_policy, cfg, r):
    """P8h on start --resume (C1 / C1-R): canary.on_resume(previous pinned policy -> the one P8p settled). A valid
    original value is kept and SET again; newly on -> planted; off / no policy / a sub-fleet -> nothing loaded.
    A missing / bad / moved canary refuses the resume (SystemExit) before config.json is saved or anything spawned."""
    from fleetmux.channels import canary as channel_canary
    try:
        return channel_canary.on_resume(run, prev_policy, cfg.get('security'), r=r)
    except (OSError, ValueError) as exc:
        raise SystemExit('cannot resume ' + str(run) + ': ' + str(exc) if isinstance(exc, ValueError)
                         else 'cannot resume ' + str(run) + ': security: canary: ' + type(exc).__name__) from None


def resume_interpreter(run, cfg):
    """section 60: a continued run takes the interpreter of the release that resumes it (``releases.python()``): the
    run config (the daemon, every actor started from now on and the hook lines they write, the history pane) and
    every sub-fleet config whose daemon is not running (a relaunch, revive or readopt). A sub-fleet whose daemon
    still runs keeps its config (its daemon would write the old value back) and switches at its own next resume."""
    old, new = cfg.get('python'), releases.python()
    cfg['python'] = new
    switched, running = subfleet.switch_python(run, new)
    if old != new or switched or running:
        print('start: this run now uses the interpreter of release ' + str(releases.own() or '?') + ': ' + new
              + ('' if old == new else ' (was ' + str(old) + ')')
              + ('; sub-fleet configs switched: ' + ', '.join(switched) if switched else '')
              + ('; still running, they switch at their own next resume: ' + ', '.join(running) if running else ''))


def require_handoff_block():
    """Refuse to start while fleet.yaml#handoff has never been readable and valid: Python holds no number to run on.
    The read also gives this process the last good values a later bad edit falls back to."""
    try:
        fleet_config.handoff()
    except ValueError as exc:
        raise SystemExit('cannot start: ' + str(exc)) from exc


def handoff_flags(args):
    """The handoff values the start command typed, as config.json keys; an untyped one stays out of config.json, so
    the run follows fleet.yaml#handoff."""
    return {k: getattr(args, k) for k in ('handoff_ratio', 'handoff_timeout') if getattr(args, k, None) is not None}


def resume(run, args):
    """start --resume: reattach an unfinished v2 run. False when it turned out finished (auto starts anew)."""
    path = run / 'config.json'
    cfg = load(path)
    from fleetmux import cmux_manager
    pin_paths(cfg)  # a resumed run keeps its own locations; an older config gets the current ones pinned
    from fleetmux import i18n  # §63: a resume is the next start: it follows the setting as it is now
    i18n.pin(None)
    cfg['ui_language'] = i18n.language()
    i18n.pin(cfg['ui_language'])
    require_handoff_block()
    cfg.setdefault('fleet_home', str(HOME.expanduser().resolve()))
    cfg.setdefault('cmux_bin', cmux_manager.CMUX)
    cfg.setdefault('cmux_state_dir', str(cfg.get('cmux_state_dir') or cmux_manager.STATE_DIR))
    if 'codex_queue' not in cfg:  # a run started before the probe: decided once here, kept for the rest of the run
        cfg['codex_queue'], cfg['codex_version'], _ = codex_queue.capability(cfg.get('codex'))
    try:
        runtime_v2.validate_config(cfg)
    except ValueError as exc:  # this release cannot run it: its open jobs go to a new run (section 58 "Restart")
        raise refused(run, 'cannot resume ' + str(run) + ': ' + str(exc)) from exc
    prev_security = copy.deepcopy(cfg.get('security'))  # P8h: the canary compares the old pin with the new one
    try:  # P8p: settled before anything is probed or spawned; saved with the rest below
        resume_security(cfg, args)
    except ValueError as exc:
        raise SystemExit('cannot resume ' + str(run) + ': ' + str(exc)) from exc
    # A resumed run follows its own backend (§5); an older run without one keeps cmux. A run whose
    # daemon is still alive is returned below before anything is spawned, so its backend is not probed.
    live_run = daemon_running(run)
    if live_run:  # an idempotent --resume: nothing to do, and nothing to probe
        print('this run still has a live daemon; nothing to resume:', run)
        return True
    if (cfg.get('mode') == 'service' and (run / 'result.json').exists() and not resumable(run)
            and open_job_rows(run)):  # section 58 "Restart": stopped with open jobs, continued; the record stays as evidence
        (run / 'result.json').replace(run / 'result.stopped.json')
        print('continuing ' + str(run) + ' (stopped with ' + str(len(open_job_rows(run))) + ' open job(s); its '
              'result.json is kept as result.stopped.json)')
    # A config without `backend` (a run started before the choice existed) is a cmux run: never re-decided
    # here, so a resume where cmux does not answer is refused as before instead of moving the run to WezTerm.
    backend, why = resolve_backend(cfg.get('backend') or 'cmux')
    if why:  # refused before anything is provisioned: Redis stays as it is
        raise SystemExit('cannot resume ' + str(run) + ': backend ' + str(backend)
                         + ' is not usable here: ' + why)
    cfg['backend'] = backend
    if args.max_attempts is not None:
        cfg['max_attempts'] = args.max_attempts
    if getattr(args, 'lease_reclaim', None):  # §24; a run without the key keeps the default (on)
        cfg['lease_reclaim'] = args.lease_reclaim
    cfg['restart_not_applied'] = continue_supervisor(cfg, args)  # section 60: never a silent no-op
    if getattr(args, 'no_restore', False):
        cfg['no_restore'] = True
    if args.roster:  # applied by the normal hot-reload on the daemon's first tick
        roster_path = Path(args.roster).expanduser().resolve()
        try:
            workers = roster.load(roster_path)[0]
            # Only added slots and newly declared sub-fleets need -<number> (§3): stored names are kept
            problems = roster.row_errors(workers, keep=cfg.get('workers') or ()) + roster.subfleet_suffix_errors(
                roster.load_subfleets(roster_path), cfg.get('subfleets'))
            if problems:
                raise ValueError('\n'.join(problems))
        except (OSError, ValueError) as exc:
            raise SystemExit('--roster ' + str(roster_path) + ' rejected:\n' + str(exc)) from exc
        old_copy = Path(cfg.get('roster') or run / roster.RUN_COPY)
        old_text = old_copy.read_text() if old_copy.is_file() else None
        roster_path = roster.copy_into_run(roster_path, run)  # open question 5: the run's own copy
        if roster_path.read_text() != old_text:  # rebuilt: loaded workflows go back in (design.md §56)
            from fleetmux import superpowers_peer
            try:
                superpowers_peer.reapply(run / superpowers_peer.RECORDS, old_text, run, roster_path,
                                         refuse=None if cfg.get('mode') == 'service' else 'a task fleet hosts no applet')
            except Exception as exc:  # never stops the resume
                print('superpowers: loaded workflows not loaded again: ' + repr(exc), file=sys.stderr)
        cfg['roster'] = str(roster_path)
        cfg['subfleets'] = roster.load_subfleets(roster_path)  # respawned supervisor_slots start in tmux
    if getattr(args, 'advisor', False):  # sugar: writes `advisor: on` into this run's roster (§2.1)
        if cfg.get('worker_kit'):
            try:
                kitmod.Kit(cfg['worker_kit'])  # refuse a bad kit before provisioning, like a bad roster
            except (OSError, ValueError) as exc:
                raise SystemExit('--worker-kit rejected: ' + str(exc)) from exc
        try:
            for roster_file_path in advisor_rosters(cfg):  # not `path`: that is config.json, saved below
                roster.write_advisor(roster_file_path, True, cfg['workers'])
        except (OSError, ValueError) as exc:
            raise SystemExit('--advisor: roster not written: ' + str(exc)) from exc
    if not live_run and not backends_check(backend):  # a live daemon keeps its own backend running
        raise SystemExit(unavailable_text(backend))
    if cfg.get('subfleets') and subfleet.backend_problem(cfg):
        raise SystemExit('cannot resume ' + str(run) + ': ' + subfleet.backend_problem(cfg))
    sweep_orphan_redis(keep=run)
    try:
        r = connect(cfg)  # only the daemon died: its Redis is still up
    except Exception:
        if not Path(cfg['redis_runtime_dir']).is_dir():
            raise refused(run, 'cannot resume ' + str(run) + ': its Redis runtime directory is gone')
        try:
            r = start_redis(cfg, tries=300)  # reloads the AOF from the same directory
        except (SystemExit, subprocess.CalledProcessError, OSError) as exc:
            raise refused(run, 'cannot resume ' + str(run) + ': its Redis does not load: '
                          + str(getattr(exc, 'code', None) or exc)) from exc
    task = {} if cfg.get('mode') == 'service' else r.hgetall(TASK)
    present, value = (False, {}) if cfg.get('mode') == 'service' else result_state(run)
    result = value if isinstance(value, dict) else {}
    # One strict classification for the whole public path (A42-LINUX-RESTORE-FAILURE-04), reused by the daemon's
    # RuntimeV2.classify_resume as its second layer: 'none' (never submitted a task), 'task' (a real id + a usable
    # deadline) or a damaged record — including an empty fx:task contradicted by a durable result.json, and a
    # result.json that exists but cannot be read (never the same as an absent one). It runs before result.json is
    # deleted and before any daemon is spawned.
    kind = None
    if cfg.get('mode') != 'service':
        try:
            kind = lifecycle_hold.classify(task, result, present, value is not None)
        except ValueError as exc:
            raise SystemExit('cannot resume ' + str(run) + ': ' + str(exc)) from exc
    no_task = kind == 'none'
    if no_task:
        print('this run never submitted a task (' + lifecycle_hold.STARTUP_FAILURE
              + '); starting its initialization, nothing to reopen')
    elif kind == 'task' and task.get('status', 'running') != 'running' and resumable(run):
        try:  # the lifecycle hold stopped it on purpose: the operator's --resume reopens the task
            lifecycle_hold.reopen(r, task, getattr(args, 'extend_hours', None), result=result)
        except ValueError as exc:
            if args.resume != 'auto':
                raise SystemExit('cannot resume ' + str(run) + ': ' + str(exc)) from exc
            try:  # auto: like a finished run, start a new one (Redis down again, AOF directory kept)
                r.shutdown()
            except Exception:
                pass  # redis closes the socket while acknowledging shutdown
            print('--resume auto: ' + str(run) + ': ' + str(exc) + '; starting a new run')
            return False
        (run / 'result.json').unlink()
        task = r.hgetall(TASK)
        print('reopened ' + task.get('id', '') + ' (stopped by the lifecycle hold) for --resume')
    if kind != 'none' and clear_resumable_result(run, task):  # closed from the fleetmux sidebar: task still running
        print('reopened ' + task.get('id', '') + ' (closed from the fleetmux sidebar) for --resume')
    if not no_task and task.get('status', 'running') != 'running':
        save_result(run, task)
        try:
            r.shutdown()
        except Exception:
            pass  # redis closes the socket while acknowledging shutdown
        if args.resume != 'auto':
            raise SystemExit('cannot resume ' + str(run) + ': task already ' + task['status'])
        print('--resume auto: ' + str(run) + ': task already ' + task['status'] + '; starting a new run')
        return False
    canary_resume(run, prev_security, cfg, r)  # P8h: needs the run's Redis; refused before save / spawn
    resume_interpreter(run, cfg)
    # switch baseline: restart switches that resume may apply take this machine's values (one line per change);
    # here, after every refusal above, so a refused resume prints and records nothing
    baseline.apply_resume(cfg, args, run)
    save(path, cfg)
    mgr = manager_for(cfg)
    command = [cfg['python'], str(Path(__file__).resolve()), 'daemon', str(path), '--resume']
    command = keep_awake_prefix() + command  # keep_awake_prefix: caffeinate / systemd-inhibit / nothing
    result = mgr.spawn_window('daemon', shlex.join(command), metadata={'type': 'daemon'}, start_directory=str(run))
    if not result.success:  # Redis stays up: start --resume can be retried
        raise SystemExit(result.error or 'backend ' + backend + ': the daemon tab could not be opened')
    if not cfg.get('parent_config'):  # a sub-fleet run (a baseline restart) is never this home's latest run
        save(LATEST, dict(cfg, config=str(path)))
    print('fleet resumed (' + cfg.get('mode', 'task') + ' mode). Run directory:', run)
    notify_route.start_warning(cfg)  # §24 "No route for human notices"
    return True


def daemon(cfg_path, resume=False):
    cfg = load(cfg_path)
    require_handoff_block()
    fleet = None
    with (Path(cfg['run']) / 'daemon.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:  # design.md §56 "Releases": the release this daemon runs, for status, refresh and load
            releases.record(cfg['run'])
        except OSError as exc:
            print('cannot record the running release: ' + str(exc), file=sys.stderr)
        # cmux creates a fresh login shell: it does not inherit the start command's
        # FLEETMUX_STATE. Pin the registry in config, never fall back for new runs.
        fleet = Fleet(cfg_path, connect(cfg), manager_for(cfg))
        fleet.resume_requested = resume
        try:
            notify_route.startup_alarm(fleet)  # §24 "No route for human notices": audit notify.no_route
        except Exception as exc:  # noqa: BLE001  (a warning never stops the daemon)
            print('notify route check failed: ' + repr(exc), file=sys.stderr)

        def stop(signum, frame):
            fleet.stopping = True
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, stop)
        try:
            fleet.run()
        finally:
            try:  # the hot fleet-superpowers blocks go back to the file before Redis may go down
                from fleetmux import fleet_superpowers_hot
                fleet_superpowers_hot.flush(fleet.r, force=True)
            except Exception:
                pass
            try:  # §62: the decompose parameters likewise (a root fleet only)
                fleet.decompose_flush(force=True)
            except Exception:
                pass
            if not fleet.v2 or fleet.explicit_stop:
                try:
                    fleet.r.shutdown()
                except Exception:
                    pass  # redis closes the socket while acknowledging shutdown
                subfleet.stop_tmux(cfg)  # the fleet's own tmux server goes last (§19 dual display)
    if fleet is not None:
        lifecycle_close.after_daemon(fleet)  # lock released, Redis down: close this run's workspace


def resolve_supervisor(args, default):
    """Fill the supervisor/worker CLIs and the supervisor's pi model/thinking from the pair table
    default. Precedence for a pi supervisor (first hit wins):
      model:    --supervisor-pi-model (or a mapped old --supervisor-profile) > explicit --pi-model
                > default.supervisor_pi_model (only when the supervisor CLI came from the table default)
      thinking: --supervisor-pi-thinking (or the mapped profile's) > explicit --pi-thinking
                > default.supervisor_pi_thinking (same condition) > off
    The table default never overrides a flag the operator typed. An old Codex profile flag on a
    defaulted pi supervisor is mapped (cli_pairs.LEGACY_SUPERVISOR_PROFILES) or refused with the
    new usage (ValueError) -- never a silent official Codex TUI."""
    explicit = getattr(args, 'supervisor_cli', None)
    args.supervisor_cli = explicit or default.get('supervisor_cli') or 'codex'
    args.worker_cli = getattr(args, 'worker_cli', None) or default.get('worker_cli') or 'codex'
    model, thinking = getattr(args, 'supervisor_pi_model', None), getattr(args, 'supervisor_pi_thinking', None)
    if args.supervisor_cli != 'pi':
        if model or thinking:
            raise ValueError('--supervisor-pi-model / --supervisor-pi-thinking need a pi supervisor '
                             '(--supervisor-cli pi, or the default)')
        if (args.supervisor_cli == 'codex' and default.get('supervisor_profile')
                and not getattr(args, 'supervisor_profile', None) and not getattr(args, 'profile', None)):
            args.supervisor_profile = default['supervisor_profile']
        args.pi_models, args.pi_thinkings = {}, {}
        args.pi_thinking = getattr(args, 'pi_thinking', None) or worker_thinking()
        return
    legacy = getattr(args, 'supervisor_profile', None) or (
        getattr(args, 'profile', None) if args.worker_cli != 'codex' else None)
    if legacy and not explicit:
        mapped_model, mapped_thinking = cli_pairs.legacy_supervisor_profile(legacy)
        if model and model != mapped_model:
            raise ValueError('--supervisor-profile ' + legacy + ' maps to ' + mapped_model + ' but --supervisor-pi-model '
                             'says ' + model + '; pass only --supervisor-pi-model')
        model, thinking = mapped_model, thinking or mapped_thinking
        print('--supervisor-profile/--profile ' + legacy + ' is the old Codex profile; now running the pi supervisor '
              'on ' + model + ' (thinking ' + thinking + '). Use --supervisor-cli codex --supervisor-profile ' + legacy
              + ' for the official Codex TUI.', file=sys.stderr)
        args.supervisor_profile = None
    elif getattr(args, 'supervisor_profile', None):
        raise ValueError('--supervisor-profile is a Codex profile; a pi supervisor takes --supervisor-pi-model '
                         'provider/model [--supervisor-pi-thinking LEVEL]')
    run_model, run_thinking = getattr(args, 'pi_model', None), getattr(args, 'pi_thinking', None)
    if not explicit:  # the table default only fills what neither a supervisor nor a run-wide flag set
        model = model or (None if run_model else default.get('supervisor_pi_model'))
        thinking = thinking or (None if run_thinking else default.get('supervisor_pi_thinking'))
    args.pi_thinking = run_thinking or worker_thinking()  # unset --pi-thinking: discovery.worker_thinking (high)
    args.supervisor_pi_model, args.supervisor_pi_thinking = model, thinking
    args.pi_models = {'supervisor': model} if model else {}
    args.pi_thinkings = {'supervisor': thinking} if thinking else {}


def worker_thinking():
    """fleet.yaml#discovery.worker_thinking: a worker's default thinking / effort (user 2026-11-11: high)."""
    try:
        return (fleet_config.block('discovery') or {}).get('worker_thinking') or 'high'
    except (OSError, ValueError, KeyError):
        return 'high'


def has_desktop():
    """A graphical session for `fleetmux gui` (design §3.6.8): macOS, or Linux with DISPLAY / WAYLAND_DISPLAY."""
    if sys.platform == 'darwin':
        return True
    return sys.platform.startswith('linux') and bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def interactive_terminal():
    """A person ran `start` at a terminal: stdin and stdout are both TTYs (not launchd, not a script's pipe)."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def inside_fleetmux_pane():
    """`start` runs inside a fleetmux pane: some ancestor process is a `fleetmux server`."""
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1:
            return False
        try:
            out = subprocess.run(['ps', '-o', 'ppid=', '-o', 'command=', '-p', str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.split(None, 1)
        except (OSError, subprocess.SubprocessError):
            return False
        if not out:
            return False
        argv = out[1].split() if len(out) > 1 else []
        if len(argv) > 1 and Path(argv[0]).name == 'fleetmux' and argv[1] == 'server':
            return True
        try:
            pid = int(out[0])
        except ValueError:
            return False
    return False


def viewer_running(runtime):
    """A `fleetmux gui` / `fleetmux attach` is already open on this runtime directory."""
    try:
        out = subprocess.run(['ps', '-axo', 'command='], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in out.splitlines():
        argv = line.split()
        if (len(argv) > 3 and Path(argv[0]).name == 'fleetmux' and argv[1] in ('gui', 'attach')
                and argv[2] == '--runtime' and argv[3] == str(runtime)):
            return True
    return False


def window_here(binary):
    """A window can open for the person at this terminal: a desktop, `fleetmux-web`, and not an ssh session.
    Otherwise the view is `fleetmux attach` (auto_view, and bare `fleetmux` in fleetmux/open_window.py)."""
    return bool(has_desktop() and fleetmux_web_bin(binary) and not os.environ.get('SSH_CONNECTION'))


def auto_view(run, binary, gui, attach):
    """`start` on fleetmux without --attach / --gui / --no-view: open a view for the person at the terminal.

    A desktop with fleetmux-web (and not an ssh session) gets the window, started in the background so `start`
    still returns; its output goes to <run>/gui.log. Otherwise the attach command is returned for `start` to
    exec. Nothing is opened (None) without a terminal, inside a fleetmux pane, or when a viewer is already open
    on this runtime.
    """
    if not interactive_terminal() or inside_fleetmux_pane() or viewer_running(gui[-1]):
        return None
    if window_here(binary):
        log = Path(run) / 'gui.log'
        with open(log, 'ab') as out:
            proc = subprocess.Popen(gui, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
        if sys.platform == 'darwin':
            bring_window_to_front(proc.pid)
        print('Opened a window (log: ' + str(log) + ')')
        return None
    return attach


def bring_window_to_front(gui_pid, wait_s=5.0):
    """macOS does not let a window started from a background process take the focus, so the new window opens
    behind the terminal. Wait for `fleetmux gui`'s `fleetmux-web` child and raise it through System Events.
    Best effort: any failure leaves the window where it is."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            pids = subprocess.run(['pgrep', '-P', str(gui_pid), 'fleetmux-web'], capture_output=True, text=True,
                                  timeout=2).stdout.split()
        except (OSError, subprocess.SubprocessError):
            return False
        if pids:
            script = ('tell application "System Events" to set frontmost of '
                      '(first process whose unix id is ' + str(int(pids[0])) + ') to true')
            try:
                for _ in range(10):  # the process exists before its window does
                    done = subprocess.run(['osascript', '-e', script], capture_output=True, timeout=5)
                    if done.returncode == 0:
                        return True
                    time.sleep(0.3)
            except (OSError, subprocess.SubprocessError):
                pass
            return False
        time.sleep(0.2)
    return False


def pane_path(cfg, path, pi):
    """A pane's PATH: the daemon's, plus for a pi agent the directories start pinned so pi's `#!/usr/bin/env node`
    finds the node of the one lookup (``pi_path_dirs``, docs/design.md §50.3) when the daemon's PATH lacks it."""
    have = [d for d in (path or '').split(os.pathsep) if d]
    extra = [d for d in (cfg.get('pi_path_dirs') or []) if d and d not in have] if pi else []
    return os.pathsep.join([path] + extra) if path and extra else (path or os.pathsep.join(extra))


def fleetmux_web_bin(fleetmux):
    """`fleetmux-web` in the order `fleetmux` looks (PROTOCOL.md §9.4): next to the `fleetmux` binary, each PATH
    entry, then $FLEETMUX_WEB_BIN; only an executable regular file counts. None when there is none."""
    dirs = [os.path.dirname(fleetmux), os.path.dirname(os.path.realpath(fleetmux))] if fleetmux else []
    dirs += [d for d in os.environ.get('PATH', '').split(os.pathsep) if d]
    for path in [os.path.join(d, 'fleetmux-web') for d in dirs] + [os.environ.get('FLEETMUX_WEB_BIN') or '']:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


GUI_NO_HISTORY = '这个 HOME 没有可恢复的 fleet；用 start 新起，或 start --resume <run>'


def gui_problem():
    """Why `gui` cannot open a window here (None: it can): a desktop, and a usable fleetmux backend (§18.2)."""
    from fleetmux.backends import unavailable_reason
    if not has_desktop():
        return 'gui needs a desktop (macOS, or Linux with DISPLAY / WAYLAND_DISPLAY) -- use attach instead'
    return unavailable_reason('fleetmux')


def gui_live_run():
    """The run `LATEST` names when its daemon is alive (None: nothing is running, gui owns the cards, §18.2)."""
    try:
        run = Path(load(LATEST)['run'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return run if daemon_running(run) else None


def gui():
    """`fleetmux gui` and bare `fleetmux`: the root fleet's window, else a window on this HOME's resumable runs
    (§18.2 / §18.7-2).

    First, when this HOME runs no fleet of its own or runs the root itself, this machine's live root fleet gets its
    window (`open_window.open_root_window`, design.md section 50: raise it, open it, or attach without a desktop).
    With no root and no fleet of this HOME's own, the fleet.yaml#bare root fleet is started (resumed when it can be)
    and its window shown (`open_window.start_bare_root`); `bare.enabled: false` skips that.
    Otherwise (this HOME runs another fleet, or bare is off), four steps, in this order: refuse without a desktop / fleetmux; scan the history; with a
    live daemon let its own cards stand, otherwise write kv `restorable` + kv `sidebar` (grey cards); then become
    `fleetmux gui --runtime <this HOME's runtime>`. No history and no daemon: say so and exit 1, without
    starting a server (§18.7-2: kv `sidebar` cannot hold 0 cards, so an empty window is not an option).
    """
    from fleetmux import cmux_manager, fleetmux_history, open_window
    from fleetmux.backends.fleetmux import fleetmux_bin
    live = gui_live_run()
    # Bare `fleetmux` / `fleetmux gui` (design.md section 50): this machine's live root fleet first, also when this
    # HOME's latest.json is a join stub; before gui_problem, because without a desktop the root view is attach.
    if open_window.open_root_window(HOME, live) is not None:
        return
    # No root fleet, and this HOME runs none of its own: start the fleet.yaml#bare root (section 50, item 5)
    if live is None and open_window.start_bare_root() is not None:
        return
    problem = gui_problem()
    if problem:
        raise SystemExit(problem)
    state_dir = str(cmux_manager.STATE_DIR)  # the same value `start` pins into every run's config.json (§18.1)
    runs = fleetmux_history.restorable_runs(HOME, state_dir)
    if live is None and not runs:
        print(GUI_NO_HISTORY)
        raise SystemExit(1)
    mgr = manager_for({'backend': 'fleetmux', 'session': SESSION, 'cmux_state_dir': state_dir})
    if not mgr.get_or_create_session():
        raise SystemExit('cannot start the fleetmux server: ' + (getattr(mgr, 'last_error', '') or 'unknown error'))
    if live is None:  # no daemon to push cards: the history is this window's sidebar (§18.2 step 3)
        fleetmux_history.push(mgr, runs, current=runs[0]['id'])
    binary = fleetmux_bin()
    view = [binary or 'fleetmux', 'gui', '--runtime', str(mgr.runtime_root)]
    print('Watch it in a window:', shlex.join(view))
    sys.stdout.flush()
    os.execvp(view[0], view)


def run_start(argv, env):
    """`start <argv>` of this release in a child process with the environment ``env``; its exit code. stdin is
    /dev/null, so `start`'s automatic view stays off (no terminal) whatever terminal the caller has: bare `fleetmux`
    (fleetmux/open_window.py) shows the window itself. stdout / stderr are the caller's."""
    return subprocess.run([sys.executable, str(Path(__file__).resolve()), 'start', *argv], stdin=subprocess.DEVNULL,
                          env=env).returncode


VIEW_NO_RUN = 'no fleet is running in this HOME; start one with `fleetmux start`'


def view():
    """`fleetmux view`: the live page of this HOME's running fleet, in this terminal (design.md section 55).

    `fleetmux attach` needs the runtime directory; this finds it from latest.json, so a person (or `fleetmux
    remote` over ssh) need not know it. No live daemon, or a run on another backend: one line, exit 1.
    """
    from fleetmux.backends.fleetmux import fleetmux_bin
    if gui_live_run() is None:
        raise SystemExit(VIEW_NO_RUN)
    cfg = pin_paths(load(LATEST))
    if run_backend(cfg) != 'fleetmux':
        raise SystemExit('view needs a run on the fleetmux backend (it execs `fleetmux attach`); this run uses '
                         + str(run_backend(cfg)) + ' -- start the fleet with --backend fleetmux')
    attach = [fleetmux_bin() or 'fleetmux', 'attach', '--runtime', str(manager_for(cfg).runtime_root)]
    os.execvp(attach[0], attach)


def start(args):
    if getattr(args, 'max_attempts', None) is not None and args.max_attempts < 1:
        raise SystemExit('--max-attempts must be at least 1')
    kit_fleet = getattr(args, 'kit_fleet', None)
    if kit_fleet is not None:  # flag checks only: a resumed run keeps its copy even when the kit is gone
        if not re.fullmatch(KIT_FLEET_RE, kit_fleet):
            raise SystemExit('--kit-fleet: name must match ' + KIT_FLEET_RE)
        if args.roster or args.workers is not None or args.worker_names:
            raise SystemExit('--kit-fleet replaces --roster / --workers / --worker-names; pass only one of them')
    args.carry_from = []  # section 58 "Restart": runs of this fleet home whose open jobs the new run takes over
    if getattr(args, 'resume', None):
        run = resume_target(args.resume)
        try:
            if run and resume(run, args):
                return
        except NotContinuable as exc:
            if args.resume != 'auto':
                raise
            print('--resume auto: ' + str(exc.code) + '; its open jobs go to a new run')
            args.carry_from.append({'run': str(run), 'reason': str(exc.code)})
    if getattr(args, 'resume', None) in (None, 'auto') and continue_home(args):
        return  # section 58 "Restart": this fleet home's run with open jobs was continued
    if args.transport_version != 2:
        raise SystemExit('new fleet instances require Redis transport v2; v1 is only readable for existing runs')
    # A live root fleet on this machine takes the work (fleetmux/join.py); never on an explicit --resume <run>;
    # never when this start takes over the open jobs of a run of its own fleet home
    if (not getattr(args, 'no_join', False) and getattr(args, 'resume', None) in (None, 'auto')
            and not args.carry_from):
        from fleetmux import join
        if join.attach(args):
            return
    return start_new_run(args)


def start_new_run(args):
    """`start` past the resume / continue / attach checks: a new run of this fleet home."""
    kit_fleet = getattr(args, 'kit_fleet', None)
    # The private config file is resolved once, here, before the first block read; config.json records it
    # and every process of the run reads that file (pin_paths), not whatever its environment says.
    private_config = fleet_config.from_env()
    fleet_config.pin(private_config)
    require_handoff_block()
    # A new run without an explicit CLI takes the pair table's default combination (pi supervisor on
    # the table's model, e.g. openai-codex/gpt-6-astra thinking high, + official Claude workers); the
    # config written below always names both CLIs, so the codex fallback in pair_problem only serves
    # old configs. The official Codex supervisor stays available with --supervisor-cli codex.
    # §52: the first start probes this machine once (discovered.yaml); with neither CLI flag typed, its ranked
    # default pair replaces the table default, and fills only the model flags the operator left unset.
    found = (discover.ensure() or {}).get('default')
    if getattr(args, 'supervisor_cli', None) or getattr(args, 'worker_cli', None):
        found = None
    default = cli_pairs.load_pairs().default
    if found:
        default = {k: found[k] for k in cli_pairs.DEFAULT_KEYS if k in found}
    try:
        resolve_supervisor(args, default)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if found:
        discover.apply_models(args, found)
    args.worker_model = args.worker_model or DEFAULTS['worker_model']
    args.supervisor_model = args.supervisor_model or DEFAULTS['supervisor_model']
    args.profile = getattr(args, 'profile', None) or DEFAULTS['profile']
    lifecycle_problem = None
    try:  # --lifecycle decides cfg['mode'] (its base) before the CLI-pair check reads both keys
        args.lifecycle, args.mode = lifecycle.resolve_start(getattr(args, 'lifecycle', None), getattr(args, 'mode', None))
    except ValueError as exc:
        if getattr(args, 'lifecycle', None):
            raise SystemExit(str(exc)) from exc
        lifecycle_problem = str(exc)  # missing --lifecycle: refused below, after the config checks
        args.lifecycle, args.mode = None, getattr(args, 'mode', None) or DEFAULTS['mode']
    baseline_record = None  # switch baseline (fleetmux/baseline.py): flags not typed take this machine's values
    if not getattr(args, 'parent_config', None):
        baseline_record = baseline.apply_new_run(args, 'root_service' if args.mode == 'service' else None)
        kit_fleet = getattr(args, 'kit_fleet', None)
    budget = None
    if getattr(args, 'budget', None):
        if args.mode != 'service':
            raise SystemExit('--budget is the service budget card; a task carries its own "budget" in the task spec')
        try:
            budget = brake.validate_budget(json.loads(args.budget))
        except ValueError as exc:
            raise SystemExit('--budget rejected: ' + str(exc)) from exc
    try:
        runtime_v2.validate_config(vars(args))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    roster_path, night_path, worker_roles, worker_profiles, profile_clis = None, None, {}, {}, set()
    roster_channels = []  # §53: the roster's `channels:` entries, pinned into config.json below
    if kit_fleet is not None:  # validated in the kit like --roster; copied into the run below
        args.roster = str(kit_fleet_roster(args.worker_kit, kit_fleet))
    if getattr(args, 'night_roster', None) and not args.roster:
        raise SystemExit('--night-roster needs --roster (the day roster with the same worker names)')
    if args.roster:  # validate before any provisioning
        if args.workers is not None or args.worker_names:
            raise SystemExit('--roster replaces --workers / --worker-names; pass only one of them')
        roster_path = Path(args.roster).expanduser().resolve()
        probe = dict(DEFAULTS, **{k: v for k, v in vars(args).items() if v is not None})
        try:
            workers, worker_roles, worker_profiles = roster.load_entries(roster_path)
            if roster.has_channels(roster_path.read_bytes()):  # §53: a local service fleet only (v1 refused above)
                if args.mode != 'service' or getattr(args, 'parent_config', None):
                    raise ValueError(roster.SERVICE_ONLY)
                roster_channels = roster.load_channels(roster_path)
            # New slots end with -<number> (docs/design.md §3), a declared sub-fleet's workers too
            problems = roster.row_errors(workers) + roster.subfleet_suffix_errors(roster.load_subfleets(roster_path))
            if problems:
                raise ValueError('\n'.join(problems))
            launches = list(night.resolve_launches(probe, worker_profiles, workers).values())
            roster_pi = roster.pi_settings(roster_path.read_bytes())
            problem = roster.pi_cli_problem(*roster_pi, args.worker_cli)
            if problem:
                raise ValueError(problem)
        except (OSError, ValueError) as exc:
            raise SystemExit('--roster ' + str(roster_path) + ' rejected:\n' + str(exc)) from exc
        args.pi_models = dict(roster_pi[0], **args.pi_models)
        args.pi_thinkings = dict(roster_pi[1], **args.pi_thinkings)
        if getattr(args, 'night_roster', None):
            night_path = Path(args.night_roster).expanduser().resolve()
            try:
                night.check_rosters(probe, roster_path, night_path)
                launches += night.resolve_launches(probe, roster.load_entries(night_path)[2]).values()
                problem = roster.pi_cli_problem(*roster.pi_settings(night_path.read_bytes()), args.worker_cli)
                if problem:
                    raise ValueError(problem)
            except (OSError, ValueError) as exc:
                raise SystemExit('--night-roster: ' + str(exc)) from exc
        profile_clis = {launch['overrides']['worker_cli'] for launch in launches}
    elif args.worker_names:  # new slots end with -<number> (docs/design.md §3), checked before provisioning
        problems = roster.suffix_errors(args.worker_names.split(','), where=lambda i, n: '--worker-names: ' + n)
        if problems:
            raise SystemExit('\n'.join(problems))
    if lifecycle_problem:
        raise SystemExit(lifecycle_problem)
    gate = lifecycle.clock_gate_problem(args.lifecycle)
    if gate:
        raise SystemExit(gate)
    try:  # P8p (brief v2.2 §8.1): before any provisioning; pinned into config.json below
        security_policy, security_source = resolve_security(
            args, stall_window=args.stall_window if getattr(args, 'stall_window', None) is not None
            else watchdog.DEFAULTS['stall_window'])
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        initiator = lifecycle_close.initiator_for_start(getattr(args, 'initiator', None))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    # A start without profiles runs every worker on the fleet default: its model card caps it too.
    try:
        problem = model_usage.default_problem(
            {'worker_cli': args.worker_cli, 'worker_model': args.worker_model, 'pi_model': args.pi_model,
             'pi_models': args.pi_models, 'host': args.host.strip().lower() if args.host and args.host.strip() else None},
            [w for w in workers if not worker_profiles.get(w)] if roster_path else (args.worker_names.split(',') if args.worker_names else
                                         ['worker-' + str(i + 1) for i in range(max(1, args.workers or 1))]),
            AgentRegistry())
    except (AgentRegistryError, OSError) as exc:
        raise SystemExit('cannot read agent cards: ' + str(exc)) from exc
    if problem:
        raise SystemExit(problem)
    # The kit is checked wherever the advisor comes online at start: the --advisor sugar, or a roster that
    # already says `advisor: on` (the daemon would refuse it only after Redis and the windows are up).
    if args.worker_kit and (getattr(args, 'advisor', False) or (roster_path and roster.advisor(roster_path.read_bytes()))):
        try:
            kitmod.Kit(args.worker_kit)  # refuse a bad kit before provisioning, like a bad roster
        except (OSError, ValueError) as exc:
            raise SystemExit('--worker-kit rejected: ' + str(exc)) from exc
    worker_domains = parse_worker_domains(getattr(args, 'worker_domains', None), args.worker_kit)
    backend, why = resolve_backend(getattr(args, 'backend', None))
    if getattr(args, 'attach', False) and backend != 'fleetmux':  # refuse before provisioning (exit 2, as usage)
        sys.stderr.write('start: --attach needs the fleetmux backend (it execs `fleetmux attach`); this run would '
                         'use ' + str(backend) + ' -- drop --attach or pass --backend fleetmux\n')
        raise SystemExit(2)
    if getattr(args, 'gui', False):  # refuse before provisioning, like --attach (exit 2); a missing / mismatched
        # fleetmux-web is `fleetmux gui`'s own exit 4 (PROTOCOL.md §9.5), not start's
        if getattr(args, 'attach', False):
            problem = '--attach and --gui are two ways to watch the same screen; pass one of them'
        elif backend != 'fleetmux':
            problem = ('--gui needs the fleetmux backend (it execs `fleetmux gui`); this run would use ' + str(backend)
                       + ' -- drop --gui or pass --backend fleetmux')
        elif not has_desktop():
            problem = '--gui needs a desktop (macOS, or Linux with DISPLAY / WAYLAND_DISPLAY) -- use --attach instead'
        else:
            problem = None
        if problem:
            sys.stderr.write('start: ' + problem + '\n')
            raise SystemExit(2)
    if why:  # the chosen backend cannot run here: refuse before provisioning
        if getattr(args, 'backend', None) is None:  # the default (fleetmux): its own reason
            raise SystemExit(why)
        raise SystemExit('--backend ' + str(args.backend) + ' cannot run here: ' + why)
    if not backends_check(backend):
        raise SystemExit(unavailable_text(backend))
    if roster_path and roster.load_subfleets(roster_path) and subfleet.backend_problem({'backend': backend}):
        raise SystemExit('--roster ' + str(roster_path) + ' rejected:\n' + subfleet.backend_problem({'backend': backend}))
    clis = {args.supervisor_cli, args.worker_cli} | profile_clis  # night cards need their CLI at start

    def find(name):  # the process PATH, then the login shell's (discover.search_path, design §50.3) for a needed one
        return shutil.which(name) or (discover.which(name) if name in clis | {'redis-server'} else None)
    codex = args.codex or find('codex')
    pi = args.pi or find('pi')
    redis_server = args.redis_server or find('redis-server')
    opencode = args.opencode or find('opencode')
    dsh, dsh_node = dsh_adapter.resolve_binaries(args.dsh or shutil.which('dsh'), shutil.which('node'))
    binaries = {'codex': codex, 'pi': pi, 'claude': args.claude or find('claude'),
                'opencode': opencode, 'dsh': dsh, 'redis-server': redis_server}
    missing = [n for n in sorted(clis | {'redis-server'}) if not binaries[n] or not shutil.which(binaries[n])]
    if missing:
        last = [n + ' ' + discover.last_found(n) for n in missing if discover.last_found(n)]
        raise SystemExit('binaries not found: ' + ', '.join(missing) + '. Install them with `fleetmux setup --install`'
                         ' (it asks first), or pass the corresponding binary flag'
                         + ''.join('; ' + line for line in last))
    pi_path_dirs = discover.run_dirs(pi) if 'pi' in clis else []  # pi's node, when only the lookup has it
    queue_ok, codex_version, queue_why = codex_queue.capability(codex)  # pinned for the run (config.json)
    if 'pi' in clis:
        if not args.allow_unsandboxed_pi:
            raise SystemExit('pi has no sandbox: explicitly pass --allow-unsandboxed-pi after reviewing permissions')
        unset = [n for n in (['supervisor'] if args.supervisor_cli == 'pi' else [])
                 + (([w for w in workers if not worker_profiles.get(w)] if roster_path else ['workers'])
                    if args.worker_cli == 'pi' else [])
                 if not args.pi_models.get(n) and not args.pi_model]
        if unset:
            raise SystemExit('--pi-model provider/model is required for pi agents (or --supervisor-pi-model / '
                             'roster pi_model); no model for: ' + ', '.join(unset))
        version = subprocess.run([pi, '--version'], text=True, capture_output=True, timeout=15, check=True, env=dict(
            os.environ, PATH=pane_path({'pi_path_dirs': pi_path_dirs}, os.environ.get('PATH', ''), True))).stdout.strip()
        match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', version)
        if not match or tuple(map(int, match.groups())) < (1, 1, 0):
            raise SystemExit('pi >= 1.1.0 is required (agent_settled); found: ' + version)
        if discover.below_min('pi', version):
            raise SystemExit('start: ' + discover.below_min('pi', version))
    # An agent CLI below its deps.yaml minimum is not used (design §50.3): refuse with one line, never fall back.
    too_old = discover.below_min('codex', codex_version) if 'codex' in clis else None
    if 'claude' in clis and not too_old:
        try:
            too_old = discover.below_min('claude', subprocess.run([binaries['claude'], '--version'], text=True,
                                                                  capture_output=True, timeout=15).stdout)
        except (OSError, subprocess.SubprocessError):
            pass  # unreadable: not judged here
    if too_old:
        raise SystemExit('start: ' + too_old)
    if 'codex' in clis and not queue_ok:
        print('official Codex: `codex queue` channel unsupported (' + queue_why + '); Codex slots use typed delivery',
              file=sys.stderr)
    if 'opencode' in clis:
        problem = opencode_start_problem(args, opencode)
        if problem:
            raise SystemExit(problem)
    if 'dsh' in clis and args.dsh_model is None:  # explicit config, recorded as the run's dsh_model
        args.dsh_model = fleet_config.dsh_default_model()
        print('dsh workers: --dsh-model not given; fleet.yaml#dsh.default_model = ' + args.dsh_model, file=sys.stderr)
    if 'dsh' in clis:
        problem = dsh_adapter.start_problem({'supervisor_cli': args.supervisor_cli, 'transport_version': args.transport_version,
                                             'dsh_model': args.dsh_model, 'pi_agent_dir': args.pi_agent_dir,
                                             'dsh': dsh, 'dsh_node': dsh_node, 'dsh_home': args.dsh_home})
        if problem:
            raise SystemExit(problem)
    if getattr(args, 'name', None) is not None and not args.name.strip():
        raise SystemExit('--name must not be empty')
    titles = {'agents_title': 'Fleet ' + args.name.strip()} if getattr(args, 'name', None) else {}
    titles['backend'] = backend  # the registry this fleet gets; the run's config keeps it for resume (§5)
    state_dir = getattr(args, 'cmux_state_dir', None)
    mgr = manager_for(dict(titles, cmux_state_dir=str(Path(state_dir).expanduser().resolve())) if state_dir else titles)
    if mgr.session_exists():
        raise SystemExit('fleetmux tabs already exist; run `stop` and close them with the ✕ first')
    if not roster_path:
        workers = (args.worker_names.split(',') if args.worker_names
                   else ['worker-' + str(i + 1) for i in range(max(1, args.workers or 1))])
    if len(set(workers)) != len(workers) or not all(re.fullmatch(WORKER_NAME_RE, w) and w not in ('supervisor', ADVISOR)
                                                    for w in workers):
        raise SystemExit('--worker-names: distinct names matching ' + WORKER_NAME_RE + ', not supervisor / advisor '
                         '(no dots: dots mark generations)')
    if args.roles and not Path(args.roles).expanduser().is_dir():
        raise SystemExit('--roles must be a directory of <agent-name>.md files')
    profiles = [args.supervisor_profile or args.profile] if args.supervisor_cli == 'codex' else []
    if args.worker_cli == 'codex':
        profiles.append(args.worker_profile or args.profile)
    home = Path(getattr(args, 'codex_home', None) or HOME / 'codex-home').expanduser()
    if profiles:
        prepare_codex_home(home, profiles)
    sweep_orphan_redis()
    token = uuid.uuid4().hex[:8]
    try:
        redis_cfg = redis_runtime.prepare(args.redis_runtime_dir, token)
    except (OSError, ValueError) as exc:
        raise SystemExit('cannot prepare private Redis runtime: ' + str(exc)) from exc
    if getattr(args, 'project_dir', None):  # before the first file appears under DIR/.fleetmux
        exclude_project_fleet_dir(args.project_dir)
    run = Path(getattr(args, 'runs_dir', None) or HOME / 'runs').expanduser() / ('fleet-' + time.strftime('%Y%m%d-%H%M%S-') + token)
    (run / 'workspace').mkdir(parents=True)
    if kit_fleet is not None:  # the kit is read-only and versioned: the run follows (and hot-edits) its own copy
        kit_roster = roster_path
        roster_path = (copy_kit_fleet(kit_roster.parent, run / 'fleet-config') / 'roster.yaml').resolve()
        workers, worker_roles, worker_profiles = roster.load_entries(roster_path)  # role paths into the copy
    elif roster_path:  # distributed-development.md open question 5: the run follows its own copy, never the source
        roster_path = roster.copy_into_run(roster_path, run)
        print('roster copied into the run; edit this copy while the run goes on:', roster_path)
    carried = previous_loads(run)  # design.md §56: workflows loaded into the previous run of this fleet home
    if carried:
        from fleetmux import superpowers_peer
        refuse = None if args.mode == 'service' else ('this run is a task fleet, which hosts no applet; the next '
                                                       'service start of this fleet home loads it again')
        try:
            if superpowers_peer.reapply(carried[0], carried[1], run, roster_path, refuse=refuse):
                workers, worker_roles, worker_profiles = roster.load_entries(roster_path)  # a host worker row
        except Exception as exc:  # never stops the start; the old run keeps its records for the next one
            print('superpowers: loaded workflows not carried over: ' + repr(exc), file=sys.stderr)
            carried = None
    from fleetmux import backends, cmux_manager, i18n
    cfg = {'run': str(run), 'python': sys.executable, **redis_cfg,
           'backend': backend,  # decided once here; the daemon, actors, --resume and sub-fleets read it
           'cmux_state_dir': str(mgr.meta_path.parent),
           'fleet_home': str(HOME.expanduser().resolve()), 'cmux_bin': cmux_manager.CMUX,
           'private_config': private_config,
           'ui_language': i18n.language(),  # §63: resolved once here; every process of the run pins it
           'workers': workers, 'mode': args.mode, 'lifecycle': args.lifecycle, 'roles': args.roles,
           'supervisor_split': bool(getattr(args, 'supervisor_split', False)),
           **({'channels': roster_channels} if roster_channels else {}),  # §53, contracts §8
           **({'security': security_policy, 'security_source': security_source}
              if security_policy is not None else {}),  # P8p: brief v2.2 §8.1
           'max_busy': OPENCODE_MAX_BUSY if args.max_busy is None and args.worker_cli == 'opencode'
                       else dsh_adapter.DEFAULT_MAX_BUSY if args.max_busy is None and args.worker_cli == 'dsh'
                       else DEFAULTS['max_busy'] if args.max_busy is None else args.max_busy,
           **({'budget': budget} if budget else {}),
           **({'brake_notify': shlex.split(args.brake_notify)} if getattr(args, 'brake_notify', None) else {}),
           'request_timeout': args.request_timeout, 'supervisor_profile': args.supervisor_profile,
           'clear_after_result': args.clear_after_result, 'clear_timeout': args.clear_timeout,
           **({'worker_idle_reclaim_min': None if reclaim == 'off' else reclaim}
              if (reclaim := getattr(args, 'worker_idle_reclaim_min', None)) is not None else {}),
           **({'claude_hook_delivery': False} if getattr(args, 'claude_hook_delivery', True) is False else {}),
           'worker_profile': args.worker_profile, 'profile': args.profile, 'codex': codex,
           'codex_queue': queue_ok, 'codex_version': codex_version, 'redis_server': redis_server, 'codex_home': str(home),
           **handoff_flags(args),
           'window': args.window, 'stall_timeout': args.stall_timeout,
           'stall_grace': args.stall_grace, 'stall_escapes': args.stall_escapes,
           'stall_window': args.stall_window, 'stall_cpu_pct': args.stall_cpu_pct,
           'lease_reclaim': args.lease_reclaim or 'on',  # §24, contracts §4
           **({'prompt_table': str(Path(args.prompt_table).expanduser().resolve())} if args.prompt_table else {}),
           'compact_limit': args.compact_limit, 'chrome_script': args.chrome_script,
           'chrome_min_interval': args.chrome_min_interval, 'worker_cli': args.worker_cli,
           'worker_model': args.worker_model, 'worker_kit': args.worker_kit,
           **({'worker_domains': worker_domains} if worker_domains else {}),
           'supervisor_model': args.supervisor_model,
           'claude': args.claude or shutil.which('claude'), 'claude_window': args.claude_window,
           'host': args.host.strip().lower() if args.host and args.host.strip() else None,
           'supervisor_cli': args.supervisor_cli, 'pi': pi, 'pi_model': args.pi_model,
           **({'pi_path_dirs': pi_path_dirs} if pi_path_dirs else {}),
           **({'opencode': opencode, 'opencode_model': args.opencode_model, 'opencode_window': args.opencode_window}
              if 'opencode' in clis else {}),
           **({'dsh': dsh, 'dsh_node': dsh_node, 'dsh_model': args.dsh_model,
               'dsh_home': str(Path(args.dsh_home or dsh_adapter.default_home()).expanduser())} if 'dsh' in clis else {}),
           'pi_thinking': args.pi_thinking, 'pi_models': args.pi_models, 'pi_thinkings': args.pi_thinkings, 'allow_unsandboxed_pi': args.allow_unsandboxed_pi,
           'worker_fresh': roster.fresh(roster_path.read_bytes()) if roster_path else {},  # §45
           'pi_agent_dir': args.pi_agent_dir, 'allow_profile_live_switch': args.allow_profile_live_switch,
           'transport_version': args.transport_version, 'allow_development_v2': args.allow_development_v2,
           'claude_supervisor_hook_delivery': True,  # launch-time fence; old/resumed runs keep their old channel
           'adopted_supervisor_hook_delivery': True,  # same fence for sub-fleet supervisors (adopted panes, §19/§29)
           'max_attempts': args.max_attempts or DEFAULTS['max_attempts'], 'token': token,
           **({'carry_from': args.carry_from} if getattr(args, 'carry_from', None) else {}),
           **({'previous_run': previous_run()} if previous_run() else {}),  # section 60: the run before this one
           **({'no_restore': True} if getattr(args, 'no_restore', False) else {}),
           'initiator': initiator,
           **({'channel_user_resume': args.channel_user_resume} if getattr(args, 'channel_user_resume', None) else {})}
    if getattr(args, 'advisor', False):  # sugar: the switch lives in the roster, never in config.json (§2.1)
        try:
            for path in advisor_rosters({'run': str(run), 'roster': roster_path and str(roster_path),
                                         'roster_night': night_path and str(night_path)}):
                roster.write_advisor(path, True, workers)
        except (OSError, ValueError) as exc:
            raise SystemExit('--advisor: roster not written: ' + str(exc)) from exc
    if getattr(args, 'name', None):
        cfg.update(name=args.name.strip(), **titles)
    if roster_path:
        # subfleets before the first spawn: a supervisor_slot starts in the fleet's tmux (§29 / dual display)
        cfg.update(roster=str(roster_path), worker_roles=worker_roles, worker_profiles=worker_profiles,
                   subfleets=roster.load_subfleets(roster_path))
        # Before the first spawn. A task fleet hosts no applet of its own (applet-1), only its sub-fleets do.
        session_env.update_config(cfg, roster.load_applets(roster_path) if args.mode == 'service' else [],
                                  cfg['subfleets'])
    if night_path:
        cfg['roster_night'] = str(night_path)
    if kit_fleet is not None:
        cfg['kit_fleet'] = {'name': kit_fleet, 'kit': str(Path(args.worker_kit).expanduser().resolve()),
                            'roster_md5': hashlib.md5(kit_roster.read_bytes()).hexdigest()}
    if 'pi' in clis:
        cfg['pi_home'] = str(pi_adapter.prepare_home(run / 'pi-home', args.pi_agent_dir, fleet_config.block('pi_context')))
    if getattr(args, 'no_view', False):  # a script's run: the daemon does not open the window either (gui_keep)
        cfg['gui_keep_open'] = False
    canary_value = canary_start(cfg)  # P8h: after P8p pinned cfg['security']; the file now, the Redis key below
    save(run / 'config.json', cfg)
    if baseline_record is not None:  # the daemon's first check reports these deviations
        baseline.save_record(run, baseline_record)
    start_redis(cfg)
    if canary_value:  # P8h: off / no policy -> no extra Redis call
        from fleetmux.channels import canary as channel_canary
        connect(cfg).set(channel_canary.channels.CANARY, canary_value)
    command = [cfg['python'], str(Path(__file__).resolve()), 'daemon', str(run / 'config.json')]
    command = keep_awake_prefix() + command  # keep_awake_prefix: caffeinate / systemd-inhibit / nothing
    result = mgr.spawn_window('daemon', shlex.join(command), metadata={'type': 'daemon'}, start_directory=str(run))
    if not result.success:
        connect(cfg).shutdown()
        raise SystemExit(result.error or 'backend ' + backend + ': the daemon tab could not be opened')
    if hasattr(mgr, 'claim_run'):
        mgr.claim_run(str(run))  # the lifecycle closer only closes a registry tagged with this run
    save(LATEST, dict(cfg, config=str(run / 'config.json')))
    if carried:  # the new run holds them now
        superpowers_peer.forget(carried[0])
    print('fleet started (' + (args.lifecycle or '-') + ' lifecycle, ' + args.mode + ' mode): supervisor + ' + str(len(cfg['workers']))
          + ' worker(s) initialising. Run directory:', run)
    notify_route.start_warning(cfg)  # §24 "No route for human notices"
    if backend == 'fleetmux':  # the fleet's screen is a server: say how to look at it, or go there (--attach / --gui)
        from fleetmux.backends.fleetmux import fleetmux_bin
        binary = fleetmux_bin()
        attach = [binary or 'fleetmux', 'attach', '--runtime', str(mgr.runtime_root)]
        gui = [binary or 'fleetmux', 'gui', '--runtime', str(mgr.runtime_root)]
        if has_desktop() and fleetmux_web_bin(binary):  # the window first on a desktop (design §8 item 14)
            print('Watch it in a window:', shlex.join(gui))
        print('Watch it with:', shlex.join(attach))
        if getattr(args, 'attach', False) or getattr(args, 'gui', False):
            view = gui if getattr(args, 'gui', False) else attach
        elif getattr(args, 'no_view', False):
            view = None
        else:
            view = auto_view(run, binary, gui, attach)
        if view is gui and gui_keep.gui_running(mgr.runtime_root):  # the daemon's first check may have opened it
            print('A window is already open on this runtime.')
            view = None
        if view:
            sys.stdout.flush()
            os.execvp(view[0], view)


def night_cmd(cfg, state):
    """`night on|off`: validate both rosters, then write <run>/night.json; the daemon does the rest."""
    run = cfg.get('run')
    if not run or not Path(run).is_dir():
        raise SystemExit('night: no run directory in ' + str(cfg.get('config') or cfg))
    if state == 'status':
        return dict(night.read_state(run), roster=night.roster_for(cfg))
    if not cfg.get('roster_night'):
        raise SystemExit('night: this run has no night roster (start it with --roster and --night-roster)')
    try:
        night.check_rosters(dict(DEFAULTS, **cfg), cfg['roster'], cfg['roster_night'])
    except (OSError, ValueError) as exc:
        raise SystemExit('night ' + state + ' refused, nothing changed:\n' + str(exc)) from exc
    return dict(night.write_state(run, state == 'on'), roster=cfg['roster_night' if state == 'on' else 'roster'])


def previous_run():
    """section 60: the run latest.json names before this start writes it (not a join stub), else ''."""
    try:
        latest = load(LATEST)
        return '' if 'join' in latest else str(Path(latest['run']).expanduser().resolve())
    except (OSError, ValueError, KeyError, TypeError):
        return ''


def previous_loads(run):
    """design.md §56: (records directory, roster text or None) of the run latest.json names before this start
    writes it, when that run is a stopped root fleet with `fleetmux superpowers load` records; else None."""
    from fleetmux import superpowers_peer
    try:
        prev = Path(load(LATEST)['run']).expanduser().resolve()
        prev_cfg = load(prev / 'config.json')
    except (OSError, ValueError, KeyError, TypeError):
        return None  # no latest run, or a join stub
    records = prev / superpowers_peer.RECORDS
    if prev == Path(run).resolve() or prev_cfg.get('parent_config') or not any(records.rglob('*.json')):
        return None
    if daemon_running(prev):
        print('superpowers: ' + str(prev) + ' still runs; the workflows loaded there stay there')
        return None
    copy = Path(prev_cfg.get('roster') or prev / roster.RUN_COPY)
    try:
        return records, copy.read_text()
    except OSError:
        return records, None


def kit_fleet_roster(worker_kit, name):
    """start --kit-fleet NAME: <worker kit>/fleets/NAME/roster.yaml, or SystemExit naming what is missing."""
    if not worker_kit:
        raise SystemExit('--kit-fleet needs --worker-kit (the kit that carries fleets/' + name + '/roster.yaml)')
    path = Path(worker_kit).expanduser().resolve() / 'fleets' / name / 'roster.yaml'
    if not path.is_file():
        raise SystemExit('--kit-fleet ' + name + ': the worker kit has no ' + str(path))
    return path


def copy_kit_fleet(source, target):
    """Copy a kit fleet directory (roster, sub-rosters, roles) into the run, writable (the kit is read-only)."""
    shutil.copytree(source, target)
    for root, dirs, files in os.walk(target):
        for entry in [root] + [os.path.join(root, f) for f in files]:
            os.chmod(entry, os.stat(entry).st_mode | stat.S_IWUSR)
    return target


def parse_worker_domains(value, worker_kit):
    """start --worker-domains a,b: [] when not given; refused unless every name is in the kit's domain table."""
    if value is None:
        return []
    names = list(dict.fromkeys(d.strip() for d in value.split(',') if d.strip()))
    if not names:
        raise SystemExit('--worker-domains is empty')
    if not worker_kit:
        raise SystemExit('--worker-domains needs --worker-kit (a domain kit with context_domains.yaml)')
    try:
        kit = kitmod.Kit(worker_kit)
    except (OSError, ValueError) as exc:
        raise SystemExit('--worker-domains needs a domain kit; --worker-kit rejected: ' + str(exc)) from exc
    unknown = [d for d in names if d not in kit.domains]
    if unknown:
        raise SystemExit('--worker-domains not in the domain table of ' + str(worker_kit) + ': '
                         + ', '.join(unknown) + ' (the table has: ' + ', '.join(kit.domains) + ')')
    return names


def advisor_switch(cfg, on):
    """`fleet advisor on|off`: only edits the run's roster; the daemon's roster_tick applies it."""
    run_cfg = load(Path(cfg['run']) / 'config.json')
    if on and run_cfg.get('worker_kit'):
        try:
            kitmod.Kit(run_cfg['worker_kit'])
        except (OSError, ValueError) as exc:
            raise SystemExit('--worker-kit rejected: ' + str(exc)) from exc
    try:
        paths = [roster.write_advisor(path, on, run_cfg['workers']) for path in advisor_rosters(run_cfg)]
    except (OSError, ValueError) as exc:
        raise SystemExit('roster not changed: ' + str(exc)) from exc
    return ('advisor ' + ('on' if on else 'off') + ' written to ' + ', '.join(map(str, paths))
            + '; the daemon applies it on its next tick')


def operator_channel_problem(cfg):
    """Why operator text cannot be queued to this run's supervisor through the bus, or None (design §5 / §48).
    Queued text reaches the supervisor only on a channel that never types into a Claude input box: the pi bridge
    or a Claude supervisor's Stop-hook mailbox (direct v2, or an adopted pane marked adopt.hook)."""
    if is_pi(cfg, 'supervisor'):
        return None
    if is_claude(cfg, 'supervisor'):
        if claude_hook.enabled(cfg, 'supervisor'):
            return None
        return ('this run\'s Claude supervisor has no Stop-hook mailbox (started before hook delivery, or an '
                'adopted pane without adopt.hook); fleet never types into a Claude input box: start a new run')
    if is_codex(cfg, 'supervisor'):
        return ('a Codex supervisor has no operator queue: the daemon queues only its own deliveries through '
                '`codex queue`; type into the Codex supervisor pane itself, or submit with `request` / `task -`')
    return 'supervisor CLI ' + str(agent_cli(cfg, 'supervisor')) + ' takes no operator input'


def operator_send(cfg, text, wait=30):
    """`operator`: queue operator text for the current supervisor without its input box (design §48).
    The same operator_input event the pi extension and the Claude UserPromptSubmit hook post (§5), carrying the
    supervisor's current generation / thread; the daemon decides (accepted with an id / rejected with a reason).
    A reply not seen within ``wait`` seconds is reported as pending (unconfirmed), never retried."""
    if not isinstance(text, str) or not text.strip():
        return {'status': 'rejected', 'reason': 'operator text is empty'}
    if cfg.get('transport_version') != 2:
        return {'status': 'rejected', 'reason': 'operator needs a transport v2 run (the fleet bus)'}
    problem = operator_channel_problem(cfg)
    if problem:
        return {'status': 'rejected', 'reason': problem}
    r = connect(cfg)
    try:
        info = r.hgetall(key('agent', 'supervisor'))
        bus = runtime_v2.bus_for(cfg, r)
        correlation = 'ui-' + uuid.uuid4().hex
        runtime_v2.post_event(bus, dict(kind='operator_input', agent='supervisor',
                                        generation=int(info.get('generation') or 0), run_id=bus.run_id,
                                        thread=info.get('thread') or '', text=text, request_id=correlation,
                                        source='cli'), correlation)
        return runtime_v2.wait_reply(bus, correlation, timeout=wait)
    finally:
        r.close(); r.connection_pool.disconnect()


def request(cfg, event, wait=30):
    import redis as redis_lib
    r = connect(cfg)
    event['request_id'] = uuid.uuid4().hex
    if cfg.get('transport_version', 1) == 2:
        bus = runtime_v2.bus_for(cfg, r)
        try:
            runtime_v2.post_event(bus, event, event['request_id'])
            return runtime_v2.wait_reply(bus, event['request_id'], timeout=wait)
        finally:
            r.close(); r.connection_pool.disconnect()
    r.rpush(EVENTS, json.dumps(event, ensure_ascii=False))
    for _ in range(wait * 10):
        try:
            raw = r.get('fx:reply:' + event['request_id'])
        except redis_lib.ConnectionError:
            if event['kind'] == 'stop':  # the daemon shuts its private redis right after replying
                return {'status': 'stopped'}
            raise
        if raw:
            return json.loads(raw)
        time.sleep(.1)
    return {'status': 'pending', 'request_id': event['request_id']}


def tab_restart(cfg, name, wait=30):
    """`fleetmux tab restart NAME` (§31): only a tab the run's roster declares; the daemon reopens it."""
    try:
        declared = [t['name'] for t in roster.tabs(Path(cfg['roster']).read_bytes(), Path(cfg['roster']).parent)]
    except (KeyError, TypeError, OSError, ValueError) as exc:
        return {'status': 'rejected', 'reason': 'no roster with tabs: for this run (' + str(exc)[:200] + ')'}
    if name not in declared:
        return {'status': 'rejected', 'reason': 'the roster declares no tab ' + name + ' (tabs: '
                + (', '.join(declared) or 'none') + ')'}
    r = connect(cfg)
    try:
        return tabs.request_restart(r, name, wait=wait)
    finally:
        r.close(); r.connection_pool.disconnect()


def job_rows(r):
    """§58 `status`: the open jobs of a root fleet (service, or a task root: §19), oldest first."""
    raw = r.get(JOBS)
    jobs = json.loads(raw) if raw else {}
    return [{'id': jid, 'state': job.get('state'), 'slot': job.get('slot') or '', 'subfleet': job.get('task') or '',
             'goal': job.get('goal', '')[:120], 'waiting': job.get('waiting') or '', 'created': job.get('created'),
             'name': (job.get('spawn') or {}).get('name') or '', 'added_worker': job.get('hot_slot') or '',
             # §58: each slot's cli:model, the chain entries skipped before it (with why) and the wall switches
             'model_chain': ((job.get('run') and run_model_chain(job['run']))
                             or (job.get('chain') or {}).get('model_chain') or {})}
            for jid, job in sorted(jobs.items(), key=lambda x: x[1].get('created') or 0)]


def status(cfg):
    if cfg.get('transport_version', 1) == 2:
        from fleetmux.control_view import snapshot
        out = snapshot(cfg)
        children = subfleet.tree(cfg)
        if children:
            out['subfleets'] = children  # the whole tree under this fleet
        r = connect(cfg)
        try:
            task_clock(out.get('task'), awake.of(r).now())  # budget figures in the fleet's awake time
            rows = job_rows(r) if cfg.get('mode') == 'service' or not cfg.get('parent_config') else None
            waiting = named_subfleet.waiting_rows(r)  # D28: why a declared sub-fleet that is not up yet waits
            service = r.hgetall(SERVICE)
        finally:
            r.close(); r.connection_pool.disconnect()
        if waiting:
            out['subfleet_waiting'] = waiting
        if rows is not None:  # §58 / §19: open jobs of a root
            if rows or cfg.get('mode') == 'service':
                out['jobs'] = rows
        applets = applet_host.tree_rows(cfg)  # applet-interface.md section 5
        if applets:
            out['applets'] = applets
            applet_lease.annotate_cfg(cfg, out)  # each leased slot: applet_lease {id, holder, kind, until}
        from fleetmux import superpowers_peer
        loads = superpowers_peer.run_loads(cfg['run'])  # design.md §56: loaded / skipped workflows
        if loads:
            out['superpowers_load'] = loads
        release = releases.field(cfg)  # design.md §56 "Releases": running vs ~/fleetmux-current
        if release:
            out['release'] = release
        attention = blocked_scope.rows(cfg)  # §24: sub-fleets that wait for a human (the root is not paused)
        if attention:
            out['attention'] = attention
        out.update(pause_fields(service))  # §11 "Pause cause", §24 pause review
        notice = notify_route.summary(cfg)  # §24 "No route for human notices"
        if notice:
            out['human_notice'] = notice
        print(json.dumps(out, ensure_ascii=False, indent=2))
        status_attention_lines(attention)
        status_notice_line(notice)
        if release and release['restart_needed']:
            print(releases.restart_line(cfg.get('name') or cfg['run'], release['running'], release['installed']),
                  file=sys.stderr)
        return
    r = connect(cfg)
    out = {'mode': cfg.get('mode', 'task'), 'lifecycle': cfg.get('lifecycle'),
           'task': task_clock(r.hgetall(TASK), awake.of(r).now()), 'service': r.hgetall(SERVICE),
           'pending_requests': r.llen(REQQ), 'daemon_beat': float(r.get('fx:daemon:beat') or 0),
           'agents': {n: dict(r.hgetall(key('agent', n)), inbox=r.llen(key('inbox', n)))
                      for n in ['supervisor'] + cfg['workers']}}
    attention = blocked_scope.rows(cfg)  # §24
    if attention:
        out['attention'] = attention
    notice = notify_route.summary(cfg)  # §24 "No route for human notices"
    if notice:
        out['human_notice'] = notice
    print(json.dumps(out, ensure_ascii=False, indent=2))
    status_attention_lines(attention)
    status_notice_line(notice)


def status_notice_line(notice):
    line = notify_route.status_line(notice)
    if line:
        print(line, file=sys.stderr)


def pause_fields(service):
    """status: ``pause_cause`` / ``pause_review`` from fx:service (§11 "Pause cause"), each only when not empty."""
    out = {}
    for field, name in (('cause', 'pause_cause'), ('review', 'pause_review')):
        raw = service.get(field) or ''
        if raw:
            try:
                out[name] = json.loads(raw)
            except ValueError:
                out[name] = raw
    return out


def status_attention_lines(attention):
    """§24: one stderr line per sub-fleet that waits for a human after the root supervisor answered BLOCKED."""
    for row in attention:
        print('sub-fleet ' + str(row['subfleet']) + ' waits for a human (' + str(row.get('notice', '')) + ', '
              + time.strftime('%Y-%m-%d %H:%M', time.localtime(float(row.get('at') or 0))) + '): '
              + str(row.get('reason', ''))[:300] + '; run: ' + str(row['run'])
              + ('; ' + row['key'] + ', answer: ' + row['answer'] if row.get('answer') else ''), file=sys.stderr)


def print_inspection(args):
    """Print an offline registry, route, or plan without loading fleet config."""
    registry = AgentRegistry(args.registry)
    if args.cmd == 'agents' and args.agents_cmd == 'list':
        result = {'agents': registry.as_list()}
    elif args.cmd == 'agents':
        result = route_task(args.text, registry).to_dict()
    else:
        plan = build_dispatch_plan(args.goal, registry)
        result = plan.to_dict()
        if args.workers:
            workers = [worker.strip() for worker in args.workers.split(',')]
            result['profile_assignments'] = assign_profiles(plan, workers, registry)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cli():
    """The argument parser (main); split out so a test can parse flags without running a fleet."""
    p = argparse.ArgumentParser(prog='fleetmux', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('start', help='provision the private redis + cmux control surface (inside cmux)')
    s.add_argument('--workers', type=int, help='number of workers worker-1..N (default 1)')
    s.add_argument('--transport-version', type=int, choices=(2,), default=2,
                   help='new runs require Redis v2; existing v1 runs must be drained and replaced, not hot-flipped')
    s.add_argument('--allow-development-v2', action='store_true',
                   help='opt into incomplete v2 acceptance; no production-readiness claim')
    s.add_argument('--profile', default=None,
                   help='codex config profile for every Codex agent (default: fleet). With the default pi '
                        'supervisor an old supervisor profile (fleet-astra) maps to its pi model, others are refused')
    s.add_argument('--codex', help='codex binary (default: first on PATH)')
    s.add_argument('--redis-server', help='redis-server binary (default: first on PATH)')
    s.add_argument('--redis-runtime-dir', default=str(redis_runtime.DEFAULT_ROOT),
                   help='private Redis runtime root (default: project .runtime/redis; no /tmp fallback)')
    s.add_argument('--fleet-home', help='fleet home (latest.json, default runs/codex-home); pinned in config.json'
                   ' (default: $FLEETMUX_HOME or ~/.fleetmux)')
    s.add_argument('--backend', choices=('cmux', 'wezterm', 'fleetmux'),
                   help='terminal backend of this run, written into config.json and followed by --resume and '
                        'sub-fleets (default: fleetmux on every platform)')
    s.add_argument('--attach', action='store_true',
                   help='fleetmux only: once start has finished, replace this process with '
                        '`fleetmux attach --runtime <run runtime>`')
    s.add_argument('--gui', action='store_true',
                   help='fleetmux only, needs a desktop and fleetmux-web: once start has finished, replace this '
                        'process with `fleetmux gui --runtime <run runtime>` (a window; not together with --attach)')
    s.add_argument('--no-view', action='store_true',
                   help='fleetmux only: do not open a view. Without --attach / --gui / --no-view, a start run from a '
                        'terminal opens a window in the background (desktop with fleetmux-web, not over ssh) or '
                        'else attaches; nothing opens without a terminal, inside a fleetmux pane, or when a '
                        'viewer is already open on the runtime')
    s.add_argument('--no-restore', action='store_true',
                   help='design section 60: do not restore the previous run of this fleet home automatically (attached '
                        'sub-fleets); the reconcile ledger still lists every item for the supervisor')
    s.add_argument('--no-join', action='store_true',
                   help='start a fleet of its own even when a root fleet of this user already runs on this machine '
                        '(default: attach to it and write a join stub; a task start then takes work with `task '
                        '--config <stub> -`, a service start becomes a service sub-fleet of the root that takes '
                        '`request --config <stub> -`; see fleetmux/join.py)')
    s.add_argument('--cmux-bin', help='cmux binary; pinned in config.json (default: $CMUX_BIN or the app bundle)')
    s.add_argument('--runs-dir', help='where run directories are created (default: <fleet home>/runs)')
    s.add_argument('--project-dir', help='create the run under DIR/.fleetmux/runs (not with --runs-dir), so every '
                   'agent cwd sits inside DIR and Claude Code / Codex load DIR\'s CLAUDE.md / AGENTS.md as a parent '
                   'directory (no @import, no dialog); `.fleetmux/` is added once to DIR\'s git exclude')
    s.add_argument('--cmux-state-dir', help='cmux registry directory; pinned in config.json'
                   ' (default: $FLEETMUX_STATE or ~/.fleetmux/state)')
    s.add_argument('--codex-home', help='isolated CODEX_HOME for the agents (login + profile symlinked from yours;'
                   ' no hooks/MCP; default: <fleet home>/codex-home)')
    s.add_argument('--handoff-ratio', type=float, default=None,
                   help='rotate a session once its context use reaches this share of the window (default: '
                        'fleet.yaml#handoff.ratio; roster handoff: and agent cards rank above it, docs/design.md §22)')
    s.add_argument('--handoff-timeout', type=float, default=None,
                   help='seconds to wait for the model to write its handoff before using the skeleton '
                        '(default: fleet.yaml#handoff.handoff_timeout)')
    s.add_argument('--window', type=int, default=DEFAULTS['window'],
                   help='context window fallback when the rollout has not reported one yet')
    s.add_argument('--stall-timeout', type=float, default=DEFAULTS['stall_timeout'],
                   help='seconds without rollout activity before a busy agent is treated as stalled (0 = off); '
                        'in transport v2 the hard limit behind --stall-window')
    s.add_argument('--stall-window', type=float, default=watchdog.DEFAULTS['stall_window'],
                   help='transport v2: seconds the transcript, the screen and the pane process-tree CPU must all '
                        'stay still before a busy agent is treated as stalled (0 = off, only --stall-timeout)')
    s.add_argument('--stall-cpu-pct', type=float, default=watchdog.DEFAULTS['stall_cpu_pct'],
                   help='transport v2: CPU (%% of one core, averaged over --stall-window) the pane process tree '
                        'must use to count as moving; an idle TUI uses about 1 %%')
    s.add_argument('--lease-reclaim', choices=('on', 'off'), default=None,
                   help='transport v2: an official Claude / Codex lease that ran out only because the daemon did not '
                        'run (lid-closed sleep) is taken back and the work goes on (default on); off = hold that slot '
                        '(service / supervisor: and pause) as before. --resume keeps the run\'s value unless given')
    s.add_argument('--stall-grace', type=float, default=watchdog.DEFAULTS['stall_grace'],
                   help='transport v2: seconds a stalled agent gets after its first Escape before it is treated '
                        'as frozen (killed and restarted in place)')
    s.add_argument('--stall-escapes', type=int, default=watchdog.DEFAULTS['stall_escapes'],
                   help='transport v2: Escape presses for a stalled agent; all of them leaving the screen '
                        'unchanged means frozen')
    s.add_argument('--prompt-table', help='transport v2: watchdog dialog whitelist YAML (hot-reloaded; default '
                                          'config/fleet.yaml#watchdog_prompts)')
    s.add_argument('--compact-limit', type=int, help='codex model_auto_compact_token_limit (default: codex decides)')
    s.add_argument('--chrome-script', help='optional browser broker script run by the daemon for sandboxed agents')
    s.add_argument('--chrome-min-interval', type=float, default=DEFAULTS['chrome_min_interval'])
    s.add_argument('--supervisor-cli', choices=cli_pairs.CLIS, default=None,
                   help='CLI for the supervisor (default: fleet.yaml#cli_pairs default)')
    s.add_argument('--supervisor-model', default=None, help='claude supervisor: --model value')
    s.add_argument('--worker-cli', choices=cli_pairs.WORKER_CLIS, default=None,
                   help='CLI for workers (default: fleet.yaml#cli_pairs default; opencode / dsh are worker-only)')
    s.add_argument('--opencode', help='opencode binary (>= 2.0.29; default: first on PATH)')
    s.add_argument('--opencode-model', default=None,
                   help='opencode workers: provider/model, e.g. opencode/deepseek-v4.1-flash-free (required; no code '
                   'default, the free list changes; never replaced by a paid model)')
    s.add_argument('--opencode-window', type=int, default=OPENCODE_WINDOW,
                   help='opencode workers: context window for the handoff check (default 200000 = free tier)')
    s.add_argument('--dsh', help='dsh binary (== ' + dsh_adapter.VERSION + '; default: first on PATH)')
    s.add_argument('--dsh-home',
                   help='dsh workers: the shared DSH_HOME with the dsh-tui profile installed (dsh-tui == '
                   + dsh_adapter.TUI_VERSION + '; default: ~/.fleetmux/dsh-home;'
                   ' never installed per run)')
    s.add_argument('--dsh-model', default=None,
                   help='dsh workers: provider/model of a pi models.json entry, e.g. flashnext/<model id> (the same '
                   'definition a pi worker uses; default: fleet.yaml#dsh.default_model)')
    s.add_argument('--pi', help='pi binary (>= 1.1.0; default: first on PATH)')
    s.add_argument('--pi-model', help='explicit provider/model for every pi agent without its own model '
                   '(--supervisor-pi-model, roster pi_model); no implicit cloud fallback')
    s.add_argument('--supervisor-pi-model', default=None,
                   help='pi supervisor: provider/model (default: --pi-model if given, else fleet.yaml#cli_pairs '
                        'default.supervisor_pi_model when the supervisor CLI comes from the default)')
    s.add_argument('--supervisor-pi-thinking', choices=pi_adapter.THINKING, default=None,
                   help='pi supervisor: thinking level (default: --pi-thinking if given, else fleet.yaml#cli_pairs '
                        'default.supervisor_pi_thinking when the supervisor CLI comes from the default, else --pi-thinking'"'"'s default)')
    s.add_argument('--pi-thinking', choices=pi_adapter.THINKING, default=None,
                   help='pi thinking level for every pi agent without its own (default: fleet.yaml#discovery.worker_thinking, high; an explicit value '
                        'also beats fleet.yaml#cli_pairs default.supervisor_pi_thinking)')
    s.add_argument('--pi-agent-dir', default=os.environ.get('PI_CODING_AGENT_DIR') or str(Path.home() / '.pi/agent'),
                   help='source of auth.json/models.json only, linked into a fresh per-run pi home')
    s.add_argument('--allow-unsandboxed-pi', action='store_true',
                   help='acknowledge that pi tools run with your OS permissions, without a sandbox')
    s.add_argument('--allow-profile-live-switch', action='store_true',
                   help='allow task apply_profiles_live=true to respawn idle workers for assigned profiles')
    s.add_argument('--worker-model', default=None, help='claude workers: --model value')
    s.add_argument('--advisor', action='store_true',
                   help='start with the advisor online: only writes "advisor: on" into the roster (--roster, else '
                        '<run>/roster.yaml). Online, the advisor (official Claude CLI, fable/high) is consulted before '
                        'every dispatch, picks worker model / effort / domains and authorizes the respawns its sheets '
                        'need. Default off; turn it on / off while running with `advisor on|off`')
    s.add_argument('--worker-domains', help='claude workers: comma-separated domains of the --worker-kit domain table '
                   'every Claude worker always gets (advisor sheets only add to them; with the advisor off they '
                   'are linked into the default layout too)')
    s.add_argument('--worker-kit', help='claude workers: dir with .claude/{agents,skills} and mcp.json copied into each worker cwd')
    s.add_argument('--claude', help='claude binary (default: first on PATH)')
    s.add_argument('--claude-window', type=int, default=DEFAULTS['claude_window'], help='claude models not in the built-in 1M list ([1m]/opus/fable/sonnet): context window (§22)')
    s.add_argument('--worker-idle-reclaim-min', metavar='MIN|off', type=reclaim_minutes,
                   help='service: this run\'s idle-worker reclaim in minutes, or off (default: fleet.yaml#lifecycle_modes)')
    s.add_argument('--clear-after-result', action='store_true',
                   help='retired 2026-11-09 (typed /clear into the Claude input box): start refuses it; use dispatch "fresh"')
    s.add_argument('--clear-timeout', type=float, default=20, help='seconds to confirm context clear (default: 20)')
    s.add_argument('--no-claude-hook-delivery', dest='claude_hook_delivery', action='store_false',
                   help='retired 2026-11-09 (typed delivery into the Claude input box): start refuses it')
    s.add_argument('--lifecycle', metavar='MODE',
                   help='required: ' + ' | '.join(lifecycle.MODE_NAMES) + ' (config/fleet.yaml#lifecycle_modes); '
                        'decides what done means, how the run is closed, and --mode')
    s.add_argument('--host', default=None,
                   help='this machine for agent-card hosts (e.g. studio); unset = cards are not filtered by host')
    s.add_argument('--mode', choices=('task', 'service'), default=None,
                   help='transport mode, derived from --lifecycle (base); passing a different one is refused')
    s.add_argument('--worker-names', help='comma-separated worker names (default worker-1..N), each ending with '
                   '-<number>, e.g. exit-1,reentry-1,rung-1')
    s.add_argument('--roster', help='worker roster YAML (workers: [{name, role?}]); edits while running add or '
                                    'retire workers. Replaces --workers / --worker-names')
    s.add_argument('--kit-fleet', metavar='NAME',
                   help='take the roster from the --worker-kit: copies <kit>/fleets/NAME/ to <run>/fleet-config/, then '
                        'runs like --roster <run>/fleet-config/roster.yaml; --resume keeps the copy (design.md §30)')
    s.add_argument('--night-roster', help='night roster YAML: the same worker names as --roster, each with its night '
                                          'profile; `night on|off` switches between them (docs/design.md §25)')
    s.add_argument('--roles', help='dir of <agent-name>.md files appended to that agent\'s AGENTS.md (one prompt per worker)')
    s.add_argument('--supervisor-split', action='store_true',
                   help='service: the supervisor may start a task sub-fleet for a request itself (dispatch[].split, '
                        'docs/design.md §44 「监工直接开子 fleet」); off by default')
    s.add_argument('--supervisor-profile', help='codex profile for a Codex supervisor (default: --profile); without '
                   '--supervisor-cli, fleet-astra maps to the pi supervisor on openai-codex/gpt-6-astra, others are refused')
    s.add_argument('--worker-profile', help='codex profile for codex workers (default: --profile)')
    s.add_argument('--max-busy', type=int, default=None,
                   help='at most this many workers working at once (0 = no limit; default 0, '
                   'or 2 for opencode workers: free-tier concurrency is untested, and for dsh workers: shared FlashNext)')
    s.add_argument('--budget', help='service mode: budget card JSON, e.g. \'{"pool": "pool-a", "max_usd": 40, '
                   '"max_calls": 200}\' (pool ids: $FLEETMUX_CONFIG or ~/.fleetmux/fleet.yaml; tasks put "budget" '
                   'in the task spec); the daemon pauses at any line')
    s.add_argument('--brake-notify', help='command run (no shell) with one message argument when the brake pauses '
                   'the fleet, e.g. "/usr/local/bin/tg-send --chat ops"')
    s.add_argument('--request-timeout', type=float, default=DEFAULTS['request_timeout'],
                   help='service mode: default deadline (seconds) of requests without timeout_s, after which an '
                        'unanswered one is published as timeout (default: none; health is the watchdog\'s)')
    s.add_argument('--resume', metavar='RUN_DIR|auto',
                   help='reattach an unfinished v2 run instead of creating one (auto: the run in latest.json; '
                        'a finished or missing run starts a new one). Other start flags except --roster / '
                        '--max-attempts are ignored when a run is reattached')
    s.add_argument('--name', help='human-readable fleet name used in sub-fleet titles (default: task goal or run token)')
    s.add_argument('--max-attempts', type=int,
                   help='times one dispatch/request may run in total across start --resume (default 2)')
    s.add_argument('--initiator', metavar='KIND:ID',
                   help='who started this run, notified when it closes (claude:<session>, launchd:<label>, '
                        'program:<id>; default $FLEETMUX_INITIATOR or the launchd label)')
    s.add_argument('--extend-hours', type=float,
                   help='with --resume of a run the lifecycle hold stopped for its time budget: hours to add')
    s.add_argument('--channel-user-resume', help='channel_user_resume in the run config (validated by the daemon)')
    sec = s.add_mutually_exclusive_group()
    sec.add_argument('--security', metavar='PATH', help='security.yaml for this run (default: '
                     '$FLEETMUX_CONFIG_DIR or ~/.fleetmux/config, security.yaml there when it exists)')
    sec.add_argument('--no-security', action='store_true', help='no security policy for this run, even when '
                     'the default security.yaml exists')
    nt = sub.add_parser('night', help='switch the worker roster to the night (on) or day (off) profiles; status prints it')
    nt.add_argument('state', choices=('on', 'off', 'status'))
    nt.add_argument('--config', default=str(LATEST))
    ag = sub.add_parser('agents', help='inspect offline agent profiles without starting or contacting a fleet')
    ag_sub = ag.add_subparsers(dest='agents_cmd', required=True)
    ag_list = ag_sub.add_parser('list', help='list validated agent cards as JSON')
    ag_list.add_argument('--registry', help='agent card directory (default: project or package agents/)')
    ag_list.add_argument('--json', action='store_true', help='JSON output (already the default)')
    ag_match = ag_sub.add_parser('match', help='select an agent profile for task text')
    ag_match.add_argument('text')
    ag_match.add_argument('--registry', help='agent card directory (default: project or package agents/)')
    ag_match.add_argument('--json', action='store_true', help='JSON output (already the default)')
    pl = sub.add_parser('plan', help='build an offline rule-based dispatch plan as JSON')
    pl.add_argument('goal')
    pl.add_argument('--registry', help='agent card directory (default: project or package agents/)')
    pl.add_argument('--workers', help='comma-separated stable worker slots; include profile assignments when supplied')
    pl.add_argument('--json', action='store_true', help='JSON output (already the default)')
    sf = sub.add_parser('subfleet', help='sub-fleets (operator/test entry; the daemon executes it)')
    sf_sub = sf.add_subparsers(dest='subfleet_cmd', required=True)
    sp = sf_sub.add_parser('spawn', help='hand a worker pane over as supervisor of a new sub-fleet')
    sp.add_argument('--slot', required=True, help='worker slot of the owning fleet, e.g. worker-1')
    sp.add_argument('--task-id', required=True, help='M<n> under the main fleet, M0.<n> under sub-fleet M0')
    sp.add_argument('--goal', required=True)
    sp.add_argument('--workers', type=int, default=2)
    sp.add_argument('--output', help='absolute path of the deliverable the parent expects')
    sp.add_argument('--config', default=str(LATEST))
    lc = sub.add_parser('lifecycle-close', help='close a stopped run\'s cmux workspace (internal; detached)')
    lc.add_argument('cfg')
    dm = sub.add_parser('daemon', help='run the coordinator (internal)')
    dm.add_argument('cfg')
    dm.add_argument('--resume', action='store_true', help='reconcile and replay (internal; from start --resume)')
    t = sub.add_parser(
        'task', help='submit a goal: JSON file or - for stdin '
        '({"goal":..., "hours":..., "artifacts":[...], "auto_agents":true, "agent_registry":...}). On a service '
        'fleet it is a job: it runs in its own task-mode sub-fleet and its result is read with `result <job id>` '
        '({"goal":..., "hours":..., "artifacts":[...], "workers":..., "supervisor_profile":..., '
        '"worker_profiles":[...], "lifecycle":..., "budget":{...}}; docs/design.md §58). On a task fleet whose own '
        'task runs it is a job too (§19 "Task root")')
    t.add_argument('spec', nargs='?')
    t.add_argument('--recheck', metavar='ID', help='run the dev_task delivery gate again on the files as they are now, '
                   'for this run\'s own task or a job (job-...) that the gate blocked; on pass the task completes (a job '
                   'gets its result published again as ok). Dispatches no work (lifecycle_recheck)')
    t.add_argument('--cancel', metavar='JOB', help='cancel one job of this root (section 58): a queued, starting or suspended '
                   'job is cancelled at once and gets a failed result; a running one gets its sub-fleet stopped')
    t.add_argument('--job', action='store_true', help='submit only as a job: refused when the fleet would take it '
                   'as its own task (a join stub sends this)')
    for name in ('status', 'stop', 'resume'):
        sp = sub.add_parser(name)
        sp.add_argument('--config', default=str(LATEST))
        if name == 'stop':
            sp.add_argument('--final', action='store_true',
                            help='stop for good (design section 60): open jobs are cancelled and attached sub-fleets are '
                                 'not brought back by the next start; without it the next start restores the fleet '
                                 'and opens a reconcile ledger')
    rc = sub.add_parser('reconcile', help='the reconcile ledger after a restart or a supervisor change (section 60)')
    rc.add_argument('action', choices=('show', 'rule'))
    rc.add_argument('item', nargs='?', help='rule: the item id (R<n>)')
    rc.add_argument('--verdict', choices=('recovered', 'redo', 'drop'))
    rc.add_argument('--reason', default='')
    rc.add_argument('--ledger', default=None, help='rule: the ledger id (default: the open one)')
    rc.add_argument('--config', default=str(LATEST))
    sub.add_parser('gui', help='show this machine\'s root fleet window; with no root fleet, start the fleet.yaml#bare '
                                    'one (bare.enabled false: a window on this HOME\'s resumable fleets)')
    sub.add_parser('view', help='show the live page of this HOME\'s running fleet in this terminal '
                   '(design.md section 55)')
    rm = sub.add_parser('remote', help='run a fleet command on the machine that runs your fleet, over ssh '
                        '(fleet-superpowers machines; no command: its live page) (design.md section 55)')
    rm.add_argument('--host', help='a machine name from fleet-superpowers (default: fleet.yaml#remote.default, or '
                    'the only other machine)')
    rm.add_argument('--home', help='run the far command with FLEETMUX_HOME=HOME (a leading ~/ is the far home)')
    rm.add_argument('argv', nargs=argparse.REMAINDER, metavar='COMMAND',
                    help='the fleet command and its arguments, as typed on that machine')
    su = sub.add_parser('setup', help='probe this machine\'s agent CLIs, logins and models; rank them and cache the '
                        'default supervisor x worker pair (docs/design.md §52)')
    su.add_argument('--json', action='store_true', help='print the cached result as JSON instead of the table')
    su.add_argument('--install', action='store_true',
                    help='first install what a fleet needs and this machine lacks (redis-server, git, tmux, Node.js '
                         '22, pi), after you confirm; programs already there are never changed')
    su.add_argument('--yes', action='store_true', help='with --install: do not ask')
    hm = sub.add_parser('home', help='per-user data directories (docs/design.md §50.1)')
    hm_sub = hm.add_subparsers(dest='home_cmd', required=True)
    hmm = hm_sub.add_parser('migrate', help='move data kept under the names used before fleetmux (per-user and '
                                            'per-fleet homes, state, config) under ~/.fleetmux, leave a symlink at '
                                            'each old path and update run records (no fleet may run)')
    hmm.add_argument('--dry-run', action='store_true', help='print the plan, change nothing')
    sp.add_argument('--raise', dest='raise_', action='append', metavar='LIMIT=VALUE',
                    help='after a budget pause: raise a budget line (e.g. max_calls=400); repeatable')
    ap = sub.add_parser('applet', add_help=False, help='applets of a host fleet (docs/applet-interface.md section 5); '
                                                       '`fleetmux applet --help` lists the commands')
    ap.add_argument('applet_args', nargs=argparse.REMAINDER)
    sub.add_parser('upgrade', add_help=False, help='move this machine\'s running fleet tree to another release: '
                   'stop root first, then sub-fleets, start the new release, check (fleetmux upgrade --help)')
    sub.add_parser('demo', add_help=False, help='watch the acceptance at work in a one-off fleet of scripted agents: '
                   'no model, no login (fleetmux demo --help)')
    ts = sub.add_parser('tabs', add_help=False, help='list a running fleet\'s tabs, or print one tab\'s text '
                        '(read-only; `fleetmux tabs --help`)')
    ts.add_argument('tabs_args', nargs=argparse.REMAINDER)
    tb = sub.add_parser('tab', help='companion tabs (roster tabs:, docs/design.md §31)')
    tb_sub = tb.add_subparsers(dest='tab_cmd', required=True)
    tr = tb_sub.add_parser('restart', help='reopen a tab of the running daemon now and forget its crash history '
                                           '(the way back after tab.crashloop); prints accepted / rejected / failed / pending')
    tr.add_argument('name')
    tr.add_argument('--config', default=str(LATEST))
    tr.add_argument('--wait', type=int, default=30, help='seconds to wait for the daemon (default 30)')
    av = sub.add_parser('advisor', help='turn the advisor on / off for a run: edits its roster (--roster, else '
                                        '<run>/roster.yaml); the daemon applies it without a restart')
    av.add_argument('state', choices=('on', 'off'))
    av.add_argument('--config', default=str(LATEST))
    ls = sub.add_parser('logs', help='read-only v2 audit timeline/details')
    ls.add_argument('--config', default=str(LATEST))
    for field in ('request', 'slot', 'event', 'level'):
        ls.add_argument('--' + field)
    ls.add_argument('--generation', type=int)
    ls.add_argument('--after', default='0-0', help='resume after this Redis audit Stream ID')
    ls.add_argument('--limit', type=int, default=100, help='maximum records per read/export page')
    ls.add_argument('--json', action='store_true')
    ls.add_argument('--follow', action='store_true')
    q = sub.add_parser('request', help='service mode: submit a request (JSON file or - for stdin)')
    q.add_argument('spec')
    q.add_argument('--config', default=str(LATEST))
    ps = sub.add_parser('pause', help='service mode: stop delivering and rotating')
    ps.add_argument('--reason', default='paused by operator')
    ps.add_argument('--config', default=str(LATEST))
    op = sub.add_parser('operator', help='send operator text to the supervisor over the fleet bus, not its input '
                                         'box (text, or - / nothing for stdin); prints accepted / rejected / pending')
    op.add_argument('text', nargs='?', default='-')
    op.add_argument('--config', default=str(LATEST))
    op.add_argument('--wait', type=float, default=30, help='seconds to wait for the daemon\'s answer (default 30)')
    an = sub.add_parser('answer', help='answer a sub-fleet request that waits for you (a report:<task>:<id> key from '
                                       'status or the push) without the supervisor; prints accepted / rejected '
                                       '(docs/design.md section 24)')
    an.add_argument('key', help='the report:<task>:<id> key')
    an.add_argument('decision', choices=('approve', 'reject'))
    an.add_argument('--note', default='', help='what the sub-fleet is told with the ruling (how far an approve goes)')
    an.add_argument('--config', default=str(LATEST))
    an.add_argument('--wait', type=float, default=30, help='seconds to wait for the daemon\'s answer (default 30)')
    rr = sub.add_parser('result', help='service mode: print the result of one request as a JSON line; exit 0 found, '
                        '3 pending, 2 unknown id; never removes it (docs/distributed-development.md)')
    rr.add_argument('id', nargs='?', help='request / job id (a join stub defaults to its last job)')
    rr.add_argument('--wait', type=float, default=0, help='seconds to wait while the request is pending (default 0)')
    rr.add_argument('--config', default=str(LATEST))
    rs = sub.add_parser('results', help='service mode: print finished results (oldest first)')
    rs.add_argument('--drain', action='store_true', help='remove the printed results from the list')
    rs.add_argument('--config', default=str(LATEST))
    t.add_argument('--config', default=str(LATEST))
    n = sub.add_parser('notify', help='codex notify hook (internal)')
    for arg in ('cfg', 'agent', 'generation', 'payload'):
        n.add_argument(arg)
    c = sub.add_parser('chrome', help='run a vetted browser verb through the daemon (sandboxed agents)')
    c.add_argument('--config', default=str(LATEST))
    c.add_argument('argv', nargs=argparse.REMAINDER)
    c = sub.add_parser('peer', help='send a peer notice to a worker through the daemon (contract tasks, §21)')
    c.add_argument('--config', default=str(LATEST))
    c.add_argument('--from', dest='sender', required=True)
    c.add_argument('--to', required=True)
    c.add_argument('--iface', required=True)
    c.add_argument('--text-file', required=True)
    h = sub.add_parser('claude-stop', help='Claude Code Stop hook (internal; hook JSON on stdin)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('claude-rewake', help='Claude Code asyncRewake Stop hook of hook-channel workers (internal)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('claude-posttool', help='Claude Code PostToolUse hook: mid-turn handoff prompt (internal)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('claude-pretool', help='Claude Code PreToolUse hook: P8i action gate (internal; hook JSON on stdin)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('claude-stop-failure', help='Claude Code StopFailure hook (internal; hook JSON on stdin)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('claude-clear', help='Claude Code SessionStart(clear) hook of clear-after-result workers (internal)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('claude-prompt', help='Claude Code UserPromptSubmit hook (internal; hook JSON on stdin)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('pi-event', help='Pi extension callback (internal; event JSON on stdin)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    a = sub.add_parser('actor', help='exec the configured CLI for one agent (internal)')
    for arg in ('cfg', 'agent', 'prompt'):
        a.add_argument(arg)
    a.add_argument('--generation', type=int, help='pinned spawn generation (internal)')
    from fleetmux.channels import cli as channel_cli  # parser only; adapters load inside the commands
    channel_cli.add_parser(sub)
    return p


# §50.1: these check for data and settings under the old names; the first four stop on them, status / stop only
# print the line (a fleet started from an older release must still be stoppable before `home migrate`).
HUMAN_COMMANDS = ('start', 'setup', 'gui', 'resume', 'status', 'stop')


def main(argv=None):
    rest = list(sys.argv[1:] if argv is None else argv)
    if rest[:1] == ['applet']:  # applet-interface.md section 5: its own command line and dispatch table
        from fleetmux import applet_cli
        sys.exit(applet_cli.main(rest[1:]))
    if rest[:1] == ['superpowers']:  # the fleet-superpowers inventory: its own command table
        from fleetmux import fleet_superpowers_cli
        sys.exit(fleet_superpowers_cli.main(rest[1:]))
    if rest[:1] == ['relay']:  # docs/relay.md: multi-machine relay development, its own command table
        from fleetmux.relay import cli as relay_cli
        sys.exit(relay_cli.main(rest[1:]))
    if rest[:1] == ['tabs']:  # read-only tab titles and pane text from the fleetmux server
        from fleetmux import tabs_cli
        sys.exit(tabs_cli.main(rest[1:]))
    if rest[:1] == ['acp']:  # docs/omnigent.md: an ACP agent over stdio; stdout carries only JSON-RPC
        from fleetmux import acp_server
        sys.exit(acp_server.main(rest[1:]))
    if rest[:1] == ['cancel']:  # stop one job of the running root fleet by its id, never the root
        from fleetmux import job_cancel
        sys.exit(job_cancel.main(rest[1:]))
    if rest[:1] == ['tree']:  # §62: check a tree file before submitting it (fleetmux/tree_cli.py)
        from fleetmux import tree_cli
        sys.exit(tree_cli.main(rest[1:]))
    if rest[:1] == ['upgrade']:  # move a running fleet tree to another release (fleetmux/upgrade.py)
        from fleetmux import upgrade
        sys.exit(upgrade.main(rest[1:]))
    if rest[:1] == ['demo']:  # a one-off scripted fleet: the acceptance at work, no model (fleetmux/demo.py)
        from fleetmux import demo
        sys.exit(demo.main(rest[1:]))
    p = cli()
    args = p.parse_args(argv)
    if args.cmd == 'home':  # §50.1
        from fleetmux import home_migrate
        sys.exit(home_migrate.migrate(dry_run=args.dry_run))
    if args.cmd in HUMAN_COMMANDS:  # internal entry points (hooks, actors, the daemon) stay quiet
        from fleetmux import home_migrate
        home_migrate.check(fatal=args.cmd not in ('status', 'stop'))
    if args.cmd == 'start':
        pin_paths({'fleet_home': args.fleet_home and str(Path(args.fleet_home).expanduser().resolve()),
                   'cmux_bin': args.cmux_bin and str(Path(args.cmux_bin).expanduser().resolve())})
        if getattr(args, 'project_dir', None):  # user 2026-11-11: the run lives inside the project
            if args.runs_dir:
                raise SystemExit('--project-dir and --runs-dir are exclusive (--project-dir puts runs in '
                                 'DIR/' + PROJECT_FLEET_DIR + '/runs)')
            project = Path(args.project_dir).expanduser().resolve()
            if not project.is_dir():
                raise SystemExit('--project-dir: not a directory: ' + str(project))
            args.project_dir = str(project)
            args.runs_dir = str(project / PROJECT_FLEET_DIR / 'runs')
        args.runs_dir = args.runs_dir or str(HOME / 'runs')
        args.codex_home = args.codex_home or str(HOME / 'codex-home')
        start(args)
        return
    if args.cmd == 'setup':  # §52
        if args.install:
            from fleetmux import setup_install
            code = setup_install.main(yes=args.yes)
            if code:
                sys.exit(code)
        result = discover.run()
        discover.save(result)
        from fleetmux import i18n  # §63: the interface language in effect, and the template in the private file
        tpl_status, where = i18n.ensure_template()  # not `status`: that name is the status() command in main()
        result = dict(result, ui_language=i18n.language_info(),
                      ui_template={'status': tpl_status, 'path': str(where) if where else None})
        print(json.dumps(result, ensure_ascii=False, indent=1) if args.json
              else discover.render(result) + '\n' + i18n.render_setup(result['ui_language'], tpl_status, where))
        return
    if args.cmd == 'gui':  # §18.2: a window on the resumable runs; no config.json, nothing pinned
        discover.ensure()  # §52: first open
        gui()
        return
    if args.cmd == 'view':  # design.md section 55: this HOME's live page
        view()
        return
    if args.cmd == 'remote':  # design.md section 55: the same commands on the machine that runs the fleet
        from fleetmux import remote
        try:
            target = remote.host(args.host)
        except (OSError, ValueError) as exc:
            sys.stderr.write('fleetmux remote: ' + str(exc) + '\n')
            sys.exit(2)
        sys.exit(remote.run(target, args.argv, home=args.home))
    if args.cmd == 'channel':  # §53 / contracts §8: login / test / send / status / actor (P1g)
        from fleetmux.channels import cli as channel_cli
        sys.exit(channel_cli.run(args))
    if getattr(args, 'cfg', None):  # internal entry points: daemon, actor, hooks, notify, lifecycle-close
        _pin_from(args.cfg)
    if args.cmd == 'daemon':
        daemon(args.cfg, args.resume)
    elif args.cmd == 'lifecycle-close':
        lifecycle_close.close_workspace(args.cfg)
    elif args.cmd == 'notify':
        notify(args.cfg, args.agent, args.generation, args.payload)
    elif args.cmd == 'claude-stop':
        out = claude_stop(args.cfg, args.agent, args.generation, sys.stdin.read())
        if isinstance(out, dict) and out.get('decision') == 'block':  # hook channel: the next delivery
            print(json.dumps(out, ensure_ascii=False))
    elif args.cmd == 'claude-rewake':
        text = claude_rewake(args.cfg, args.agent, args.generation, sys.stdin.read())
        if text:  # exit 2 wakes Claude Code with stderr as "<rewakeMessage> <text>"
            sys.stderr.write(text)
            sys.exit(2)
    elif args.cmd == 'claude-clear':
        claude_clear(args.cfg, args.agent, args.generation, sys.stdin.read())
    elif args.cmd == 'claude-pretool':
        from fleetmux import guard
        out = guard.pretool(args.cfg, args.agent, args.generation, sys.stdin.read())
        if out:
            print(out)
    elif args.cmd == 'claude-posttool':
        sys.stdin.read()
        prompt = claude_posttool(args.cfg, args.agent, args.generation)
        if prompt:
            print(json.dumps({'decision': 'block', 'reason': prompt}))
    elif args.cmd == 'claude-stop-failure':
        claude_stop_failure(args.cfg, args.agent, args.generation, sys.stdin.read())
    elif args.cmd == 'claude-prompt':
        reason = claude_prompt_main(args.cfg, args.agent, args.generation, sys.stdin.read())
        if reason:  # nothing at all on pass: a UserPromptSubmit hook's stdout becomes model context
            print(json.dumps({'decision': 'block', 'reason': reason}, ensure_ascii=False))
    elif args.cmd == 'pi-event':
        pi_event(args.cfg, args.agent, args.generation, sys.stdin.read(1000001))
    elif args.cmd == 'actor':
        actor(args.cfg, args.agent, args.prompt, args.generation)
    elif args.cmd in ('agents', 'plan'):
        print_inspection(args)
    else:
        try:
            raw = load(args.config)
        except FileNotFoundError:
            raise SystemExit('fleetmux ' + args.cmd + ': no fleet has run here yet (' + str(args.config) + ' is missing);'
                             ' start one with `fleetmux start`' if Path(args.config) == LATEST else
                             'fleetmux ' + args.cmd + ': ' + str(args.config) + ' does not exist') from None
        from fleetmux import join
        if join.is_stub(raw):  # `start` attached to a live root fleet: talk to that root's job
            try:  # a service stub runs logs / operator / pause / resume on its child's own config
                child = join.redirect(args, Path(args.config), raw)
            except join.JoinError as exc:
                sys.stderr.write('fleetmux ' + args.cmd + ': ' + str(exc) + '\n')
                sys.exit(2)
            if child is None:
                code = join.cli(args, Path(args.config), raw)
                if code:
                    sys.exit(code)
                return
            args.config, raw = str(child), load(child)
        cfg = pin_paths(raw)
        if args.cmd not in ('chrome', 'peer'):  # worker sessions call these; a resume keeps their old CLI path
            releases.warn_cli(cfg)  # design.md §56 "Releases": this CLI vs the running daemon
        if args.cmd == 'logs':
            from fleetmux.control_view import iter_events
            for record in iter_events(cfg, after=args.after, limit=args.limit,
                                      request_id=args.request, slot=args.slot,
                                      generation=args.generation, event=args.event, level=args.level,
                                      follow=args.follow):
                print(json.dumps(record, ensure_ascii=False) if args.json else
                      runtime_v2.sanitize_text(json.dumps(record, ensure_ascii=False)), flush=True)
        elif args.cmd == 'status':
            status(cfg)
        elif args.cmd == 'night':
            print(json.dumps(night_cmd(cfg, args.state), ensure_ascii=False))
        elif args.cmd == 'advisor':
            print(advisor_switch(cfg, args.state == 'on'))
        elif args.cmd == 'tab':
            ans = tab_restart(cfg, args.name, args.wait)
            print(json.dumps(ans, ensure_ascii=False))
            sys.exit({'accepted': 0, 'rejected': 2, 'failed': 2}.get(ans.get('status'), 3))
        elif args.cmd == 'subfleet':
            print(json.dumps(subfleet.cli_spawn(cfg, args), ensure_ascii=False))
        elif args.cmd == 'chrome':
            ans = request(cfg, {'kind': 'chrome', 'argv': args.argv}, wait=150)
            print(ans.get('out') or ans.get('reason') or json.dumps(ans, ensure_ascii=False))
            sys.exit(ans.get('rc', 1) if ans.get('status') == 'ok' else 2)
        elif args.cmd == 'peer':
            ans = request(cfg, {'kind': 'peer', 'from': args.sender, 'to': args.to, 'iface': args.iface,
                                'text_file': str(Path(args.text_file).resolve())})
            ok = ans.get('status') == 'ok'
            print('peer: delivered to ' + ', '.join(ans['delivered']) if ok else
                  ans.get('reason') or json.dumps(ans, ensure_ascii=False))
            sys.exit(0 if ok else 2)
        elif args.cmd == 'stop':
            print(json.dumps(request(cfg, dict({'kind': 'stop'}, **({'final': True} if getattr(args, 'final', False)
                                                                   else {}))), ensure_ascii=False))
        elif args.cmd == 'reconcile':  # section 60
            if args.action == 'show':
                print(restart_state.show(cfg))
            else:
                if not args.item or not args.verdict:
                    raise SystemExit('reconcile rule needs the item (R<n>) and --verdict')
                ans = request(cfg, {'kind': 'reconcile', 'ledger': args.ledger, 'by': 'operator', 'rulings': [
                    {'item': args.item, 'verdict': args.verdict, 'reason': args.reason}]})
                print(json.dumps(ans, ensure_ascii=False))
                sys.exit(0 if ans.get('status') == 'accepted' and not ans.get('refused') else 2)
        elif args.cmd in ('pause', 'resume'):
            ev = {'kind': args.cmd, 'reason': getattr(args, 'reason', '')}
            if getattr(args, 'raise_', None):
                ev['raise'] = args.raise_
            answer = request(cfg, ev)
            print(json.dumps(answer, ensure_ascii=False))
            if isinstance(answer, dict) and answer.get('status') == 'rejected' and answer.get('held'):
                sys.exit(2)  # §24: a held slot that resume could not release (the reason and next step are printed)
        elif args.cmd == 'results':
            r = connect(cfg)
            while True:
                raw = r.lpop(RESULTS) if args.drain else None
                if not args.drain:
                    for raw in r.lrange(RESULTS, 0, -1):
                        print(raw)
                    break
                if not raw:
                    break
                print(raw)
        elif args.cmd == 'result':  # docs/distributed-development.md: a peer reads its answer
            if not args.id:
                raise SystemExit('result needs a request id')
            from fleetmux import remote
            sys.exit(remote.result_cli(connect(cfg), args.id, args.wait))
        elif args.cmd == 'answer':  # §24: the user's own ruling, never from an agent pane of this fleet
            ans = blocked_scope.answer_send(cfg, args.key, args.decision, args.note, wait=args.wait)
            print(json.dumps(ans, ensure_ascii=False))
            if ans.get('status') == 'accepted' and ans.get('restart'):  # switch baseline: the approved restart
                sys.exit(0 if baseline_check.restart_after_approval(cfg, ans) == 0 else 3)
            sys.exit({'accepted': 0, 'rejected': 2}.get(ans.get('status'), 3))
        elif args.cmd == 'operator':
            ans = operator_send(cfg, sys.stdin.read() if args.text == '-' else args.text, wait=args.wait)
            print(json.dumps(ans, ensure_ascii=False))
            sys.exit({'accepted': 0, 'rejected': 2}.get(ans.get('status'), 3))
        elif args.cmd == 'request':
            spec = json.loads(sys.stdin.read() if args.spec == '-' else Path(args.spec).read_text())
            print(json.dumps(request(cfg, {'kind': 'request', 'spec': spec}), ensure_ascii=False))
        elif getattr(args, 'cancel', None):  # section 58 "Restart": cancel one job on this root (a join stub's stop)
            ans = request(cfg, {'kind': 'job_cancel', 'job': args.cancel})
            print(json.dumps(ans, ensure_ascii=False))
            sys.exit(0 if ans.get('status') in ('cancelled', 'stopping', 'ended') else 2)
        elif getattr(args, 'recheck', None):
            from fleetmux import lifecycle_recheck
            code, ans = lifecycle_recheck.cli(cfg, args.recheck)
            print(json.dumps(ans, ensure_ascii=False))
            sys.exit(code)
        else:
            if not args.spec:
                raise SystemExit('task needs a spec (JSON file or -), or --recheck ID')
            spec = json.loads(sys.stdin.read() if args.spec == '-' else Path(args.spec).read_text())
            event = {'kind': 'task', 'spec': spec}
            if getattr(args, 'job', False):
                event['job'] = True
            print(json.dumps(request(cfg, event), ensure_ascii=False))


if __name__ == '__main__':
    main()
