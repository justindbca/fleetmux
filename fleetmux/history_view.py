"""Retired generations of every agent, in a terminal tab (fleetmux design §3.3).

fleetmux has no browser pane, so the lower-left ``history`` tab runs ``python -m fleetmux.history_view
<run>`` instead of showing ``<run>/history.html``. It reads the same ``<run>/history.jsonl`` that
``history.record`` appends to: left column picks an agent (slot), middle column a generation, right side
shows that generation's screen snapshot or handoff note; ``/`` searches every generation of every agent,
Enter jumps to the next hit; ``o`` opens ``history.html`` with the system opener (or shows its path).
The view polls the mtime of ``history.jsonl`` once a second and reloads when it moved, so the backend's
``reload_browser`` has nothing to do. ``<run>/.history_view.json`` remembers where the reader was (the
page keeps that in its URL hash).

``HistoryModel`` is the pure, file-only half; ``View`` draws onto any ``Screen`` (``size / put / getch /
refresh``), so tests drive it with a fake screen; the curses ``Screen`` lives in ``main()``.

In curses the view reads keys with a ``POLL_S`` input timeout: ``getch`` returns -1 once a second when
nothing was typed, and only then does ``run`` look at the mtime (no busy loop). Mouse clicks arrive as
``KEY_MOUSE`` and are read with ``curses.getmouse()``.
"""
from __future__ import annotations

import curses
import json
import os
import select
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fleetmux.history import SNAPSHOT_LIMIT

TAIL = SNAPSHOT_LIMIT  # bytes read from the end of a snapshot / note (scrollback can be huge)
POLL_S = 1.0  # how often the view checks history.jsonl's mtime
VIEWS = ('screen', 'handoff')


@dataclass(frozen=True)
class Entry:
    """One retired generation: a line of ``history.jsonl``. ``handoff`` / ``snapshot`` may be ''."""
    slot: str
    label: str
    generation: int = 0
    handoff: str = ''
    snapshot: str = ''
    source: str = ''
    timed_out: bool = False
    at: float = 0.0


Hit = tuple[Entry, int, str]  # a search hit: (entry, 1-based line number, line); exactly three items


FIELDS = ('slot', 'label', 'generation', 'handoff', 'snapshot', 'source', 'timed_out', 'at')
ENTER_KEYS = (10, 13, curses.KEY_ENTER)
BACKSPACE_KEYS = (8, 127, curses.KEY_BACKSPACE)
ESC = 27
CONTROL = {c: ' ' for c in range(32) if c != 10} | {127: ' '}  # curses refuses NUL; tabs / CR would misplace text


def _tail(path, limit) -> str:
    """The last ``limit`` characters of a text file, reading at most ``4 * limit`` bytes from its end."""
    try:
        with open(path, 'rb') as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - 4 * limit))
            text = f.read().decode('utf-8', errors='replace')
    except (OSError, TypeError, ValueError):
        return ''
    return text[-limit:] if len(text) > limit else text


