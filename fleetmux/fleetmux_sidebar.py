"""fleetmux sidebar cards: the daemon pushes kv ``sidebar`` once a second (plan fleetmux-sidebar §4.5).

Only a fleetmux run has cards: another backend's manager has no ``kv_get``, so :func:`tick` returns
before it touches ``f.mgr``.

kv ``sidebar`` value (compact JSON, ``ensure_ascii=False``)::

    {"v":1,"current":"fleet-20261029-231820-ab12cd34",
     "fleets":[{"id":"fleet-20261029-231820-ab12cd34","title":"fleetmux-demo","state":"running",
                "line1":"运行中 · 监工+2 worker（1忙）","line2":"~/.fleetmux/fleets/fmx-demo"}]}

``current`` is this daemon's run; ``fleets`` is that run plus every sub-fleet and sub-sub-fleet in
pre-order (flat, not nested), at most 16 entries. The server parses the value and draws the sidebar
(``rust/crates/app/src/render/sidebar.rs``); a malformed value is ignored there with a WARN.

S2 reads kv ``stop_request`` in this same tick. Only the root daemon pushes (a sub-fleet's daemon returns at
once, M4); on a server with pages every tree card carries ``page`` (M4 design §6.2).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from fleetmux import fleetmux_history, subfleet
from fleetmux.i18n import t
from fleetmux import history as history_page

MAX_FLEETS = 16
THROTTLE_S = 1.0  # every kv.set makes the server redraw, so at most one push per second
HISTORY_RESCAN_S = 30.0  # a directory scan costs more than a kv compare, so rescan the ↺ history rarely (§18.4)
STATE_TEXT = {'running': ('sidebar.state.running', 'running'), 'paused': ('sidebar.state.paused', 'paused'),
              'stopping': ('sidebar.state.stopping', 'stopping')}  # key, English (ui.language, §63)
SIDEBAR_REASON = 'fleetmux 侧栏「关闭整个 fleet」'  # §12.1: what request_close records as the reason


def refused_reason() -> str:
    """kv ``stop_request.reason`` of a refused close (the terminal shows it on the status line)."""
    return t('sidebar.refused', 'This run has no lifecycle. Run start.sh stop in a terminal.')


PAGE_ID_RE = re.compile(r'[A-Za-z0-9._-]{1,64}')  # a fleetmux page id (M4 design §4.1)
CHILD_STOP = 'child_stop'  # root-only kv: a child card's confirmed close request (design section 19)
SIDEBAR_BY = 'fleetmux-sidebar'  # who cancelled / took down a sub-fleet from the sidebar


def _fleet():
    """Lazy ``fleet`` module (as ``subfleet._fleet``: fleet imports this module at import time)."""
    from fleetmux import fleet
    return fleet


def _home(path: str) -> str:
    """The operator's home directory as ``~``; everything else verbatim."""
    home = str(Path.home())
    if path == home:
        return '~'
    if path.startswith(home + '/'):
        return '~' + path[len(home):]
    return path


def _child(key: str, item: dict, close: Optional[dict] = None) -> dict:
    """One sub-fleet card from a ``subfleet.tree()`` item: flat, no indent. A sub-fleet whose stop was sent is drawn
    ``stopping`` (no close button); ``close`` (design section 19): the texts of its close box, only for a card the
    sidebar may close (:func:`stop_path`)."""
    run = item.get('run')
    workers = item.get('workers')
    count = workers if isinstance(workers, int) else len(workers or [])
    card = {'id': Path(run).name if run else key, 'title': item.get('title') or key}
    if item.get('state') in subfleet.ACTIVE:
        stopping = item.get('state') == 'stopping'
        line1 = (t('sidebar.child.stopping', 'stopping, workers {count}', count=count) if stopping else
                 t('sidebar.child.line1', 'running, workers {count}', count=count))
        card.update(state='stopping' if stopping else 'running', line1=line1,
                    line2=t('sidebar.child.line2', 'delegated by {slot}', slot=item.get('slot') or '?'))
    else:
        card.update(state='reclaimed', line1=t('sidebar.reclaimed.line1', 'reclaimed, outcome {outcome}',
                                               outcome=item.get('outcome') or t('sidebar.no_outcome', '-')),
                    line2=readonly_line2())
    if close:
        card['close'] = close
    return card


def readonly_line2() -> str:
    """Second line of a reclaimed (read-only) sub-fleet card; lifecycle_close uses it for a tree closed with its root."""
    return t('sidebar.readonly.line2', 'read-only, log kept')


