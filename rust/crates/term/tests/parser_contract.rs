//! TermParser contract, run against `alacritty::Alacritty` (plan.md §3.2). Red until M0.

use std::thread::sleep;
use std::time::Duration;

use fleetmux_term::alacritty::{self, Alacritty, Palette};
use fleetmux_term::{Color, MouseMode, TermParser};

fn term(cols: u16, rows: u16) -> Alacritty {
    alacritty::new(cols, rows, 1000)
}

fn fed(cols: u16, rows: u16, bytes: &[u8]) -> Alacritty {
    let mut t = term(cols, rows);
    t.feed(bytes);
    t
}

fn ch(t: &Alacritty, row: u16, col: u16) -> char {
    t.cell(row, col).ch
}

#[test]
fn dsr_6n_is_answered_with_cursor_position() {
    let mut t = term(80, 24);
    t.feed(b"\x1b[3;5H\x1b[6n"); // move to row 3, col 5 (1-based), then ask
    assert_eq!(t.take_replies(), b"\x1b[3;5R".to_vec());
    // Drained: a second take has nothing.
    assert_eq!(t.take_replies(), Vec::<u8>::new());
}

#[test]
fn da_is_answered() {
    let mut t = term(80, 24);
    t.feed(b"\x1b[c");
    let reply = t.take_replies();
    assert!(
        reply.starts_with(b"\x1b[?") && reply.ends_with(b"c"),
        "DA reply {:?}",
        String::from_utf8_lossy(&reply)
    );
}

#[test]
fn osc_10_11_are_answered() {
    for (query, prefix) in [
        (&b"\x1b]10;?\x07"[..], &b"\x1b]10;rgb:"[..]),
        (&b"\x1b]11;?\x07"[..], &b"\x1b]11;rgb:"[..]),
    ] {
        let mut t = term(80, 24);
        t.feed(query);
        let reply = t.take_replies();
        assert!(
            reply.starts_with(prefix),
            "OSC reply {:?}",
            String::from_utf8_lossy(&reply)
        );
        assert!(
            reply.ends_with(b"\x07") || reply.ends_with(b"\x1b\\"),
            "OSC reply not terminated: {:?}",
            String::from_utf8_lossy(&reply)
        );
    }
}

#[test]
fn osc_replies_use_the_cmux_palette_by_default() {
    // cmux 0.65.0 measured values (`Palette::CMUX`): the reply must carry them, terminator-agnostic.
    for (query, want) in [
        (&b"\x1b]11;?\x07"[..], "rgb:1e1e/1e1e/1e1e"),
        (&b"\x1b]10;?\x07"[..], "rgb:ffff/ffff/ffff"),
        (&b"\x1b]12;?\x07"[..], "rgb:9898/9898/9d9d"),
        (&b"\x1b]4;1;?\x07"[..], "rgb:cccc/3737/2e2e"),
    ] {
        let mut t = term(80, 24);
        t.feed(query);
        let reply = String::from_utf8_lossy(&t.take_replies()).into_owned();
        assert!(
            reply.contains(want),
            "OSC query {:?} answered {:?}, want {}",
            String::from_utf8_lossy(query),
            reply,
            want
        );
    }
}

#[test]
fn osc_replies_follow_a_custom_palette() {
    let palette = Palette {
        bg: (0x12, 0x34, 0x56),
        ..Palette::CMUX
    };
    let mut t = alacritty::with_palette(80, 24, 1000, palette);
    t.feed(b"\x1b]11;?\x07");
    let reply = String::from_utf8_lossy(&t.take_replies()).into_owned();
    assert!(reply.contains("rgb:1212/3434/5656"), "OSC 11 answered {:?}", reply);
}

#[test]
fn kitty_keyboard_query_is_not_advertised() {
    // design.md §2.6: no reply, or "flags = 0"; never a non-zero kitty flag set.
    let mut t = term(80, 24);
    t.feed(b"\x1b[?u");
    let reply = t.take_replies();
    assert!(
        reply.is_empty() || reply == b"\x1b[?0u",
        "kitty query answered {:?}",
        String::from_utf8_lossy(&reply)
    );
}

