"""history_view (fleetmux design §3.3, plan §3.6): the lower-left history tab.

Phase 0: the module is a skeleton, so every test here is red with NotImplementedError; M2's
``py-resume-hist`` makes them green without changing an assertion.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fleetmux import history_view  # noqa: E402
from fleetmux.history_view import Entry, HistoryModel, View  # noqa: E402

ENTER = 10


def write_history(run, *rows):
    """Append ``rows`` (dicts, or raw strings for bad lines) to ``<run>/history.jsonl``."""
    with (Path(run) / 'history.jsonl').open('a') as f:
        for row in rows:
            f.write((row if isinstance(row, str) else json.dumps(row)) + '\n')


def retire(run, slot, label, generation, screen=None, note=None, hid='h1'):
    """One retired generation the way fleet.py writes it: files under handoffs/, a line in history.jsonl."""
    out = Path(run) / 'handoffs'
    out.mkdir(exist_ok=True)
    base = out / (slot + '-g' + str(generation) + '-' + hid)
    snapshot = handoff = ''
    if screen is not None:
        snapshot = str(base.with_suffix('.terminal.txt'))
        Path(snapshot).write_text(screen)
    if note is not None:
        handoff = str(base.with_suffix('.md'))
        Path(handoff).write_text(note)
    write_history(run, {'slot': slot, 'label': label, 'generation': generation, 'handoff': handoff,
                        'snapshot': snapshot, 'source': 'handoff', 'timed_out': False, 'at': 1000.0 + generation})


class FakeScreen:
    """The ``Screen`` protocol over a character grid; out-of-range rows / cols fail like curses would."""

    def __init__(self, rows=24, cols=120):
        self.rows, self.cols = rows, cols
        self.grid = [[' '] * cols for _ in range(rows)]
        self.refreshed = 0

    def size(self):
        return self.rows, self.cols

    def put(self, row, col, text, attr=0):
        assert 0 <= row < self.rows and 0 <= col < self.cols, (row, col, text)
        for i, ch in enumerate(text[:self.cols - col]):
            self.grid[row][col + i] = ch

    def getch(self):
        return ord('q')

    def refresh(self):
        self.refreshed += 1

    def lines(self):
        return [''.join(r) for r in self.grid]

    def cols_of(self, word):
        """Columns where ``word`` stands alone (not part of a longer label such as worker-1.1), above the
        bottom status / search line."""
        pat = re.compile(r'(?<![\w.-])' + re.escape(word) + r'(?![\w.-])')
        return [m.start() for line in self.lines()[:-1] for m in pat.finditer(line)]


def test_entries_parse_and_skip_bad_lines(tmp_path):
    write_history(tmp_path,
                  {'slot': 'worker-1', 'label': 'worker-1.1', 'generation': 1, 'handoff': '/h/worker-1-g1-a.md',
                   'snapshot': '/h/worker-1-g1-a.terminal.txt', 'source': 'midturn', 'timed_out': True, 'at': 12.5},
                  'not json at all',
                  '[1, 2]',
                  '',
                  {'label': 'no-slot', 'generation': 3},
                  {'slot': 'supervisor', 'label': 'supervisor-1', 'generation': 0, 'handoff': '', 'snapshot': '',
                   'source': 'retired', 'timed_out': False, 'at': 13.0},
                  {'slot': 'advisor', 'label': 'advisor-1'})
    got = HistoryModel(tmp_path).entries()
    assert got == [
        Entry('worker-1', 'worker-1.1', 1, '/h/worker-1-g1-a.md', '/h/worker-1-g1-a.terminal.txt', 'midturn', True, 12.5),
        Entry('supervisor', 'supervisor-1', 0, '', '', 'retired', False, 13.0),
        Entry('advisor', 'advisor-1'),
    ]
    assert HistoryModel(tmp_path / 'no-such-run').entries() == []


def test_agents_grouped_by_slot_generations_sorted(tmp_path):
    for slot, label, gen in [('worker-2', 'worker-2.2', 2), ('supervisor', 'supervisor-1', 0),
                             ('worker-2', 'worker-2', 0), ('supervisor', 'supervisor-1.1', 1),
                             ('worker-2', 'worker-2.1', 1)]:
        write_history(tmp_path, {'slot': slot, 'label': label, 'generation': gen})
    got = HistoryModel(tmp_path).agents()
    assert list(got) == ['worker-2', 'supervisor']
    assert [e.label for e in got['worker-2']] == ['worker-2', 'worker-2.1', 'worker-2.2']
    assert [e.generation for e in got['supervisor']] == [0, 1]


def test_screen_and_handoff_read_tail_only(tmp_path):
    screen = ''.join('screen line %03d\n' % i for i in range(100))
    note = 'HEAD of the note\n' + 'x' * 500 + '\nEND of the note\n'
    retire(tmp_path, 'worker-1', 'worker-1', 0, screen=screen, note=note)
    model = HistoryModel(tmp_path, tail=40)
    entry = model.entries()[0]
    assert model.screen(entry) == screen[-40:]
    assert model.handoff(entry) == note[-40:]
    assert 'HEAD' not in model.handoff(entry)
    # '' in history.jsonl (resume / retired rows): found under handoffs/ by slot and generation
    bare = Entry('worker-1', 'worker-1', 0)
    assert model.screen(bare) == screen[-40:]
    assert model.handoff(bare) == note[-40:]
    # nothing on disk: empty, no exception
    assert model.screen(Entry('worker-9', 'worker-9', 4)) == ''
    assert model.handoff(Entry('worker-9', 'worker-9', 4, handoff=str(tmp_path / 'gone.md'))) == ''
    assert HistoryModel(tmp_path).screen(entry) == screen  # default tail keeps a small file whole


def test_search_hits_across_all_generations(tmp_path):
    retire(tmp_path, 'supervisor', 'supervisor-1', 0, screen='nothing here\n', note='nor here\n')
    retire(tmp_path, 'worker-1', 'worker-1', 0, screen='one\nthe needle line\nthree\n', note='plain\n')
    retire(tmp_path, 'worker-1', 'worker-1.1', 1, screen='quiet\n', note='a\nb\nNEEDLE in the note\n')
    hits = HistoryModel(tmp_path).search('needle')
    assert all(isinstance(h, tuple) and len(h) == 3 for h in hits)  # plan §3.6: (entry, line number, line)
    assert [(entry.label, line_no, line) for entry, line_no, line in hits] == [
        ('worker-1', 2, 'the needle line'),  # generation 0's screen
        ('worker-1.1', 3, 'NEEDLE in the note'),  # generation 1's handoff note
    ]
    assert HistoryModel(tmp_path).search('absent') == []


def test_changed_follows_mtime(tmp_path):
    model = HistoryModel(tmp_path)
    assert model.entries() == []
    assert model.changed() is False  # no file yet, still none
    retire(tmp_path, 'worker-1', 'worker-1', 0, screen='s\n')
    assert model.changed() is True
    assert len(model.entries()) == 1
    assert model.changed() is False
    retire(tmp_path, 'worker-1', 'worker-1.1', 1, screen='s\n')
    st = os.stat(model.jsonl)
    os.utime(model.jsonl, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))  # same-tick writes on APFS
    assert model.changed() is True
    assert len(model.entries()) == 2
    assert model.changed() is False


def test_state_roundtrip(tmp_path):
    model = HistoryModel(tmp_path)
    assert model.state == {}
    want = {'agent': 'worker-1', 'generation': 2, 'view': 'handoff', 'query': 'needle', 'scroll': 40}
    model.state = want
    assert json.loads((tmp_path / '.history_view.json').read_text()) == want
    assert HistoryModel(tmp_path).state == want
    (tmp_path / '.history_view.json').write_text('{broken')
    assert HistoryModel(tmp_path).state == {}


def test_open_html_falls_back_to_path(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, rc)

    monkeypatch.setattr(history_view.subprocess, 'run', fake_run)  # never a real browser
    model = HistoryModel(tmp_path)
    page = str(tmp_path / 'history.html')
    rc = 0
    monkeypatch.setattr(history_view.sys, 'platform', 'darwin')
    assert model.open_html() is None
    monkeypatch.setattr(history_view.sys, 'platform', 'linux')
    assert model.open_html() is None
    assert [c[0] for c in calls] == ['open', 'xdg-open']
    assert all(c[-1] == page for c in calls)
    rc = 3
    assert model.open_html() == page

    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(history_view.subprocess, 'run', missing)
    assert model.open_html() == page


def test_view_renders_three_columns_on_fake_screen(tmp_path):
    retire(tmp_path, 'supervisor', 'supervisor-1', 0, screen='OLD-SUPERVISOR-SCREEN\n')
    retire(tmp_path, 'supervisor', 'supervisor-1.1', 1, screen='LATEST-SUPERVISOR-SCREEN\nsecond row\n')
    retire(tmp_path, 'worker-1', 'worker-1', 0, screen='WORKER-SCREEN\n')
    screen = FakeScreen(24, 120)
    view = View(screen, HistoryModel(tmp_path))
    view.draw()
    assert screen.refreshed >= 1
    # defaults: first agent, its latest generation, its screen
    assert view.current().label == 'supervisor-1.1' and view.view == 'screen'
    agent_col = min(screen.cols_of('supervisor'))
    assert screen.cols_of('worker-1') and min(screen.cols_of('worker-1')) == agent_col  # left column
    gen_col = min(screen.cols_of('supervisor-1.1'))
    assert gen_col > agent_col  # middle column
    assert screen.cols_of('supervisor-1') and min(screen.cols_of('supervisor-1')) == gen_col  # both generations listed
    text = '\n'.join(screen.lines())
    assert 'LATEST-SUPERVISOR-SCREEN' in text and 'second row' in text
    assert 'OLD-SUPERVISOR-SCREEN' not in text and 'WORKER-SCREEN' not in text
    assert min(screen.cols_of('LATEST-SUPERVISOR-SCREEN')) > gen_col  # right side


def test_view_slash_search_and_enter_jumps(tmp_path):
    retire(tmp_path, 'supervisor', 'supervisor-1', 0, screen='nothing\n')
    long_screen = ''.join(('the needle here\n' if i == 150 else 'filler %d\n' % i) for i in range(1, 201))
    retire(tmp_path, 'worker-1', 'worker-1', 0, screen=long_screen)
    retire(tmp_path, 'worker-1', 'worker-1.1', 1, screen='quiet\n', note='a\nb\nneedle in the note\n')
    screen = FakeScreen(24, 120)
    view = View(screen, HistoryModel(tmp_path))
    view.draw()
    for key in [ord('/')] + [ord(c) for c in 'needle'] + [ENTER]:
        assert view.handle(key) is True
    view.draw()
    assert view.query == 'needle' and len(view.hits) == 2
    entry = view.current()
    assert (view.agent, entry.generation, view.view) == ('worker-1', 0, 'screen')
    assert any('the needle here' in line for line in screen.lines())  # line 150 of 200 scrolled into view
    assert view.handle(ENTER) is True  # Enter again: next hit
    view.draw()
    assert (view.current().label, view.view) == ('worker-1.1', 'handoff')
    assert any('needle in the note' in line for line in screen.lines())
    assert view.handle(ord('q')) is False


def test_main_entry_takes_run_dir(tmp_path, monkeypatch, capsys):
    seen = []

    class RecordingView:
        def __init__(self, screen, model):
            seen.append(model)

        def run(self):
            seen.append('ran')

    fake_curses = mock.MagicMock()  # no terminal: every curses call is a no-op, the window too
    fake_curses.wrapper.side_effect = lambda fn, *a, **kw: fn(mock.MagicMock(), *a, **kw)
    monkeypatch.setattr(history_view, 'View', RecordingView)
    monkeypatch.setattr(history_view, 'curses', fake_curses)
    assert history_view.main(['history_view', str(tmp_path)]) == 0
    assert isinstance(seen[0], HistoryModel) and seen[0].run_dir == tmp_path and seen[1] == 'ran'
    seen.clear()
    assert history_view.main(['history_view']) == 2
    assert seen == [] and 'usage' in capsys.readouterr().err.lower()
    assert fake_curses.wrapper.call_count == 1  # the usage error never touched the terminal


# ---------------------------------------------------------------- M2 additions (brief-m03 §3.5)
import curses  # noqa: E402

from _fleetmux_milestone import milestone  # noqa: E402


class MouseScreen(FakeScreen):
    """FakeScreen plus curses' mouse half: ``getmouse()`` returns the queued ``(id, x, y, z, bstate)``.
    The view may read it as ``screen.getmouse()`` or ``curses.getmouse()``; the tests wire both."""

    def __init__(self, rows=24, cols=120):
        super().__init__(rows, cols)
        self.mouse = []

    def getmouse(self):
        return self.mouse.pop(0)

    def rows_of(self, word, col):
        """Rows where ``word`` stands alone starting at column ``col`` (above the bottom line)."""
        pat = re.compile(r'(?<![\w.-])' + re.escape(word) + r'(?![\w.-])')
        return [r for r, line in enumerate(self.lines()[:-1]) for m in pat.finditer(line) if m.start() == col]


@milestone(2)
def test_view_mouse_click_selects_agent_column(tmp_path, monkeypatch):
    retire(tmp_path, 'supervisor', 'supervisor-1', 0, screen='SUP\n')
    retire(tmp_path, 'worker-1', 'worker-1', 0, screen='W1\n')
    retire(tmp_path, 'worker-2', 'worker-2', 0, screen='W2\n')
    screen = MouseScreen(24, 120)
    monkeypatch.setattr(history_view.curses, 'getmouse', screen.getmouse, raising=False)
    view = View(screen, HistoryModel(tmp_path))
    view.draw()
    agent_col = min(screen.cols_of('supervisor'))
    row = screen.rows_of('worker-1', agent_col)[0]  # the second slot's line in the left column
    screen.mouse.append((0, agent_col + 1, row, 0, curses.BUTTON1_CLICKED))
    assert view.handle(curses.KEY_MOUSE) is True
    assert view.agent == 'worker-1' and view.current().slot == 'worker-1'
    view.draw()
    assert any('W1' in line for line in screen.lines())


@milestone(2)
def test_view_reloads_when_history_changes(tmp_path):
    """One ``getch()`` that returns -1 is one poll interval: ``run()`` then asks ``model.changed()`` and,
    when ``history.jsonl`` moved, reloads and redraws before the next key."""
    retire(tmp_path, 'worker-1', 'worker-1', 0, screen='GEN-ZERO\n')
    model = HistoryModel(tmp_path)

    class GrowingScreen(FakeScreen):
        keys = ['append', -1, -1, ord('q')]

        def getch(self):
            key = self.keys.pop(0) if self.keys else ord('q')
            if key == 'append':  # a generation retires while the view is open
                retire(tmp_path, 'worker-1', 'worker-1.1', 1, screen='GEN-ONE\n', hid='h2')
                st = os.stat(model.jsonl)
                os.utime(model.jsonl, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))  # same-tick writes on APFS
                return -1
            return key

    screen = GrowingScreen(24, 120)
    view = View(screen, model)
    view.run()
    assert [e.generation for e in model.agents()['worker-1']] == [0, 1]
    assert screen.cols_of('worker-1.1')  # the new generation is listed: the view reloaded, not just the file
    assert model.changed() is False


def test_terminal_disconnect_does_not_consume_input():
    import pty
    master, slave = pty.openpty()
    try:
        assert not history_view._terminal_disconnected(slave)
        os.write(master, b'q\n')
        assert not history_view._terminal_disconnected(slave)
        assert os.read(slave, 2) == b'q\n'
        os.close(master)
        master = None
        assert history_view._terminal_disconnected(slave)
    finally:
        if master is not None:
            os.close(master)
        os.close(slave)


def test_history_exits_after_pty_close_even_with_ignored_hup(tmp_path):
    import pty
    import signal
    import subprocess
    import sys
    import select
    master, slave = pty.openpty()
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, '-c',
             'import signal,sys; signal.signal(signal.SIGHUP, signal.SIG_IGN); '
             'from fleetmux.history_view import main; sys.exit(main(sys.argv))', str(tmp_path)],
            stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, 'TERM': 'xterm-256color'}, start_new_session=True)
        assert select.select([master], [], [], 5)[0], 'history did not draw'
        os.read(master, 65536)
        os.close(master)
        master = None
        assert proc.wait(timeout=5) == 0
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if master is not None:
            os.close(master)
        os.close(slave)


def test_kqueue_terminal_disconnect(tmp_path, monkeypatch):
    import select
    import pytest
    if not hasattr(select, 'kqueue'):
        pytest.skip('kqueue is only available on BSD/macOS')
    monkeypatch.delattr(select, 'poll', raising=False)
    test_terminal_disconnect_does_not_consume_input()
