"""Tests for cmux_fleet.terminal_theme (OSC 10/11 colour probing).

Hard rules for this file (it used to hang for 20 minutes):

* every pty read goes through ``select`` with a timeout,
* the responder thread always has a deadline and is a daemon,
* nothing calls ``tcsetattr(..., TCSADRAIN)`` on a pty whose master is not
  being drained,
* the whole file runs in well under 10 seconds.

The real ``/dev/tty`` is never touched: every tty is a pty from ``os.openpty``.
"""

import inspect
import json
import os
import select
import signal
import sys
import termios
import threading
import time
import tty

import pytest

from cmux_fleet import terminal_theme


QUERY = b'\x1b]11;?\x1b\\\x1b]10;?\x1b\\'
ANSWER_ST = (b'\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\'
             b'\x1b]10;rgb:e8e8/e6e6/e1e1\x1b\\')
ANSWER_BEL = b'\x1b]11;rgb:1e1e/1e1e/1e1e\x07\x1b]10;rgb:e8/e6/e1\x07'


# --------------------------------------------------------------------------
# helpers: bounded pty I/O only
# --------------------------------------------------------------------------

def _read_available(fd, timeout=2.0, limit=4096):
    """Read whatever is readable within ``timeout``; never blocks longer."""
    deadline = time.monotonic() + timeout
    out = b''
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return out
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError):
            return out
        if not ready:
            return out
        try:
            chunk = os.read(fd, limit)
        except OSError:
            return out
        if not chunk:
            return out
        out += chunk
        if len(out) >= limit:
            return out


def _start_responder(master, reply, timeout=2.0):
    """Read one query off ``master`` then answer; bounded, daemon, joinable."""
    done = threading.Event()
    seen = []

    def run():
        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                try:
                    ready, _, _ = select.select([master], [], [], remaining)
                except (OSError, ValueError):
                    return
                if not ready:
                    return
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                seen.append(chunk)
                if b'\x1b]11;?' in b''.join(seen) and b'\x1b]10;?' in b''.join(seen):
                    break
            try:
                os.write(master, reply)
            except OSError:
                return
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, done, seen


def _stop_responder(thread, done):
    done.wait(2.0)
    thread.join(2.0)
    assert not thread.is_alive(), 'responder thread must not outlive the test'


# Darwin's kernel sets PENDIN ("retype pending input") on a pty slave once
# something is queued for it; that bit is not ours and cannot be cleared with
# tcsetattr, so it is masked out when comparing restored attributes.
_PENDIN = getattr(termios, 'PENDIN', 0x20000000)


def _attrs_restored(after, before):
    if list(after[:3]) != list(before[:3]):        # iflag, oflag, cflag
        return False
    if (after[3] & ~_PENDIN) != (before[3] & ~_PENDIN):   # lflag
        return False
    if list(after[4:]) != list(before[4:]):        # speeds + cc[]
        return False
    return after[3] & termios.ICANON and after[3] & termios.ECHO


# --------------------------------------------------------------------------
# parse_osc_color: both terminators, both widths, both codes
# --------------------------------------------------------------------------

def test_query_constant_is_osc11_then_osc10_with_st():
    assert QUERY == b'\x1b]11;?\x1b\\\x1b]10;?\x1b\\'
    assert terminal_theme.QUERY == QUERY


