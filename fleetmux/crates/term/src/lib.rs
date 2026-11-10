//! fleetmux-term: the terminal-parsing layer behind one interface, plus the pure input encoders.
//!
//! - [`TermParser`] is the only thing the server knows about a terminal emulator (design.md §6.2): feed bytes,
//!   read cells / cursor / mode bits, collect the replies the inner program asked for, resize, read text.
//!   [`alacritty::Alacritty`] is the first implementation; it can be swapped (e.g. for libghostty-vt) later.
//! - [`encode_key`], [`encode_mouse`] and [`wrap_paste`] turn fleet / viewer input into the bytes written to a
//!   pane's PTY, following the inner program's current [`Modes`] (design.md §2.6).
//!
//! Phase 0 (plan.md §3.2): signatures only, every body is `todo!()`; the tests in `tests/` are red until M0.

/// A colour as the inner program set it. `Default` = the terminal's default foreground / background.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub enum Color {
    #[default]
    Default,
    /// 256-colour palette index (0–15 are the named ANSI colours).
    Indexed(u8),
    /// 24-bit truecolour (`SGR 38;2;r;g;b` / `48;2;r;g;b`).
    Rgb(u8, u8, u8),
}

/// SGR text attributes of a cell.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct Attrs {
    pub bold: bool,
    pub dim: bool,
    pub italic: bool,
    pub underline: bool,
    pub inverse: bool,
    pub hidden: bool,
    pub strikeout: bool,
}

/// One screen cell.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Cell {
    pub ch: char,
    /// 1 = normal, 2 = wide character, 0 = placeholder cell following a wide character.
    pub width: u8,
    pub fg: Color,
    pub bg: Color,
    pub attrs: Attrs,
}

/// Which mouse events the inner program asked to receive.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum MouseMode {
    /// `?1000`: press / release (and wheel).
    Click,
    /// `?1002`: also motion while a button is held.
    Drag,
    /// `?1003`: also motion with no button held.
    Motion,
}

/// The inner program's mode bits that change how input is encoded or how the screen is drawn.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct Modes {
    /// `?1049` (alternate screen).
    pub alt_screen: bool,
    /// `?2004`.
    pub bracketed_paste: bool,
    /// `?1000` / `?1002` / `?1003`; `None` = mouse reporting off.
    pub mouse: Option<MouseMode>,
    /// `?1006` (SGR mouse encoding; off = X10 encoding).
    pub sgr_mouse: bool,
    /// `?1` (DECCKM, application cursor keys).
    pub app_cursor: bool,
    /// `?1004`.
    pub focus_events: bool,
    /// `?2026` synchronized update in progress.
    pub sync_update: bool,
}

/// Cursor position (0-based, within the visible screen).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct Cursor {
    pub row: u16,
    pub col: u16,
    pub visible: bool,
}

/// Characters that end a word for a double click besides whitespace: Ghostty's default `selection-word-chars`
/// (cmux), so paths and SHAs select whole while `file:line`, `$VAR` and box-drawn borders split.
pub const WORD_BOUNDARY: &str = "'\"│`|:;,()[]{}<>$";

/// A terminal emulator as the server sees it.
pub trait TermParser {
    /// Feed bytes read from the PTY. `feed(&[])` only runs timers (e.g. the 150 ms `?2026` timeout).
    fn feed(&mut self, bytes: &[u8]);
    /// The cell at (`row`, `col`) of the visible screen, 0-based.
    fn cell(&self, row: u16, col: u16) -> Cell;
    fn cursor(&self) -> Cursor;
    /// Current mode bits.
    fn modes(&self) -> Modes;
    /// Bytes the terminal must write back to the PTY (answers to `CSI 6n`, DA, OSC 10 / 11); drains them.
    fn take_replies(&mut self) -> Vec<u8>;
    fn resize(&mut self, cols: u16, rows: u16);
    /// The last `lines` lines of scrollback + screen as physical rows, `\n`-joined. Only each row's trailing
    /// whitespace is stripped; blank rows (including trailing blank screen rows) are kept, not dropped.
    fn text(&self, lines: usize) -> String;
    /// Whether anything visible changed since the last call; resets the flag.
    fn take_dirty(&mut self) -> bool;

    // ---- Scrollback view, selection, search (stage A, brief-m03 §3.3; scroll / copy / page mode, M1). ----
    //
    // "View" = the visible screen scrolled up by `display_offset()` rows into the scrollback. At offset `o`,
    // view row 0 is the line `o` rows above the top row of the live screen. All (row, col) pairs below are
    // view coordinates, 0-based, row first — the same order as [`TermParser::cell`].

    /// Lines currently held in the scrollback (above the live screen); `0` on the alternate screen.
    fn scrollback_len(&self) -> usize;
    /// Scroll the view up `rows` lines into the scrollback, clamped to `scrollback_len()`; `0` = live screen.
    /// While the offset is > 0, [`TermParser::cell`] reads the scrolled view. New output does not reset it.
    fn set_display_offset(&mut self, rows: usize);
    /// Current view offset (always ≤ `scrollback_len()`).
    fn display_offset(&self) -> usize;
    /// Text of the view between `from` and `to` = (row, col), both inclusive and in either order (a drag
    /// upwards gives `from` after `to`). Stream selection like a terminal: the first row from its column to the
    /// row end, middle rows whole, the last row up to its column. Wide-character placeholder cells are skipped;
    /// each row's trailing whitespace is stripped; rows are joined with `\n`, except that a row which soft-wraps
    /// into the next (the inner program's text ran past the right edge) is joined to it unstripped, with no `\n`.
    fn selection_text(&self, from: (u16, u16), to: (u16, u16)) -> String;
    /// The word under view cell `at` = (row, col), for a double click (PROTOCOL.md §11): the longest run of cells
    /// around it holding no [`WORD_BOUNDARY`] character and no whitespace, following soft wraps, as the
    /// (from, to) pair [`TermParser::selection_text`] takes. A wide character's placeholder cell counts as the
    /// character. `None` when `at` itself is a boundary, a blank or outside the view.
    fn word_at(&self, at: (u16, u16)) -> Option<((u16, u16), (u16, u16))>;
    /// The logical line through view row `row`, for a triple click: from column 0 of its first row to the last
    /// column of its last row, following soft wraps (clamped to the view), as a (from, to) pair for
    /// [`TermParser::selection_text`].
    fn line_at(&self, row: u16) -> ((u16, u16), (u16, u16));
    /// Search upwards for `needle` (plain, case-sensitive substring of a row's text), looking only at lines
    /// above view row 0 of the view at `from_offset`, nearest first. Returns the offset that puts the hit line
    /// at view row 0 (always > `from_offset`), or `None`. Repeat with the returned offset for the next older hit.
    fn search_up(&self, needle: &str, from_offset: usize) -> Option<usize>;
    /// Approximate heap bytes held by this terminal (screen + scrollback grids, pending replies); for the
    /// `scrollback_rows` / memory budget of design.md §7.3 M3 (≤ 50 MB per 10,000 × 120 pane).
    fn memory_estimate(&self) -> usize;
}

pub mod color;

mod width;
pub use width::cell_width;

mod encoders;
pub use encoders::{
    MouseButton, MouseEvent, MouseKind, Warning, WriteMode, encode_key, encode_mouse, wrap_paste,
};

pub mod alacritty;
pub use alacritty::Palette;