class HistoryModel:
    """Everything the view needs from ``<run>``; reads files only, never writes outside ``state``."""

    run_dir: Path  # Path(run_dir)
    jsonl: Path  # <run>/history.jsonl
    handoffs: Path  # <run>/handoffs
    html: Path  # <run>/history.html
    state_path: Path  # <run>/.history_view.json
    tail: int  # bytes kept from the end of a snapshot / note; default TAIL

    def __init__(self, run_dir, tail=TAIL):
        """Derive the paths above from ``run_dir``; touches no file."""
        self.run_dir = Path(run_dir)
        self.jsonl = self.run_dir / 'history.jsonl'
        self.handoffs = self.run_dir / 'handoffs'
        self.html = self.run_dir / 'history.html'
        self.state_path = self.run_dir / '.history_view.json'
        self.tail = tail
        self._mtime = None  # history.jsonl's mtime (ns) at the last entries(); None = no file

    def _mtime_now(self):
        try:
            return os.stat(self.jsonl).st_mtime_ns
        except OSError:
            return None

    def entries(self) -> list[Entry]:
        """Every well-formed line of ``history.jsonl`` in file order; a line that is not a JSON object with a
        ``slot`` is skipped; a missing file is ``[]``. Remembers the mtime it read (see ``changed``)."""
        self._mtime = self._mtime_now()  # before reading: a write racing the read shows up as changed()
        try:
            lines = self.jsonl.read_text(errors='replace').splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not row.get('slot'):
                continue
            try:
                out.append(Entry(**{k: row[k] for k in FIELDS if row.get(k) is not None}))
            except TypeError:
                continue
        return out

    def agents(self) -> dict[str, list[Entry]]:
        """``{slot: [Entry, ...]}``: slots in order of first appearance, each list sorted by generation."""
        out: dict[str, list[Entry]] = {}
        for e in self.entries():
            out.setdefault(e.slot, []).append(e)
        for gens in out.values():
            gens.sort(key=lambda e: e.generation if isinstance(e.generation, int) else 0)
        return out

    def _file(self, entry: Entry, recorded: str, suffix: str) -> Path | None:
        if recorded:
            return Path(recorded)
        found = sorted(self.handoffs.glob(entry.slot + '-g' + str(entry.generation) + '-*' + suffix))
        return found[0] if found else None

    def screen(self, entry: Entry) -> str:
        """The last ``tail`` bytes of the generation's screen: ``entry.snapshot``, or when that is '' the first
        ``<run>/handoffs/<slot>-g<N>-*.terminal.txt``; '' when there is none."""
        path = self._file(entry, entry.snapshot, '.terminal.txt')
        return _tail(path, self.tail) if path else ''

    def handoff(self, entry: Entry) -> str:
        """The last ``tail`` bytes of the handoff note: ``entry.handoff``, or when that is '' the first
        ``<run>/handoffs/<slot>-g<N>-*.md``; '' when there is none."""
        path = self._file(entry, entry.handoff, '.md')
        return _tail(path, self.tail) if path else ''

    def search(self, q: str) -> list[Hit]:
        """Case-insensitive substring search over the screen and the note of every generation of every agent,
        in ``agents()`` order (screen lines before note lines within one generation). Each hit is a plain
        ``(entry, line number, line)``; which of the two texts it came from is the view's own business."""
        needle = q.lower()
        if not needle:
            return []
        hits: list[Hit] = []
        for gens in self.agents().values():
            for entry in gens:
                for text in (self.screen(entry), self.handoff(entry)):
                    hits.extend((entry, n, line) for n, line in enumerate(text.splitlines(), 1)
                                if needle in line.lower())
        return hits

    def changed(self) -> bool:
        """True when ``history.jsonl``'s mtime differs from the one the last ``entries()`` read."""
        return self._mtime_now() != self._mtime

    @property
    def state(self) -> dict:
        """``<run>/.history_view.json`` (agent, generation, view, query, scroll); ``{}`` if missing or bad."""
        try:
            value = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @state.setter
    def state(self, value: dict) -> None:
        """Write ``value`` to ``<run>/.history_view.json`` atomically."""
        tmp = self.state_path.with_name(self.state_path.name + '.tmp')
        tmp.write_text(json.dumps(value))
        os.replace(tmp, self.state_path)

    def open_html(self) -> str | None:
        """Open ``history.html`` with ``open`` (macOS) / ``xdg-open`` (others) on this host. None when the
        opener ran and exited 0; otherwise (non-zero exit, opener not installed: ``subprocess.run`` raising
        ``OSError``) the page's path as a string, for the view to show instead."""
        page = str(self.html)
        opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
        try:
            done = subprocess.run([opener, page], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)  # its chatter would scribble over the curses screen
        except OSError:
            return page
        return None if done.returncode == 0 else page


class Screen(Protocol):
    """The four calls ``View`` makes; ``main()`` adapts a curses window to it."""

    def size(self) -> tuple[int, int]:
        """``(rows, cols)``."""

    def put(self, row: int, col: int, text: str, attr: int = 0) -> None:
        """Write ``text`` at ``(row, col)``, clipped to the screen."""

    def getch(self) -> int:
        """Next key (curses key code); -1 when none arrived within the poll interval."""

    def refresh(self) -> None:
        """Show what was put since the last refresh."""


