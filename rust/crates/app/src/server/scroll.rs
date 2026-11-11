//! Page (scrollback) mode (PROTOCOL.md §13): prefix `[` enters it for the focused pane; keys move the
//! parser's `display_offset` or search upward, and nothing typed in the mode reaches the pane.
//!
//! B15 (copy-scroll). The server's input path (B14) routes the focused viewer's bytes here while `State::page`
//! is `Some`; the status line (B16) shows `[display_offset/scrollback_len]` from the pane's parser.
use fleetmux_proto::PaneId;
use fleetmux_term::TermParser;

/// Page mode state (`State::page`; `None` = not in page mode).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PageMode {
    pub pane: PaneId,
    /// The `/` search text being typed or last searched.
    pub query: String,
    /// `true` while the status line is taking `/` input (until `Enter` / `Esc`).
    pub typing: bool,
}

/// Result of one key in page mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PageAction {
    /// Nothing visible changed.
    Stay,
    /// `q` / `Esc`: leave page mode; the offset is back at 0.
    Exit,
    /// Offset, query or status line changed: redraw.
    Redraw,
}

/// Apply the input bytes of one key: `PgUp` / `PgDn` ±(rows−1), `Up` / `Down` ±1, `/` + `Enter` →
/// `search_up`, `n` / `N` older / newer match, `q` / `Esc` exit with the offset reset to 0.
///
/// `bytes` may also hold several keys (e.g. `/needle\r` in one `0x01` frame): they are applied in order and
/// the strongest result is returned; keys after one that exits are dropped (they were typed in page mode).
/// While `/` input is open, printable characters (UTF-8) extend the query, `Backspace` deletes one, `Enter`
/// searches and `Esc` closes the input without leaving page mode; other keys are ignored there.
pub fn on_key(mode: &mut PageMode, term: &mut dyn TermParser, bytes: &[u8]) -> PageAction {
    let mut action = PageAction::Stay;
    let mut rest = bytes;
    while let Some((key, used)) = next_key(rest) {
        rest = &rest[used..];
        match apply(mode, term, key) {
            PageAction::Exit => return PageAction::Exit,
            PageAction::Redraw => action = PageAction::Redraw,
            PageAction::Stay => {}
        }
    }
    action
}

/// One decoded key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Key<'a> {
    PgUp,
    PgDn,
    Up,
    Down,
    Enter,
    Backspace,
    Esc,
    /// One printable character (its UTF-8 bytes).
    Char(&'a str),
    /// Anything else: other escape sequences, control bytes, invalid UTF-8.
    Other,
}

/// The first key in `b` and the bytes it used; `None` when `b` is empty. A lone `ESC` (end of input or another
/// `ESC` next) is `Esc`; `ESC [` starts a CSI sequence (an unterminated one takes the rest), `ESC O` an SS3 one.
fn next_key(b: &[u8]) -> Option<(Key<'_>, usize)> {
    let first = *b.first()?;
    Some(match first {
        0x1b => match b.get(1) {
            None | Some(0x1b) => (Key::Esc, 1),
            Some(b'[') => {
                let end = b[2..].iter().position(|c| (0x40..=0x7e).contains(c));
                let Some(end) = end.map(|e| e + 2) else {
                    return Some((Key::Other, b.len()));
                };
                let key = match (&b[2..end], b[end]) {
                    (b"5", b'~') => Key::PgUp,
                    (b"6", b'~') => Key::PgDn,
                    (_, b'A') => Key::Up,
                    (_, b'B') => Key::Down,
                    _ => Key::Other,
                };
                (key, end + 1)
            }
            Some(b'O') => match b.get(2) {
                Some(b'A') => (Key::Up, 3),
                Some(b'B') => (Key::Down, 3),
                Some(_) => (Key::Other, 3),
                None => (Key::Other, 2),
            },
            Some(_) => (Key::Other, 2), // Alt + key
        },
        b'\r' | b'\n' => (Key::Enter, 1),
        0x7f | 0x08 => (Key::Backspace, 1),
        0x00..=0x1f => (Key::Other, 1),
        _ => {
            let len = match first {
                0xc0..=0xdf => 2,
                0xe0..=0xef => 3,
                0xf0..=0xf7 => 4,
                _ => 1,
            };
            match b.get(..len).map(std::str::from_utf8) {
                Some(Ok(s)) if first < 0x80 || len > 1 => (Key::Char(s), len),
                _ => (Key::Other, 1),
            }
        }
    })
}