def test_parse_st_16bit():
    assert terminal_theme.parse_osc_color(
        b'\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\', 11) == '#1e1e1e'


def test_parse_bel_8bit():
    assert terminal_theme.parse_osc_color(b'\x1b]10;rgb:e8/e6/e1\x07', 10) == '#e8e6e1'


def test_parse_mixed_widths_and_upper_case():
    assert terminal_theme.parse_osc_color(
        b'\x1b]11;rgb:1E1E/ff/1e1e\x07', 11) == '#1eff1e'
    assert terminal_theme.parse_osc_color(
        b'\x1b]10;rgb:12/3456/ab\x1b\\', 10) == '#1234ab'


def test_parse_wrong_code_or_garbage():
    reply = b'\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\'
    assert terminal_theme.parse_osc_color(reply, 10) is None
    assert terminal_theme.parse_osc_color(reply, 11) == '#1e1e1e'
    assert terminal_theme.parse_osc_color(b'hello', 11) is None
    assert terminal_theme.parse_osc_color(b'\x1b]11;rgb:zz/1e/1e\x1b\\', 11) is None
    assert terminal_theme.parse_osc_color(b'', 11) is None
    assert terminal_theme.parse_osc_color(None, 11) is None
    assert terminal_theme.parse_osc_color('\x1b]11;rgb:1e/1e/1e\x07', 11) is None
    assert terminal_theme.parse_osc_color(reply, 'nope') is None
    # unterminated reply is not an answer
    assert terminal_theme.parse_osc_color(b'\x1b]11;rgb:1e1e/1e1e/1e1e', 11) is None


def test_parse_real_cmux_sample():
    raw = (b'\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\'
           b'\x1b]10;rgb:ffff/ffff/ffff\x1b\\')
    assert terminal_theme.parse_osc_color(raw, 11) == '#1e1e1e'
    assert terminal_theme.parse_osc_color(raw, 10) == '#ffffff'


# --------------------------------------------------------------------------
# query(): answered, unanswered, garbage, not-a-tty, closed fd
# --------------------------------------------------------------------------

def test_query_default_timeout_is_300ms():
    assert inspect.signature(terminal_theme.query).parameters['timeout'].default == 0.3


def test_query_answered_restores_termios_and_flushes():
    master, slave = os.openpty()
    try:
        before = termios.tcgetattr(slave)
        thread, done, seen = _start_responder(master, ANSWER_ST)
        try:
            got = terminal_theme.query(slave)
        finally:
            _stop_responder(thread, done)
        assert got == {'bg': '#1e1e1e', 'fg': '#e8e6e1'}
        after = termios.tcgetattr(slave)
        assert _attrs_restored(after, before), (
            'termios must be restored: %r != %r' % (after, before))
        sent = b''.join(seen)
        assert b'\x1b]11;?' in sent and b'\x1b]10;?' in sent
        # nothing of the answer is left behind for the shell
        assert select.select([slave], [], [], 0) == ([], [], [])
    finally:
        os.close(slave)
        os.close(master)


def test_query_answered_bel_and_8bit():
    master, slave = os.openpty()
    try:
        thread, done, _ = _start_responder(master, ANSWER_BEL)
        try:
            got = terminal_theme.query(slave, timeout=0.5)
        finally:
            _stop_responder(thread, done)
        assert got == {'bg': '#1e1e1e', 'fg': '#e8e6e1'}
    finally:
        os.close(slave)
        os.close(master)


def test_query_no_answer_returns_none_fast():
    """Regression: nobody ever reads the query bytes off the master.

    The old code called ``tcsetattr(TCSADRAIN)`` here and blocked forever
    (the 20-minute hang); now it must return within the timeout.
    """
    master, slave = os.openpty()
    try:
        before = termios.tcgetattr(slave)
        start = time.monotonic()
        got = terminal_theme.query(slave, timeout=0.2)
        elapsed = time.monotonic() - start
        assert got is None
        assert elapsed < 1.0, 'query blocked for %.2fs' % elapsed
        assert _attrs_restored(termios.tcgetattr(slave), before)
    finally:
        os.close(slave)
        os.close(master)


def test_query_garbage_answer_returns_none():
    master, slave = os.openpty()
    try:
        thread, done, _ = _start_responder(master, b'\x1b]11;rgb:not-a-colour\x07')
        try:
            start = time.monotonic()
            got = terminal_theme.query(slave, timeout=0.2)
            elapsed = time.monotonic() - start
        finally:
            _stop_responder(thread, done)
        assert got is None
        assert elapsed < 1.0
    finally:
        os.close(slave)
        os.close(master)


def test_query_survives_pending_output_at_entry():
    """Regression: pending *output* is already queued when the probe starts.

    ``tty.setraw(fd)`` defaults to ``TCSAFLUSH``, which drains the output queue
    first and therefore blocked forever here (that is where the 20-minute hang
    lived before the ``TCSADRAIN`` restore was found).  The probe must switch
    mode with ``TCSANOW`` and still return inside its budget.
    """
    master, slave = os.openpty()
    try:
        os.write(slave, b'\x1b]11;?\x1b\\\x1b]10;?\x1b\\')   # nobody drains master
        before = termios.tcgetattr(slave)
        start = time.monotonic()
        got = terminal_theme.query(slave, timeout=0.2)
        elapsed = time.monotonic() - start
        assert got is None
        assert elapsed < 1.0, 'query blocked for %.2fs' % elapsed
        assert _attrs_restored(termios.tcgetattr(slave), before)
    finally:
        os.close(slave)
        os.close(master)


@pytest.mark.skipif(sys.platform != 'darwin', reason='a Linux pty does not wait for unread output on TCSAFLUSH '
                    '(kernel 7.0 returns at once): the blocking this pins is macOS behaviour')
def test_setraw_default_would_have_blocked():
    """Pin the reason for the explicit TCSANOW (bounded: SIGALRM after 1 s). The probe's own guarantee, returning
    in budget with output pending, is test_query_survives_pending_output_at_entry on every host."""
    master, slave = os.openpty()
    try:
        os.write(slave, b'\x1b]11;?\x1b\\')

        def _alarm(signum, frame):
            raise TimeoutError('TCSAFLUSH blocked on pending output')

        old_handler = signal.signal(signal.SIGALRM, _alarm)
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.4)
            try:
                tty.setraw(slave)          # default when=TCSAFLUSH
            except TimeoutError:
                pass                       # expected: that is why we pass TCSANOW
            else:
                raise AssertionError('TCSAFLUSH did not block; test premise changed')
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)

        start = time.monotonic()
        tty.setraw(slave, termios.TCSANOW)
        assert time.monotonic() - start < 1.0
    finally:
        # drop the unread output first: closing a pty that still has queued
        # output costs the kernel ~0.6 s here, which is bounded but pointless
        try:
            termios.tcflush(slave, termios.TCIOFLUSH)
        except (OSError, termios.error):
            pass
        os.close(slave)
        os.close(master)


