//! [`TermParser`] backed by `alacritty_terminal` 0.26 and the `vte` 0.15 it re-exports.
//!
//! - The [`Term`] holds the grids (screen + scrollback, and the alternate screen); a `vte` [`Processor`] parses
//!   the PTY bytes into it. We do not use alacritty's own event loop, so two of its jobs live here:
//!   - **Replies.** The terminal reports `CSI 6n` / DA / `CSI 18t` as `Event::PtyWrite` and OSC 10 / 11 / 4 / 12
//!     colour queries as `Event::ColorRequest`; [`Alacritty::feed`] turns them into bytes, in order, for
//!     [`TermParser::take_replies`]. The kitty keyboard protocol stays off (`Config::kitty_keyboard = false`), so
//!     `CSI ? u` gets no answer (design.md §2.6).
//!   - **The `?2026` timeout.** `vte` only arms a deadline (150 ms after each `CSI ?2026h`) and never checks it;
//!     every `feed`, including `feed(&[])`, first releases a synchronized update whose deadline has passed.
//!     Ordinary output inside the update does not move the deadline; only a new `CSI ?2026h` does (vte rule).
//! - "View" = the live screen scrolled up by `display_offset()` rows. [`TermParser::cell`] and
//!   [`TermParser::selection_text`] read the view; [`TermParser::text`] always reads the real rows
//!   (scrollback + live screen), whatever the offset.
//!
//! Colour queries (OSC 10 / 11 / 12 / 4) are answered from a [`Palette`]; the default is [`Palette::CMUX`] (the cmux
//! 0.65.0 colours), and [`with_palette`] supplies another one.

use std::mem;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Instant;

use alacritty_terminal::event::{Event, EventListener, WindowSize};
use alacritty_terminal::grid::{Dimensions, Grid, Scroll};
use alacritty_terminal::index::{Column, Line};
use alacritty_terminal::term::cell::{Cell as GridCell, Flags};
use alacritty_terminal::term::{Config, Term, TermMode};
use alacritty_terminal::vte::ansi::{self, NamedColor, Processor, Rgb, StdSyncHandler};

use crate::{Attrs, Cell, Color, Cursor, Modes, MouseMode, TermParser};

/// `vte` 0.15 `SYNC_BUFFER_SIZE` (private there): every `Processor` allocates this much for `?2026` buffering.
const SYNC_BUFFER_BYTES: usize = 0x20_0000;
/// Heap bookkeeping per grid row besides its cells (`Row { inner: Vec<Cell>, occ: usize }`).
const ROW_OVERHEAD: usize = mem::size_of::<Vec<GridCell>>() + mem::size_of::<usize>();
/// Default foreground / background answered to OSC 10 / 11 / 12 / 4 when the inner program never set them: the cmux
/// 0.65.0 palette (`Palette::CMUX`), overridable with [`with_palette`]. `Copy` so the server can hand it to every
/// pane; `PartialEq` / `Debug` so `server::Opts` keeps its derives and tests can compare palettes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Palette {
    pub fg: (u8, u8, u8),
    pub bg: (u8, u8, u8),
    pub cursor: (u8, u8, u8),
    pub ansi: [(u8, u8, u8); 16],
}

impl Palette {
    /// cmux 0.65.0 measured on this machine (OSC 10 / 11 / 12 / 4 queries); the only copy of these values in Rust.
    pub const CMUX: Palette = Palette {
        bg: (0x1e, 0x1e, 0x1e),
        fg: (0xff, 0xff, 0xff),
        cursor: (0x98, 0x98, 0x9d),
        ansi: [
            (0x1a, 0x1a, 0x1a),
            (0xcc, 0x37, 0x2e),
            (0x26, 0xa4, 0x39),
            (0xcd, 0xac, 0x08),
            (0x08, 0x69, 0xcb),
            (0x96, 0x47, 0xbf),
            (0x47, 0x9e, 0xc2),
            (0x98, 0x98, 0x9d),
            (0x46, 0x46, 0x46),
            (0xff, 0x45, 0x3a),
            (0x32, 0xd7, 0x4b),
            (0xff, 0xd6, 0x0a),
            (0x0a, 0x84, 0xff),
            (0xbf, 0x5a, 0xf2),
            (0x76, 0xd6, 0xff),
            (0xff, 0xff, 0xff),
        ],
    };
}
/// Cell size in pixels reported to `CSI 14t` (there are no real pixels behind a fleetmux pane).
const CELL_PX: (u16, u16) = (8, 16);

