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
from fleetmux import history as history_page

MAX_FLEETS = 16
THROTTLE_S = 1.0  # every kv.set makes the server redraw, so at most one push per second
HISTORY_RESCAN_S = 30.0  # a directory scan costs more than a kv compare, so rescan the ↺ history rarely (§18.4)
STATE_TEXT = {'running': '运行中', 'paused': '已暂停', 'stopping': '正在关闭'}
SIDEBAR_REASON = 'fleetmux 侧栏「关闭整个 fleet」'  # §12.1: what request_close records as the reason
REFUSED_REASON = '这个 run 没有 lifecycle，请在终端跑 start.sh stop'
PAGE_ID_RE = re.compile(r'[A-Za-z0-9._-]{1,64}')  # a fleetmux page id (M4 design §4.1)


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


def _child(key: str, item: dict) -> dict:
    """One sub-fleet card from a ``subfleet.tree()`` item: flat, no indent."""
    run = item.get('run')
    workers = item.get('workers')
    count = workers if isinstance(workers, int) else len(workers or [])
    card = {'id': Path(run).name if run else key, 'title': item.get('title') or key}
    if item.get('state') in subfleet.ACTIVE:
        card.update(state='running', line1='运行中 · %d worker' % count,
                    line2='由 %s 委派' % (item.get('slot') or '?'))
    else:
        card.update(state='reclaimed', line1='已收回 · 结局 %s' % (item.get('outcome') or '—'),
                    line2='只读，可看记录')
    return card


def _walk(tree: dict, out: list, runs: Optional[list] = None) -> None:
    """Pre-order over ``subfleet.tree()``: each sub-fleet, then its own sub-fleets (``runs``: each card's run)."""
    for key, item in (tree or {}).items():
        if len(out) >= MAX_FLEETS:
            return
        out.append(_child(key, item))
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


def _close(f, title: str, tree: dict) -> dict:
    """The close-confirm text: the root, plus every live sub-fleet it takes down with it (§12.1)."""
    body = '会停掉监工、%d 个 worker' % len(f.workers)
    live = []
    _live_fleets(tree, live)
    if live:
        body += '，以及它的子 fleet ' + '、'.join('%s（含 %d 个 worker）' % item for item in live)
    body += '。正在做的活会中断。运行记录保留，之后可以在侧栏点 ↺ 恢复，接着原来的活。'
    return {'title': '关闭整个 fleet「%s」？' % title, 'body': body}


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
        'line1': '%s · 监工+%d worker（%d忙）' % (STATE_TEXT[state], len(f.workers), busy),
        'line2': _home(cfg.get('fleet_home') or str(Path(cfg['run']).parent.parent)),
    }]
    tree = subfleet.tree(cfg)
    if _pages(f):
        runs = []
        _walk(tree, cards, runs)
        _add_pages(cards[0], runs)
    else:
        _walk(tree, cards)
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
        f.mgr._kv_set('stop_request', _kv_text({'v': 1, 'state': 'refused', 'reason': REFUSED_REASON}))
        return
    lifecycle_close.request_close(f, 'user', SIDEBAR_REASON, resumable=True, via='fleetmux-sidebar')
    f.mgr._kv_set('stop_request', _kv_text({'v': 1, 'state': 'accepted'}))


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