fn apply(mode: &mut PageMode, term: &mut dyn TermParser, key: Key<'_>) -> PageAction {
    if mode.typing {
        match key {
            Key::Char(c) => mode.query.push_str(c),
            Key::Backspace => {
                if mode.query.pop().is_none() {
                    return PageAction::Stay;
                }
            }
            Key::Esc => mode.typing = false,
            Key::Enter => {
                mode.typing = false;
                if let Some(o) = term.search_up(&mode.query, term.display_offset()) {
                    term.set_display_offset(o);
                }
            }
            _ => return PageAction::Stay,
        }
        return PageAction::Redraw;
    }
    let offset = term.display_offset();
    let target = match key {
        Key::Esc | Key::Char("q") => {
            term.set_display_offset(0);
            return PageAction::Exit;
        }
        Key::Char("/") => {
            mode.typing = true;
            mode.query.clear();
            return PageAction::Redraw;
        }
        Key::PgUp => offset.saturating_add(page(term)),
        Key::PgDn => offset.saturating_sub(page(term)),
        Key::Up => offset.saturating_add(1),
        Key::Down => offset.saturating_sub(1),
        Key::Char("n") => match term.search_up(&mode.query, offset) {
            Some(o) => o,
            None => return PageAction::Stay,
        },
        Key::Char("N") => match search_down(term, &mode.query, offset) {
            Some(o) => o,
            None => return PageAction::Stay,
        },
        _ => return PageAction::Stay,
    };
    term.set_display_offset(target);
    if term.display_offset() == offset {
        PageAction::Stay
    } else {
        PageAction::Redraw
    }
}

/// `PgUp` / `PgDn` step: the screen height − 1 (at least 1). The height is not on [`TermParser`]; `text`
/// returns every scrollback + screen row (blank rows kept), so the screen is that count minus the scrollback.
fn page(term: &dyn TermParser) -> usize {
    let all = term.text(usize::MAX).split('\n').count();
    all.saturating_sub(term.scrollback_len())
        .saturating_sub(1)
        .max(1)
}

/// The nearest hit below view row 0 of the view at `offset` (the largest hit offset `< offset`), by walking
/// [`TermParser::search_up`] from the live screen.
fn search_down(term: &dyn TermParser, needle: &str, offset: usize) -> Option<usize> {
    let (mut best, mut from) = (None, 0);
    while let Some(o) = term.search_up(needle, from) {
        if o >= offset {
            break;
        }
        best = Some(o);
        from = o;
    }
    best
}

#[cfg(test)]
mod tests {
    use super::*;
    use fleetmux_term::alacritty;

    /// 20×5 terminal that printed `L1` … `L30` (line 7 = `NEEDLE`, line 20 = `NEEDLE` again), cursor on a blank
    /// last row: 26 scrollback rows.
    fn term() -> alacritty::Alacritty {
        let mut t = alacritty::new(20, 5, 1000);
        for i in 1..=30 {
            let line = if i == 7 || i == 20 {
                "NEEDLE".to_string()
            } else {
                format!("L{i}")
            };
            t.feed(format!("{line}\r\n").as_bytes());
        }
        t
    }

    fn mode() -> PageMode {
        PageMode {
            pane: PaneId::from("w1"),
            query: String::new(),
            typing: false,
        }
    }