def stop_path(f, run) -> tuple:
    """Design section 19: how the sidebar may stop the sub-fleet in ``run``: ``(kind, key, '')`` with kind ``job`` (key:
    the job id), ``named`` or ``attached`` (key: its name), else ``(None, None, reason)``. One rule for the card's button
    and for the request the server sends after the box was confirmed: a direct child of this fleet, still running, a
    job whose row is open and not being cancelled, or a named / attached sub-fleet not already going down. Split /
    escalate children and deeper levels have no stop path here."""
    d = Path(run) if run else None
    if d is None or not (d / 'subfleet.json').is_file():
        return None, None, 'no such sub-fleet of this fleet'
    if d.parent != Path(f.cfg['run']) / 'sub':
        return None, None, 'only a direct sub-fleet of this fleet can be closed here'
    rec = subfleet.record(d)
    if rec.get('state') != 'running':
        return None, None, 'it is not running (state %s)' % (rec.get('state') or '?')
    jid = rec.get('job')
    if jid:
        rows = getattr(f, 'job_state', None)
        job = (rows() if rows else {}).get(jid)
        if job is None:
            return None, None, 'job %s is not open on this fleet' % jid
        if job.get('cancel'):
            return None, None, 'job %s is already being cancelled' % jid
        return 'job', jid, ''
    if rec.get('named'):
        from fleetmux import named_subfleet
        name = rec.get('name') or rec.get('task_id') or d.name
        if named_subfleet.ops(f, name, 'down'):
            return None, None, '%s is already going down' % name
        return ('attached' if name in named_subfleet.attached(f) else 'named'), name, ''
    return None, None, 'only a job or a named sub-fleet can be closed from the sidebar'


def _close_texts(kind: str, key: str, item: dict) -> dict:
    """The close box of a child card: it names the sub-fleet and says which path confirming takes."""
    title = item.get('title') or key
    if kind == 'job':
        return {'title': t('sidebar.child_close.job_title', 'Close sub-fleet "{title}" (job {job})?', title=title,
                           job=key),
                'body': t('sidebar.child_close.job_body', 'Cancels this job: the sub-fleet stops and its result is '
                          'recorded as cancelled (published as failed, outcome cancelled). The run log and the '
                          'artifacts written so far are kept.')}
    how = 'subfleet detach' if kind == 'attached' else 'subfleet down'
    return {'title': t('sidebar.child_close.named_title', 'Close named sub-fleet "{name}"?', name=key),
            'body': t('sidebar.child_close.named_body', 'Takes it down with {how}: it drains, then stops. The run log '
                      'is kept.', how=how)}


def _running_first(tree: dict) -> list:
    """One level of ``subfleet.tree()`` (creation order) for the sidebar: the running sub-fleets in creation order,
    then the finished ones newest first, so the card cap never hides a running one. Pages are never dropped here."""
    items = list((tree or {}).items())
    running = [kv for kv in items if kv[1].get('state') in subfleet.ACTIVE]
    finished = [kv for kv in items if kv[1].get('state') not in subfleet.ACTIVE]
    return running + finished[::-1]


def _walk(tree: dict, out: list, runs: Optional[list] = None, f=None) -> None:
    """Pre-order over ``subfleet.tree()``: each sub-fleet, then its own sub-fleets (``runs``: each card's run); at every
    level the running ones first (:func:`_running_first`). ``f`` (the root's own level only): a card the sidebar may
    close carries its ``close`` texts (design section 19); deeper levels never do."""
    for key, item in _running_first(tree):
        if len(out) >= MAX_FLEETS:
            return
        close = None
        if f is not None and item.get('state') == 'running':
            try:  # the card push never fails over its close button: on any error the card simply has none
                kind, name, _ = stop_path(f, item.get('run'))
            except Exception:
                kind = name = None
            close = _close_texts(kind, name, item) if kind else None
        out.append(_child(key, item, close))
        if runs is not None:
            runs.append((out[-1], item.get('run')))
        _walk(item.get('subfleets') or {}, out, runs)


def _pages(f) -> bool:
    """The server has pages (M4): only then do the cards carry ``page`` (an M3 server would show nothing)."""
    probe = getattr(f.mgr, 'server_has_pages', None)
    return callable(probe) and probe() is True


def _add_pages(root: dict, runs: list) -> None:
    """M4 design §6.2: the root card's page is ``''``; a sub-fleet card's page is its run record's stable
    ``task_id`` (never the ``tree()`` display key). Cards sharing a task id share one page: only the newest
    attempt's card carries it. No readable record / no valid task id -> no ``page`` (the old click)."""
    root['page'] = ''
    newest = {}
    for card, run in runs:
        rec = subfleet.record(Path(run)) if run else {}
        tid = rec.get('task_id')
        if not isinstance(tid, str) or not PAGE_ID_RE.fullmatch(tid):
            continue
        try:
            key = (int(rec.get('attempt') or 0), float(rec.get('created') or 0))
        except (TypeError, ValueError):
            key = (0, 0.0)
        if tid not in newest or key > newest[tid][0]:
            newest[tid] = (key, card)
    for tid, (_, card) in newest.items():
        card['page'] = tid


