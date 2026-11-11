//! `ByteBackend`: a ratatui [`Backend`] that writes the escape sequences for each draw into a byte buffer
//! instead of a terminal (design.md §2.7); the server sends the buffer to one viewer as `0x81` frames.
//! Colours are emitted per the viewer's [`Depth`] (RGB as is, or downgraded with `color::to_256`).
//!
//! `Depth::Rgb` sends RGB as is; `Depth::Indexed256` downgrades it with `color::to_256` (B16, in
//! [`push_color`]); indexed and named colours pass through either way.
//!
//! Cursor tracking: after an ASCII symbol the outer cursor is known to be one column further; after any other
//! symbol it is treated as unknown and the next cell gets an explicit `CUP`. That costs a few bytes on box
//! drawing and wide characters but keeps the picture right even when the outer terminal's width table disagrees
//! with ours (design.md §7.4).
use std::io::{self, Write as _};

use fleetmux_term::Color as TColor;
use fleetmux_term::color::{Depth, to_256};
use ratatui::backend::{Backend, ClearType, WindowSize};
use ratatui::buffer::Cell;
use ratatui::layout::{Position, Size};
use ratatui::style::{Color, Modifier};

/// A virtual `cols`×`rows` terminal whose output accumulates in memory until [`ByteBackend::take`].
pub struct ByteBackend {
    cols: u16,
    rows: u16,
    depth: Depth,
    out: Vec<u8>,
    cursor: Position,
    /// Whether the outer terminal's cursor is known to be at `cursor`.
    cursor_known: bool,
    /// The SGR state last emitted; `None` = unknown (emit before the next cell).
    style: Option<(Color, Color, Modifier)>,
}

impl ByteBackend {
    pub fn new(cols: u16, rows: u16, depth: Depth) -> Self {
        ByteBackend {
            cols,
            rows,
            depth,
            out: Vec::new(),
            cursor: Position::new(0, 0),
            cursor_known: false,
            style: None,
        }
    }

    /// The bytes written since the last `take`; leaves the buffer empty.
    pub fn take(&mut self) -> Vec<u8> {
        std::mem::take(&mut self.out)
    }

    /// The viewer's colour depth this backend was built for.
    pub fn depth(&self) -> Depth {
        self.depth
    }

    fn move_to(&mut self, x: u16, y: u16) {
        if !(self.cursor_known && self.cursor == Position::new(x, y)) {
            let _ = write!(self.out, "\x1b[{};{}H", y as u32 + 1, x as u32 + 1);
        }
        self.cursor = Position::new(x, y);
        self.cursor_known = true;
    }

    fn set_style(&mut self, fg: Color, bg: Color, m: Modifier) {
        if self.style == Some((fg, bg, m)) {
            return;
        }
        let mut s = String::from("\x1b[0");
        for (bit, code) in [
            (Modifier::BOLD, "1"),
            (Modifier::DIM, "2"),
            (Modifier::ITALIC, "3"),
            (Modifier::UNDERLINED, "4"),
            (Modifier::SLOW_BLINK, "5"),
            (Modifier::RAPID_BLINK, "6"),
            (Modifier::REVERSED, "7"),
            (Modifier::HIDDEN, "8"),
            (Modifier::CROSSED_OUT, "9"),
        ] {
            if m.contains(bit) {
                s.push(';');
                s.push_str(code);
            }
        }
        push_color(&mut s, fg, false, self.depth);
        push_color(&mut s, bg, true, self.depth);
        s.push('m');
        self.out.extend_from_slice(s.as_bytes());
        self.style = Some((fg, bg, m));
    }
}

/// Append `;<sgr>` for `c` as foreground or background; `Reset` adds nothing (the leading `0` reset it). RGB is
/// downgraded to the 256-colour palette when `depth` is `Indexed256`.
fn push_color(s: &mut String, c: Color, bg: bool, depth: Depth) {
    use std::fmt::Write as _;
    let base = if bg { 40 } else { 30 };
    let named = |n: u8| -> String {
        if n < 8 {
            format!("{}", base + n as u32)
        } else {
            format!("{}", base + 60 + (n - 8) as u32)
        }
    };
    let code = match c {
        Color::Reset => return,
        Color::Black => named(0),
        Color::Red => named(1),
        Color::Green => named(2),
        Color::Yellow => named(3),
        Color::Blue => named(4),
        Color::Magenta => named(5),
        Color::Cyan => named(6),
        Color::Gray => named(7),
        Color::DarkGray => named(8),
        Color::LightRed => named(9),
        Color::LightGreen => named(10),
        Color::LightYellow => named(11),
        Color::LightBlue => named(12),
        Color::LightMagenta => named(13),
        Color::LightCyan => named(14),
        Color::White => named(15),
        Color::Indexed(n) => format!("{};5;{n}", base + 8),
        Color::Rgb(r, g, b) => match (depth, to_256(TColor::Rgb(r, g, b))) {
            (Depth::Indexed256, TColor::Indexed(n)) => format!("{};5;{n}", base + 8),
            _ => format!("{};2;{r};{g};{b}", base + 8),
        },
    };
    let _ = write!(s, ";{code}");
}

impl Backend for ByteBackend {
    type Error = io::Error;