const BLANK: Cell = Cell {
    ch: ' ',
    width: 1,
    fg: Color::Default,
    bg: Color::Default,
    attrs: Attrs {
        bold: false,
        dim: false,
        italic: false,
        underline: false,
        inverse: false,
        hidden: false,
        strikeout: false,
    },
};

/// Collects the terminal events that need an answer written back to the PTY; everything else is dropped.
#[derive(Clone, Default)]
struct Events(Arc<Mutex<Vec<Event>>>);

impl EventListener for Events {
    fn send_event(&self, event: Event) {
        if matches!(
            event,
            Event::PtyWrite(_) | Event::ColorRequest(..) | Event::TextAreaSizeRequest(_)
        ) {
            self.0
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .push(event);
        }
    }
}

/// Grid size handed to alacritty (`total_lines` = screen; scrollback comes from `Config`).
struct Size {
    cols: usize,
    rows: usize,
}

impl Dimensions for Size {
    fn total_lines(&self) -> usize {
        self.rows
    }

    fn screen_lines(&self) -> usize {
        self.rows
    }

    fn columns(&self) -> usize {
        self.cols
    }
}

impl Size {
    /// Clamped so a degenerate resize cannot panic the grid (a wide char needs 2 columns).
    fn new(cols: u16, rows: u16) -> Self {
        Self {
            cols: usize::from(cols.max(2)),
            rows: usize::from(rows.max(1)),
        }
    }
}

pub struct Alacritty {
    term: Term<Events>,
    parser: Processor<StdSyncHandler>,
    events: Events,
    /// Colours answered to OSC 10 / 11 / 12 / 4 when the inner program never set them.
    palette: Palette,
    /// Answers not yet taken by [`TermParser::take_replies`].
    replies: Vec<u8>,
    dirty: bool,
    /// Scrollback rows of the primary screen, remembered while the alternate screen is active (the grid alacritty
    /// exposes is the active one); for [`TermParser::memory_estimate`].
    primary_history: usize,
}

// `server::pane` stores parsers as `Box<dyn TermParser + Send>`.
const _: fn() = || {
    fn send<T: Send>() {}
    send::<Alacritty>();
};

impl Alacritty {
    fn grid(&self) -> &Grid<GridCell> {
        self.term.grid()
    }

    /// Deadline of the synchronized update in progress, if any.
    fn sync_deadline(&self) -> Option<Instant> {
        self.parser.sync_timeout().sync_timeout()
    }

    /// Turn the queued reply events into bytes, in the order the queries arrived.
    fn collect_replies(&mut self) {
        let events = mem::take(&mut *self.events.0.lock().unwrap_or_else(PoisonError::into_inner));
        for event in events {
            let text = match event {
                Event::PtyWrite(text) => text,
                Event::ColorRequest(index, format) => {
                    format(
                        self.term.colors()[index]
                            .unwrap_or_else(|| default_rgb(index, &self.palette)),
                    )
                }
                Event::TextAreaSizeRequest(format) => format(WindowSize {
                    num_lines: self.grid().screen_lines() as u16,
                    num_cols: self.grid().columns() as u16,
                    cell_width: CELL_PX.0,
                    cell_height: CELL_PX.1,
                }),
                _ => continue,
            };
            self.replies.extend_from_slice(text.as_bytes());
        }
    }

    /// Text of grid line `line` (absolute: negative = scrollback), columns `from..=to`; placeholder cells skipped,
    /// combining characters kept, trailing whitespace stripped.
    fn line_text(&self, line: Line, from: usize, to: usize) -> String {
        let mut s = self.cells_text(line, from, to);
        s.truncate(s.trim_end().len());
        s
    }

    /// [`Alacritty::line_text`] without stripping trailing whitespace.
    fn cells_text(&self, line: Line, from: usize, to: usize) -> String {
        let row = &self.grid()[line];
        let mut s = String::new();
        for col in from..=to.min(self.grid().columns() - 1) {
            let cell = &row[Column(col)];
            if cell
                .flags
                .intersects(Flags::WIDE_CHAR_SPACER | Flags::LEADING_WIDE_CHAR_SPACER)
            {
                continue;
            }
            s.push(cell.c);
            if let Some(extra) = cell.zerowidth() {
                s.extend(extra);
            }
        }
        s
    }