def _live_fleets(tree: dict, out: list) -> None:
    """Pre-order over ``subfleet.tree()``: the live sub-fleets only, as ``(name, worker count)``."""
    for key, item in (tree or {}).items():
        if item.get('state') in subfleet.ACTIVE:
            workers = item.get('workers')
            out.append((key, workers if isinstance(workers, int) else len(workers or [])))
        _live_fleets(item.get('subfleets') or {}, out)


CLOSE_MAX = 400  # Rust's sidebar parse refuses a longer `close` string and then draws no sidebar


def _close(f, title: str, tree: dict) -> dict:
    """The close-confirm text: the root, plus every live sub-fleet it takes down with it (§12.1)."""
    body = t('close.body.head', 'Stops the supervisor and its workers ({workers})', workers=len(f.workers))
    live = []
    _live_fleets(tree, live)
    tail = t('close.body.tail', '. Running work is interrupted. The run log is kept; restore it later with the '
                                'restore button in the sidebar.')
    if live:
        items = t('close.body.sep', ', ').join(
            t('close.body.child', '{title} (workers: {workers})', title=name, workers=n) for name, n in live)
        children = t('close.body.children', ', and its sub-fleets {list}', list=items)
        if len(body) + len(children) + len(tail) > CLOSE_MAX:
            children = t('close.body.children_n', ', and its {n} sub-fleets', n=len(live))
        body += children
    body = (body + tail)[:CLOSE_MAX]
    return {'title': t('close.title', 'Close the whole fleet "{title}"?', title=title), 'body': body}


def _history(f) -> list:
    """The runs the ↺ list can bring back, scanned from ``f.cfg`` only (§18.7-3: never the environment).

    ``home`` is the same value ``line2`` shows, ``state_dir`` the one ``start`` pinned into the config; the run
    whose daemon is pushing never lists itself (its live card is already the first one).
    """
    cfg = f.cfg
    home = cfg.get('fleet_home') or str(Path(cfg['run']).parent.parent)
    here = Path(cfg['run']).name
    return [r for r in fleetmux_history.restorable_runs(home, cfg.get('cmux_state_dir')) if r['id'] != here]


def payload(f, history: Optional[list] = None) -> dict:
    """The kv ``sidebar`` value: this run's card, its sub-fleet tree, then the resumable history (≤ 16)."""
    cfg = f.cfg
    root_id = Path(cfg['run']).name
    state = 'stopping' if f.stopping else 'paused' if f.paused() else 'running'
    busy = sum(1 for name in f.workers if (f.agent(name) or {}).get('state') == 'busy')
    goal = '' if f.service else (f.task().get('goal') or '')
    title = subfleet.root_name(cfg, goal)
    cards = [{
        'id': root_id,
        'title': title,
        'state': state,
        'line1': t('sidebar.line1', '{state}, workers {workers}, busy {busy}', state=t(*STATE_TEXT[state]),
                   workers=len(f.workers), busy=busy),
        'line2': _home(cfg.get('fleet_home') or str(Path(cfg['run']).parent.parent)),
    }]
    tree = subfleet.tree(cfg)
    if _pages(f):
        runs = []
        _walk(tree, cards, runs, f=f)
        _add_pages(cards[0], runs)
    else:
        _walk(tree, cards, f=f)
    cards.extend(fleetmux_history.cards(_history(f) if history is None else history))
    return {'v': 1, 'current': root_id, 'fleets': cards[:MAX_FLEETS], 'close': _close(f, title, tree)}


