"""`fleetmux upgrade --to <release dir or sha> [--config <root config>]`: move this machine's running fleet tree (the
root, every sub-fleet at any depth, the applets) to another release with one command, and check the result.

This is the manual entry of the one release switch (fleetmux/release_drift.py, the root daemon's automatic switch
uses the same): the command resolves ``--to``, reads the tree, writes the plan and hands it to
``release_drift.switch``, then prints the outcome and the ways to recover. Stop -> switch -> start -> check exist only
there, in a fixed order (``release_drift.STEPS``, the step names this command prints):

1. ``preflight``     (here) the root config, the release ``--to`` names, a process for every daemon holding its lock,
                     the run's switch lock (one switch of a run at a time);
2. ``backup``        ONE read of ``fx:jobs`` and of the state the checks compare with, written under
                     ``<fleet home>/upgrade/<UTC time>/`` (outside the run directory);
3. ``stop_root``     SIGTERM to the root daemon, then wait for its lock;
4. ``stop_children`` SIGTERM level by level, each level only once every parent above it let go of its lock;
5. ``stop_applets``  the applet copies;
6. ``restore_files`` <run>/jobs.json and the job-runs index from the snapshot of step 2;
7. ``redis_down``    SHUTDOWN SAVE of every Redis of the tree, until none answers;
8. ``terminal_down`` the run's fleetmux server and window (the fleet's own tmux stays);
9. ``start_new``     every config's ``python`` set to the new release's interpreter, then ``start.sh --resume <run>``
                     with ``FLEETMUX_REL`` (``<to>/.venv/bin/python <to>/fleetmux/fleet.py start --resume <run>`` when
                     the fleet home has no start.sh); named sub-fleets without autostart are asked up again;
10. ``verify``       the checks of ``release_drift.CHECKS``, polled until ``--verify-timeout``, judged against the state
                     before the switch.

A failure after the root stopped goes back the same way to the release that ran before. Every daemon gets SIGTERM
only; the explicit stop (which cancels the jobs) is never used. The command first moves itself into a session of its
own (``detach``). When the switch process died between the stop and the start, running the same command again goes on
from the start.

Output: one line per step (``upgrade: <step>: ok|FAIL ...``, the checks as ``upgrade: verify <check>: ok|FAIL|n/a``,
the way back as ``upgrade: rollback <step>: ...``) and, last, one JSON line ``{"result", "step", "reason", "from",
"to", "backup", "recover", "rolled_back", "rollback_step", "checks"}`` (``rollback_step``: the step the way back
failed at, ``"verify"`` when the old release is up and only a check failed; ``""`` when it worked; null when there
was no way back). Exit codes: 0 done (or nothing to do), 1 a check failed, 2
refused before anything changed, 3 a stop step failed, 4 the new release did not start.
"""
import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from fleetmux import release_drift

STEPS = release_drift.STEPS
EXIT = {'preflight': 2, 'backup': 2, 'stop_root': 3, 'stop_children': 3, 'stop_applets': 3, 'restore_files': 3,
        'redis_down': 3, 'terminal_down': 3, 'start_new': 4, 'verify': 1}
CHECKS = release_drift.CHECKS
LAST = 'last.json'  # <fleet home>/upgrade/last.json: the last line of the last upgrade of this fleet
ALL_RUNNING = 'all-running.json'  # <FLEETMUX_HOME>/upgrade/all-running.json: the trees --all-running moved
WRAPPERS = release_drift.WRAPPERS
STOP_TIMEOUT_S = 240     # a root's SIGTERM waits for its applets' detach_wait_s (up to 120 s) before the lock goes
VERIFY_TIMEOUT_S = 300   # a running job comes back through suspended -> readopt; sub-fleets relaunch in the tick


class Failed(Exception):
    """Refused before anything changed: ``reason`` for the person."""

    def __init__(self, reason, recover=()):
        super().__init__(reason)
        self.reason, self.recover = reason, list(recover)