    #[test]
    fn pgup_pgdn_move_by_rows_minus_one_and_clamp() {
        let (mut t, mut m) = (term(), mode());
        assert_eq!(t.scrollback_len(), 26);
        assert_eq!(on_key(&mut m, &mut t, b"\x1b[5~"), PageAction::Redraw);
        assert_eq!(t.display_offset(), 4);
        assert_eq!(on_key(&mut m, &mut t, b"\x1b[A\x1bOA"), PageAction::Redraw);
        assert_eq!(t.display_offset(), 6);
        assert_eq!(on_key(&mut m, &mut t, b"\x1b[B"), PageAction::Redraw);
        assert_eq!(t.display_offset(), 5);
        assert_eq!(
            on_key(&mut m, &mut t, b"\x1b[6~\x1b[6~"),
            PageAction::Redraw
        );
        assert_eq!(t.display_offset(), 0);
        assert_eq!(
            on_key(&mut m, &mut t, b"\x1b[6~"),
            PageAction::Stay,
            "already at the bottom"
        );
        for _ in 0..10 {
            on_key(&mut m, &mut t, b"\x1b[5~");
        }
        assert_eq!(t.display_offset(), 26, "clamped to the scrollback");
        assert_eq!(on_key(&mut m, &mut t, b"\x1b[5~"), PageAction::Stay);
    }

    #[test]
    fn q_and_esc_exit_with_offset_reset() {
        for key in [&b"q"[..], b"\x1b"] {
            let (mut t, mut m) = (term(), mode());
            on_key(&mut m, &mut t, b"\x1b[5~");
            assert_eq!(on_key(&mut m, &mut t, key), PageAction::Exit, "{key:?}");
            assert_eq!(t.display_offset(), 0);
        }
        // Keys after the exit in the same frame are dropped, not applied.
        let (mut t, mut m) = (term(), mode());
        assert_eq!(on_key(&mut m, &mut t, b"q\x1b[5~"), PageAction::Exit);
        assert_eq!(t.display_offset(), 0);
    }

    #[test]
    fn slash_search_then_n_and_shift_n() {
        let (mut t, mut m) = (term(), mode());
        assert_eq!(on_key(&mut m, &mut t, b"/"), PageAction::Redraw);
        assert!(m.typing);
        // `q`, `n` while typing are text; Backspace edits.
        assert_eq!(on_key(&mut m, &mut t, b"NEEDLX\x7fE"), PageAction::Redraw);
        assert_eq!(m.query, "NEEDLE");
        assert_eq!(t.display_offset(), 0, "typing does not move yet");
        assert_eq!(on_key(&mut m, &mut t, b"\r"), PageAction::Redraw);
        assert!(!m.typing);
        let newer = t.display_offset();
        assert!(newer > 0);
        assert_eq!(t.cell(0, 0).ch, 'N', "the hit line is at view row 0");
        assert_eq!(on_key(&mut m, &mut t, b"n"), PageAction::Redraw);
        let older = t.display_offset();
        assert!(older > newer);
        assert_eq!(t.cell(0, 0).ch, 'N');
        assert_eq!(
            on_key(&mut m, &mut t, b"n"),
            PageAction::Stay,
            "no older hit"
        );
        assert_eq!(on_key(&mut m, &mut t, b"N"), PageAction::Redraw);
        assert_eq!(t.display_offset(), newer);
        assert_eq!(
            on_key(&mut m, &mut t, b"N"),
            PageAction::Stay,
            "no newer hit"
        );
    }

    #[test]
    fn one_frame_search_and_esc_while_typing_stays_in_page_mode() {
        let (mut t, mut m) = (term(), mode());
        assert_eq!(on_key(&mut m, &mut t, b"/NEEDLE\r"), PageAction::Redraw);
        assert!(t.display_offset() > 0);
        assert_eq!(on_key(&mut m, &mut t, b"/ab\x1b"), PageAction::Redraw);
        assert!(!m.typing);
        assert_eq!(m.query, "ab");
        // Unknown keys are ignored, nothing exits.
        assert_eq!(
            on_key(&mut m, &mut t, b"x\x01\x1b[C\x1bx"),
            PageAction::Stay
        );
        // A search with no hit only closes the input.
        let before = t.display_offset();
        assert_eq!(
            on_key(&mut m, &mut t, "/漢\r".as_bytes()),
            PageAction::Redraw
        );
        assert_eq!(m.query, "漢");
        assert_eq!(t.display_offset(), before);
    }
}
