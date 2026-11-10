"""UI-only cards for official-CLI slots (Codex/Claude TUIs), outside the model context.

Pi shows `<prefix>:ui:<slot>` cards through its bridge as non-model session entries.
An official interactive TUI has no such entry, and typing a card into it would make it
a model turn (docs/design.md §11 显示边界). So the daemon renders those slots' cards
off-model: one line per card in `<run>/cards/<slot>.log`, and the history page shows
that log under its "Cards" view (no new pane or tab, layout unchanged). Every rendered
card leaves a `ui.rendered` audit record for control.

The read cursor lives in Redis (`<prefix>:ui_cursor:<slot>`) and advances only after the
log line is written, so a daemon restart resumes without losing cards; a crash between
the two writes may repeat at most that batch's lines (at-least-once, never silently lost).
"""
import json
import time
from pathlib import Path

BATCH = 100


def log_path(run_dir, slot):
    return Path(run_dir) / 'cards' / (slot + '.log')


def _line(stream_id, card):
    at = time.strftime('%H:%M:%S', time.localtime(int(stream_id.split('-')[0]) / 1000))
    text = (card.get('render') or '').strip() or json.dumps(card.get('metadata') or {}, ensure_ascii=False)
    return at + '  ' + text.replace('\n', '\n          ') + '\n'


def render(v2, slots, run_dir):
    """Render pending cards of `slots`; returns how many were written."""
    r, written = v2.bus.r, 0
    for slot in slots:
        cursor_key = v2.prefix + ':ui_cursor:' + slot
        cursor = r.get(cursor_key) or '0-0'
        rows = r.xrange(v2.prefix + ':ui:' + slot, '(' + cursor, '+', count=BATCH)
        if not rows:
            continue
        path = log_path(run_dir, slot)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for stream_id, fields in rows:
            try:
                card = json.loads(fields.get('data') or '{}')
            except ValueError:
                card = {'render': '(unreadable card ' + stream_id + ')'}
            lines.append(_line(stream_id, card))
        with path.open('a') as f:
            f.write(''.join(lines))
        r.set(cursor_key, rows[-1][0])
        for stream_id, fields in rows:
            v2.audit('ui.rendered', slot=slot, detail={'event_id': fields.get('event_id', ''),
                                                        'stream_id': stream_id, 'log': str(path)})
        written += len(rows)
    return written