# ------------------------------------------------------------------ leaving the process group that is stopped
def detach():
    """Move this process into a session of its own before anything is stopped. A process-group leader cannot call
    setsid, so it forks: the child (new session, no controlling terminal, SIGHUP ignored) does the work, the parent
    waits for it and exits with its code; when the parent's group is killed by the stop, the child goes on."""
    if os.getsid(0) == os.getpid():
        return
    try:
        os.setsid()
    except OSError:
        pid = os.fork()
        if pid:
            try:
                _, status = os.waitpid(pid, 0)
                code = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                code = 1
            os._exit(code if code >= 0 else 128 - code)
        os.setsid()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


# ------------------------------------------------------------------ the process layer (tests replace it)
class Ops(release_drift.Ops):
    """The switch's process layer, plus what only the command needs."""

    def self_detach(self):
        detach()

    def running_roots(self):
        out = subprocess.run(['ps', '-axww', '-o', 'pid=,args='], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL).stdout
        return roots_in(out)

    def runtime(self, cfg):
        """The fleetmux terminal runtime of the tree (the root's), None for another backend."""
        if (cfg.get('backend') or 'cmux') != 'fleetmux':
            return None
        from fleetmux import fleet
        saved = {k: os.environ.get(k) for k in ('FLEETMUX_STATE', 'FLEETMUX_HOME')}
        os.environ.update(FLEETMUX_STATE=str(cfg.get('cmux_state_dir') or ''), FLEETMUX_HOME=str(cfg['fleet_home']))
        try:
            return str(fleet.manager_for(cfg).runtime_root)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# ------------------------------------------------------------------ releases
def releases_roots():
    roots = [os.environ.get('FLEETMUX_RELEASES'), str(Path.home() / 'fleetmux-releases')]
    return [Path(r).expanduser() for r in roots if r]


def release_dir(value):
    """The release directory ``--to`` names (a directory, or a sha / unique sha prefix under the releases root); it
    must hold .venv/bin/python*, fleetmux/fleet.py and bin/fleetmux. Raises Failed."""
    from fleetmux import releases
    try:
        named = releases.resolve(value)  # version:<v> -> versions/<v> -> the release
    except ValueError as exc:
        raise Failed('--to ' + str(value) + ': ' + str(exc))
    path = named or Path(value).expanduser()
    if not path.is_dir() and re.fullmatch(r'[0-9a-fA-F]{7,40}', str(value)):
        hits = [d for root in releases_roots() if root.is_dir() for d in root.iterdir()
                if d.is_dir() and (d.name.startswith(value.lower()) or value.lower().startswith(d.name))]
        if len(hits) == 1:
            path = hits[0]
        elif len(hits) > 1:
            raise Failed('--to ' + value + ' names more than one release: ' + ', '.join(str(h) for h in hits))
    if not path.is_dir():
        raise Failed('--to ' + str(value) + ': no such release directory (also looked under '
                     + ', '.join(str(r) for r in releases_roots()) + ')')
    path = path.resolve()
    missing = not_a_release(path)
    if missing:
        raise Failed('--to ' + str(path) + ' is not a release directory: missing ' + ', '.join(missing))
    return path


def not_a_release(path):
    """What release directory ``path`` lacks of .venv/bin/python*, fleetmux/fleet.py and bin/fleetmux."""
    missing = [p for p in ('fleetmux/fleet.py', 'bin/fleetmux') if not (Path(path) / p).exists()]
    if not to_python(path, must=False):
        missing.append('.venv/bin/python')
    return missing


def to_python(rel, must=True):
    """The interpreter of release directory ``rel``: its .venv/bin/python, python3 or python3.x."""
    venv = Path(rel) / '.venv' / 'bin'
    names = ['python', 'python3'] + sorted((p.name for p in venv.glob('python3.*')), reverse=True) \
        if venv.is_dir() else []
    for name in names:
        if (venv / name).exists():
            return str(venv / name)
    if must:
        raise Failed('no interpreter in ' + str(venv))
    return None


