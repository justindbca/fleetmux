"""Lifecycle closing (lifecycle design §2–§3, master design v2 §4.11/§4.14): what the daemon does once a
run is done, stuck or out of time, per the mode table (config/fleet.yaml#lifecycle_modes, re-read every tick).

* task modes, terminal state:
  - ``completed``: dev_task first passes the trusted-Python gate (lifecycle_deliver); review_loop needs a
    clean PASS verdict. Then stop + close (``closed_by: auto``). A failed gate turns the task blocked.
  - ``blocked`` / ``budget_exhausted``: hold the run as it is for ``hold_hours`` (default 12), then
    ``stop_keep_resumable`` (``closed_by: timeout``, result.json ``resumable: true``: start --resume
    reopens it). scheduled (``on_fail``) and smoke (``on_any_end``) close at once instead.
  - a review_loop that reaches ``max_rounds`` FAIL verdicts (dev_task: ``review_step.max_rounds`` with
    ``on_cap: stop_and_report``) is ended blocked and closed with an outstanding-issues list; scheduled past ``hard_timeout_min`` is ended
    ``budget_exhausted`` and closed (``closed_by: timeout``).
* service: idle workers are reclaimed and respawned on demand; optional ``idle_stop_hours``
  (lifecycle_hold).
* Closing reuses the explicit-stop tail of RuntimeV2.run (descendants first, every runtime retired and
  confirmed). Only then result.json gets ``closed_by`` / ``closed_at``, the notifications go out (TG +
  the session that started the run) and, after the daemon has shut its Redis down, a detached closer
  closes this run's cmux workspace (registry ownership checked). A stop that cannot be confirmed keeps
  the workspace and Redis and raises an alarm instead.
* Sub-fleets are not closed here: their parent collects and closes them (subfleet.tick).
* Runs without a lifecycle (older runs) are untouched.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from fleetmux import fleet_config, lifecycle, lifecycle_deliver, lifecycle_hold, paths

LIFE = 'fx:lifecycle'  # per-run lifecycle state: started:/gate:/cap:/timeout:/held:<task id>, service_active_at
NOTIFIER = None        # tests / embedders: callable(record) -> status string; replaces every real channel
SPAWN_CLOSER = None    # tests: callable(cfg_path) instead of launching the detached closer
TG_ENV, PARENT_ENV, INITIATOR_ENV = 'FLEETMUX_TG_CMD', 'FLEETMUX_PARENT_CMD', 'FLEETMUX_INITIATOR'
INITIATOR_KINDS = ('claude', 'codex', 'launchd', 'program', 'fleet', 'cmux', 'user')


# ------------------------------------------------------------------ initiator (who started the run)
def parse_initiator(value):
    """'kind:id' -> {'kind', 'id'}; ValueError when malformed."""
    kind, sep, ident = (value or '').partition(':')
    if not sep or kind not in INITIATOR_KINDS or not ident.strip():
        raise ValueError('--initiator must be KIND:ID with KIND one of ' + ', '.join(INITIATOR_KINDS)
                         + ' (e.g. claude:<session name>, launchd:<label>, program:<id>)')
    return {'kind': kind, 'id': ident.strip()}


def initiator_for_start(value=None, env=None):
    """Explicit --initiator, else $FLEETMUX_INITIATOR, else a launchd job's label, else None."""
    env = os.environ if env is None else env
    if value:
        return parse_initiator(value)
    if env.get(INITIATOR_ENV):
        return parse_initiator(env[INITIATOR_ENV])
    label = env.get('XPC_SERVICE_NAME', '')
    if label and label != '0' and not label.startswith('application.'):
        return {'kind': 'launchd', 'id': label}
    return None


# ------------------------------------------------------------------ notifications
def _safe(text):
    return re.sub(r'[^A-Za-z0-9._-]', '_', text)[:120] or 'unknown'


def _run_command(command, data):
    try:
        p = subprocess.run(shlex.split(command), input=data, text=True, capture_output=True, timeout=30)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return 'failed: ' + str(exc)
    return 'sent' if p.returncode == 0 else 'failed: exit ' + str(p.returncode) + ' ' + (p.stderr or '')[-200:]