#[test]
fn alt_screen_mode_bit_tracks_1049() {
    let mut t = term(80, 24);
    assert!(!t.modes().alt_screen);
    t.feed(b"\x1b[?1049h");
    assert!(t.modes().alt_screen);
    t.feed(b"\x1b[?1049l");
    assert!(!t.modes().alt_screen);
}

#[test]
fn bracketed_paste_bit_tracks_2004() {
    let mut t = term(80, 24);
    assert!(!t.modes().bracketed_paste);
    t.feed(b"\x1b[?2004h");
    assert!(t.modes().bracketed_paste);
    t.feed(b"\x1b[?2004l");
    assert!(!t.modes().bracketed_paste);
}

#[test]
fn mouse_bits_track_1000_1002_1003_1006() {
    let mut t = term(80, 24);
    assert_eq!(t.modes().mouse, None);
    assert!(!t.modes().sgr_mouse);
    for (set, reset, want) in [
        (&b"\x1b[?1000h"[..], &b"\x1b[?1000l"[..], MouseMode::Click),
        (&b"\x1b[?1002h"[..], &b"\x1b[?1002l"[..], MouseMode::Drag),
        (&b"\x1b[?1003h"[..], &b"\x1b[?1003l"[..], MouseMode::Motion),
    ] {
        t.feed(set);
        assert_eq!(t.modes().mouse, Some(want));
        t.feed(reset);
        assert_eq!(t.modes().mouse, None, "after reset of {want:?}");
    }
    t.feed(b"\x1b[?1006h");
    assert!(t.modes().sgr_mouse);
    t.feed(b"\x1b[?1006l");
    assert!(!t.modes().sgr_mouse);
}

#[test]
fn sync_update_2026_holds_cells_until_end() {
    let mut t = term(80, 24);
    t.feed(b"\x1b[?2026hX");
    assert_ne!(ch(&t, 0, 0), 'X', "cell drawn before ?2026l");
    t.feed(b"\x1b[?2026l");
    assert_eq!(ch(&t, 0, 0), 'X');
    assert!(!t.modes().sync_update);
}

#[test]
fn sync_update_times_out_after_150ms() {
    let mut t = term(80, 24);
    t.feed(b"\x1b[?2026hX");
    assert_ne!(ch(&t, 0, 0), 'X', "cell drawn before the timeout");
    sleep(Duration::from_millis(250));
    t.feed(b""); // runs timers only
    assert_eq!(ch(&t, 0, 0), 'X', "held cells not released after 150 ms");
    assert!(!t.modes().sync_update);
}

#[test]
fn truecolor_cell_keeps_rgb() {
    let t = fed(80, 24, b"\x1b[38;2;10;20;30;48;2;40;50;60mA\x1b[0mB");
    let a = t.cell(0, 0);
    assert_eq!(a.ch, 'A');
    assert_eq!(a.fg, Color::Rgb(10, 20, 30));
    assert_eq!(a.bg, Color::Rgb(40, 50, 60));
    let b = t.cell(0, 1);
    assert_eq!((b.ch, b.fg, b.bg), ('B', Color::Default, Color::Default));
}

#[test]
fn wide_char_followed_by_placeholder_cell() {
    let t = fed(80, 24, "中a".as_bytes());
    let wide = t.cell(0, 0);
    assert_eq!((wide.ch, wide.width), ('中', 2));
    assert_eq!(t.cell(0, 1).width, 0);
    let a = t.cell(0, 2);
    assert_eq!((a.ch, a.width), ('a', 1));
    assert_eq!(t.cursor().col, 3);
}

#[test]
fn text_strips_trailing_blanks_and_returns_physical_lines() {
    // "0123456789XY" is one logical line that wraps into two physical rows at 10 columns.
    let t = fed(10, 5, b"abc   \r\n0123456789XY\x1b[44m   \x1b[0m");
    let text = t.text(100);
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(
        &lines[..3],
        &["abc", "0123456789", "XY"],
        "text was {text:?}"
    );
    for l in &lines {
        assert_eq!(*l, l.trim_end(), "trailing blanks kept in {l:?}");
    }
    // Trailing blank rows are kept: 5 physical rows (the last two blank) need at least 4 separators,
    // whether rows are `\n`-joined (4) or `\n`-terminated (5).
    assert!(
        text.matches('\n').count() >= 4,
        "trailing blank rows dropped: {text:?}"
    );
}