def test_query_uses_tcsanow_for_raw_mode(monkeypatch):
    """The raw-mode switch must not be tty.setraw's TCSAFLUSH default."""
    master, slave = os.openpty()
    calls = []
    real_setraw = terminal_theme.tty.setraw
    try:
        def spy(fd, when=None):
            calls.append(when)
            return real_setraw(fd, termios.TCSANOW if when is None else when)

        monkeypatch.setattr(terminal_theme.tty, 'setraw', spy)
        thread, done, _ = _start_responder(master, ANSWER_ST)
        try:
            got = terminal_theme.query(slave, timeout=0.3)
        finally:
            _stop_responder(thread, done)
        assert got == {'bg': '#1e1e1e', 'fg': '#e8e6e1'}
        assert calls and all(c == termios.TCSANOW for c in calls), calls
    finally:
        os.close(slave)
        os.close(master)


def test_query_never_writes_when_nonblocking_setup_fails(monkeypatch):
    """If the fd cannot be made non-blocking, do not risk a blocking write."""
    master, slave = os.openpty()
    writes = []
    try:
        def boom(*args, **kwargs):
            raise OSError('fcntl not allowed')

        monkeypatch.setattr(terminal_theme.fcntl, 'fcntl', boom)
        monkeypatch.setattr(terminal_theme.os, 'write',
                            lambda *a, **k: writes.append(a))
        assert terminal_theme._write_all(slave, b'x', time.monotonic() + 1.0) is False
        assert writes == [], 'no os.write may be attempted'
        start = time.monotonic()
        assert terminal_theme.query(slave, timeout=0.2) is None
        assert time.monotonic() - start < 1.0
        assert writes == [], 'query must not write either'
    finally:
        os.close(slave)
        os.close(master)