def notify(f, event, text, policy=None):
    """One notification per channel (tg, parent); every record lands in <run>/notifications.jsonl.

    No channel command configured ($FLEETMUX_TG_CMD / $FLEETMUX_PARENT_CMD or the mode table's
    common.notify_commands) means nothing leaves the machine: the record says ``no_channel`` (TG) or
    sits in the initiator's mailbox under the fleet state directory (parent)."""
    policy = policy or {}
    commands = policy.get('notify_commands') or {}
    channels = ['tg'] + (['parent'] if policy.get('notify_parent_session', True) else [])
    cfg, out = f.cfg, []
    for channel in channels:
        rec = {'event': event, 'channel': channel, 'text': text, 'run': cfg.get('run'),
               'lifecycle': cfg.get('lifecycle'), 'task_id': f.task().get('id', '') if not f.service else '',
               'initiator': cfg.get('initiator'), 'at': time.time()}
        injected = getattr(f, 'notifier', None) or NOTIFIER
        if injected:
            rec['status'] = injected(dict(rec))
        elif channel == 'tg' and cfg.get('channels'):  # contracts §8: the run's channels replace $FLEETMUX_TG_CMD
            rec['status'] = _channel_notify(cfg, text)
        elif channel == 'tg':
            command = os.environ.get(TG_ENV) or commands.get('tg')
            rec['status'] = _run_command(command, text) if command else 'no_channel'
        else:
            who = cfg.get('initiator')
            if not who:
                rec['status'] = 'no_initiator'
            else:
                box = Path(cfg.get('cmux_state_dir') or paths.state_dir()) / 'mailbox'
                box.mkdir(parents=True, exist_ok=True)
                with open(box / (who['kind'] + '-' + _safe(who['id']) + '.jsonl'), 'a', encoding='utf-8') as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
                command = os.environ.get(PARENT_ENV) or commands.get('parent')
                rec['status'] = _run_command(command, json.dumps(rec, ensure_ascii=False)) if command else 'mailbox'
        with open(Path(cfg['run']) / 'notifications.jsonl', 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        f.log('lifecycle.notified', what=event, channel=channel, status=rec['status'])
        out.append(rec)
    return out


def _channel_notify(cfg, text):
    """The tg leg of a run with ``channels:``: one run_closed item to last through channels.actor.send_direct (a
    library call in this process; no queue, no lease). No entry lists run_closed in ``push`` -> 'no_channel'."""
    if not any('run_closed' in (e.get('push') or ()) for e in cfg['channels'] if isinstance(e, dict)):
        return 'no_channel'
    from fleetmux.channels import actor, envelope
    try:
        res = actor.send_direct(cfg, envelope.outbound('run_closed', target='last', text=text))
    except Exception as exc:  # noqa: BLE001  (a notification never breaks the close)
        print('lifecycle notify: channel send failed: ' + type(exc).__name__, file=sys.stderr)
        return 'failed'
    status = res.get('status') if isinstance(res, dict) else None
    return 'sent' if status == 'sent' else (status or 'failed')


# ------------------------------------------------------------------ helpers
def _hget(f, field):
    return f.r.hget(LIFE, field)


def _hset(f, **fields):
    f.r.hset(LIFE, mapping=fields)


def task_spec(task):
    try:
        return json.loads(task.get('lifecycle_spec') or '{}')
    except ValueError:
        return {}


def fleet_name(f):
    return f.cfg.get('name') or f.cfg.get('token') or Path(f.cfg['run']).name


def set_task_status(f, status, reason):
    """Rewrite a terminal task's status (the completion gate failed) through a v2 transaction."""
    from fleetmux.fleet import TASK
    f.v2.transaction('lifecycle-status-' + f.task().get('id', '') + '-' + status,
                     lambda: f.r.hset(TASK, mapping={'status': status, 'reason': reason}))
    f.log('lifecycle.status', status=status, reason=reason)


def request_close(f, closed_by, reason, *, resumable=False, report=None, via=None):
    """Stop the run through the explicit-stop tail of RuntimeV2.run; finalize() runs after retirement.

    ``via`` records who asked (``'fleetmux-sidebar'`` for the sidebar's close button): finalize() copies it
    into result.json as ``closed_via``, so the closer can keep the server for ↺ (§13.5).
    """
    if f.lifecycle_close:
        return
    f.lifecycle_close = {'closed_by': closed_by, 'reason': reason, 'resumable': resumable, 'report': report}
    if via:
        f.lifecycle_close['via'] = via
    f.explicit_stop = f.stopping = True
    f.log('lifecycle.closing', closed_by=closed_by, reason=reason, resumable=resumable)


def managed(f):
    return bool(f.cfg.get('lifecycle')) and not f.cfg.get('parent_config') and f.v2 is not None


# ------------------------------------------------------------------ the daemon tick
def tick(f, now=None):
    """Called by Fleet.tick before write_result (a failed completion gate must reach result.json)."""
    if not managed(f) or f.stopping:
        return
    now = time.time() if now is None else now
    try:
        policy = lifecycle.get_mode(f.cfg['lifecycle'])
    except ValueError as exc:
        f.log('lifecycle.table_error', error=str(exc))
        return
    if 'worker_idle_reclaim_min' in f.cfg:  # start --worker-idle-reclaim-min: this run's own value
        policy['worker_idle_reclaim_min'] = f.cfg['worker_idle_reclaim_min']
    if f.service:
        return lifecycle_hold.service_tick(f, policy, now)
    task = f.task()
    tid = task.get('id')
    if not tid:
        return
    if task.get('status') == 'running':
        running_checks(f, policy, task, now)
        task = f.task()
        if task.get('status') == 'running':
            return
    terminal(f, policy, task, now)


def running_checks(f, policy, task, now):
    tid = task['id']
    started = _hget(f, 'started:' + tid)
    if started is None:
        _hset(f, **{'started:' + tid: now})
        started = now
    limit = policy.get('hard_timeout_min')
    if limit is not None and now - float(started) >= float(limit) * 60:
        _hset(f, **{'timeout:' + tid: 1})
        f.finish('budget_exhausted', 'hard_timeout_min ' + str(limit) + ' exceeded (' + policy['name'] + ')')
        return
    spec = task_spec(task)
    if policy['name'] == 'review_loop' and spec.get('review_file'):
        review_cap(f, tid, spec['review_file'], int(policy.get('max_rounds') or 3), 'review_loop')
    review = policy.get('review_step') or {}
    if (policy['name'] == 'dev_task' and review.get('max_rounds') and review.get('on_cap') == 'stop_and_report'
            and (spec.get('deliver') or {}).get('review_file')):
        # dev_task review rounds are capped too (10/13 codex-notype ran to r16): at the cap no new round
        # starts; the open P0/P1 go to the user in the result instead.
        review_cap(f, tid, spec['deliver']['review_file'], int(review['max_rounds']), 'dev_task review')


def review_cap(f, tid, review_file, cap, label):
    """A FAIL verdict at round >= cap ends the task blocked with the outstanding-issues list."""
    verdict, _ = lifecycle_deliver.read_verdict(f.workspace() / review_file)
    if verdict and verdict['verdict'] == 'FAIL' and verdict['round'] >= cap:
        report = write_outstanding(f, verdict, cap, label)
        _hset(f, **{'cap:' + tid: str(report)})
        left = len(lifecycle_deliver.outstanding(verdict))
        f.finish('blocked', label + ': round ' + str(verdict['round']) + ' of max_rounds ' + str(cap)
                 + ' still FAIL; ' + str(left) + ' open issue(s) handed to the user: ' + str(report))


def write_outstanding(f, verdict, cap, label='review_loop'):
    """The outstanding-issues list (挂账清单) of a capped review loop: JSON + a readable Markdown."""
    items = lifecycle_deliver.outstanding(verdict)
    data = {'task_id': f.task().get('id', ''), 'round': verdict['round'], 'max_rounds': cap,
            'reviewer': verdict.get('reviewer'), 'issues': items, 'at': time.time()}
    out = f.run_dir / 'outstanding.json'
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    lines = ['# 挂账清单：' + label + ' 第 ' + str(verdict['round']) + ' 轮仍 FAIL（max_rounds ' + str(cap) + '）', '']
    lines += ['- [' + str(i.get('severity', '?')) + '] ' + str(i.get('id', '')) + ' ' + str(i.get('title', ''))
              for i in items] or ['- （verdict 未列出具体问题）']
    (f.run_dir / 'outstanding.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return out


def terminal(f, policy, task, now):
    tid, status, name = task['id'], task.get('status'), policy['name']
    spec = task_spec(task)
    if status == 'completed':
        problem = completion_problem(f, policy, task, spec)
        if problem:
            set_task_status(f, 'blocked', problem)
            task, status = f.task(), 'blocked'
        else:
            f.write_result()
            return request_close(f, 'auto', name + ' completed')
    f.write_result()
    if status == 'cancelled':
        return request_close(f, 'auto', name + ' cancelled: ' + task.get('reason', ''))
    if _hget(f, 'cap:' + tid):
        return request_close(f, 'auto', task.get('reason', ''), report=_hget(f, 'cap:' + tid))
    if _hget(f, 'timeout:' + tid):
        return request_close(f, 'timeout', task.get('reason', ''))
    if policy.get('on_any_end') == 'stop_and_close' or policy.get('on_fail') == 'stop_and_close':
        return request_close(f, 'auto', name + ' ' + status + ': ' + task.get('reason', ''))
    lifecycle_hold.hold_tick(f, policy, task, now)


def completion_problem(f, policy, task, spec):
    """None when a completed task really is done by its mode's done_when; else why not."""
    name = policy['name']
    if name == 'dev_task':
        raw = _hget(f, 'gate:' + task['id'])
        if raw is None:
            deliver = spec.get('deliver')
            if not deliver:
                rec = {'ok': False, 'problem': 'dev_task has no deliver spec: done_when cannot be judged'}
            else:
                review = policy.get('review_step') or {}
                rec = lifecycle_deliver.deliver(deliver, f.workspace(), task_id=task['id'], run=str(f.run_dir),
                                                reviewer=review.get('reviewer', 'opus'),
                                                must_pass=review.get('must_pass', True))
            _hset(f, **{'gate:' + task['id']: json.dumps(rec, ensure_ascii=False)})
            tests = rec.get('tests') or {}
            f.log('lifecycle.gate', ok=rec['ok'], problem=rec.get('problem', ''), steps=rec.get('steps', []),
                  tests={'returncode': tests.get('returncode'), 'log': tests.get('log') or '',
                         'tail': '\n'.join((tests.get('tail') or '').splitlines()[-20:])})
        else:
            rec = json.loads(raw)
        return None if rec['ok'] else 'dev_task done_when not met: ' + rec.get('problem', '')
    if name == 'review_loop' and spec.get('review_file'):
        verdict, problem = lifecycle_deliver.read_verdict(f.workspace() / spec['review_file'])
        if verdict is None:
            return 'review_loop done_when not met: ' + problem
        problem = lifecycle_deliver.review_problem(verdict)
        return 'review_loop done_when not met: ' + problem if problem else None
    return None


# ------------------------------------------------------------------ after retirement (RuntimeV2.run tail)
def closing_text(f, closing, res, stopped):
    name, tid = f.cfg.get('lifecycle'), res.get('task_id') or ''
    head = '[fleet ' + fleet_name(f) + '] ' + name + (' ' + tid if tid else '') + ' '
    if not stopped:
        return head + '停机未确认：工作区与 Redis 保留，需要人工检查。run: ' + f.cfg['run']
    delivery = res.get('delivery') or {}
    if res.get('status') == 'completed' and delivery.get('ok'):
        q = delivery.get('queue_entry') or {}
        body = ('完成：' + str(delivery.get('sha', ''))[:12] + ' 已合入 ' + str(q.get('deploy_branch', ''))
                + '，已入队待批（未上线）')
    elif closing.get('resumable'):
        body = (res.get('status', '') + '，保留现场 hold 已到时，已停机并保留可续跑：fleetmux start --resume '
                + f.cfg['run'])
    else:
        body = res.get('status', 'stopped') + '：' + (closing.get('reason') or res.get('reason') or '')
    if closing.get('report'):
        body += '；挂账清单 ' + str(closing['report'])
    return head + body + '（closed_by=' + closing['closed_by'] + '）'


def finalize(f, stopped):
    """After the stop tail: result.json fields, notifications, and the workspace-close request."""
    if not managed(f):
        return
    closing = f.lifecycle_close or {'closed_by': 'user', 'reason': 'explicit fleet stop'}
    try:
        policy = lifecycle.get_mode(f.cfg['lifecycle'])
    except ValueError:
        policy = {}
    from fleetmux.fleet import load, save
    path = f.run_dir / 'result.json'
    try:
        res = load(path)
    except (OSError, ValueError):
        res = {}
    if stopped:
        res.update(mode=f.cfg['lifecycle'], closed_by=closing['closed_by'], closed_at=time.time())
        if closing.get('via'):
            res['closed_via'] = closing['via']
        if closing.get('resumable'):
            res['resumable'] = True
        if closing.get('report'):
            res['outstanding'] = str(closing['report'])
        raw = None if f.service else _hget(f, 'gate:' + (res.get('task_id') or ''))
        if raw:
            res['delivery'] = json.loads(raw)
        close_ws = bool(policy.get('close_workspace_after_stop', True))
        res['workspace'] = 'closing' if close_ws else 'kept'
        save(path, res)
        f.close_workspace_pending = close_ws
    notify(f, 'closed' if stopped else 'stop_unconfirmed', closing_text(f, closing, res, stopped), policy)


def after_daemon(f):
    """daemon(): Redis is down; launch the detached closer for this run's cmux workspace."""
    if not getattr(f, 'close_workspace_pending', False):
        return
    if SPAWN_CLOSER:
        return SPAWN_CLOSER(str(f.path))
    log = open(f.run_dir / 'lifecycle-close.log', 'a')
    from fleetmux import fleet as fleet_mod
    subprocess.Popen([f.cfg.get('python') or sys.executable, str(Path(fleet_mod.__file__).resolve()), 'lifecycle-close',
                      str(f.path)], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                     cwd=str(f.run_dir))


def ownership_problem(meta, run):
    """None when the cmux registry provably belongs to this run (run tag, or every surface's log under it)."""
    run = str(Path(run).resolve())
    if meta.get('run') and str(Path(meta['run']).resolve()) != run:
        return 'cmux registry belongs to run ' + meta['run'] + ', not ' + run
    outputs = [info.get('output_file') for info in meta.get('surfaces', {}).values() if info.get('output_file')]
    foreign = [o for o in outputs if not str(Path(o).resolve()).startswith(run + os.sep)]
    if foreign:
        return 'cmux registry holds surfaces of another run: ' + foreign[0]
    if not meta.get('run') and not outputs:
        return 'cmux registry has no evidence it belongs to ' + run
    return None


def all_gone(mgr, meta, wait=10.0):
    """Re-check after close_all reported a failure: on real cmux the last surface of a workspace can
    linger a few seconds after its process tree is killed (and closing it closes the workspace, so the
    later close-workspace finds nothing). Closed only when no surface of ours is live any more.

    Asked through the backend interface (``session_exists``): on cmux that is exactly "a live surface in
    the registry's agents or control workspace" (close_all never drops those two keys, so ``meta`` and
    the registry name the same workspaces); on WezTerm it is "our private mux server still lists a pane"."""
    _ = meta
    deadline = time.monotonic() + wait
    while True:
        live = mgr.session_exists()
        if not live or time.monotonic() >= deadline:
            return not live
        time.sleep(.5)


SIDEBAR_VIA = 'fleetmux-sidebar'  # result.json closed_via of a sidebar × close (§12.1 / §13.5)
CLOSED_LINE1 = '已关闭 · 可恢复'  # the grey card's first line after such a close (§13.3)
TREE_CLOSED_LINE1 = '已收回 · 随根关闭'  # a sub-fleet card closed with its root (brief-m4 §9 defect A)
TREE_CLOSED_LINE2 = '只读，可看记录'  # same second line as a reclaimed sub-fleet card (fleetmux_sidebar._child)
STOPPED_STATE, STOPPED_LINE1 = 'stopped', '已停止'  # a stopped run that is no ↺ history entry: grey, no ×, no ↺
RESUME_ENV_CFG = (('FLEETMUX_HOME', 'fleet_home'), ('FLEETMUX_STATE', 'cmux_state_dir'),
                  ('FLEETMUX_CONFIG', 'private_config'))  # the run's pinned locations
RESUME_ENV_OS = ('FLEETMUX_BIN', 'FLEETMUX_THEME', 'PATH')  # what the ↺ pane inherits


def sidebar_keeps_the_server(res, cfg):
    """A resumable fleetmux run closed from the sidebar: its server stays up for ↺ (§13.5)."""
    from fleetmux import fleet as fleet_mod
    return (res.get('closed_via') == SIDEBAR_VIA and res.get('resumable') is True
            and fleet_mod.run_backend(cfg) == 'fleetmux')


def resume_env(cfg):
    """The env the ↺ pane gets: this run's pinned locations plus the fleetmux binary / theme / PATH."""
    env = {}
    for name, key in RESUME_ENV_CFG:
        value = cfg.get(key)
        if value and value != fleet_config.NONE:
            env[name] = str(value)
    for name in RESUME_ENV_OS:
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def resume_entry(cfg, run, res):
    """kv ``restorable`` item: exactly the ``start --resume`` command the sidebar's ↺ runs (plan §18.3)."""
    from fleetmux import fleet as fleet_mod
    from fleetmux import subfleet
    run = Path(run)
    return {'id': run.name, 'title': subfleet.root_name(cfg, res.get('goal') or ''),
            'closed_at': int(res.get('closed_at') or time.time()),
            'argv': [str(cfg.get('python') or sys.executable), str(Path(fleet_mod.__file__).resolve()),
                     'start', '--resume', str(run)],
            'cwd': str(run), 'env': resume_env(cfg)}


def subtree_card_ids(cfg):
    """Card ids of every sub-fleet under this run, any depth: read from the run's own ``sub/`` records
    (``subfleet.tree``) with the sidebar's id rule (``fleetmux_sidebar._child``), never guessed from an id prefix."""
    from fleetmux import fleetmux_sidebar, subfleet
    ids = set()

    def walk(tree):
        for key, item in (tree or {}).items():
            ids.add(fleetmux_sidebar._child(key, item)['id'])
            walk(item.get('subfleets') or {})
    try:
        walk(subfleet.tree(cfg))
    except (OSError, ValueError, KeyError, TypeError):
        return set()  # an unreadable tree: only the root card is greyed, as before
    return ids


def mark_sidebar_closed(mgr, run_id, subtree=(), state='closed', line1=CLOSED_LINE1):
    """Rewrite this run's card in kv ``sidebar``: grey dot, ``已关闭 · 可恢复`` (§13.5) — or the given ``state`` /
    ``line1`` (:func:`retire_cards`).

    ``subtree`` (card ids of this run's sub-fleets, :func:`subtree_card_ids`): each of those cards that still reads
    running / paused / stopping becomes ``reclaimed`` / ``已收回 · 随根关闭`` / ``只读，可看记录`` - the tree was
    closed with its root (brief-m4 §9 defect A). A card already reclaimed and every card outside the tree keep
    their text; the next daemon push after ↺ rebuilds the tree cards from the records."""
    get, put = getattr(mgr, 'kv_get', None), getattr(mgr, '_kv_set', None)
    if get is None or put is None:
        return False
    try:
        data = json.loads(get('sidebar') or 'null')
    except ValueError:
        return False
    if not isinstance(data, dict) or not isinstance(data.get('fleets'), list):
        return False
    changed = False
    for card in data['fleets']:
        if isinstance(card, dict) and card.get('id') == run_id:
            card.update(state=state, line1=line1)
            changed = True
        elif isinstance(card, dict) and card.get('id') in subtree and card.get('state') in LIVE_CARD_STATES:
            card.update(state='reclaimed', line1=TREE_CLOSED_LINE1, line2=TREE_CLOSED_LINE2)
            changed = True
    if changed:
        put('sidebar', json.dumps(data, ensure_ascii=False, separators=(',', ':')))
    return changed


LIVE_CARD_STATES = ('running', 'paused', 'stopping')  # a card that still runs is never regreyed or dropped


def merge_history_cards(mgr, history_cards, current):
    """kv ``sidebar`` keeps its cards in place and gains the history's grey cards (§13.6-1 / §18.2).

    The just-closed run's card was already greyed in place by ``mark_sidebar_closed``; every other history card
    is appended newest first while the 16-card cap allows it. A card that still reads running / paused /
    stopping is never rewritten to closed and never dropped to make room (§22.3 keeps the S2b assertions:
    the other cards stay as they were). When the daemon never pushed a kv ``sidebar`` at all, the history
    cards are written from scratch: the window must still show the history (§18.2). ``current`` becomes the
    run just closed.
    """
    from fleetmux import fleetmux_history
    get, put = getattr(mgr, 'kv_get', None), getattr(mgr, '_kv_set', None)
    if get is None or put is None:
        return False
    raw = get('sidebar')
    try:
        data = json.loads(raw or 'null')
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get('fleets'), list):
        data = {'v': 1, 'fleets': []}  # no push ever landed: the history cards are all the sidebar has (§18.2)
    fleets = [c for c in data['fleets'] if isinstance(c, dict)]
    by_id = {c.get('id'): c for c in fleets}
    for card in history_cards:
        here = by_id.get(card['id'])
        if here is not None:
            if here.get('state') not in LIVE_CARD_STATES:  # a live card keeps its state and text
                here.update({key: card[key] for key in ('state', 'line1', 'line2')})
            continue
        if len(fleets) >= fleetmux_history.MAX_CARDS:
            break  # §18.3: 16 cards at most, and the ones already there are never dropped for a grey one
        fleets.append(card)
        by_id[card['id']] = card
    if not fleets:
        return False  # an empty card list is not a legal kv sidebar (§18.7-2)
    data['fleets'] = fleets[:fleetmux_history.MAX_CARDS]
    data['current'] = current
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    if raw == text:
        return False
    return put('sidebar', text)


def register_history(mgr, cfg, run, entry):
    """kv ``restorable`` + kv ``sidebar`` for the whole history: the just-closed run first (§18.2 / §18.6).

    The closer is the one writer that knows this close for certain (``finalize`` wrote result.json just before),
    so its entry is prepended instead of trusting the scan to find it; the rest comes from
    ``fleetmux_history.restorable_runs`` so the list can never drift from the ``gui`` subcommand's. Both keys
    get the same runs, newest first and capped at 16: ``restorable_kv`` for the ↺ commands, ``cards`` for the
    grey cards merged into the sidebar the fleet already had.
    """
    from fleetmux import fleetmux_history
    home = cfg.get('fleet_home') or str(Path(run).parent.parent)
    runs = [r for r in fleetmux_history.restorable_runs(home, cfg.get('cmux_state_dir')) if r['id'] != entry['id']]
    runs = ([entry] + runs)[:fleetmux_history.MAX_CARDS]
    runs.sort(key=lambda r: (-r['closed_at'], r['id']))
    wanted = json.dumps(fleetmux_history.restorable_kv(runs), ensure_ascii=False, separators=(',', ':'))
    if mgr.kv_get('restorable') != wanted:
        mgr._kv_set('restorable', wanted)
    merge_history_cards(mgr, fleetmux_history.cards(runs), entry['id'])
    return True


def retire_cards(mgr, cfg, run, res):
    """``close_all`` kept the server for a busy bash page (2026-11-08): no daemon pushes kv ``sidebar`` any more, so
    this run's cards must leave running / paused / stopping here, or the reaper (§13.4) never lets the server go.
    A ↺ history run (resumable, closed from the sidebar or by a timeout, §18.6) gets what the sidebar path writes:
    a ``closed`` card with its own line and its kv ``restorable`` entry. Any other run (``fleet.sh stop``) is no
    history entry: its card turns ``stopped`` / ``已停止`` (grey, no ×, no ↺). The tree's cards as §9 defect A."""
    from fleetmux import fleetmux_history
    run = Path(run)
    subtree = subtree_card_ids(cfg) - {run.name}
    line1 = fleetmux_history._line1(res) if res.get('resumable') is True else None
    if line1 is None:
        return mark_sidebar_closed(mgr, run.name, subtree, state=STOPPED_STATE, line1=STOPPED_LINE1)
    mark_sidebar_closed(mgr, run.name, subtree, line1=line1)
    return register_history(mgr, cfg, run, dict(resume_entry(cfg, run, res), line1=line1))


def close_workspace(cfg_path, wait=60.0, mgr=None):
    """The detached closer: wait for the daemon to exit, check ownership, close only this run's tabs and
    (now empty) workspaces. Result goes to result.json ``workspace`` and the closer log."""
    from fleetmux import fleet as fleet_mod
    cfg = fleet_mod.load(cfg_path)
    run = Path(cfg['run'])
    deadline = time.monotonic() + wait
    while fleet_mod.daemon_running(run) and time.monotonic() < deadline:
        time.sleep(.5)
    if fleet_mod.daemon_running(run):
        outcome = 'kept: daemon still running'
    else:
        mgr = mgr or fleet_mod.manager_for(cfg)
        meta = mgr._meta()
        problem = ownership_problem(meta, run)
        try:
            res = fleet_mod.load(run / 'result.json')
        except (OSError, ValueError):
            res = {}
        if problem:
            outcome = 'kept: ' + problem
        elif sidebar_keeps_the_server(res, cfg):
            # A sidebar × close: keep the server and register this run's start --resume for the card's ↺.
            if mgr.close_panes() or all_gone(mgr, meta):
                entry = resume_entry(cfg, run, res)
                mgr.mark_closed(entry)
                mgr._kv_set('stop_request', '')  # §13.6-7: a stale 'accepted' would hide the × after the resume
                mark_sidebar_closed(mgr, run.name, subtree_card_ids(cfg) - {run.name})
                register_history(mgr, cfg, run, entry)  # the other resumable runs stay in the ↺ list (§18.2)
                outcome = 'closed (server kept for ↺)'
            else:
                outcome = 'close_failed: a pane could not be confirmed closed'
        elif mgr.close_all() or all_gone(mgr, meta):
            mgr.meta_path.unlink(missing_ok=True)
            outcome = 'closed'
            kept = getattr(mgr, 'kept_bash_pages', None)
            if kept and isinstance(kept, list):  # the server outlives the fleet (fleetmux, busy bash page)
                retire_cards(mgr, cfg, run, res)
        else:
            outcome = 'close_failed: a surface or workspace could not be confirmed closed'
        kept = getattr(mgr, 'kept_bash_pages', None)  # fleetmux: busy bash pages the close left open
        if kept and isinstance(kept, list):
            outcome += '; bash tabs kept, a program runs: ' + ', '.join(map(str, kept))
    try:
        res = fleet_mod.load(run / 'result.json')
        res.update(workspace=outcome, workspace_closed_at=time.time())
        fleet_mod.save(run / 'result.json', res)
    except (OSError, ValueError):
        pass
    print(time.strftime('%Y-%m-%d %H:%M:%S'), 'lifecycle-close', run, outcome, flush=True)
    return outcome