#[test]
fn scrollback_keeps_last_n_lines() {
    let (rows, scrollback) = (5usize, 10usize);
    let mut t = alacritty::new(20, rows as u16, scrollback);
    for i in 0..50 {
        t.feed(format!("L{i}\r\n").as_bytes());
    }
    let text = t.text(10_000);
    let kept: Vec<&str> = text.lines().filter(|l| !l.is_empty()).collect();
    // A contiguous tail ending at L49: at least the scrollback depth, at most scrollback + screen.
    assert!(
        kept.len() >= scrollback && kept.len() <= scrollback + rows,
        "kept {} lines: {kept:?}",
        kept.len()
    );
    let first = 50 - kept.len();
    let want: Vec<String> = (first..50).map(|i| format!("L{i}")).collect();
    assert_eq!(kept, want.iter().map(String::as_str).collect::<Vec<_>>());
}

#[test]
fn resize_reflows_without_panic() {
    let line: String = ('a'..='z').chain('A'..='Z').collect(); // 52 chars
    let mut t = alacritty::new(20, 5, 100);
    t.feed(format!("{line}\r\nend").as_bytes());
    for (cols, rows) in [(10, 5), (80, 24), (5, 3), (2, 2), (40, 10)] {
        t.resize(cols, rows);
        let c = t.cursor();
        assert!(
            c.row < rows && c.col <= cols,
            "cursor {c:?} outside {cols}x{rows}"
        );
        let joined: String = t.text(10_000).lines().collect();
        assert!(
            joined.contains(&line),
            "content lost after resize to {cols}x{rows}: {joined:?}"
        );
    }
}

// ---- Stage A additions (brief-m03 §3.4; package B23 `wide-char`, M3). ----

#[test]
fn ambiguous_width_chars_take_one_cell() {
    // East Asian Ambiguous (`│` U+2502, `■` U+25A0, `α` U+03B1) take one cell; `漢` takes two + a placeholder.
    let t = fed(20, 5, "│■α漢x".as_bytes());
    for (col, want) in [(0, '│'), (1, '■'), (2, 'α')] {
        let c = t.cell(0, col);
        assert_eq!((c.ch, c.width), (want, 1), "col {col}");
    }
    let wide = t.cell(0, 3);
    assert_eq!((wide.ch, wide.width), ('漢', 2));
    assert_eq!(t.cell(0, 4).width, 0);
    let x = t.cell(0, 5);
    assert_eq!((x.ch, x.width), ('x', 1));
    assert_eq!(t.cursor().col, 6);
    // `cell_width` uses the same rule as the grid.
    for (ch, w) in [('│', 1), ('■', 1), ('α', 1), ('漢', 2), ('x', 1)] {
        assert_eq!(fleetmux_term::cell_width(ch), w, "cell_width({ch:?})");
    }
}

#[test]
fn memory_estimate_under_5kb_per_scrollback_row() {
    // design.md §7.3 M3: 10,000 scrollback rows × 120 columns ≤ 50 MB, i.e. ≤ 5 KiB per row.
    let (cols, rows, scrollback) = (120u16, 40u16, 10_000usize);
    let mut t = alacritty::new(cols, rows, scrollback);
    let mut bytes = Vec::new();
    for i in 0..scrollback + rows as usize {
        bytes.extend_from_slice(format!("{i:x>119}\r\n").as_bytes());
    }
    t.feed(&bytes);
    assert_eq!(t.scrollback_len(), scrollback);
    let est = t.memory_estimate();
    // Lower bound: a real estimate counts at least one byte per scrollback cell (a stub returning 0 fails).
    assert!(
        est >= scrollback * cols as usize,
        "estimate {est} too small to be real"
    );
    assert!(
        est <= scrollback * 5 * 1024,
        "estimate {est} bytes > 5 KiB per scrollback row"
    );
}