def test_query_bg_only_waits_for_fg_until_deadline():
    """Documented semantics: bg alone is enough, but fg is awaited till the end."""
    master, slave = os.openpty()
    try:
        thread, done, _ = _start_responder(
            master, b'\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\')
        try:
            start = time.monotonic()
            got = terminal_theme.query(slave, timeout=0.3)
            elapsed = time.monotonic() - start
        finally:
            _stop_responder(thread, done)
        assert got == {'bg': '#1e1e1e', 'fg': None}
        assert 0.2 <= elapsed < 1.0, elapsed
    finally:
        os.close(slave)
        os.close(master)


def test_query_not_a_tty():
    r, w = os.pipe()
    try:
        assert terminal_theme.query(r) is None
        assert terminal_theme.query(r, timeout=0.1) is None
    finally:
        os.close(r)
        os.close(w)


def test_query_closed_fd_is_safe():
    r, w = os.pipe()
    os.close(r)
    try:
        assert terminal_theme.query(r) is None
    finally:
        os.close(w)


def test_probe_without_dev_tty(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError('no tty here')

    monkeypatch.setattr(terminal_theme.os, 'open', boom)
    assert terminal_theme.probe() is None


# --------------------------------------------------------------------------
# capture() / read(): atomic write, strict validation, old file kept
# --------------------------------------------------------------------------

def test_capture_writes_file_and_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        terminal_theme, 'probe',
        lambda timeout=0.3: {'bg': '#1e1e1e', 'fg': '#e8e6e1'})
    theme = terminal_theme.capture(tmp_path)
    assert theme['bg'] == '#1e1e1e'
    assert theme['fg'] == '#e8e6e1'
    assert theme['source'] == 'osc'
    assert isinstance(theme['at'], float)

    path = tmp_path / terminal_theme.FILE
    assert path.exists()
    raw = json.loads(path.read_text())
    assert raw['bg'] == '#1e1e1e'
    assert raw['fg'] == '#e8e6e1'
    assert raw['source'] == 'osc'
    assert isinstance(raw['at'], float)
    assert not (tmp_path / (terminal_theme.FILE + '.tmp')).exists()
    assert terminal_theme.read(tmp_path) == {'bg': '#1e1e1e', 'fg': '#e8e6e1'}


def test_capture_replaces_old_file_atomically(tmp_path, monkeypatch):
    path = tmp_path / terminal_theme.FILE
    path.write_text(json.dumps({'bg': '#ffffff', 'fg': None, 'source': 'osc',
                                'at': 1.0}))
    monkeypatch.setattr(
        terminal_theme, 'probe',
        lambda timeout=0.3: {'bg': '#1e1e1e', 'fg': None})
    theme = terminal_theme.capture(tmp_path)
    assert theme['bg'] == '#1e1e1e'
    assert json.loads(path.read_text())['bg'] == '#1e1e1e'
    assert not (tmp_path / (terminal_theme.FILE + '.tmp')).exists()


def test_capture_without_terminal_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_theme, 'probe', lambda timeout=0.3: None)
    assert terminal_theme.capture(tmp_path) is None
    assert not (tmp_path / terminal_theme.FILE).exists()


def test_capture_without_terminal_keeps_old_file(tmp_path, monkeypatch):
    path = tmp_path / terminal_theme.FILE
    path.write_text(json.dumps({'bg': '#1e1e1e', 'fg': None, 'source': 'osc',
                                'at': 1.0}))
    monkeypatch.setattr(terminal_theme, 'probe', lambda timeout=0.3: None)
    assert terminal_theme.capture(tmp_path) is None
    assert json.loads(path.read_text())['bg'] == '#1e1e1e'
    assert terminal_theme.read(tmp_path) == {'bg': '#1e1e1e', 'fg': None}


