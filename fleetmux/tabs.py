"""Companion tabs (docs/design.md §31): roster ``tabs:`` programs opened as tabs after control.

The fleet that loads the roster opens them once per daemon start (start, start --resume, a named sub-fleet's
``daemon --resume``), on the first tick after the roster is accepted: in its own workspace, in the pane of
history / control, after control (roster order), ``--focus false``, titled with their name - or, in a fleet
started from a kit (config ``kit_fleet``, inherited by its named sub-fleets), ``kit-<n>-<name>`` with n the
1-based position in ``tabs:``. They are surfaces of this fleet's cmux registry (``type: tab``, ``tab: <name>``),
so stop / close_all close them too; they are no agent. A tab without ``restart:`` is not watched afterwards: closed
while the daemon runs, it stays closed until the fleet is started again.

``restart: on-exit`` (keepalive): every CHECK_S the daemon looks for the tab's program (the same exact match as
the clear below, one ``ps`` for all tabs; a fresh tab has GRACE_S to show up). Gone -> reopened like at the
start (old surface closed, program cleared, opened, docked back in roster order) after a backoff (``first_s``,
doubling up to ``max_s``; a run longer than the crash-loop window starts it over). ``restarts`` reopenings
within ``minutes`` -> no more: ``tab.crashloop``, a notice card in the supervisor's column and a ``tab_crashloop``
channel push (main service fleet with ``channels:``). Never a pause, never a held slot. Way back:
``fleetmux tab restart <name>`` (any tab this daemon opened). The watch uses the specs opened at the start
(hot edits of ``tabs:`` wait for the next start, as before) and ends with an explicit stop / lifecycle close.

Before a tab opens, every process whose command line is that tab's program is stopped (SIGTERM, up to
KILL_WAIT s, then SIGKILL) so a restart never runs the program twice. Matching (``same_program``) is exact:
the ``ps`` command line must equal the tab's argv (``shlex.split(command)``, joined by spaces), except that the
first word may also be argv[0]'s resolved path (``which`` + ``realpath``), or - for a macOS framework Python -
that interpreter's ``Python.app/Contents/MacOS/Python`` stub of the same ``Versions/X.Y`` directory (framework
Python re-execs itself there, so ``ps`` shows the stub as argv[0]). Every other word must be identical.

Internal API
  open_all(fleet)   Fleet.tick: once per daemon, after the roster is accepted
  watch(fleet)      Fleet.tick, right after open_all: operator restarts, keepalive of ``restart: on-exit`` tabs
  stop_all(fleet)   explicit stop tail: close every tab of this registry; True when all are gone
  request_restart(r, name)  ``fleetmux tab restart`` (CLI side): hand the request to the daemon
"""
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import uuid

KILL_WAIT = 5.0
CHECK_S = 5.0     # keepalive: how often the programs of ``restart: on-exit`` tabs are looked for
GRACE_S = 10.0    # a freshly opened program has this long to show up in ``ps`` before it counts as gone
REQ = 'fx:tab:restart'   # hash {tab name: {"id", "at"}}: `fleetmux tab restart` -> the daemon's next tick
REPLY = 'fx:tab:reply:'  # + request id: the daemon's answer (JSON, EX 3600)
STALE_S = 120            # a request older than this (e.g. left by a stopped daemon) is refused, not run


def heads(argv0):
    """The first words a process running ``argv0`` may show in ``ps``."""
    out = {argv0}
    path = shutil.which(argv0)
    if path:
        real = os.path.realpath(path)
        out |= {path, real}
        m = re.fullmatch(r'(.*/Python\.framework/Versions/[^/]+)/bin/python[0-9.]*', real)
        if m:
            out.add(m.group(1) + '/Resources/Python.app/Contents/MacOS/Python')
    return out


def same_program(line, argv, first=None):
    """Is the ``ps`` command ``line`` exactly the program ``argv``? (rule in the module docstring)"""
    rest = ''.join(' ' + w for w in argv[1:])
    return any(line == h + rest for h in (first if first is not None else heads(argv[0])))