def _kv_text(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _stop_request(f) -> None:
    """Handle kv ``stop_request`` once: the sidebar's 关闭整个 fleet asks this daemon to close itself.

    Read on every tick, before the card throttle (§12.1: a close must not wait for the next second).
    A run without a lifecycle is refused here instead of silently doing nothing.
    """
    try:
        req = json.loads(f.mgr.kv_get('stop_request') or 'null')
    except ValueError:
        return
    if not isinstance(req, dict) or req.get('state') != 'requested':
        return
    if f.stopping or getattr(f, 'lifecycle_close', None):
        return  # already closing: the second tick must not ask again
    from fleetmux import lifecycle_close
    if not lifecycle_close.managed(f):
        f.mgr._kv_set('stop_request', _kv_text({'v': 1, 'state': 'refused', 'reason': refused_reason()}))
        return
    lifecycle_close.request_close(f, 'user', SIDEBAR_REASON, resumable=True, via='fleetmux-sidebar')
    f.mgr._kv_set('stop_request', _kv_text({'v': 1, 'state': 'accepted'}))


def close_child(f, card_id: str) -> tuple:
    """Design section 19: stop the sub-fleet of child card ``card_id`` (its run directory's name) once the user confirmed
    its box. The card is checked again by :func:`stop_path`, never trusted by its id alone. ``('accepted', note)`` or
    ``('refused', reason)``: a job through ``Fleet.job_cancel`` (its result is published failed with outcome
    ``cancelled`` when the sub-fleet is collected), a named sub-fleet through an approved ``subfleet.down`` (the
    confirmation is the approval; the gate still refuses an autostart one), an attached one through ``detach``."""
    try:
        d = next((d for d in subfleet.children_dirs(f.cfg['run']) if d.name == card_id), None) if card_id else None
        kind, key, why = stop_path(f, d)
        if kind == 'job':
            f.job_cancel(key, by=SIDEBAR_BY)
            return 'accepted', 'job %s 已取消，子 fleet 正在停' % key
        if kind in ('named', 'attached'):
            from fleetmux import named_subfleet
            why = named_subfleet.sidebar_down(f, key, attached=kind == 'attached')
            if not why:
                return 'accepted', '%s 正在收回（%s）' % (key, 'subfleet detach' if kind == 'attached' else 'subfleet down')
    except Exception as exc:  # never kills the daemon tick; the user sees why
        why = 'the stop failed: %s' % str(exc)[:200]
    f.log('sidebar.child_stop_refused', card=card_id, reason=why)
    return 'refused', why


def _child_stop_request(f) -> None:
    """Handle kv ``child_stop`` once per request (``id`` + ``at``): only a ``requested`` value, which the server writes
    after ``y`` in a child card's box, is acted on, and it is answered ``accepted`` / ``refused`` with a reason."""
    try:
        req = json.loads(f.mgr.kv_get(CHILD_STOP) or 'null')
    except ValueError:
        return
    if not isinstance(req, dict) or req.get('state') != 'requested':
        return
    key = (str(req.get('id') or ''), req.get('at'))
    if key == getattr(f, '_fmx_child_stop', None):
        return  # already handled: a repeated tick or a value written back never acts twice
    f._fmx_child_stop = key
    state, reason = close_child(f, key[0])
    f.mgr._kv_set(CHILD_STOP, _kv_text({'v': 1, 'state': state, 'id': key[0], 'reason': reason}))


def _clear_restorable(f) -> None:
    """A live daemon's cards replace the closed ones: drop the closer's kv ``restorable`` (§13.5 / §13.6)."""
    if f.mgr.kv_get('restorable'):
        f.mgr._kv_set('restorable', '')


def tick(f, now: Optional[float] = None) -> None:
    """Push kv ``sidebar`` when the cards changed: at most once a second, fleetmux runs only.

    ``now`` (monotonic seconds) is for tests; the daemon passes nothing. The throttle time lives on the
    Fleet (``_fmx_sidebar_at``), so a restarted daemon pushes again on its first tick and the server,
    whose kv is empty after a restart, gets the cards back within a second.
    """
    if f.cfg.get('parent_config'):
        return  # a sub-fleet's daemon (M4): kv sidebar / stop_request / restorable are the root's alone
    if _fleet().run_backend(f.cfg) != 'fleetmux':
        return  # another backend's manager has no kv_get: never touch f.mgr
    _stop_request(f)  # before the throttle: a close request is handled on the tick that reads it
    _child_stop_request(f)  # design section 19: a child card's confirmed close, same rule
    _clear_restorable(f)  # the daemon is back: the ↺ list belongs to a server without a fleet
    now = time.monotonic() if now is None else now
    last = getattr(f, '_fmx_sidebar_at', None)
    if last is not None and now - last < THROTTLE_S:
        return
    f._fmx_sidebar_at = now
    # User 2026-11-09: every sidebar bash is mapped into the main fleet's worker area and managed by it -- take new
    # ones into the registry (and the workers group), forget gone ones, and relist them on the history page whenever
    # the registered set differs from the one this daemon last listed (start_all writes the page before ``arrange``
    # takes a bash opened while no fleet ran in, so the first tick always lists them once).
    adopt = getattr(f.mgr, 'adopt_shells', None)
    if adopt is not None:
        adopt()
        shells = f.mgr.shell_slots()
        if shells != getattr(f, '_fmx_shells', None):
            f._fmx_shells = shells
            history_page.write_page(f.run_dir, f.history_slots(), f.history_links())
    scanned = getattr(f, '_fmx_history_at', None)
    history = getattr(f, '_fmx_history', None)
    if history is None or scanned is None or now - scanned >= HISTORY_RESCAN_S:  # §18.4-4: one scan per 30 s
        history = _history(f)
        f._fmx_history_at, f._fmx_history = now, history
    text = _kv_text(payload(f, history=history))
    if f.mgr.kv_get('sidebar') != text:  # compare first: every kv.set redraws the server
        f.mgr._kv_set('sidebar', text)