    /// The grid cell at view (`row`, `col`); both must be inside the view.
    fn view_cell(&self, row: usize, col: usize) -> &GridCell {
        let grid = self.grid();
        &grid[Line(row as i32 - grid.display_offset() as i32)][Column(col)]
    }

    /// View row `row` soft-wraps into the next one (alacritty marks its last cell `WRAPLINE`).
    fn wraps(&self, row: usize) -> bool {
        let grid = self.grid();
        row < grid.screen_lines()
            && self
                .view_cell(row, grid.columns() - 1)
                .flags
                .contains(Flags::WRAPLINE)
    }

    /// View cell (`row`, `col`) is part of a word ([`TermParser::word_at`]).
    fn is_word(&self, row: usize, col: usize) -> bool {
        let cell = self.view_cell(row, col);
        if cell.flags.contains(Flags::WIDE_CHAR_SPACER) {
            return col > 0 && self.is_word(row, col - 1);
        }
        if cell.flags.contains(Flags::LEADING_WIDE_CHAR_SPACER) {
            return true; // the blank before a wide character that wrapped to the next row
        }
        let c = cell.c;
        c != '\0' && !c.is_whitespace() && !crate::WORD_BOUNDARY.contains(c)
    }
}

impl TermParser for Alacritty {
    fn feed(&mut self, bytes: &[u8]) {
        let mut changed = false;
        if self
            .sync_deadline()
            .is_some_and(|deadline| Instant::now() >= deadline)
        {
            self.parser.stop_sync(&mut self.term);
            changed = true;
        }
        if !bytes.is_empty() {
            let held = self.sync_deadline().is_some();
            let buffered = self.parser.sync_bytes_count();
            self.parser.advance(&mut self.term, bytes);
            // Bytes that only went into the `?2026` buffer change nothing visible yet.
            let only_buffered = held
                && self.sync_deadline().is_some()
                && self.parser.sync_bytes_count() == buffered + bytes.len();
            changed |= !only_buffered;
        }
        self.collect_replies();
        if !self.term.mode().contains(TermMode::ALT_SCREEN) {
            self.primary_history = self.grid().history_size();
        }
        self.dirty |= changed;
    }

    fn cell(&self, row: u16, col: u16) -> Cell {
        let grid = self.grid();
        if usize::from(row) >= grid.screen_lines() || usize::from(col) >= grid.columns() {
            return BLANK;
        }
        let line = Line(i32::from(row) - grid.display_offset() as i32);
        let cell = &grid[line][Column(usize::from(col))];
        let flags = cell.flags;
        Cell {
            ch: cell.c,
            width: if flags.contains(Flags::WIDE_CHAR) {
                2
            } else if flags.contains(Flags::WIDE_CHAR_SPACER) {
                0
            } else {
                1
            },
            fg: color(cell.fg),
            bg: color(cell.bg),
            attrs: Attrs {
                bold: flags.contains(Flags::BOLD),
                dim: flags.contains(Flags::DIM),
                italic: flags.contains(Flags::ITALIC),
                underline: flags.intersects(Flags::ALL_UNDERLINES),
                inverse: flags.contains(Flags::INVERSE),
                hidden: flags.contains(Flags::HIDDEN),
                strikeout: flags.contains(Flags::STRIKEOUT),
            },
        }
    }

    /// Position on the live screen; reported invisible while the view is scrolled back (offset > 0).
    fn cursor(&self) -> Cursor {
        let point = self.grid().cursor.point;
        Cursor {
            row: point.line.0.max(0) as u16,
            col: point.column.0 as u16,
            visible: self.term.mode().contains(TermMode::SHOW_CURSOR) && self.display_offset() == 0,
        }
    }

    fn modes(&self) -> Modes {
        let mode = *self.term.mode();
        let mouse = if mode.contains(TermMode::MOUSE_MOTION) {
            Some(MouseMode::Motion)
        } else if mode.contains(TermMode::MOUSE_DRAG) {
            Some(MouseMode::Drag)
        } else if mode.contains(TermMode::MOUSE_REPORT_CLICK) {
            Some(MouseMode::Click)
        } else {
            None
        };
        Modes {
            alt_screen: mode.contains(TermMode::ALT_SCREEN),
            bracketed_paste: mode.contains(TermMode::BRACKETED_PASTE),
            mouse,
            sgr_mouse: mode.contains(TermMode::SGR_MOUSE),
            app_cursor: mode.contains(TermMode::APP_CURSOR),
            focus_events: mode.contains(TermMode::FOCUS_IN_OUT),
            // True from `CSI ?2026h` until the update is released by `?2026l` or by a `feed` after the deadline.
            sync_update: self.sync_deadline().is_some(),
        }
    }