def ps_lines():
    """``pid command`` lines of every process; raises when ``ps`` fails."""
    p = subprocess.run(['ps', '-axww', '-o', 'pid=,command='], capture_output=True, text=True, timeout=20)
    if p.returncode:
        raise RuntimeError('ps failed: ' + (p.stderr or '').strip()[:200])
    return p.stdout.splitlines()


def matching(argv, lines=None):
    """PIDs (not ours) whose command line is exactly ``argv``; ``lines`` = one ``ps_lines()`` shared by several
    tabs (None: run ``ps`` now); raises when ``ps`` fails."""
    first, found = heads(argv[0]), set()
    for line in ps_lines() if lines is None else lines:
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and same_program(parts[1], argv, first):
            found.add(int(parts[0]))
    return found - {os.getpid()}


def signal_all(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def wait_gone(argv, pids, timeout):
    deadline = time.monotonic() + timeout
    left = matching(argv) & pids
    while left and time.monotonic() < deadline:
        time.sleep(.2)
        left = matching(argv) & pids
    return left


def clear(f, spec):
    """Stop every running copy of this tab's program; True when none is left."""
    argv = shlex.split(spec['command'])
    pids = matching(argv)
    if not pids:
        return True
    signal_all(pids, signal.SIGTERM)
    left = wait_gone(argv, pids, KILL_WAIT)
    if left:
        signal_all(left, signal.SIGKILL)
        left = wait_gone(argv, left, 2.0)
    f.log('tab.cleared', level='error' if left else 'info', tab=spec['name'], pids=sorted(pids),
          killed=sorted(pids - left), left=sorted(left))
    return not left


def launch(spec):
    env = ' '.join(shlex.quote(k + '=' + v) for k, v in sorted(spec['env'].items()))
    return ('env ' + env + ' ' if env else '') + spec['command']  # spawn_window runs ``exec <this>``


def title(f, index, name):
    """The tab's surface title: ``kit-<index>-<name>`` in a fleet started from a kit (§31), else ``name``."""
    return 'kit-' + str(index) + '-' + name if (getattr(f, 'cfg', None) or {}).get('kit_fleet') else name


def start_one(f, spec, label, history, after, **why):
    """Clear the program, open its tab titled ``label``, dock it after ``after``; the surface id, or None (logged).
    ``why``: extra audit fields of a reopening (reason, attempt)."""
    name = spec['name']
    extra = dict(why, **({'title': label} if label != name else {}))
    try:
        ok = clear(f, spec)
    except Exception as exc:  # ps failed: fail visibly, never open a possible second copy
        f.log('tab.start_failed', level='error', tab=name, error='process scan: ' + str(exc), **extra)
        return None
    if not ok:
        f.log('tab.start_failed', level='error', tab=name, error='old process still running', **extra)
        return None
    res = f.mgr.spawn_window(label, launch(spec), metadata={'type': 'tab', 'tab': name}, start_directory=spec['cwd'])
    if not res.success:
        f.log('tab.start_failed', level='error', tab=name, error=res.error or 'no surface', **extra)
        return None
    if history and not f.mgr.dock_tab(res.pane_id, history, after=after):
        f.log('tab.dock_failed', level='warn', tab=name, pane=res.pane_id)
    f.log('tab.started', tab=name, pane=res.pane_id, command=spec['command'], cwd=spec['cwd'], **extra)
    return res.pane_id


def open_all(f):
    if f.tab_specs is None or f.tabs_opened:
        return
    f.tabs_opened = True
    from fleetmux.fleet import SERVICE
    for sid, name in f.mgr.tab_panes().items():  # a previous daemon's tab still shown: close it first
        f.mgr.kill_pane(sid)
    history, prev, now = f.r.hget(SERVICE, 'history_pane'), None, time.time()
    f.tab_watch = {}  # name -> state, in roster order: what watch() keeps alive / restarts (this start's specs)
    for index, spec in enumerate(f.tab_specs, 1):
        st = {'spec': spec, 'title': title(f, index, spec['name']), 'pane': None, 'started': now, 'state': 'down',
              'delay': None, 'next': None, 'restarts': []}
        f.tab_watch[spec['name']] = st
        t0 = time.time()
        st['pane'] = start_one(f, spec, st['title'], history, prev)
        st['started'] = now = now + (time.time() - t0)  # the grace counts from the open, not from before the clear
        if st['pane']:
            prev, st['state'] = st['pane'], 'running'
        elif spec.get('restart'):
            down(f, st, now, exited=False)  # keepalive: a failed first open is retried like an exit


# ------------------------------------------------------------------ keepalive (restart: on-exit)
def watch(f, now=None):
    """Fleet.tick: answer ``fleetmux tab restart`` requests, reopen the due tabs, look for exited programs."""
    live = getattr(f, 'tab_watch', None)
    if not live or getattr(f, 'stopping', False) or getattr(f, 'explicit_stop', False):
        return  # nothing opened yet, or the fleet is stopping / closing: never reopen
    now = time.time() if now is None else now
    answer_requests(f, live, now)
    kept = [st for st in live.values() if st['spec'].get('restart')]
    for st in kept:
        if st['state'] == 'waiting' and now >= st['next']:
            st['restarts'].append(now)
            reopen(f, st, now, 'exited')
    if now < getattr(f, 'tab_next_check', 0):
        return
    f.tab_next_check = now + CHECK_S
    running = [st for st in kept if st['state'] == 'running' and now - st['started'] >= GRACE_S]
    if not running:
        return
    try:
        lines = ps_lines()
    except Exception as exc:  # no verdict without a scan: look again next time, say so once
        if not getattr(f, 'tab_scan_failed', False):
            f.log('tab.check_failed', level='error', error=str(exc)[:300])
        f.tab_scan_failed = True
        return
    f.tab_scan_failed = False
    for st in running:
        if not matching(shlex.split(st['spec']['command']), lines):
            down(f, st, now, exited=True)


def down(f, st, now, exited):
    """The program of a keepalive tab is gone (or its tab did not open): schedule the next opening with the
    backoff, or stop at the crash-loop limit."""
    r, name = st['spec']['restart'], st['spec']['name']
    if exited and now - st['started'] >= r['window_s']:
        st['delay'] = None  # it ran longer than the crash-loop window: the backoff starts over
    st['restarts'] = [t for t in st['restarts'] if now - t < r['window_s']]
    if len(st['restarts']) >= r['restarts']:
        st.update(state='crashloop', next=None)
        crashloop(f, st)
        return
    st['delay'] = r['first_s'] if st['delay'] is None else min(st['delay'] * 2, r['max_s'])
    st.update(state='waiting', next=now + st['delay'])
    f.log('tab.down', level='warn', tab=name, reason='exited' if exited else 'start_failed',
          ran_s=round(now - st['started'], 1), restart_in_s=st['delay'])


def reopen(f, st, now, reason):
    """Reopen one tab the §31 way (close its old surface, clear the program, open, dock back in roster order)."""
    from fleetmux.fleet import SERVICE
    shown = f.mgr.tab_panes()
    if st['pane'] and st['pane'] in shown:
        f.mgr.kill_pane(st['pane'])
    prev = None  # the nearest earlier tab still shown (None: right after control)
    for other in f.tab_watch.values():
        if other is st:
            break
        if other['pane'] and other['pane'] in shown:
            prev = other['pane']
    t0 = time.time()
    st['pane'] = start_one(f, st['spec'], st['title'], f.r.hget(SERVICE, 'history_pane'), prev,
                           reason=reason, attempt=len(st['restarts']))
    st['started'] = now = now + (time.time() - t0)  # clear + spawn take seconds: the grace starts after them
    if st['pane']:
        st['state'] = 'running'
    elif st['spec'].get('restart'):
        down(f, st, now, exited=False)
    else:
        st['state'] = 'down'
    return st['pane']


def crashloop(f, st):
    r, name = st['spec']['restart'], st['spec']['name']
    cfg = getattr(f, 'cfg', None) or {}
    config = os.path.join(cfg['run'], 'config.json') if cfg.get('run') else '<run>/config.json'
    text = ('tab ' + st['title'] + ' keeps exiting: reopened ' + str(len(st['restarts'])) + ' times in '
            + ('%g' % (r['window_s'] / 60)) + ' minutes, stopped reopening (the fleet carries on, nothing paused, '
            + 'no slot taken). After fixing it: fleetmux tab restart ' + name + ' --config ' + config)
    f.log('tab.crashloop', level='error', tab=name, title=st['title'], restarts=len(st['restarts']),
          window_s=r['window_s'], line=text)
    notice(f, text, st['title'])


def notice(f, text, label):
    """The crash-loop notice: a card in the supervisor's column (v2) and a ``tab_crashloop`` push on the run's
    channels (only the main service fleet runs the channel actor). Failures are logged, never raised."""
    v2 = getattr(f, 'v2', None)
    if v2 is not None:
        try:
            from fleetmux.presentation import routing_card
            card = routing_card(text, kind='notice', slot=label, severity='warn')
            card['metadata']['event_id'] = 'ui-tab-crashloop-' + uuid.uuid4().hex[:24]
            v2.ui('supervisor', card)
        except Exception as exc:
            f.log('tab.notice_failed', level='error', via='card', error=str(exc)[:300])
    try:
        from fleetmux.channels import envelope, queue, supervise
        if supervise.wanted(f):
            name = (getattr(f, 'cfg', None) or {}).get('name') or ''
            queue.enqueue(f.r, envelope.outbound('tab_crashloop', target='last',
                                                 text=('[fleet ' + name + '] ' if name else '') + text))
    except Exception as exc:
        f.log('tab.notice_failed', level='error', via='channel', error=str(exc)[:300])


def answer_requests(f, live, now):
    """``fleetmux tab restart <name>``: forget the tab's crash history and reopen it now (any tab of this start)."""
    for name, raw in (f.r.hgetall(REQ) or {}).items():
        f.r.hdel(REQ, name)
        try:
            req = json.loads(raw)
            rid, at = str(req['id']), float(req['at'])
        except (TypeError, ValueError, KeyError):
            continue
        st = live.get(name)
        if now - at > STALE_S:
            answer = {'status': 'rejected', 'reason': 'stale request (older than ' + str(STALE_S) + ' s)'}
        elif st is None:
            answer = {'status': 'rejected', 'reason': 'this daemon opened no tab ' + name + ' (tabs: '
                      + ', '.join(live) + ')'}
        else:
            f.log('tab.restart_requested', tab=name, was=st['state'], restarts=len(st['restarts']))
            st.update(restarts=[], delay=None)
            if reopen(f, st, now, 'operator'):
                answer = {'status': 'accepted', 'tab': name, 'title': st['title'], 'pane': st['pane']}
            else:
                answer = {'status': 'failed', 'tab': name, 'reason': 'the tab did not open (control: tab.start_failed)'
                          + ('; retried in ' + str(st['delay']) + ' s' if st['state'] == 'waiting' else '')}
        f.r.set(REPLY + rid, json.dumps(answer, ensure_ascii=False), ex=3600)


def request_restart(r, name, wait=30, clock=time.time, sleep=time.sleep):
    """CLI side of ``fleetmux tab restart``: leave the request for the daemon, wait for its answer."""
    rid = uuid.uuid4().hex
    r.hset(REQ, name, json.dumps({'id': rid, 'at': clock()}))
    deadline = clock() + wait
    while True:
        raw = r.get(REPLY + rid)
        if raw:
            return json.loads(raw)
        if clock() >= deadline:
            return {'status': 'pending', 'request_id': rid}
        sleep(.2)


def stop_all(f):
    """Explicit stop: close every tab of this registry; True when all are gone."""
    f.tab_specs = None
    f.tab_watch = {}  # keepalive ends with the fleet
    ok = True
    for sid, name in f.mgr.tab_panes().items():
        gone = f.mgr.kill_pane(sid) or f.mgr.get_pane_status(sid) is None
        f.log('tab.stopped', level='info' if gone else 'error', tab=name, pane=sid, confirmed=gone)
        ok = ok and gone
    return ok