def test_capture_failed_replace_leaves_no_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(
        terminal_theme, 'probe',
        lambda timeout=0.3: {'bg': '#1e1e1e', 'fg': None})

    def boom(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(terminal_theme.os, 'replace', boom)
    assert terminal_theme.capture(tmp_path) is None
    assert not (tmp_path / (terminal_theme.FILE + '.tmp')).exists()


def test_read_is_strict_about_bg(tmp_path):
    """No strip / no case folding / fullmatch: only exact lower-case #rrggbb."""
    path = tmp_path / terminal_theme.FILE
    for bad in ('red', '#1E1E1E', ' #1e1e1e', '#1e1e1e ', '#1e1e1e\n',
                '#1e1e1\n', '#12345', '#1234567', '#gggggg', '', '#1e1e1e\t'):
        path.write_text(json.dumps({'bg': bad}))
        assert terminal_theme.read(tmp_path) is None, bad

    path.write_text(json.dumps({'bg': '#1e1e1e'}))
    assert terminal_theme.read(tmp_path) == {'bg': '#1e1e1e', 'fg': None}
    # a raw trailing newline in the file is fine (that is JSON whitespace)
    path.write_text(json.dumps({'bg': '#1e1e1e'}) + '\n')
    assert terminal_theme.read(tmp_path) == {'bg': '#1e1e1e', 'fg': None}


def test_read_is_strict_about_fg(tmp_path):
    path = tmp_path / terminal_theme.FILE
    for bad in ('nope', '#FFF', '#ffffff\n', '#ffffff ', 255, 0, True,
                ['#ffffff'], {'hex': '#ffffff'}):
        path.write_text(json.dumps({'bg': '#1e1e1e', 'fg': bad}))
        assert terminal_theme.read(tmp_path) is None, repr(bad)

    for good in (None, '#e8e6e1'):
        path.write_text(json.dumps({'bg': '#1e1e1e', 'fg': good}))
        assert terminal_theme.read(tmp_path) == {'bg': '#1e1e1e', 'fg': good}

    path.write_text(json.dumps({'bg': '#1e1e1e'}))
    assert terminal_theme.read(tmp_path) == {'bg': '#1e1e1e', 'fg': None}


def test_read_rejects_junk_files(tmp_path):
    path = tmp_path / terminal_theme.FILE
    path.write_text(json.dumps({'fg': '#ffffff'}))
    assert terminal_theme.read(tmp_path) is None

    path.write_text(json.dumps(['#1e1e1e']))
    assert terminal_theme.read(tmp_path) is None

    path.write_text('null')
    assert terminal_theme.read(tmp_path) is None

    path.write_text('{not json')
    assert terminal_theme.read(tmp_path) is None

    # undecodable bytes must fall back safely, not raise
    path.write_bytes(b'{"bg": "#1e1e1e", "fg": "\xff\xfe"}')
    assert terminal_theme.read(tmp_path) is None

    path.write_bytes(b'\xff\xfe\x00\x01')
    assert terminal_theme.read(tmp_path) is None

    assert terminal_theme.read(tmp_path / 'missing') is None
    assert terminal_theme.read(tmp_path / 'missing' / 'deeper') is None


# --------------------------------------------------------------------------
# css(): every variable, colour-scheme, light vs dark
# --------------------------------------------------------------------------

def _vars(css):
    body = css[len(':root{'):-1]
    out = {}
    for item in body.split(';'):
        key, _, value = item.partition(':')
        out[key.strip()] = value.strip()
    return out


def test_css_dark_has_all_variables():
    css = terminal_theme.css({'bg': '#1e1e1e', 'fg': None})
    got = _vars(css)
    assert got['color-scheme'] == 'dark'
    assert got['--bg'] == '#1e1e1e'
    assert got['--fg'] == '#e8e6e1'          # dark scheme default
    assert got['--accent'] == '#7fb0e0'
    assert got['--mark'] == '#6b5a1a'
    assert got['--cur'] == '#b8641c'
    for key in ('--bg', '--fg', '--muted', '--line', '--panel', '--accent',
                '--mark', '--cur', 'color-scheme'):
        assert key in got, key
    assert css.startswith(':root{') and css.endswith('}')


def test_css_light_and_mixes():
    css = terminal_theme.css({'bg': '#ffffff', 'fg': '#000000'})
    got = _vars(css)
    assert got['color-scheme'] == 'light'
    assert got['--accent'] == '#2f5d8a'
    assert got['--fg'] == '#000000'
    # bg mixed towards fg by 6 % / 12 % / 55 %
    assert got['--panel'] == '#f0f0f0'
    assert got['--line'] == '#e0e0e0'
    assert got['--muted'] == '#737373'


def test_css_uses_terminal_fg_for_mixing():
    got = _vars(terminal_theme.css({'bg': '#101010', 'fg': '#f0f0f0'}))
    assert got['--fg'] == '#f0f0f0'
    assert got['--panel'] == '#1d1d1d'
    assert got['--line'] == '#2b2b2b'
    assert got['--muted'] == '#8b8b8b'


def test_css_invalid_theme_is_empty():
    assert terminal_theme.css(None) == ''
    assert terminal_theme.css({}) == ''
    assert terminal_theme.css({'bg': 'red'}) == ''
    assert terminal_theme.css({'bg': '#1E1E1E'}) == ''          # no case folding
    assert terminal_theme.css({'bg': '#1e1e1e\n'}) == ''        # no strip
    assert terminal_theme.css({'bg': '#1e1e1e', 'fg': 'nope'}) == ''
    assert terminal_theme.css({'bg': '#1e1e1e', 'fg': 255}) == ''