    fn take_replies(&mut self) -> Vec<u8> {
        mem::take(&mut self.replies)
    }

    fn resize(&mut self, cols: u16, rows: u16) {
        self.term.resize(Size::new(cols, rows));
        self.dirty = true;
    }

    fn text(&self, lines: usize) -> String {
        let grid = self.grid();
        let (top, bottom) = (grid.topmost_line().0, grid.bottommost_line().0);
        let total = (bottom - top + 1) as usize;
        let start = bottom + 1 - lines.min(total) as i32;
        let last_col = grid.columns() - 1;
        (start..=bottom)
            .map(|line| self.line_text(Line(line), 0, last_col))
            .collect::<Vec<_>>()
            .join("\n")
    }

    fn take_dirty(&mut self) -> bool {
        mem::take(&mut self.dirty)
    }

    fn scrollback_len(&self) -> usize {
        self.grid().history_size()
    }

    fn set_display_offset(&mut self, rows: usize) {
        let target = rows.min(self.scrollback_len());
        let current = self.display_offset();
        if target != current {
            self.term
                .scroll_display(Scroll::Delta(target as i32 - current as i32));
            self.dirty = true;
        }
    }

    fn display_offset(&self) -> usize {
        self.grid().display_offset()
    }

    fn selection_text(&self, from: (u16, u16), to: (u16, u16)) -> String {
        let (start, end) = if from <= to { (from, to) } else { (to, from) };
        let grid = self.grid();
        let (rows, last_col) = (grid.screen_lines(), grid.columns() - 1);
        let offset = grid.display_offset() as i32;
        let mut out = String::new();
        for row in (start.0..=end.0).take_while(|&row| usize::from(row) < rows) {
            let first = if row == start.0 {
                usize::from(start.1)
            } else {
                0
            };
            let last = if row == end.0 {
                usize::from(end.1)
            } else {
                last_col
            };
            // A soft-wrapped row runs on into the next: no `\n`, and its trailing blanks are text.
            let wraps = row != end.0 && self.wraps(usize::from(row));
            if first <= last_col {
                let line = Line(i32::from(row) - offset);
                if wraps {
                    out.push_str(&self.cells_text(line, first, last));
                } else {
                    out.push_str(&self.line_text(line, first, last));
                }
            }
            if row != end.0 && !wraps {
                out.push('\n');
            }
        }
        out
    }

    fn word_at(&self, at: (u16, u16)) -> Option<((u16, u16), (u16, u16))> {
        let grid = self.grid();
        let (rows, cols) = (grid.screen_lines(), grid.columns());
        let (row, col) = (usize::from(at.0), usize::from(at.1));
        if row >= rows || col >= cols || !self.is_word(row, col) {
            return None;
        }
        // Walk left / right over word cells, crossing a row edge only where the upper row soft-wraps.
        let (mut s, mut e) = ((row, col), (row, col));
        loop {
            let prev = match s {
                (r, 0) if r > 0 && self.wraps(r - 1) => (r - 1, cols - 1),
                (_, 0) => break,
                (r, c) => (r, c - 1),
            };
            if !self.is_word(prev.0, prev.1) {
                break;
            }
            s = prev;
        }
        loop {
            let next = match e {
                (r, c) if c + 1 < cols => (r, c + 1),
                (r, _) if r + 1 < rows && self.wraps(r) => (r + 1, 0),
                _ => break,
            };
            if !self.is_word(next.0, next.1) {
                break;
            }
            e = next;
        }
        Some(((s.0 as u16, s.1 as u16), (e.0 as u16, e.1 as u16)))
    }

    fn line_at(&self, row: u16) -> ((u16, u16), (u16, u16)) {
        let grid = self.grid();
        let (rows, cols) = (grid.screen_lines(), grid.columns());
        let row = usize::from(row).min(rows - 1);
        let (mut first, mut last) = (row, row);
        while first > 0 && self.wraps(first - 1) {
            first -= 1;
        }
        while last + 1 < rows && self.wraps(last) {
            last += 1;
        }
        ((first as u16, 0), (last as u16, (cols - 1) as u16))
    }