def release_dir_of(python):
    """The release directory of an interpreter path ``<release>/.venv/bin/python``, else None."""
    p = Path(python or '')
    return p.parents[2] if len(p.parents) > 2 and p.parent.parent.name == '.venv' else None


# ------------------------------------------------------------------ one tree
def _stamp_dir(base):
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    d, n = base / stamp, 1
    while d.exists():
        n += 1
        d = base / (stamp + '-' + str(n))
    return d


def make_plan(cfg_path, cfg, to, to_name, from_name, ops, verify_timeout, stop_timeout):
    """The plan of release_drift.switch for this tree (the fields of release_drift.launch, plus the manual ones)."""
    run = Path(cfg['run'])
    home = Path(cfg['fleet_home'])
    start = home / release_drift.START
    old_dir = release_dir_of(cfg.get('python'))
    env = {'FLEETMUX_HOME': str(home)}
    if cfg.get('cmux_state_dir'):
        env['FLEETMUX_STATE'] = str(cfg['cmux_state_dir'])
    return {'run': str(run), 'config': str(cfg_path), 'fleet_home': str(home),
            'start': str(start) if start.is_file() else '', 'name': cfg.get('name') or run.name,
            'old': {'release': from_name, 'dir': str(old_dir) if old_dir else ''},
            'new': {'release': to_name, 'dir': str(to)}, 'replace': [],
            'subs': [str(d) for d in release_drift.sub_runs(run) if ops.daemon_running(str(d))], 'applets': [],
            'server_runtime': ops.runtime(cfg), 'stop_wait_s': stop_timeout, 'verify_s': verify_timeout,
            'max_failures': release_drift.DEFAULTS['max_failures'], 'pythonpath': os.environ.get('PYTHONPATH') or '',
            'at': time.time(), 'backup_dir': str(_stamp_dir(home / 'upgrade')), 'manual': True, 'env': env}