    fn draw<'a, I>(&mut self, content: I) -> Result<(), Self::Error>
    where
        I: Iterator<Item = (u16, u16, &'a Cell)>,
    {
        for (x, y, cell) in content {
            if x >= self.cols || y >= self.rows {
                continue;
            }
            self.move_to(x, y);
            self.set_style(cell.fg, cell.bg, cell.modifier);
            let sym = cell.symbol();
            let sym = if sym.is_empty() { " " } else { sym };
            self.out.extend_from_slice(sym.as_bytes());
            let ascii = sym.len() == 1
                && (sym.as_bytes()[0] == b' ' || sym.as_bytes()[0].is_ascii_graphic());
            if ascii {
                self.cursor.x = x + 1;
                // The cursor stays in the last column after writing it (pending wrap): unknown from here.
                self.cursor_known = self.cursor.x < self.cols;
            } else {
                self.cursor_known = false;
            }
        }
        Ok(())
    }

    fn hide_cursor(&mut self) -> Result<(), Self::Error> {
        self.out.extend_from_slice(b"\x1b[?25l");
        Ok(())
    }

    fn show_cursor(&mut self) -> Result<(), Self::Error> {
        self.out.extend_from_slice(b"\x1b[?25h");
        Ok(())
    }

    fn get_cursor_position(&mut self) -> Result<Position, Self::Error> {
        Ok(self.cursor)
    }

    fn set_cursor_position<P: Into<Position>>(&mut self, position: P) -> Result<(), Self::Error> {
        let p = position.into();
        self.cursor_known = false;
        self.move_to(p.x, p.y);
        Ok(())
    }

    fn clear(&mut self) -> Result<(), Self::Error> {
        self.clear_region(ClearType::All)
    }

    fn clear_region(&mut self, clear_type: ClearType) -> Result<(), Self::Error> {
        // Erase uses the current background, so reset the attributes first.
        self.out.extend_from_slice(b"\x1b[0m");
        self.style = Some((Color::Reset, Color::Reset, Modifier::empty()));
        let seq: &[u8] = match clear_type {
            ClearType::All => b"\x1b[H\x1b[2J",
            ClearType::AfterCursor => b"\x1b[J",
            ClearType::BeforeCursor => b"\x1b[1J",
            ClearType::CurrentLine => b"\x1b[2K",
            ClearType::UntilNewLine => b"\x1b[K",
        };
        self.out.extend_from_slice(seq);
        if matches!(clear_type, ClearType::All) {
            self.cursor = Position::new(0, 0);
            self.cursor_known = true;
        }
        Ok(())
    }

    fn size(&self) -> Result<Size, Self::Error> {
        Ok(Size::new(self.cols, self.rows))
    }

    fn window_size(&mut self) -> Result<WindowSize, Self::Error> {
        Ok(WindowSize {
            columns_rows: Size::new(self.cols, self.rows),
            pixels: Size::new(0, 0),
        })
    }

    fn flush(&mut self) -> Result<(), Self::Error> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::buffer::Buffer;
    use ratatui::layout::Rect;
    use ratatui::style::Style;

    fn text(b: &[u8]) -> String {
        String::from_utf8_lossy(b).into_owned()
    }

    #[test]
    fn draw_positions_styles_and_tracks_ascii_cursor() {
        let mut b = ByteBackend::new(10, 3, Depth::Rgb);
        let mut buf = Buffer::empty(Rect::new(0, 0, 10, 3));
        buf.set_string(2, 1, "ab", Style::default().fg(Color::Red));
        let empty = Buffer::empty(buf.area);
        b.draw(empty.diff(&buf).into_iter()).unwrap();
        // One CUP for the run, one SGR, then both symbols.
        assert_eq!(text(&b.take()), "\x1b[2;3H\x1b[0;31mab");
        assert!(b.take().is_empty(), "take drains the buffer");
    }

    #[test]
    fn non_ascii_symbol_forces_explicit_position_and_rgb_is_kept() {
        let mut b = ByteBackend::new(10, 1, Depth::Rgb);
        let mut buf = Buffer::empty(Rect::new(0, 0, 10, 1));
        buf.set_string(
            0,
            0,
            "│x",
            Style::default()
                .fg(Color::Rgb(1, 2, 3))
                .add_modifier(Modifier::BOLD),
        );
        b.draw(Buffer::empty(buf.area).diff(&buf).into_iter())
            .unwrap();
        assert_eq!(text(&b.take()), "\x1b[1;1H\x1b[0;1;38;2;1;2;3m│\x1b[1;2Hx");
    }

    #[test]
    fn indexed256_downgrades_rgb_and_keeps_indexed() {
        let mut b = ByteBackend::new(10, 1, Depth::Indexed256);
        let mut buf = Buffer::empty(Rect::new(0, 0, 10, 1));
        buf.set_string(
            0,
            0,
            "a",
            Style::default()
                .fg(Color::Rgb(255, 128, 0))
                .bg(Color::Indexed(17)),
        );
        b.draw(Buffer::empty(buf.area).diff(&buf).into_iter())
            .unwrap();
        let out = text(&b.take());
        assert_eq!(out, "\x1b[1;1H\x1b[0;38;5;214;48;5;17ma");
        assert!(!out.contains("38;2;"), "{out:?}");
    }

    #[test]
    fn clear_cursor_and_size() {
        let mut b = ByteBackend::new(80, 24, Depth::Rgb);
        b.clear().unwrap();
        b.set_cursor_position((4, 2)).unwrap();
        b.hide_cursor().unwrap();
        b.show_cursor().unwrap();
        assert_eq!(
            text(&b.take()),
            "\x1b[0m\x1b[H\x1b[2J\x1b[3;5H\x1b[?25l\x1b[?25h"
        );
        assert_eq!(b.size().unwrap(), Size::new(80, 24));
        assert_eq!(b.get_cursor_position().unwrap(), Position::new(4, 2));
    }
}