class View:
    """Three columns (agents | generations | screen or note) plus a status / search line at the bottom.

    Keys: up/down or j/k move in the focused column, left/right or h/l change column, Tab / ``v`` toggles
    screen and note, ``/`` starts a search (typed into the bottom line, Enter runs it and jumps to the first
    hit), Enter jumps to the next hit, ``o`` opens history.html, ``q`` quits (saving ``model.state``)."""

    screen: Screen
    model: HistoryModel
    agent: str | None  # selected slot; starts None = the first one
    generation: int | None  # selected generation of that slot; starts None = its latest
    view: str  # 'screen' or 'handoff'; starts 'screen'
    scroll: int  # first line of the right-hand text shown; starts 0
    query: str  # last search; starts ''
    hits: list[Hit]  # results of the last search; starts []
    hit: int  # index into hits of the one shown last; starts -1
    message: str  # bottom line: errors, the history.html path when it could not be opened; starts ''

    def __init__(self, screen: Screen, model: HistoryModel):
        """Keep ``screen`` and ``model``, set the starting values above; draws nothing."""
        self.screen = screen
        self.model = model
        self.agent = None
        self.generation = None
        self.view = 'screen'
        self.scroll = 0
        self.query = ''
        self.hits = []
        self.hit = -1
        self.message = ''
        self.focus = 0  # 0 agents, 1 generations, 2 text
        self.typing: str | None = None  # the search being typed into the bottom line; None = not typing
        self._hit_views: list[str] = []  # 'screen' / 'handoff' per hit
        self._rows: dict[int, str] = {}  # screen row -> slot of the agents column in the last draw
        self._cols = (0, 0, 0)  # x of the agents, generations and text columns in the last draw
        self._texts: dict[tuple[Entry, str], list[str]] = {}
        self._agents = model.agents()

    # ---------------------------------------------------------------- selection
    def _slots(self) -> list[str]:
        return list(self._agents)

    def _gens(self) -> list[Entry]:
        slot = self.agent if self.agent is not None else next(iter(self._agents), None)
        return self._agents.get(slot, []) if slot is not None else []

    def current(self) -> Entry | None:
        """The selected generation's entry (latest generation of the first agent by default)."""
        gens = self._gens()
        if not gens:
            return None
        if self.generation is not None:
            for e in gens:
                if e.generation == self.generation:
                    return e
        return gens[-1]

    def _lines(self, entry: Entry | None = None, view: str | None = None) -> list[str]:
        entry = entry or self.current()
        if entry is None:
            return []
        key = (entry, view or self.view)
        if key not in self._texts:
            text = self.model.screen(entry) if key[1] == 'screen' else self.model.handoff(entry)
            self._texts[key] = text.translate(CONTROL).splitlines()
        return self._texts[key]

    def _select_agent(self, slot: str) -> None:
        if slot != (self.agent if self.agent is not None else next(iter(self._agents), None)):
            self.generation, self.scroll = None, 0
        self.agent = slot

    def _move(self, delta: int) -> None:
        if self.focus == 0:
            slots = self._slots()
            if slots:
                cur = self.current()
                i = slots.index(cur.slot) if cur else 0
                self._select_agent(slots[max(0, min(len(slots) - 1, i + delta))])
        elif self.focus == 1:
            gens, cur = self._gens(), self.current()
            if gens and cur is not None:
                i = gens.index(cur)
                self.generation = gens[max(0, min(len(gens) - 1, i + delta))].generation
                self.scroll = 0
        else:
            self.scroll = max(0, self.scroll + delta)

    def _reload(self) -> None:
        self._agents = self.model.agents()
        self._texts.clear()
        if self.agent is not None and self.agent not in self._agents:
            self.agent, self.generation, self.scroll = None, None, 0

    # ---------------------------------------------------------------- search
    def _search(self, query: str) -> None:
        self.query = query
        self.hits = self.model.search(query)
        self._reload()  # search re-read history.jsonl: keep the columns in step with what it found
        self._hit_views = []
        last: tuple[Entry, str, int] | None = None
        for entry, n, line in self.hits:
            # within one generation the model lists screen lines, then note lines, each in line order
            in_note = last is not None and last[0] == entry and (last[1] == 'handoff' or n <= last[2])
            if not in_note:
                lines = self._lines(entry, 'screen')
                in_note = not (n <= len(lines) and lines[n - 1] == line.translate(CONTROL))
            view = 'handoff' if in_note else 'screen'
            self._hit_views.append(view)
            last = (entry, view, n)
        self.hit = -1
        if self.hits:
            self._jump(0)
        else:
            self.message = 'no match for ' + query

    def _jump(self, i: int) -> None:
        self.hit = i % len(self.hits)
        entry, n, _ = self.hits[self.hit]
        self.agent, self.generation, self.view = entry.slot, entry.generation, self._hit_views[self.hit]
        self.scroll = max(0, n - 1 - max(1, self._body_rows() // 3))
        self.focus = 2
        self.message = 'hit %d/%d' % (self.hit + 1, len(self.hits))

    def _body_rows(self) -> int:
        return max(0, self.screen.size()[0] - 2)  # minus the header row and the bottom line

    # ---------------------------------------------------------------- drawing
    def draw(self) -> None:
        """Paint all three columns and the bottom line, then ``screen.refresh()``."""
        rows, cols = self.screen.size()
        put = self.screen.put
        for r in range(rows):
            put(r, 0, ' ' * cols)
        body = max(0, rows - 2)
        w_agent = max(10, min(24, cols // 6))
        w_gen = max(12, min(28, cols // 5))
        x_gen = w_agent + 1
        x_text = x_gen + w_gen + 1
        self._cols = (0, x_gen, x_text)
        self._rows = {}
        cur = self.current()
        reverse, bold = curses.A_REVERSE, curses.A_BOLD
        if rows >= 2:
            for x, title, col in ((0, 'agents', 0), (x_gen, 'generations', 1),
                                  (x_text, 'screen' if self.view == 'screen' else 'handoff note', 2)):
                if x < cols:
                    put(0, x, (' ' + title)[:max(0, cols - x)], bold if self.focus == col else 0)
            for x in (w_agent, x_gen + w_gen):
                if x < cols:
                    for r in range(body + 1):
                        put(r, x, '│')
            slots = self._slots()
            sel = slots.index(cur.slot) if cur else 0
            for i, slot in self._window(slots, sel, body):
                r = 1 + i
                self._rows[r] = slot
                put(r, 0, (' ' + slot)[:w_agent].ljust(w_agent), reverse if cur and slot == cur.slot else 0)
            gens = self._gens()
            sel = gens.index(cur) if cur in gens else 0
            if x_gen < cols:
                for i, e in self._window(gens, sel, body):
                    r = 1 + i
                    label = ' ' + e.label + (' !' if e.timed_out else '')
                    put(r, x_gen, label[:w_gen].ljust(w_gen), reverse if e == cur else 0)
            if x_text + 1 < cols:
                lines = self._lines()
                self.scroll = max(0, min(self.scroll, len(lines) - body))
                for i, line in enumerate(lines[self.scroll:self.scroll + body]):
                    put(1 + i, x_text + 1, line[:cols - x_text - 1])
        if rows >= 1:
            put(rows - 1, 0, self._status()[:cols], reverse if self.typing is not None else 0)
        self.screen.refresh()

    @staticmethod
    def _window(items: list, sel: int, height: int):
        """(row offset, item) for the slice of ``items`` that fits ``height`` rows and shows ``sel``."""
        top = max(0, min(sel - height + 1, len(items) - height)) if sel >= height else 0
        return [(i - top, items[i]) for i in range(top, min(len(items), top + height))]

    def _status(self) -> str:
        if self.typing is not None:
            return '/' + self.typing
        if self.message:
            return self.message
        cur = self.current()
        where = '%s g%s %s' % (cur.label, cur.generation, self.view) if cur else 'no retired generations yet'
        return where + '   jk move  hl column  v screen/note  / search  Enter next  o open  q quit'

    # ---------------------------------------------------------------- keys
    def handle(self, key: int) -> bool:
        """Apply one key; False means quit."""
        if key == -1:
            return True
        if self.typing is not None:
            if key in ENTER_KEYS:
                query, self.typing = self.typing, None
                self._search(query)
            elif key == ESC:
                self.typing = None
            elif key in BACKSPACE_KEYS:
                self.typing = self.typing[:-1]
            elif 32 <= key < 127:
                self.typing += chr(key)
            return True
        self.message = ''
        if key == ord('q'):
            cur = self.current()
            self.model.state = {'agent': cur.slot if cur else self.agent, 'generation': cur.generation if cur else None,
                                'view': self.view, 'query': self.query, 'scroll': self.scroll}
            return False
        if key in (curses.KEY_UP, ord('k')):
            self._move(-1)
        elif key in (curses.KEY_DOWN, ord('j')):
            self._move(1)
        elif key in (curses.KEY_LEFT, ord('h')):
            self.focus = max(0, self.focus - 1)
        elif key in (curses.KEY_RIGHT, ord('l')):
            self.focus = min(2, self.focus + 1)
        elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            step = max(1, self._body_rows() - 1)
            self.scroll = max(0, self.scroll + (step if key == curses.KEY_NPAGE else -step))
        elif key in (9, ord('v')):
            self.view = 'handoff' if self.view == 'screen' else 'screen'
            self.scroll = 0
        elif key == ord('/'):
            self.typing = ''
        elif key in ENTER_KEYS:
            if self.hits:
                self._jump(self.hit + 1)
            elif self.query:
                self._search(self.query)
        elif key == ord('o'):
            path = self.model.open_html()
            self.message = 'history.html opened' if path is None else 'cannot open, page is at ' + path
        elif key == curses.KEY_MOUSE:
            self._mouse()
        return True

    def _mouse(self) -> None:
        try:
            _, x, y, _, bstate = curses.getmouse()
        except curses.error:
            return
        wheel_up, wheel_down = getattr(curses, 'BUTTON4_PRESSED', 0), getattr(curses, 'BUTTON5_PRESSED', 0)
        if bstate & wheel_up or bstate & wheel_down:
            self.scroll = max(0, self.scroll + (-3 if bstate & wheel_up else 3))
            return
        _, x_gen, x_text = self._cols
        col = 0 if x < x_gen else 1 if x < x_text else 2
        self.focus = col
        if col == 0 and y in self._rows:
            self._select_agent(self._rows[y])
        elif col == 1:
            gen = self._gen_at(y)
            if gen is not None:
                self.generation, self.scroll = gen, 0

    def _gen_at(self, y: int):
        gens = self._gens()
        cur = self.current()
        sel = gens.index(cur) if cur in gens else 0
        for i, e in self._window(gens, sel, self._body_rows()):
            if 1 + i == y:
                return e.generation
        return None

    def run(self) -> None:
        """Draw, read keys, reload when ``model.changed()``; returns on ``q``."""
        self._restore(self.model.state)
        self.draw()
        while True:
            key = self.screen.getch()  # curses: -1 after POLL_S without a key; that is the only time we stat
            if key == -1:
                if self.model.changed():
                    self._reload()
                    self.draw()
                continue
            if not self.handle(key):
                return
            self.draw()

    def _restore(self, state: dict) -> None:
        """Pick up where the reader left off (``<run>/.history_view.json``), ignoring what no longer exists."""
        if state.get('agent') in self._agents:
            self.agent = state['agent']
            if any(e.generation == state.get('generation') for e in self._agents[self.agent]):
                self.generation = state['generation']
        if state.get('view') in VIEWS:
            self.view = state['view']
        if isinstance(state.get('scroll'), int):
            self.scroll = max(0, state['scroll'])
        if isinstance(state.get('query'), str):
            self.query = state['query']


def _terminal_disconnected(fd: int) -> bool:
    """Check hangup without consuming keys (macOS kqueue / Linux poll)."""
    try:
        if hasattr(select, 'poll'):
            poller = select.poll()
            poller.register(fd, select.POLLHUP | select.POLLERR | select.POLLNVAL)
            return any(events & (select.POLLHUP | select.POLLERR | select.POLLNVAL)
                       for _, events in poller.poll(0))
        queue = select.kqueue()
        try:
            events = queue.control([select.kevent(fd, filter=select.KQ_FILTER_READ,
                                                  flags=select.KQ_EV_ADD)], 1, 0)
            return any(event.flags & (select.KQ_EV_EOF | select.KQ_EV_ERROR) for event in events)
        finally:
            queue.close()
    except (OSError, ValueError):
        return True


def main(argv) -> int:
    """``python -m fleetmux.history_view <run dir>``: 2 with a usage line on stderr when the run dir is
    missing; otherwise runs ``View(<curses Screen>, HistoryModel(run_dir)).run()`` under ``curses.wrapper``
    and returns 0. The curses ``Screen`` adapter is defined in here."""
    args = list(argv[1:])
    if len(args) != 1 or not Path(args[0]).is_dir():
        print('usage: python -m fleetmux.history_view <run dir>', file=sys.stderr)
        return 2
    model = HistoryModel(args[0])

    class CursesScreen:
        def __init__(self, win):
            self.win = win

        def size(self):
            return self.win.getmaxyx()

        def put(self, row, col, text, attr=0):
            rows, cols = self.win.getmaxyx()
            if 0 <= row < rows and 0 <= col < cols:
                try:
                    self.win.addnstr(row, col, text, cols - col, attr)
                except curses.error:  # the bottom-right cell: curses writes it, then complains
                    pass

        def getch(self):
            key = self.win.getch()
            if key == -1 and _terminal_disconnected(sys.stdin.fileno()):
                raise EOFError('history terminal disconnected')
            return key

        def refresh(self):
            self.win.refresh()

    def body(stdscr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        stdscr.keypad(True)
        stdscr.timeout(int(POLL_S * 1000))  # getch blocks up to POLL_S, then returns -1: run() checks the mtime
        View(CursesScreen(stdscr), model).run()

    try:
        curses.wrapper(body)
    except EOFError:
        pass
    except curses.error:
        # endwin() can fail while wrapper restores an already disconnected tty.
        if not _terminal_disconnected(sys.stdin.fileno()):
            raise
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