def upgrade_tree(cfg_path, to, ops, out, verify_timeout=VERIFY_TIMEOUT_S, stop_timeout=STOP_TIMEOUT_S):
    """Move one root tree: preflight here, the rest in release_drift.switch. Returns the exit code."""
    from fleetmux import fleet, releases, restart_state
    cfg_path = Path(cfg_path).resolve()
    cfg = fleet.load(cfg_path)
    cfg.setdefault('fleet_home', str(fleet.HOME))  # an older config: as start --resume pins it
    run, base = Path(cfg['run']), Path(cfg['fleet_home']) / 'upgrade'
    to_name = releases.release_of(to) or to.name
    from_name = releases.running(cfg)
    fleetmux = str(to / 'bin' / 'fleetmux')

    def finish(result, step, reason, recover=(), frm=None, backup='', more=None):
        doc = dict({'result': result, 'step': step, 'reason': reason, 'from': frm or from_name, 'to': to_name,
                    'backup': backup, 'recover': list(recover), 'rollback_step': None}, **(more or {}))
        out(json.dumps(doc, ensure_ascii=False))
        try:  # what `upgrade status` (rollout's health after a detached restart) reads
            restart_state.write_json(base / LAST, dict(doc, config=str(cfg_path), at=time.time()))
        except OSError:
            pass
        return 0 if result == 'pass' else EXIT[step]

    def refuse(reason):
        out('upgrade: preflight: FAIL ' + reason)
        return finish('fail', 'preflight', reason)

    def say(step, status, detail):
        out('upgrade: ' + step + ': ' + status + (' ' + detail if detail else ''))

    def report(outcome):
        more = {'rolled_back': outcome.get('rolled_back'), 'rollback_step': outcome.get('rollback_step'),
                'checks': outcome.get('checks') or {}}
        if outcome['ok']:
            return finish('pass', '', 'the fleet runs release ' + to_name, frm=outcome['from'],
                          backup=outcome.get('backup', ''), more=more)
        reason = str(outcome.get('reason'))
        if outcome.get('rolled_back'):
            reason += '; the tree is back on release ' + str(outcome['from'])
        elif outcome.get('rolled_back') is False:
            reason += '; going back to release ' + str(outcome['from']) + ' failed too: ' + str(
                outcome.get('rollback_reason'))
        return finish('fail', outcome.get('step') or 'verify', reason, outcome.get('recover') or [],
                      frm=outcome['from'], backup=outcome.get('backup', ''), more=more)

    base.mkdir(parents=True, exist_ok=True)
    with open(base / '.lock', 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return refuse('another fleetmux upgrade of this fleet is running')
        held = release_drift.acquire(run)
        if held is None:
            return refuse('a release switch of this run is running (' + str(run / release_drift.LOCK)
                          + ' is held); nothing was changed')
        try:
            if not ops.daemon_running(str(run)):
                plan = release_drift._json(run / release_drift.PLAN)
                if (isinstance(plan, dict) and plan.get('before') and plan.get('backup_dir')
                        and not (run / release_drift.OUTCOME).exists()):  # died after the stop
                    if not releases.same(plan['new']['release'], to_name):
                        return refuse('a switch to release ' + str(plan['new']['release']) + ' stopped this fleet and '
                                      'did not start it again: run the same command with --to '
                                      + str(plan['new']['dir']))
                    out('upgrade: preflight: ok (continuing the switch to ' + to_name + ' that stopped between the '
                        'stop and the start; backup ' + plan['backup_dir'] + ')')
                    plan.update(resume=True, manual=True, verify_s=verify_timeout, stop_wait_s=stop_timeout)
                    return report(release_drift.switch(plan, ops, say=say, lock=held))
                return refuse('the root fleet of ' + str(cfg_path) + ' is not running: nothing to move (start it '
                              'with `' + fleetmux + ' start --resume ' + str(run) + '`)')
            if releases.same(from_name, to_name):
                return finish('pass', '', 'the fleet already runs release ' + to_name + '; nothing to do')
            for d in [run] + release_drift.sub_runs(run):
                if ops.daemon_running(str(d)) and not release_drift._daemon_pids(ops, d):
                    key = 'root' if d == run else release_drift._sub_name(run, d)
                    return refuse('the daemon of ' + key + ' (' + str(d) + ') holds its lock but no process was '
                                  'found for it; nothing was changed')
            plan = make_plan(cfg_path, cfg, to, to_name, from_name, ops, verify_timeout, stop_timeout)
            release_drift._write(run / release_drift.PLAN, plan)
            out('upgrade: preflight: ok (' + str(from_name) + ' -> ' + to_name + ')')
            return report(release_drift.switch(plan, ops, say=say, lock=held))
        finally:
            held.close()


# ------------------------------------------------------------------ entry
def parser():
    p = argparse.ArgumentParser(prog='fleetmux upgrade', description=(
        "Move this machine's running fleet tree (root, sub-fleets, applets) to another release with the release "
        'switch the root daemon uses: back up the job table, stop the root first and then each level of sub-fleets '
        '(SIGTERM only, nothing is cancelled), stop the applets, write the resume files from the snapshot, save and '
        'stop Redis, stop the terminal server, start the new release with start --resume, then check; a failure '
        'goes back to the release before. Last output line: JSON {result, step, reason, from, to, backup, recover, '
        'rolled_back, rollback_step, checks}. Exit: 0 done, 1 a check failed, 2 refused (nothing changed), '
        '3 a stop step failed, 4 the new release did not start.'))
    p.add_argument('--to', required=True, help='the release directory, its sha under $FLEETMUX_RELEASES / '
                                               '~/fleetmux-releases, or version:<v> (the installer\'s '
                                               'versions/<v> link)')
    which = p.add_mutually_exclusive_group()
    which.add_argument('--config', help="the root fleet's config.json (default: this machine's root fleet)")
    which.add_argument('--all-running', action='store_true',
                       help='every root fleet tree whose root daemon runs on this machine, one after the other; a '
                            'failed tree does not stop the others; the exit code is the most severe one')
    p.add_argument('--verify-timeout', type=float, default=VERIFY_TIMEOUT_S,
                   help='seconds the checks may take to pass (default %d)' % VERIFY_TIMEOUT_S)
    p.add_argument('--stop-timeout', type=float, default=STOP_TIMEOUT_S,
                   help='seconds each daemon has to let go of its lock after SIGTERM (default %d)' % STOP_TIMEOUT_S)
    return p


def find_root_config():
    from fleetmux import fleet, join
    root = join.find_root(fleet.HOME)
    return root and root.get('config')


def main(argv=None, ops=None, out=None, roots=None, ops_for=None):
    """``roots`` (the root configs to move with --all-running) and ``ops_for`` (the Ops of one tree) are injectable:
    tests never enumerate or touch the fleets running on the machine."""
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out or _print
    if argv[:1] == ['status']:
        return status_main(argv[1:], ops=ops, out=out, roots=roots, ops_for=ops_for)
    if argv[:1] == ['running']:
        return running_main(argv[1:], ops=ops, out=out, roots=roots)
    args = parser().parse_args(argv)
    ops = ops or Ops()
    ops.self_detach()  # before anything: the stop must not end this command (it may run in a pane or an applet group)
    try:
        to = release_dir(args.to)
        if not args.all_running:
            cfg_path = args.config or find_root_config()
            if not cfg_path or not Path(cfg_path).is_file():
                raise Failed('no root fleet config: pass --config <root run>/config.json')
    except Failed as exc:
        out('upgrade: preflight: FAIL ' + exc.reason)
        out(json.dumps({'result': 'fail', 'step': 'preflight', 'reason': exc.reason, 'from': None, 'to': args.to,
                        'backup': '', 'recover': []}))
        return EXIT['preflight']
    if args.all_running:
        return all_running(to, ops, out, roots or ops.running_roots, ops_for or (lambda cfg: ops), args)
    return upgrade_tree(cfg_path, to, ops, out, verify_timeout=args.verify_timeout, stop_timeout=args.stop_timeout)


# ------------------------------------------------------------------ every root tree of the machine
SEVERITY = (0, 2, 1, 3, 4)  # least to most severe: done, refused (nothing changed), a check, the stop, the start


def worst_exit(codes):
    return max(codes, key=SEVERITY.index, default=0)


def roots_in(table):
    """The root configs in a ``ps -o pid=,args=`` table: ``fleet.py daemon <config>`` lines (not a keep-awake
    wrapper's) whose config has no ``parent_config``; each once, in table order."""
    from fleetmux import fleet
    found = []
    for line in table.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].isdigit() or Path(parts[1]).name in WRAPPERS:
            continue
        args = parts[1:]
        for i in range(1, len(args) - 1):
            if args[i] == 'daemon' and args[i - 1].endswith('fleet.py'):
                cfg = Path(args[i + 1])
                try:
                    if cfg not in found and cfg.is_file() and not fleet.load(cfg).get('parent_config'):
                        found.append(cfg)
                except (OSError, ValueError):
                    pass
                break
    return found