    /// An empty `needle` matches nothing.
    fn search_up(&self, needle: &str, from_offset: usize) -> Option<usize> {
        if needle.is_empty() {
            return None;
        }
        let history = self.scrollback_len();
        let last_col = self.grid().columns() - 1;
        (from_offset.saturating_add(1)..=history).find(|&k| {
            self.line_text(Line(-(k as i32)), 0, last_col)
                .contains(needle)
        })
    }

    /// Cells of both screens and the scrollback (zero-width extras not counted), row bookkeeping, the `vte`
    /// `?2026` buffer and pending replies.
    fn memory_estimate(&self) -> usize {
        let grid = self.grid();
        let screen = grid.screen_lines();
        let rows = if self.term.mode().contains(TermMode::ALT_SCREEN) {
            // Active = alternate screen (no scrollback); the inactive primary keeps its scrollback.
            screen + self.primary_history + screen
        } else {
            grid.total_lines() + screen
        };
        rows * (grid.columns() * mem::size_of::<GridCell>() + ROW_OVERHEAD)
            + SYNC_BUFFER_BYTES
            + self.replies.capacity()
    }
}

/// alacritty's cell colour → ours: palette 0–15 and dim variants → `Indexed`, the default fg / bg → `Default`.
fn color(c: ansi::Color) -> Color {
    match c {
        ansi::Color::Spec(Rgb { r, g, b }) => Color::Rgb(r, g, b),
        ansi::Color::Indexed(i) => Color::Indexed(i),
        ansi::Color::Named(named) => {
            let i = named as usize;
            let dim = NamedColor::DimBlack as usize..=NamedColor::DimWhite as usize;
            if i < 16 {
                Color::Indexed(i as u8)
            } else if dim.contains(&i) {
                Color::Indexed((i - NamedColor::DimBlack as usize) as u8)
            } else {
                Color::Default
            }
        }
    }
}

/// xterm default colour for alacritty colour slot `index` (0–255 palette, then fg / bg / cursor / dim / bright fg),
/// taking the 16 base colours and the default fg / bg / cursor from `p`.
fn default_rgb(index: usize, p: &Palette) -> Rgb {
    const CUBE: [u8; 6] = [0, 95, 135, 175, 215, 255];
    let rgb = |(r, g, b): (u8, u8, u8)| Rgb { r, g, b };
    let dim = |c: Rgb| Rgb {
        r: c.r / 3 * 2,
        g: c.g / 3 * 2,
        b: c.b / 3 * 2,
    };
    const DIM_BLACK: usize = NamedColor::DimBlack as usize;
    match index {
        0..=15 => rgb(p.ansi[index]),
        16..=231 => {
            let i = index - 16;
            rgb((CUBE[i / 36], CUBE[i / 6 % 6], CUBE[i % 6]))
        }
        232..=255 => {
            let v = (8 + 10 * (index - 232)) as u8;
            rgb((v, v, v))
        }
        _ if index == NamedColor::Background as usize => rgb(p.bg),
        _ if index == NamedColor::Cursor as usize => rgb(p.cursor),
        _ if (DIM_BLACK..DIM_BLACK + 8).contains(&index) => dim(rgb(p.ansi[index - DIM_BLACK])),
        _ if index == NamedColor::DimForeground as usize => dim(rgb(p.fg)),
        _ => rgb(p.fg),
    }
}

/// A `cols` × `rows` terminal keeping `scrollback` lines of history, answering colour queries with `palette`.
pub fn with_palette(cols: u16, rows: u16, scrollback: usize, palette: Palette) -> Alacritty {
    let events = Events::default();
    let config = Config {
        scrolling_history: scrollback,
        kitty_keyboard: false,
        ..Config::default()
    };
    let term = Term::new(config, &Size::new(cols, rows), events.clone());
    Alacritty {
        term,
        parser: Processor::new(),
        events,
        palette,
        replies: Vec::new(),
        dirty: true,
        primary_history: 0,
    }
}

/// A `cols` × `rows` terminal keeping `scrollback` lines of history, answering colour queries with [`Palette::CMUX`].
pub fn new(cols: u16, rows: u16, scrollback: usize) -> Alacritty {
    with_palette(cols, rows, scrollback, Palette::CMUX)
}