def machine_record():
    """The root trees the last ``--all-running`` moved (``upgrade status`` keeps checking a tree whose root daemon is
    down, which ps no longer shows)."""
    from fleetmux import paths
    return Path(paths.home()) / 'upgrade' / ALL_RUNNING


def all_running(to, ops, out, roots, ops_for, args):
    from fleetmux import releases, restart_state
    cfgs = [Path(c) for c in roots()]
    to_name = releases.release_of(to) or to.name
    try:
        restart_state.write_json(machine_record(), {'to': to_name, 'configs': [str(c) for c in cfgs],
                                                    'at': time.time()})
    except OSError as exc:
        out('upgrade: could not record the root trees in %s: %s' % (machine_record(), exc))
    trees, codes = [], []
    for cfg in cfgs:
        tag = 'tree ' + Path(cfg).parent.name + ': '
        lines = []

        def tree_out(line, lines=lines, tag=tag):
            lines.append(line)
            out(tag + line)
        code = upgrade_tree(cfg, to, ops_for(cfg), tree_out, verify_timeout=args.verify_timeout,
                            stop_timeout=args.stop_timeout)
        try:
            doc = json.loads(lines[-1]) if lines else {}
        except ValueError:
            doc = {}
        codes.append(code)
        trees.append({'config': str(cfg), 'result': doc.get('result', 'fail'), 'step': doc.get('step', ''),
                      'reason': doc.get('reason', ''), 'backup': doc.get('backup', ''), 'exit': code,
                      'recover': doc.get('recover', [])})
    failed = [t for t in trees if t['result'] != 'pass']
    worst = worst_exit(codes)
    first = next((t for t in failed if t['exit'] == worst), failed[0] if failed else None)
    if not cfgs:
        reason = 'no root fleet is running on this machine; nothing to move'
    elif failed:
        reason = '; '.join(t['config'] + ': ' + t['step'] + ': ' + t['reason'] for t in failed)
    else:
        reason = 'every root tree runs release ' + to_name
    out(json.dumps({'result': 'fail' if failed else 'pass', 'step': first['step'] if first else '',
                    'reason': reason, 'to': to_name, 'trees': trees,
                    'recover': [r for t in failed for r in t['recover']]}, ensure_ascii=False))
    return worst


# ------------------------------------------------------------------ upgrade status (rollout health)
DEFER_S = 60


def status_parser():
    p = argparse.ArgumentParser(prog='fleetmux upgrade status', description=(
        'Health of an upgrade (rollout health protocol): pass when every daemon of every root tree runs --to and its '
        'last upgrade passed; defer while an upgrade is running or none to --to has finished yet; fail when the last '
        'upgrade failed or a daemon is not on --to. Last output line: JSON {result, step, reason, to, trees, '
        'next_check_at}. Exit: 0 pass, 1 fail, 2 defer.'))
    p.add_argument('--to', required=True, help='the release that should run: directory, sha or version:<v>')
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument('--config', help="one root fleet's config.json")
    which.add_argument('--all-running', action='store_true',
                       help='the root trees running now plus the ones the last --all-running moved')
    return p


def status_main(argv, ops=None, out=None, roots=None, ops_for=None):
    from fleetmux import releases
    args = status_parser().parse_args(argv)
    ops = ops or Ops()
    out = out or _print
    try:
        to = release_dir(args.to)
    except Failed as exc:
        out(json.dumps({'result': 'fail', 'step': 'status', 'reason': exc.reason, 'to': args.to, 'trees': []}))
        return 1
    to_name = releases.release_of(to) or to.name
    if args.all_running:
        cfgs = [Path(c) for c in (roots or ops.running_roots)()]
        try:
            rec = json.loads(machine_record().read_text())
            cfgs += [Path(c) for c in rec.get('configs') or [] if Path(c) not in cfgs]
        except (OSError, ValueError, AttributeError):
            pass
    else:
        cfgs = [Path(args.config)]
    trees = [tree_status(cfg, to, to_name, (ops_for or (lambda c: ops))(cfg)) for cfg in cfgs]
    worst = next((r for r in ('fail', 'defer') if any(t['result'] == r for t in trees)), 'pass')
    first = next((t for t in trees if t['result'] == worst), None)
    doc = {'result': worst, 'step': (first or {}).get('step', '') if worst != 'pass' else '',
           'reason': ('; '.join(t['config'] + ': ' + t['reason'] for t in trees if t['result'] == worst)
                      if worst != 'pass' else ('every root tree runs release ' + to_name if trees
                                               else 'no root fleet on this machine')),
           'to': to_name, 'trees': trees}
    if worst == 'defer':
        doc['next_check_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(time.time() + DEFER_S))
    out(json.dumps(doc, ensure_ascii=False))
    return {'pass': 0, 'fail': 1, 'defer': 2}[worst]


def tree_status(cfg_path, to, to_name, ops):
    """pass / fail / defer of one root tree (see status_parser)."""
    from fleetmux import fleet, releases
    row = {'config': str(cfg_path), 'result': 'fail', 'step': '', 'reason': ''}
    try:
        cfg = fleet.load(cfg_path)
    except (OSError, ValueError) as exc:
        row['reason'] = 'config unreadable: ' + str(exc)
        return row
    base = Path(cfg.get('fleet_home') or fleet.HOME) / 'upgrade'
    if upgrade_running(base) or release_drift.switch_running(cfg['run']):
        return dict(row, result='defer', reason='an upgrade is in progress')
    last = last_switch(base, Path(cfg['run']), to_name)
    if not releases.same(last.get('to'), to_name):
        return dict(row, result='defer', reason='no finished upgrade to %s yet (last: %s)'
                    % (to_name, (last.get('to') or 'none')))
    if last.get('result') != 'pass':
        return dict(row, step=last.get('step', ''), reason='the upgrade to %s failed at %s: %s'
                    % (to_name, last.get('step', ''), last.get('reason', '')))
    if not ops.daemon_running(cfg['run']):
        return dict(row, step='daemons', reason='root daemon not running')
    bad = []
    run = Path(cfg['run'])
    for d in [run] + release_drift.sub_runs(run):
        if d != run and not ops.daemon_running(str(d)):
            continue  # a sub-fleet that ended; the upgrade's own checks judged the ones that had to come back
        rel = releases.running(release_drift._json(d / 'config.json') or {'run': str(d)})
        if not releases.same(rel, to_name):
            bad.append(('root' if d == run else release_drift._sub_name(run, d)) + ' runs release ' + str(rel))
    if bad:
        return dict(row, step='daemons', reason='; '.join(bad))
    return dict(row, result='pass', reason='runs release ' + to_name)


# ------------------------------------------------------------------ upgrade running (rollout's check reads it)
def running_parser():
    p = argparse.ArgumentParser(prog='fleetmux upgrade running', description=(
        'Print the release directories the fleet daemons of this machine run now (root fleets and their running '
        'sub-fleets), each once. Without a running daemon: the installed release (~/fleetmux-current), with '
        '"running": false; with neither: an empty list. Last stdout line: JSON {"result": "pass", "releases": '
        '[<absolute dirs>], "running": ..., "daemons": [...]}, exit 0. A daemon whose release is unknown or whose '
        'release directory is gone: {"result": "fail", "step": "running", ...}, exit 1. It only reads.'))
    p.add_argument('--config', help="one root fleet's config.json (default: every root fleet running now)")
    return p


def running_main(argv, ops=None, out=None, roots=None):
    from fleetmux import fleet, releases
    args = running_parser().parse_args(argv)
    ops = ops or Ops()
    out = out or _print
    daemons, dirs = [], []
    try:
        cfgs = [Path(args.config)] if args.config else [Path(c) for c in (roots or ops.running_roots)()]
        for cfg_path in cfgs:
            try:
                run = Path(fleet.load(cfg_path)['run'])
            except (OSError, ValueError, KeyError) as exc:
                raise Failed('config %s unreadable: %s' % (cfg_path, exc))
            for d in [run] + release_drift.sub_runs(run):
                if not ops.daemon_running(str(d)):
                    continue
                rel = running_dir(release_drift._json(d / 'config.json') or {'run': str(d)}, d)
                daemons.append({'run': str(d), 'release': rel.name, 'dir': str(rel)})
                if str(rel) not in dirs:
                    dirs.append(str(rel))
        if daemons:
            reason = 'running fleet daemons run %d release(s)' % len(dirs)
        else:
            link = Path(releases.LINK).expanduser()
            if link.exists():
                inst = link.resolve()
                missing = not_a_release(inst)
                if missing:
                    raise Failed('no fleet daemon runs, and the installed release %s is not a release directory: '
                                 'missing %s' % (inst, ', '.join(missing)))
                dirs, reason = [str(inst)], 'no fleet daemon runs: the installed release ' + inst.name
            else:
                reason = 'no fleet daemon runs and no release is installed: nothing to switch from'
    except Failed as exc:
        out(json.dumps({'result': 'fail', 'step': 'running', 'reason': exc.reason, 'releases': [],
                        'running': bool(daemons), 'daemons': daemons}, ensure_ascii=False))
        return 1
    out(json.dumps({'result': 'pass', 'step': '', 'reason': reason, 'releases': dirs, 'running': bool(daemons),
                    'daemons': daemons}, ensure_ascii=False))
    return 0


def running_dir(cfg, run):
    """The release directory the daemon of ``run`` runs: its ``<run>/release`` record (else the release in the
    config's ``python`` path), found as the directory of that ``python`` or under the releases roots. Raises Failed
    when the release is unknown or no directory of it is left."""
    from fleetmux import releases
    name = releases.running(cfg)
    by_python = release_dir_of(cfg.get('python'))
    if not name:
        raise Failed('the daemon of %s runs an unknown release (no %s record, and its python %s is not in a '
                     'release directory)' % (run, releases.FILE, cfg.get('python')))
    seen = []
    for cand in ([by_python] if by_python and releases.same(by_python.name, name) else []) \
            + [r / name for r in releases_roots()]:
        seen.append(str(cand))
        if cand.is_dir() and not not_a_release(cand):
            return cand.resolve()
    raise Failed('the daemon of %s runs release %s, but no release directory of it is left (looked at %s)'
                 % (run, name, ', '.join(seen)))


def last_switch(base, run, to_name):
    """The record ``upgrade status`` judges: the newer of the last manual upgrade (``<fleet home>/upgrade/last.json``)
    and the run's last switch outcome (``<run>/release-drift-outcome.json``, which the root daemon's own switch
    writes too), counting only the ones to ``to_name``. A manual upgrade refused at preflight changed nothing: it
    counts only when no switch to ``to_name`` is on record (the daemon's switch held the run lock and did the work)."""
    from fleetmux import releases
    try:
        last = json.loads((base / LAST).read_text())
    except (OSError, ValueError):
        last = {}
    last = last if isinstance(last, dict) else {}
    o = release_drift._json(run / release_drift.OUTCOME)
    switch = ({'result': 'pass' if o.get('ok') else 'fail', 'step': '' if o.get('ok') else (o.get('step') or 'verify'),
               'reason': o.get('reason') or '', 'to': o.get('to'), 'at': o.get('at') or 0}
              if isinstance(o, dict) and releases.same(o.get('to'), to_name) else None)
    if switch is None or not releases.same(last.get('to'), to_name):
        return switch or last
    if last.get('step') == 'preflight' and last.get('result') != 'pass':
        return switch
    return last if float(last.get('at') or 0) >= float(switch['at']) else switch


def upgrade_running(base):
    """True while another process holds the fleet's upgrade lock."""
    lock_path = base / '.lock'
    if not lock_path.exists():
        return False
    with open(lock_path, 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def _print(line):
    """stdout may be the pane this command was started in, gone once the terminal server stops: the line is kept
    in the upgrade's log either way."""
    try:
        print(line, flush=True)
    except OSError:
        pass
