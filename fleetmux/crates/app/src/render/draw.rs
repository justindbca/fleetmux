//! The screen one viewer sees (design.md §2.7, §3.1; PROTOCOL.md §10–§14): three tab groups with borders, the
//! tabs sitting on the groups' border rows (content background, a thin `│` between neighbours), the focused
//! group highlighted, `•` on unseen tabs, the zoomed pane alone, the status line (page
//! mode shows `[offset/scrollback]`), diffed against the viewer's previous canvas.
//!
//! B7 (attach-render) draws borders, tab labels, the selected pane of each group (or the zoomed pane alone),
//! the focus highlight, the status line with the prefix hint and the focused pane's cursor. B16 adds the
//! separator highlight while one is dragged, the zoomed title and the page-mode `[offset/scrollback]` / search
//! text on the status line, and `•` in the marker cell left of an unseen tab's title (the 256-colour downgrade
//! is in `backend.rs`); B22 the perf work: `render_all` draws through [`draw_cached`], which copies a displayed
//! pane's cells from the viewer's previous canvas when that pane's parser reported no change (its generation in
//! `ViewerHub::pane_gen`) and its rectangle is the same, and parser cells are only read for the part of a pane
//! that is on this viewer's canvas. Pacing (`Throttle`, at most [`MAX_FPS`]) and the on-demand render loop are
//! B6 / B7's (`boot.rs::render`).
//!
//! Geometry: the layout is computed at the PTY size (`ViewerHub::layout_size`, cached by the hub so it stays
//! right while `render_all` has the viewer map taken out), and drawn onto this viewer's own `cols × rows`
//! canvas: a larger viewer shows blank margins, a smaller one is cropped (PROTOCOL.md §6.3). Tab labels use
//! `Layout::tab_rects_titled` only — the same rectangles `hit_test_titled` uses — and the real pane titles,
//! truncated to the rectangle's width. S1 draws the fleet sidebar ([`super::sidebar`]) over the layout's leftmost
//! `Layout::sidebar_cols` columns, after the groups and before the status line.
use std::time::{Duration, Instant};

use fleetmux_layout::{Group, Hit, Rect as LRect};
use fleetmux_proto::PaneId;
use fleetmux_term::{Attrs, Color as TColor, cell_width};
use ratatui::backend::Backend;
use ratatui::buffer::{Buffer, Cell};
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};

use super::backend::ByteBackend;
use crate::server::State;
use crate::server::view::{Selection, Viewer};

/// Upper bound on frames per second per viewer.
pub const MAX_FPS: u32 = 60;

/// The focused group's colour (its border and its selected tab) and the other groups' (fix-m1-tabs: a clearly
/// visible grey, never colour 8, which dark themes draw almost black). Both are base-16 colours, so they look
/// the same in 16, 256 and 24-bit colour.
const FOCUS_COLOR: Color = Color::Cyan;
const OTHER_COLOR: Color = Color::Gray;
/// Border style of the focused group (bold + cyan) and of the others.
const FOCUSED: Style = Style::new().fg(FOCUS_COLOR).add_modifier(Modifier::BOLD);
const PLAIN: Style = Style::new().fg(OTHER_COLOR);
/// [M59]: tabs sit on their group's border row, so the row's background is the content background (the `─` cells
/// are the group's border line, drawn by `frame`: cyan bold for the focused group, grey for the others). Only a
/// selected tab has a block: a dark-grey (`DarkGray`, 8) pill of ` title ` with a padding column on each side.
/// Its three states are (background, foreground, bold): focused group `White` bold on `DarkGray`, unfocused
/// group `Gray` on the same `DarkGray`, unselected `Gray` with no background. Neighbouring tabs of one group are
/// separated by a thin `DarkGray` `│` in the one-column gap (`r.x + r.w`); after a group's last tab the row is
/// the border line again. The whole tab palette is ANSI-16 (no 256-colour shades, no RGB), so 16-colour,
/// 256-colour and 24-bit terminals show the same three states, each in its own shades; on a light terminal theme
/// the grey text and the dark-grey pill are faint but readable.
/// [M59]: the grip on the horizontal separator row (which is also the lower group's tab row), drawn whenever the
/// layout is not zoomed, so the separator is visible as a drag point while nothing is dragged. It sits on that
/// row's border cells only (never on a tab block or its neighbouring column, where the thin `│` separator sits),
/// which `hit_test` already reports as `HSep`; B16's drag highlight recolours it like the rest of the separator.
const GRIP: char = '═';
const GRIP_W: u16 = 5;
const GRIP_ST: Style = Style::new().fg(Color::Gray);
/// Tab blocks: unselected = plain grey text, no background; selected in the focused group = white bold text on a
/// dark-grey block; selected elsewhere = grey text on the same block. The three differ in base-16 colours /
/// bold, not only in 256-colour shades.
const TAB_IDLE: Style = Style::new().fg(Color::Gray);
const TAB_FOCUSED: Style = Style::new()
    .fg(Color::White)
    .bg(Color::DarkGray)
    .add_modifier(Modifier::BOLD);
const TAB_SELECTED: Style = Style::new().fg(Color::Gray).bg(Color::DarkGray);
/// The thin line between two neighbouring tabs of the same group (the gap column is `r.x + r.w`).
const TAB_SEP: char = '│';
const TAB_SEP_ST: Style = Style::new().fg(Color::DarkGray);
/// The sidebar scrollbar's thumb (like cmux): the heavy form of the divider's `│`, in the divider's grey.
const SIDEBAR_THUMB: char = '┃';
/// A separator being dragged (PROTOCOL.md §11).
const DRAGGED: Style = Style::new().fg(Color::Yellow).add_modifier(Modifier::BOLD);
/// The mark before an unseen tab's title (PROTOCOL.md §14).
const UNSEEN: char = '•';
/// Gaps of at most this many columns between two cells sent on the same row are filled with the canvas's own
/// cells instead of a cursor jump (a `CUP` is 6–10 bytes), so text with inner blanks goes out as one run.
const GAP_FILL: u16 = 8;

/// Draw `state` for `viewer` and return the bytes for a `0x81` frame: only what changed since
/// `viewer.prev` (a full screen when `prev` is `None` or the size changed), then store the new canvas in
/// `viewer.prev`. Background tabs are not drawn.
pub fn draw(state: &State, viewer: &mut Viewer) -> Vec<u8> {
    draw_cached(state, viewer, &mut DrawnPanes::default())
}

/// What [`draw_cached`] painted into one viewer's `prev` canvas from each pane: (pane, inner rectangle,
/// generation), and (fix-m0-3) the outer cursor its last update left (`None` = not known). It describes `prev`
/// and what the client shows once its queued updates are written: replaced whenever `prev` is, cleared whenever
/// `prev` is reset to `None`.
#[derive(Default)]
pub struct DrawnPanes(Vec<(PaneId, LRect, u64)>, Option<OuterCursor>);

impl DrawnPanes {
    pub fn clear(&mut self) {
        self.0.clear();
        self.1 = None;
    }

    fn has(&self, id: &str, r: LRect, generation: u64) -> bool {
        self.0
            .iter()
            .any(|(p, pr, g)| p == id && *pr == r && *g == generation)
    }
}

/// Where the outer cursor is left after an update: shown at a canvas cell, or hidden.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum OuterCursor {
    At(u16, u16),
    Hidden,
}

/// [`draw`] with `drawn` = what the viewer's `prev` holds from each pane: a displayed pane whose generation
/// (`ViewerHub::pane_gen`) and rectangle match is copied from `prev` instead of read from its parser. On a
/// non-empty update `drawn` is replaced together with `prev`; on an empty one both stay as they are.
///
/// fix-m0-3: when no cell changed but the outer cursor did (the inner program only moved its cursor: shell line
/// editing, vim motions), the update is just the cursor (`CUP` + show, or hide) and only the cursor in `drawn`
/// changes. Nothing changed at all, or the cursor last sent is not known (`draw` without a record): no bytes.
pub fn draw_cached(state: &State, viewer: &mut Viewer, drawn: &mut DrawnPanes) -> Vec<u8> {
    let area = Rect::new(0, 0, viewer.cols, viewer.rows);
    let reuse = viewer
        .prev
        .as_ref()
        .filter(|p| p.area == area)
        .map(|p| (p, &*drawn));
    let mut now_drawn = DrawnPanes::default();
    let mut canvas = paint(
        state,
        viewer.cols,
        viewer.rows,
        dragged_separator(state, viewer),
        viewer.selection.as_ref(),
        reuse,
        &mut now_drawn,
    );
    if state.layout.native_header() && !viewer.native_header && state.side.data.is_some() {
        let (lc, _) = state.viewers.layout_size();
        super::sidebar::draw_terminal_header(&mut canvas, &state.side, state.layout.sidebar_cols(lc), lc);
        if state.side.confirm {
            let (_, lr) = state.viewers.layout_size();
            super::sidebar::draw_modal(&mut canvas, &state.side, lc, lr);
        }
    }
    let mut be = ByteBackend::new(viewer.cols, viewer.rows, viewer.depth);
    match &viewer.prev {
        Some(prev) if prev.area == canvas.area => {
            let diff = prev.diff(&canvas);
            if diff.is_empty() {
                let target = cursor_target(state, viewer.cols, viewer.rows);
                return match drawn.1 {
                    Some(last) if last != target => {
                        put_cursor(&mut be, target);
                        drawn.1 = Some(target);
                        be.take()
                    }
                    _ => Vec::new(),
                };
            }
            let _ = be.draw(fill_short_gaps(&canvas, diff).into_iter());
        }
        _ => {
            // Full redraw: clear, then every cell that differs from a blank screen.
            let _ = be.clear();
            let blank = Buffer::empty(canvas.area);
            let _ = be.draw(fill_short_gaps(&canvas, blank.diff(&canvas)).into_iter());
        }
    }
    let target = cursor_target(state, viewer.cols, viewer.rows);
    put_cursor(&mut be, target);
    viewer.prev = Some(canvas);
    now_drawn.1 = Some(target);
    *drawn = now_drawn;
    be.take()
}

/// `updates` (row-major, as `Buffer::diff` returns them) plus, between two updates on the same row that are at
/// most [`GAP_FILL`] columns apart, the canvas cells in that gap. The outer screen already shows those cells
/// (they equal the previous canvas, or the cleared screen on a full redraw), so rewriting them changes nothing
/// on screen; it keeps `ROW 9` one contiguous run instead of `ROW`, a cursor jump and `9`. Wide characters are
/// stepped over by their width, and a gap cell that would overlap the next update is not added. The updates
/// themselves, their order and the wide-character trailing cells `diff` emits are kept as they are.
fn fill_short_gaps<'a>(
    canvas: &'a Buffer,
    updates: Vec<(u16, u16, &'a Cell)>,
) -> Vec<(u16, u16, &'a Cell)> {
    let mut out = Vec::with_capacity(updates.len());
    // (row, first column after the last cell sent on it)
    let mut end: Option<(u16, u16)> = None;
    for (x, y, cell) in updates {
        if let Some((ey, ex)) = end
            && ey == y
            && x > ex
            && x - ex <= GAP_FILL
        {
            let mut gx = ex;
            while gx < x {
                let gap = &canvas[(gx, y)];
                let w = width(gap);
                if gx + w > x {
                    break;
                }
                out.push((gx, y, gap));
                gx += w;
            }
        }
        out.push((x, y, cell));
        let next = x.saturating_add(width(cell));
        end = Some(match end {
            Some((ey, ex)) if ey == y => (y, ex.max(next)),
            _ => (y, next),
        });
    }
    out
}

/// Columns a canvas cell takes (`fleetmux_term::cell_width`, the parser's own width rule), at least 1.
fn width(cell: &Cell) -> u16 {
    let w: u16 = cell
        .symbol()
        .chars()
        .map(|c| u16::from(cell_width(c)))
        .sum();
    w.max(1)
}

/// The separator being dragged, if any (`Hit::VSep` / `Hit::HSep`). The drag is shared by all viewers (the
/// ratios are), so every viewer shows the highlight; B14's `ViewerHub::drag_hit` stays readable during
/// `render_all` (only the viewer map is taken out).
fn dragged_separator(state: &State, viewer: &Viewer) -> Option<Hit> {
    let _ = viewer;
    state
        .viewers
        .drag_hit()
        .filter(|h| matches!(h, Hit::VSep | Hit::HSep))
}

/// The whole canvas for a `cols × rows` viewer; `drag` = the separator being dragged; `sel` = this viewer's
/// selection; `reuse` = the previous canvas (same size) and what it holds from each pane; `drawn` collects what
/// this canvas holds from each pane.
fn paint(
    state: &State,
    cols: u16,
    rows: u16,
    drag: Option<Hit>,
    sel: Option<&Selection>,
    reuse: Option<(&Buffer, &DrawnPanes)>,
    drawn: &mut DrawnPanes,
) -> Buffer {
    let mut buf = Buffer::empty(Rect::new(0, 0, cols, rows));
    let (lc, lr) = state.viewers.layout_size();
    let layout = &state.layout;
    let rects = layout.rects(lc, lr);
    let focus = layout.focus();
    // S2b (plan §13.3 / §13.6-3; §18.4-5), S1c (plan §14.5-6): a fleet window with no pane of its own and no card
    // still up owns the grid area — no frame, only the placeholder lines [`draw_placeholder`] centres there. The
    // sidebar's bash tabs (`type "shell"`, user 2026-11-09: in the root page's worker area) are no fleet agents, so
    // they do not keep the placeholder away; while a bash card is selected (or anything is zoomed), though, the screen
    // is that shell and the placeholder steps aside. A bare service (no kv `sidebar`) keeps its frames as before.
    let no_fleet_pane = Group::ALL.iter().all(|g| {
        layout
            .group(*g)
            .iter()
            .all(|id| state.panes.info(id).is_some_and(|p| p.ty == super::sidebar::SHELL_TYPE))
    });
    let closed_placeholder = state
        .side
        .data
        .as_ref()
        .is_some_and(|d| !super::sidebar::has_live_card(d))
        && no_fleet_pane
        && layout.zoom().is_none()
        && state.side.bash_sel.is_none();

    if closed_placeholder {
        // Plan §13.3: the grid draws no frame while the placeholder stands in for it.
    } else if let (Some(zid), Some(z)) = (layout.zoom(), rects.zoom) {
        pane_cached(state, &mut buf, zid, z, sel, reuse, drawn);
    } else {
        // Focused group last, so its highlight wins on the shared separator cells.
        let mut order: Vec<Group> = Group::ALL.iter().copied().filter(|g| *g != focus).collect();
        order.push(focus);
        for g in &order {
            let r = group_rect(&rects, *g);
            frame(&mut buf, r, if *g == focus { FOCUSED } else { PLAIN });
        }
        let tabs = layout.tab_rects_titled(lc, lr, |id| label_cols(&state.panes, id));
        if let Some((x0, y)) = hsep_grip(rects.lower, &tabs) {
            for x in x0..x0 + GRIP_W {
                put(&mut buf, x, y, GRIP, GRIP_ST);
            }
        }
        if let Some(h) = drag {
            let edge = layout.sidebar_rect(lc, lr).map_or(0, |band| {
                if layout.sidebar_on_right() {
                    band.x.saturating_sub(1)
                } else {
                    band.w
                }
            });
            highlight_separator(&mut buf, &rects, h, edge);
        }
        for (n, (g, i, r)) in tabs.iter().enumerate() {
            let (g, i, r) = (*g, *i, *r);
            let Some(id) = layout.group(g).get(i) else {
                continue;
            };
            let info = state.panes.info(id);
            let unseen = info.as_ref().is_some_and(|p| p.unseen);
            let text = info
                .map(|p| tab_text(&p.title, p.unseen))
                .unwrap_or_default();
            let st = match (layout.selected(g) == Some(id), g == focus) {
                (true, true) => TAB_FOCUSED,
                (true, false) => TAB_SELECTED,
                (false, _) => TAB_IDLE,
            };
            tab_block(&mut buf, r, &text, unseen, st);
            // A thin line in the gap column between two neighbouring tabs of the same group; after a group's
            // last tab the row stays the border line `frame()` drew.
            if tabs.get(n + 1).is_some_and(|(ng, _, _)| *ng == g) {
                put(&mut buf, r.x.saturating_add(r.w), r.y, TAB_SEP, TAB_SEP_ST);
            }
        }
        for g in Group::ALL {
            if let Some(id) = layout.selected(g) {
                pane_cached(
                    state,
                    &mut buf,
                    id,
                    group_rect(&rects, g),
                    sel,
                    reuse,
                    drawn,
                );
            }
        }
        // M4 (design m4 R7): the current page is a reclaimed child fleet — a read-only record. Its supervisor and
        // worker groups say so; the lower group shows its `history_view` tab as usual.
        if let Some(title) = reclaimed_title(state) {
            for g in [Group::Supervisor, Group::Workers] {
                if layout.selected(g).is_none() {
                    draw_read_only_banner(&mut buf, group_rect(&rects, g), &title);
                }
            }
        }
    }
    // The fleet sidebar (S1, plan §4.3) over the layout's sidebar band: the leftmost columns, or the rightmost
    // ones while the content is mirrored (A26 rev3, the Linux layout). It clears its own cells first, so no pane
    // character or older card survives there, and it stays while a pane is zoomed. The mirrored band is drawn
    // into its own `sb`-wide buffer and copied to its columns, so every card position stays relative to `sb`.
    // A26 rev7: the cards start on the header band's last row (`Layout::header_rows`), which is the workspace's
    // topmost tab row on both platforms, so the first fleet card and that tab row begin on the same line.
    let sb = layout.sidebar_cols(lc);
    let top = layout.header_rows();
    if sb > 0 {
        let band = layout.sidebar_rect(lc, lr).unwrap_or_default();
        if layout.native_header() || layout.sidebar_on_right() {
            // Native headers and the legacy right band have no toolbar row (the native headerbar carries it), so only the cards go into the
            // band; `draw_topbar` puts the toolbar on row 0's left.
            let mut side_buf = Buffer::empty(Rect::new(0, 0, sb, lr));
            super::sidebar::draw_band(&mut side_buf, &state.side, sb, lr.saturating_sub(1), top);
            blit(&mut buf, &side_buf, band.x);
        } else {
            super::sidebar::draw(&mut buf, &state.side, sb, lr.saturating_sub(1), top);
        }
    }
    // S1f (plan §20.4-4): row 0 is the topbar — its left part (the toolbar, drawn by `draw` while the sidebar is
    // shown, by `draw_topbar` while it is collapsed), the `│` divider on the sidebar's drag line and the current
    // fleet's `title · line1`. Drawn after the panes and the sidebar so it owns row 0.
    let left = layout.topbar_left(lc);
    if left > 0 {
        super::sidebar::draw_topbar(&mut buf, &state.side, sb, left, lc, layout.sidebar_on_right());
    }
    // Frameless content (a zoomed pane, the closed placeholder) gets cmux's two thin lines instead of frames, in the
    // unfocused border colour: the sidebar divider in the band's column next to the content (the left band's blank
    // gap column `sb − 1`, the mirrored band's first column) down to the row above the status line, and, under an
    // in-grid topbar, a `─` on the topbar's blank separation row (`top − 1`) across the content only, meeting the
    // divider in `├` / `┤`. When the topbar's own `│` sits right beside the divider (column `sb`, mirrored
    // `cols − sb − 1`, over the framed layout's border) the divider takes row 0 instead, so the line runs unbroken;
    // the title keeps its column. Only glyph and colour are set: a selected card's blue bar keeps its background.
    if closed_placeholder || layout.zoom().is_some() {
        let line = |buf: &mut Buffer, x: u16, y: u16, ch: char| {
            if let Some(c) = buf.cell_mut((x, y)) {
                c.set_char(ch).set_fg(OTHER_COLOR);
            }
        };
        let mirrored = layout.sidebar_on_right();
        let band = layout.sidebar_rect(lc, lr);
        let under_topbar = (layout.topbar() > 0 && top > 0).then(|| top - 1);
        if let Some(b) = band {
            let x = if mirrored { b.x } else { b.x + b.w - 1 };
            let mut y0 = top;
            if let Some(y) = under_topbar {
                y0 = y;
                let beside = if mirrored {
                    b.x.checked_sub(1)
                } else {
                    (left == b.w).then_some(left)
                };
                if let Some(o) = beside {
                    if let Some(c) = buf.cell_mut((o, 0))
                        && c.symbol() == "│"
                    {
                        c.reset();
                    }
                    y0 = 0;
                }
            }
            for y in y0..rects.status.y {
                line(&mut buf, x, y, '│');
            }
            if let Some(y) = under_topbar {
                let ch = match (mirrored, y0 < y) {
                    (false, true) => '├',
                    (false, false) => '┌',
                    (true, true) => '┤',
                    (true, false) => '┐',
                };
                line(&mut buf, x, y, ch);
            }
        }
        if let Some(y) = under_topbar {
            let (x0, x1) = match band {
                Some(b) if mirrored => (0, b.x),
                Some(b) => (b.x + b.w, lc),
                None => (0, lc),
            };
            for x in x0..x1 {
                line(&mut buf, x, y, '─');
            }
        }
    }
    // The sidebar's scrollbar (like cmux), only while its cards overflow: a grey `┃` thumb in the band's column next
    // to the content — the divider's column above (the left band's gap column `sb − 1`, the mirrored band's first
    // column) — on the rows `sidebar::scrollbar` gives. Drawn after the frameless divider, so over frameless content
    // the thumb replaces those rows of the `│` and the divider keeps the rest; with a frame it stands alone in the
    // blank gap. Only glyph and colour are set: a selected card's blue bar keeps its background.
    if sb > 0
        && let Some(b) = layout.sidebar_rect(lc, lr)
        && let Some((y0, h)) = super::sidebar::scrollbar(&state.side, lr.saturating_sub(1), top)
    {
        let x = if layout.sidebar_on_right() { b.x } else { b.x + b.w - 1 };
        for y in y0..y0.saturating_add(h) {
            if let Some(c) = buf.cell_mut((x, y)) {
                c.set_char(SIDEBAR_THUMB).set_fg(OTHER_COLOR);
            }
        }
    }
    // S2b (plan §13.6-3): the closed fleet's placeholder beside the sidebar (the whole width while hidden), from
    // row 1 down to the row above the status line.
    if closed_placeholder {
        let band = layout.sidebar_rect(lc, lr).unwrap_or_default();
        let (x0, w) = if layout.sidebar_on_right() {
            (0, lc.saturating_sub(band.w))
        } else {
            (band.x + band.w, lc.saturating_sub(band.x + band.w))
        };
        // A26 rev5/rev7: the placeholder starts below the header band (`top`): the topbar row plus its separation
        // row on macOS, the blank separation row under the native headerbar on Linux.
        draw_placeholder(&mut buf, &state.side, x0, w, rows, top);
    }
    // The status line is this viewer's own bottom row, across its own width (not the layout-size status row):
    // every viewer reads its status where its terminal ends, even when the layout is sized for another viewer.
    if rows > 0 {
        let bottom = LRect {
            x: 0,
            y: rows - 1,
            w: cols,
            h: 1,
        };
        status(state, &mut buf, bottom);
    }
    // S1b (plan §2.5 / §11.3): the close confirmation box over everything, after the status line so it never covers
    // it. `forget_drawn` (view.rs) re-reads the panes when the box closes, so no box character survives there.
    if state.side.confirm {
        super::sidebar::draw_modal(&mut buf, &state.side, lc, lr);
    }
    buf
}

/// M4 (design m4 R7): the title of the current page while it is a reclaimed child page; `None` otherwise.
fn reclaimed_title(state: &State) -> Option<String> {
    let page = state.fleet_pages.pages.get(&state.fleet_pages.current)?;
    page.reclaimed.then(|| page.title.clone())
}

/// M4 (design m4 R7): the three lines of a read-only page, centred in the group rectangle `r` (clipped to it).
fn draw_read_only_banner(buf: &mut Buffer, r: LRect, title: &str) {
    let lines = [
        (
            format!("子 fleet {title} 已收回 · 只读"),
            Style::new().fg(Color::White).add_modifier(Modifier::BOLD),
        ),
        (
            "下面是它的运行记录；点侧栏别的卡片切回".to_string(),
            Style::new().fg(Color::Gray),
        ),
        (
            "同名再起时这一页会重新启用".to_string(),
            Style::new().fg(Color::DarkGray),
        ),
    ];
    if r.w == 0 || r.h == 0 {
        return;
    }
    let y0 = r.y + r.h.saturating_sub(lines.len() as u16) / 2;
    for (k, (text, st)) in lines.iter().enumerate() {
        let y = y0 + k as u16;
        if y >= r.y + r.h {
            break;
        }
        let x = r.x + r.w.saturating_sub(text_cols(text)) / 2;
        let area = buf.area;
        let room = (r.x + r.w).min(area.width).saturating_sub(x) as usize;
        if y < area.height && room > 0 {
            buf.set_stringn(x, y, text, room, *st);
        }
    }
}

/// The inner rectangle of group `g`.
pub(crate) fn group_rect(rects: &fleetmux_layout::Rects, g: Group) -> LRect {
    match g {
        Group::Supervisor => rects.supervisor,
        Group::Lower => rects.lower,
        Group::Workers => rects.workers,
    }
}

/// The closed fleet's placeholder (plan §13.3 / §13.6-3 / §18.7-6): centred in the area beside the sidebar
/// (`x0` / `w`: right of it, or left of it while the content is mirrored) from row 1 down to the row above the
/// status line. Only drawn while kv `sidebar` parses, no card is still up and the layout holds no pane at all
/// ([`paint`] then draws no frame). A single just-closed card keeps the S2b three lines; a history list says no
/// fleet is running (§18.4-5).
fn draw_placeholder(
    buf: &mut Buffer,
    side: &super::sidebar::Side,
    x0: u16,
    w: u16,
    rows: u16,
    top: u16,
) {
    let Some(data) = side.data.as_ref() else {
        return;
    };
    let white = Style::new()
        .fg(Color::White)
        .add_modifier(Modifier::BOLD);
    let exit = (
        super::sidebar::PLACEHOLDER_EXIT.to_string(),
        Style::new().fg(Color::DarkGray),
    );
    let lines = if super::sidebar::just_closed(data) {
        let name = data
            .fleets
            .iter()
            .find(|e| e.id == data.current)
            .or_else(|| data.fleets.first())
            .map(|e| e.title.as_str())
            .unwrap_or_default();
        vec![
            (
                format!(
                    "{}{name}{}",
                    super::sidebar::PLACEHOLDER_TITLE_HEAD,
                    super::sidebar::PLACEHOLDER_TITLE_TAIL
                ),
                white,
            ),
            (
                super::sidebar::PLACEHOLDER_RESTORE.to_string(),
                Style::new().fg(Color::Gray),
            ),
            exit,
        ]
    } else {
        vec![
            (super::sidebar::PLACEHOLDER_NO_FLEET.to_string(), white),
            exit,
        ]
    };
    let area = LRect {
        x: x0,
        y: top,
        w,
        h: rows.saturating_sub(1).saturating_sub(top),
    };
    if area.w == 0 || area.h == 0 {
        return;
    }
    let top = area.y + area.h.saturating_sub(lines.len() as u16) / 2;
    for (k, (text, st)) in lines.iter().enumerate() {
        let y = top + k as u16;
        if y >= area.y + area.h {
            break;
        }
        let x = area.x + area.w.saturating_sub(text_cols(text)) / 2;
        put_text(buf, x, y, text, *st);
    }
}

/// Copy the `sb`-wide band buffer `src` into `buf` starting at column `dx` (the mirrored sidebar band, A26 rev3).
fn blit(buf: &mut Buffer, src: &Buffer, dx: u16) {
    for y in 0..src.area.height {
        for x in 0..src.area.width {
            let Some(c) = src.cell((x, y)).cloned() else {
                continue;
            };
            if let Some(d) = buf.cell_mut((dx.saturating_add(x), y)) {
                *d = c;
            }
        }
    }
}

/// Write `text` from `(x, y)` on, advancing by each character's display width (`fleetmux_term::cell_width`).
fn put_text(buf: &mut Buffer, x: u16, y: u16, text: &str, st: Style) {
    let mut cx = x;
    for ch in text.chars() {
        put(buf, cx, y, ch, st);
        cx = cx.saturating_add(u16::from(cell_width(ch)));
    }
}

/// Set one cell if it is on the canvas.
fn put(buf: &mut Buffer, x: u16, y: u16, ch: char, st: Style) {
    if let Some(c) = buf.cell_mut((x, y)) {
        c.reset();
        c.set_char(ch).set_style(st);
    }
}

/// The box around inner rectangle `r` (one cell outside it on every side).
fn frame(buf: &mut Buffer, r: LRect, st: Style) {
    if r.w == 0 || r.h == 0 {
        return;
    }
    let (x0, y0) = (r.x.saturating_sub(1), r.y.saturating_sub(1));
    let (x1, y1) = (r.x.saturating_add(r.w), r.y.saturating_add(r.h));
    for x in x0..=x1 {
        put(buf, x, y0, '─', st);
        put(buf, x, y1, '─', st);
    }
    for y in y0..=y1 {
        put(buf, x0, y, '│', st);
        put(buf, x1, y, '│', st);
    }
    put(buf, x0, y0, '┌', st);
    put(buf, x1, y0, '┐', st);
    put(buf, x0, y1, '└', st);
    put(buf, x1, y1, '┘', st);
}

/// Restyle the cells of the dragged separator: the column between the left column and `workers` (`VSep`),
/// the row between `supervisor` and `lower` (`HSep`, which also carries the lower tab labels, drawn later), or
/// the content's left border column while the sidebar's edge is dragged (`SidebarEdge`, S1d).
fn highlight_separator(
    buf: &mut Buffer,
    rects: &fleetmux_layout::Rects,
    h: Hit,
    sidebar_edge: u16,
) {
    let cells: Vec<(u16, u16)> = match h {
        Hit::VSep => {
            let x = rects.workers.x.max(rects.supervisor.x).saturating_sub(1);
            (rects.workers.y.saturating_sub(1)..=rects.workers.y.saturating_add(rects.workers.h))
                .map(|y| (x, y))
                .collect()
        }
        Hit::HSep => {
            let (l, y) = (rects.lower, rects.lower.y.saturating_sub(1));
            (l.x.saturating_sub(1)..=l.x.saturating_add(l.w))
                .map(|x| (x, y))
                .collect()
        }
        Hit::SidebarEdge => {
            // S1d (plan §16.2 拖动中; supervisor ruling [M2]): every content box's left border column is the
            // sidebar's right edge, so the whole column is painted — top border row down to the row above the
            // status line, which is never painted.
            (0..rects.status.y).map(|y| (sidebar_edge, y)).collect()
        }
        _ => return,
    };
    for (x, y) in cells {
        if let Some(c) = buf.cell_mut((x, y)) {
            c.set_style(DRAGGED);
        }
    }
}

/// [M59]: where the horizontal separator's grip goes: `(x0, row)` of `GRIP_W` cells on the lower group's tab row
/// (`lower` = its inner rectangle), centred on the row when that span is free, else centred in the longest free
/// run; `None` when no run is long enough. A cell is free when it is in the row's inner span and neither in a
/// lower tab block nor next to one.
fn hsep_grip(lower: LRect, tabs: &[(Group, usize, LRect)]) -> Option<(u16, u16)> {
    if lower.w == 0 || lower.y == 0 {
        return None;
    }
    let (lo, hi) = (lower.x, lower.x.saturating_add(lower.w));
    let taken = |x: u16| {
        tabs.iter()
            .any(|(g, _, r)| *g == Group::Lower && x + 1 >= r.x && x <= r.x.saturating_add(r.w))
    };
    let fits = |x0: u16| x0 >= lo && x0 + GRIP_W <= hi && (x0..x0 + GRIP_W).all(|x| !taken(x));
    let centred = (lo + lower.w / 2).saturating_sub(GRIP_W / 2);
    if fits(centred) {
        return Some((centred, lower.y - 1));
    }
    let mut best: Option<(u16, u16)> = None; // (start, len)
    let mut start = None;
    for x in lo..=hi {
        match (x < hi && !taken(x), start) {
            (true, None) => start = Some(x),
            (false, Some(s)) => {
                if best.is_none_or(|(_, l)| x - s > l) {
                    best = Some((s, x - s));
                }
                start = None;
            }
            _ => {}
        }
    }
    let (s, len) = best?;
    (len >= GRIP_W).then(|| (s + (len - GRIP_W) / 2, lower.y - 1))
}

/// fix-m1-tabs (supervisor ruling [M48], API B): the text inside a tab block, `• ` + title when the tab is unseen.
/// Drawing, `Layout::tab_rects_titled` / `hit_test_titled` (through [`label_cols`]) and every mouse hit use this
/// one definition, so the drawn text and the hit geometry always agree.
pub(crate) fn tab_text(title: &str, unseen: bool) -> String {
    if unseen {
        format!("{UNSEEN} {title}")
    } else {
        title.to_string()
    }
}

/// Display columns of `text`, by the parser's own width rule (`fleetmux_term::cell_width`: wide = 2, East Asian
/// Ambiguous = 1, zero-width = 0).
pub(crate) fn text_cols(text: &str) -> u16 {
    let w: usize = text.chars().map(|c| usize::from(cell_width(c))).sum();
    w.min(usize::from(u16::MAX)) as u16
}

/// `label_cols` for `Layout::tab_rects_titled` / `hit_test_titled`: the columns of pane `id`'s block text
/// ([`tab_text`]), without the two padding columns; 0 for a pane the store does not know.
pub(crate) fn label_cols(panes: &crate::server::pane::PaneStore, id: &str) -> u16 {
    panes
        .info(id)
        .map(|p| text_cols(&tab_text(&p.title, p.unseen)))
        .unwrap_or(0)
}

/// fix-m1-tabs: one tab block, exactly the rectangle `tab_rects_titled` gives (never wider): filled with `st`, a
/// padding column on each side, and `text` ([`tab_text`]: `•` when unseen, then the title) in between, truncated
/// so both paddings stay.
fn tab_block(buf: &mut Buffer, r: LRect, text: &str, unseen: bool, st: Style) {
    label(buf, r, "", st);
    if r.w < 3 {
        return; // no room between the paddings
    }
    let inner = LRect {
        x: r.x + 1,
        w: r.w - 2,
        ..r
    };
    label(buf, inner, text, st);
    if unseen && let Some(c) = buf.cell_mut((inner.x, inner.y)) {
        c.set_fg(Color::Yellow);
    }
}

/// A tab label: the title, truncated to the label rectangle (never past its right edge); the whole rectangle
/// takes `st` (its cells are reset first so labels do not inherit the border glyph or colour).
fn label(buf: &mut Buffer, r: LRect, title: &str, st: Style) {
    let area = buf.area;
    if r.w == 0 || r.y >= area.height || r.x >= area.width {
        return;
    }
    let max = r.w.min(area.width - r.x) as usize;
    for x in r.x..r.x + max as u16 {
        if let Some(c) = buf.cell_mut((x, r.y)) {
            c.reset();
            c.set_style(st);
        }
    }
    buf.set_stringn(r.x, r.y, title, max, st);
}

/// Pane `id` inside rectangle `r`: copied from the previous canvas when it holds this pane at this rectangle
/// and generation, else read from the parser. The canvas's last row is always read again: the status line is
/// drawn over it afterwards only as long as its text, so the previous canvas may hold an older, longer status
/// text there instead of pane cells. A copied pane keeps the selection highlight it was drawn with: changing a
/// viewer's selection clears its `drawn` record (`view::select`), so every pane is read again.
fn pane_cached(
    state: &State,
    buf: &mut Buffer,
    id: &str,
    r: LRect,
    sel: Option<&Selection>,
    reuse: Option<(&Buffer, &DrawnPanes)>,
    drawn: &mut DrawnPanes,
) {
    let generation = state.viewers.pane_gen(id);
    let mut from_row = 0;
    if let (Some(g), Some((prev, old))) = (generation, reuse)
        && old.has(id, r, g)
    {
        let status = buf.area.height.saturating_sub(1);
        from_row = status.saturating_sub(r.y).min(r.h);
        copy_rows(prev, buf, r, from_row);
    }
    pane(state, buf, id, r, from_row, sel);
    if let Some(g) = generation {
        drawn.0.push((id.to_owned(), r, g));
    }
}

/// Copy rows `0..n` of rectangle `r` (the part on the canvas) from `prev` (same area as `buf`).
fn copy_rows(prev: &Buffer, buf: &mut Buffer, r: LRect, n: u16) {
    let area = buf.area;
    let (x0, x1) = (r.x.min(area.width), r.x.saturating_add(r.w).min(area.width));
    if x0 >= x1 || prev.area != area {
        return;
    }
    for y in r.y..r.y.saturating_add(n).min(area.height) {
        let i = buf.index_of(x0, y);
        let j = i + usize::from(x1 - x0);
        buf.content[i..j].clone_from_slice(&prev.content[i..j]);
    }
}

/// The visible screen of pane `id` inside rectangle `r`, from pane row `from_row` on. Only cells on this
/// viewer's canvas are read from the parser. Cells inside `sel` (when it is this pane's, made at the view's current
/// offset) are drawn with reverse video toggled.
fn pane(
    state: &State,
    buf: &mut Buffer,
    id: &str,
    r: LRect,
    from_row: u16,
    sel: Option<&Selection>,
) {
    let (Some(term), Some(info)) = (state.panes.term(id), state.panes.info(id)) else {
        return;
    };
    let sel = sel
        .filter(|s| s.pane == id && s.offset == term.display_offset())
        .map(|s| {
            if s.from <= s.to {
                s.from..=s.to
            } else {
                s.to..=s.from
            }
        });
    let area = buf.area;
    let w = r.w.min(info.cols).min(area.width.saturating_sub(r.x));
    let h = r.h.min(info.rows).min(area.height.saturating_sub(r.y));
    for row in from_row..h {
        for col in 0..w {
            let c = term.cell(row, col);
            if c.width == 0 {
                continue; // placeholder after a wide character
            }
            let ch = if c.ch == '\0' { ' ' } else { c.ch };
            let mut st = cell_style(c.fg, c.bg, c.attrs);
            if sel.as_ref().is_some_and(|s| s.contains(&(row, col))) {
                st = if c.attrs.inverse {
                    st.remove_modifier(Modifier::REVERSED)
                } else {
                    st.add_modifier(Modifier::REVERSED)
                };
            }
            put(buf, r.x + col, r.y + row, ch, st);
        }
    }
}

fn color(c: TColor) -> Color {
    match c {
        TColor::Default => Color::Reset,
        TColor::Indexed(n) => Color::Indexed(n),
        TColor::Rgb(r, g, b) => Color::Rgb(r, g, b),
    }
}

fn cell_style(fg: TColor, bg: TColor, a: Attrs) -> Style {
    let mut m = Modifier::empty();
    for (on, bit) in [
        (a.bold, Modifier::BOLD),
        (a.dim, Modifier::DIM),
        (a.italic, Modifier::ITALIC),
        (a.underline, Modifier::UNDERLINED),
        (a.inverse, Modifier::REVERSED),
        (a.hidden, Modifier::HIDDEN),
        (a.strikeout, Modifier::CROSSED_OUT),
    ] {
        if on {
            m |= bit;
        }
    }
    Style::new().fg(color(fg)).bg(color(bg)).add_modifier(m)
}

/// `^b` for the prefix byte 0x02, `C-x` style names for anything else printable.
pub fn prefix_hint(prefix: &[u8]) -> String {
    match prefix {
        [b @ 0x01..=0x1a] => format!("^{}", (b + 0x60) as char),
        [b] if b.is_ascii_graphic() => (*b as char).to_string(),
        _ => String::from("^?"),
    }
}

fn status(state: &State, buf: &mut Buffer, r: LRect) {
    let area = buf.area;
    if r.y >= area.height {
        return;
    }
    let hint = prefix_hint(&state.prefix);
    let focus = match state.layout.focus() {
        Group::Supervisor => "supervisor",
        Group::Lower => "lower",
        Group::Workers => "workers",
    };
    let mut text = String::from(" fleetmux");
    // S1b (plan §11.1): the close request's state stays on the line until it changes, then the one-shot toolbar
    // notice (cleared by the next key press or mouse press).
    match &state.side.stop {
        super::sidebar::Stop::None => {}
        super::sidebar::Stop::Requested => text.push_str("  已请求关闭，等 daemon…"),
        super::sidebar::Stop::Accepted => text.push_str("  正在关闭…"),
        super::sidebar::Stop::Refused(reason) => {
            text.push_str(&format!("  关闭被拒：{reason}"))
        }
    }
    // S2b (plan §13.6-4): the restore pane runs until kv `restorable` is cleared (the daemon's cards are back).
    if state.side.resuming {
        text.push_str("  正在恢复…");
    }
    // A42-LINUX-RESTORE-FAILURE-04: a failed restore keeps its reason on the line until the operator retries.
    if let crate::server::view::ResumeAttempt::Failed { reason } = &state.resume {
        text.push_str("  ");
        text.push_str(reason);
    }
    if let Some(note) = &state.side.notice {
        text.push_str(&format!("  {note}"));
    }
    // Page mode first: `[offset/scrollback]` is the first `[` on the line (PROTOCOL.md §13).
    if let Some(page) = &state.page {
        let (off, total) = state
            .panes
            .term(&page.pane)
            .map(|t| (t.display_offset(), t.scrollback_len()))
            .unwrap_or((0, 0));
        text.push_str(&format!("  [{off}/{total}]"));
        if page.typing {
            text.push_str(&format!("  /{}", page.query));
        } else if !page.query.is_empty() {
            text.push_str(&format!("  search: {}", page.query));
        }
    }
    if let Some(z) = state.layout.zoom() {
        let title = state
            .panes
            .info(z)
            .map(|p| p.title)
            .unwrap_or_else(|| z.clone());
        text.push_str(&format!("  zoom: {title}"));
    }
    text.push_str(&format!("  {hint} prefix  {hint} d detach"));
    if state.side.data.is_some() {
        // S1 (plan §2.2): the sidebar's toggle key, only while the daemon has pushed a sidebar.
        let what = if state.side.hidden { "显示侧栏" } else { "隐藏侧栏" };
        text.push_str(&format!("  {hint} s {what}"));
    }
    // M4 (design m4 R6): a child page names itself right before `focus:` (the root page adds nothing).
    if let Some(p) = state.fleet_pages.pages.get(&state.fleet_pages.current) {
        text.push_str(&format!("  page: {}", p.title));
    }
    text.push_str(&format!("  focus: {focus}"));
    let width = r.w.min(area.width.saturating_sub(r.x)) as usize;
    if r.x < area.width {
        buf.set_stringn(
            r.x,
            r.y,
            text,
            width,
            Style::new().add_modifier(Modifier::DIM),
        );
    }
}

/// The outer cursor goes where the focused pane's cursor is, when it is visible and on this canvas.
fn cursor_target(state: &State, cols: u16, rows: u16) -> OuterCursor {
    let layout = &state.layout;
    let (lc, lr) = state.viewers.layout_size();
    let rects = layout.rects(lc, lr);
    let (id, r) = match (layout.zoom(), rects.zoom) {
        (Some(z), Some(zr)) => (Some(z), zr),
        _ => {
            let g = layout.focus();
            (layout.selected(g), group_rect(&rects, g))
        }
    };
    let cur = id.and_then(|id| state.panes.term(id)).map(|t| t.cursor());
    match cur {
        Some(c)
            if c.visible
                && c.col < r.w
                && c.row < r.h
                && r.x + c.col < cols
                && r.y + c.row < rows =>
        {
            OuterCursor::At(r.x + c.col, r.y + c.row)
        }
        _ => OuterCursor::Hidden,
    }
}

/// Emit `target`: the cell updates before it moved the terminal's cursor, so a shown cursor is always placed.
fn put_cursor(be: &mut ByteBackend, target: OuterCursor) {
    match target {
        OuterCursor::At(x, y) => {
            let _ = be.set_cursor_position((x, y));
            let _ = be.show_cursor();
        }
        OuterCursor::Hidden => {
            let _ = be.hide_cursor();
        }
    }
}

/// Frame-rate limiter: at most `fps` `ready() == true` per second, no timers of its own (the event loop asks
/// when something is dirty).
pub struct Throttle {
    min_gap: Duration,
    last: Option<Instant>,
}

impl Throttle {
    pub fn new(fps: u32) -> Self {
        Throttle {
            min_gap: Duration::from_secs(1) / fps.max(1),
            last: None,
        }
    }

    /// Whether a frame may be drawn at `now`; `true` also records `now` as the last frame time.
    pub fn ready(&mut self, now: Instant) -> bool {
        if self.until_ready(now).is_zero() {
            self.last = Some(now);
            true
        } else {
            false
        }
    }

    /// How long from `now` until `ready` would return `true` (zero = now). The event loop sleeps this once,
    /// only while something is dirty.
    pub fn until_ready(&self, now: Instant) -> Duration {
        match self.last {
            Some(last) => (last + self.min_gap).saturating_duration_since(now),
            None => Duration::ZERO,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use fleetmux_term::color::Depth;

    #[test]
    fn throttle_spaces_frames_by_one_over_fps() {
        let t0 = Instant::now();
        let mut t = Throttle::new(MAX_FPS);
        assert!(t.ready(t0));
        assert!(!t.ready(t0 + Duration::from_millis(5)));
        let wait = t.until_ready(t0 + Duration::from_millis(5));
        assert!(
            wait > Duration::from_millis(10) && wait < Duration::from_millis(13),
            "{wait:?}"
        );
        assert!(t.ready(t0 + Duration::from_millis(17)));
        assert_eq!(Throttle::new(0).min_gap, Duration::from_secs(1));
    }

    #[test]
    fn prefix_hint_names_control_keys() {
        assert_eq!(prefix_hint(&[0x02]), "^b");
        assert_eq!(prefix_hint(&[0x01]), "^a");
    }

    fn bytes(prev: Option<&Buffer>, next: &Buffer) -> String {
        let mut be = ByteBackend::new(next.area.width, next.area.height, Depth::Rgb);
        let base = prev.cloned().unwrap_or_else(|| Buffer::empty(next.area));
        let _ = be.draw(fill_short_gaps(next, base.diff(next)).into_iter());
        String::from_utf8(be.take()).unwrap()
    }

    #[test]
    fn full_redraw_keeps_inner_blanks_in_one_run() {
        let mut buf = Buffer::empty(Rect::new(0, 0, 40, 2));
        buf.set_string(10, 1, "ROW 9", Style::new());
        buf.set_string(20, 1, "a        b", Style::new()); // 8 blanks: filled
        buf.set_string(0, 0, "x", Style::new());
        buf.set_string(30, 0, "y", Style::new()); // 29 blanks: a cursor jump
        let out = bytes(None, &buf);
        assert!(out.contains("ROW 9"), "{out:?}");
        assert!(out.contains("a        b"), "{out:?}");
        assert!(
            out.contains("x\x1b[1;31Hy"),
            "long gaps still jump: {out:?}"
        );
    }

    #[test]
    fn gap_fill_steps_over_wide_chars_and_keeps_styles() {
        let mut buf = Buffer::empty(Rect::new(0, 0, 20, 1));
        buf.set_string(0, 0, "A", Style::new());
        buf.set_string(2, 0, "漢", Style::new().fg(Color::Rgb(255, 128, 0)));
        buf.set_string(5, 0, "B", Style::new().add_modifier(Modifier::BOLD));
        let out = bytes(None, &buf);
        // A, blank, 漢 (2 columns, its trailing cell not written), blank, B: one run, each with its own SGR.
        assert!(
            out.starts_with("\x1b[1;1H\x1b[0mA \x1b[0;38;2;255;128;0m漢"),
            "{out:?}"
        );
        assert!(out.ends_with("\x1b[0m \x1b[0;1mB"), "{out:?}");
        assert_eq!(out.matches('漢').count(), 1);
    }

    #[test]
    fn diff_frame_only_adds_unchanged_cells_inside_short_gaps() {
        let mut prev = Buffer::empty(Rect::new(0, 0, 40, 3));
        prev.set_string(0, 1, "count 1 of 9 done", Style::new());
        let mut next = prev.clone();
        next.set_string(6, 1, "2", Style::new());
        next.set_string(11, 1, "8", Style::new());
        let out = bytes(Some(&prev), &next);
        // `2`, the unchanged ` of ` between, then `8`; nothing else on the screen is resent.
        assert_eq!(out, "\x1b[2;7H\x1b[0m2 of 8");
        assert!(bytes(Some(&next), &next).is_empty());
    }

    fn spawn(st: &mut State, title: &str, ty: fleetmux_layout::PaneType) -> PaneId {
        let g = st.layout.rects(240, 60);
        let s = fleetmux_proto::Spawn {
            argv: vec!["sleep".into(), "5".into()],
            cwd: None,
            env: Default::default(),
            title: title.into(),
            ty: "worker".into(),
            group: None,
            anchor: None,
            before: false,
        };
        let group = match ty {
            fleetmux_layout::PaneType::Supervisor => Group::Supervisor,
            _ => Group::Workers,
        };
        let r = group_rect(&g, group);
        let id = st.panes.spawn(&s, r.w, r.h).expect("spawn").pane;
        st.layout.insert(id.clone(), ty);
        st.layout.select(id.clone());
        id
    }

    fn state() -> State {
        State {
            panes: crate::server::pane::PaneStore::new("e".into(), 100, None),
            layout: fleetmux_layout::Layout::new(),
            kv: Default::default(),
            viewers: Default::default(),
            page: None,
            counters: Default::default(),
            runtime: std::path::PathBuf::from("/nonexistent"),
            epoch: "e".into(),
            start_time: 0,
            prefix: vec![0x02],
            web: Default::default(),
            side: Default::default(),
            resume: Default::default(),
            fleet_pages: Default::default(),
        }
    }

    fn viewer(cols: u16, rows: u16) -> Viewer {
        Viewer {
            native_header: false,
            cols,
            rows,
            depth: Depth::Rgb,
            last_input_at: None,
            prev: None,
            selection: None,
        }
    }

    /// One render step as `render_all` does it for one viewer.
    fn step(st: &mut State, v: &mut Viewer, drawn: &mut DrawnPanes) -> Vec<u8> {
        crate::server::view::take_pane_changes(st);
        draw_cached(st, v, drawn)
    }

    /// The canvas `draw` paints from scratch for a `cols × rows` viewer.
    fn fresh(st: &State, cols: u16, rows: u16) -> Buffer {
        let mut v = viewer(cols, rows);
        draw(st, &mut v);
        v.prev.expect("a full redraw stores its canvas")
    }

    fn feed_lines(st: &mut State, id: &str, tag: &str, n: usize) {
        for k in 0..n {
            let line = format!("{tag}{k:03} {}\r\n", "x".repeat(70));
            st.panes.feed_output(id, line.as_bytes());
        }
    }

    #[test]
    fn clean_pane_is_copied_from_prev_and_matches_a_fresh_paint() {
        let mut st = state();
        let a = spawn(&mut st, "sup", fleetmux_layout::PaneType::Supervisor);
        let b = spawn(&mut st, "wrk", fleetmux_layout::PaneType::Worker);
        feed_lines(&mut st, &a, "A", 60);
        feed_lines(&mut st, &b, "B", 60);
        // 100 × 20 on a 240 × 60 layout: cropped, so the supervisor pane runs under this viewer's status line.
        let (cols, rows) = (100, 20);
        let mut v = viewer(cols, rows);
        let mut drawn = DrawnPanes::default();
        assert!(!step(&mut st, &mut v, &mut drawn).is_empty());
        assert_eq!(v.prev.as_ref(), Some(&fresh(&st, cols, rows)));
        let bottom: String = (0..cols)
            .map(|x| v.prev.as_ref().unwrap()[(x, rows - 1)].symbol().to_string())
            .collect();
        assert!(
            bottom.contains("focus: supervisor") && bottom.trim_end().ends_with('x'),
            "A's cells show right of the status text: {bottom:?}"
        );
        let rect_a = group_rect(&st.layout.rects(240, 60), Group::Supervisor);
        let gen_a = st.viewers.pane_gen(&a).unwrap();
        assert!(drawn.has(&a, rect_a, gen_a));

        // Only B changes; the status line gets shorter ("focus: supervisor" → "focus: workers").
        st.layout.focus_group(Group::Workers);
        feed_lines(&mut st, &b, "C", 5);
        assert!(!step(&mut st, &mut v, &mut drawn).is_empty());
        assert_eq!(st.viewers.pane_gen(&a), Some(gen_a), "A did not change");
        assert!(drawn.has(&a, rect_a, gen_a), "A was copied from prev");
        assert_eq!(
            v.prev.as_ref(),
            Some(&fresh(&st, cols, rows)),
            "copying A (with its rows under the status line read again) equals painting it"
        );

        // Now A changes: it is read from its parser again.
        feed_lines(&mut st, &a, "D", 3);
        assert!(!step(&mut st, &mut v, &mut drawn).is_empty());
        assert_ne!(st.viewers.pane_gen(&a), Some(gen_a));
        assert_eq!(v.prev.as_ref(), Some(&fresh(&st, cols, rows)));

        // Nothing changed: no bytes, prev and record kept.
        let before = v.prev.clone();
        assert!(step(&mut st, &mut v, &mut drawn).is_empty());
        assert_eq!(v.prev, before);
    }

    #[test]
    fn cursor_only_move_sends_just_the_cursor_and_nothing_when_unchanged() {
        let mut st = state();
        let a = spawn(&mut st, "sup", fleetmux_layout::PaneType::Supervisor);
        st.panes.feed_output(&a, b"hello world");
        let r = group_rect(&st.layout.rects(240, 60), Group::Supervisor);
        let cup = |col: u16| format!("\x1b[{};{}H", r.y + 1, r.x + col + 1);
        let (cols, rows) = (120, 40);
        let mut v = viewer(cols, rows);
        let mut drawn = DrawnPanes::default();
        let full = String::from_utf8(step(&mut st, &mut v, &mut drawn)).unwrap();
        assert!(full.ends_with(&format!("{}\x1b[?25h", cup(11))), "{full:?}");

        // The inner program only moves its cursor (readline `\b`, vim `h`): no cell changes.
        st.panes.feed_output(&a, b"\x1b[6D");
        let out = String::from_utf8(step(&mut st, &mut v, &mut drawn)).unwrap();
        assert_eq!(out, format!("{}\x1b[?25h", cup(5)), "just the cursor");
        assert!(
            step(&mut st, &mut v, &mut drawn).is_empty(),
            "nothing changed: no bytes"
        );
        st.panes.feed_output(&a, b"\x1b[?25l");
        assert_eq!(step(&mut st, &mut v, &mut drawn), b"\x1b[?25l");
        assert!(step(&mut st, &mut v, &mut drawn).is_empty());
        st.panes.feed_output(&a, b"\x1b[?25h\x1b[2C");
        let out = String::from_utf8(step(&mut st, &mut v, &mut drawn)).unwrap();
        assert_eq!(out, format!("{}\x1b[?25h", cup(7)));

        // A cell change still carries the cursor; `draw` without a record keeps its old empty answer.
        st.panes.feed_output(&a, b"X");
        let out = String::from_utf8(step(&mut st, &mut v, &mut drawn)).unwrap();
        assert!(out.ends_with(&format!("X{}\x1b[?25h", cup(8))), "{out:?}");
        st.panes.feed_output(&a, b"\x1b[1D");
        crate::server::view::take_pane_changes(&mut st);
        assert!(
            draw(&st, &mut v).is_empty(),
            "unknown last cursor: no guess"
        );

        // After a reset (resize / no room) the record is cleared with `prev`: a full redraw places the cursor.
        v.prev = None;
        drawn.clear();
        let out = String::from_utf8(step(&mut st, &mut v, &mut drawn)).unwrap();
        assert!(
            out.starts_with("\x1b[2J") || out.contains("hello"),
            "{out:?}"
        );
        assert!(out.ends_with(&format!("{}\x1b[?25h", cup(7))), "{out:?}");
        assert!(step(&mut st, &mut v, &mut drawn).is_empty());
    }

    /// fix-m0-4 (red until the tab-geometry ruling): after a supervisor rotation at the operator's 196 × 50 —
    /// plenty of room — the successor's full label `fx-supervisor-1.1` is on the tab row where `fx-supervisor-1`
    /// was, every title cell hits that same tab (drawing and `hit_test` share `tab_rects`), and gen 0's label and
    /// the worker's stay right.
    #[test]
    fn rotated_supervisor_tab_shows_its_full_generation_label() {
        use fleetmux_layout::PaneType::{Supervisor, Worker};
        let (cols, rows) = (196, 50);
        let mut st = state();
        st.viewers.set_default_size((cols, rows));
        let mut v = viewer(cols, rows);
        let mut drawn = DrawnPanes::default();
        let mut fails: Vec<String> = Vec::new();
        // (stage, title, group, tab index, expected column or None = anywhere)
        let mut check = |st: &mut State,
                         v: &mut Viewer,
                         drawn: &mut DrawnPanes,
                         stage: &str,
                         want: &[(&str, Group, usize, Option<usize>)]|
         -> Vec<usize> {
            step(st, v, drawn);
            let row: Vec<String> = (0..cols)
                .map(|x| v.prev.as_ref().unwrap()[(x, 0)].symbol().to_string())
                .collect();
            let text: String = row.concat();
            let mut cols_found = Vec::new();
            for (title, g, i, at) in want {
                let n = title.chars().count();
                // A whole label: the title, not followed by more title text (so `fx-supervisor-1` must not be the
                // first 15 columns of a longer title).
                let found = (0..row.len().saturating_sub(n)).find(|&c| {
                    row[c..c + n].concat() == *title
                        && row
                            .get(c + n)
                            .is_none_or(|s| s == " " || s == "─" || s == "┐" || s == "•")
                });
                match found {
                    None => fails.push(format!(
                        "{stage}: {title:?} not whole on the tab row: {text:?}"
                    )),
                    Some(c) => {
                        if at.is_some_and(|a| a != c) {
                            fails.push(format!(
                                "{stage}: {title:?} at column {c}, expected {}",
                                at.unwrap()
                            ));
                        }
                        for x in c..c + n {
                            let h = st.layout.hit_test_titled(cols, rows, x as u16, 0, |id| {
                                label_cols(&st.panes, id)
                            });
                            if h != (Hit::Tab {
                                group: *g,
                                index: *i,
                            }) {
                                fails.push(format!(
                                    "{stage}: {title:?} cell x={x} hits {h:?}, not tab {g:?}/{i}"
                                ));
                                break;
                            }
                        }
                        cols_found.push(c);
                    }
                }
            }
            cols_found
        };

        let s0 = spawn(&mut st, "fx-supervisor-1", Supervisor);
        let _w0 = spawn(&mut st, "fx-worker-1", Worker);
        let at = check(
            &mut st,
            &mut v,
            &mut drawn,
            "gen 0",
            &[
                ("fx-supervisor-1", Group::Supervisor, 0, None),
                ("fx-worker-1", Group::Workers, 0, None),
            ],
        );
        let (sup_x, wrk_x) = (at.first().copied(), at.get(1).copied());

        // Handoff: the successor is inserted at index 0 while gen 0 is alive (and still selected).
        let s1 = spawn(&mut st, "fx-supervisor-1.1", Supervisor);
        st.layout.select(s0.clone());
        check(
            &mut st,
            &mut v,
            &mut drawn,
            "handoff",
            &[
                ("fx-supervisor-1.1", Group::Supervisor, 0, sup_x),
                ("fx-supervisor-1", Group::Supervisor, 1, None),
                ("fx-worker-1", Group::Workers, 0, wrk_x),
            ],
        );

        // Gen 0 is gone: the successor is the only, selected supervisor tab.
        st.layout.remove(s0);
        st.layout.select(s1);
        check(
            &mut st,
            &mut v,
            &mut drawn,
            "after rotation",
            &[
                ("fx-supervisor-1.1", Group::Supervisor, 0, sup_x),
                ("fx-worker-1", Group::Workers, 0, wrk_x),
            ],
        );
        assert!(fails.is_empty(), "fix-m0-4:\n{}", fails.join("\n"));
    }

    #[test]
    fn hsep_grip_is_centred_when_free_and_never_on_or_next_to_a_tab_block() {
        let lower = LRect {
            x: 1,
            y: 36,
            w: 40,
            h: 10,
        };
        let tab = |x: u16, w: u16| (Group::Lower, 0, LRect { x, y: 35, w, h: 1 });
        // Free row: centred on the inner span (1 + 40 / 2 = 21 → cells 19..24), one row above the inner area.
        assert_eq!(hsep_grip(lower, &[]), Some((19, 35)));
        // A block over the middle (17..27): the grip moves into the longest free run, one column clear of it.
        let (x0, _) = hsep_grip(lower, &[tab(17, 10)]).unwrap();
        assert!(
            x0 + GRIP_W < 17 || x0 > 27,
            "grip at {x0} touches the block 17..27"
        );
        // Other groups' tabs do not count; a row with no free run of GRIP_W cells has no grip.
        assert_eq!(
            hsep_grip(
                lower,
                &[(
                    Group::Workers,
                    0,
                    LRect {
                        x: 19,
                        y: 0,
                        w: 5,
                        h: 1
                    }
                )]
            ),
            Some((19, 35))
        );
        assert_eq!(hsep_grip(LRect { w: 4, ..lower }, &[]), None);
        assert_eq!(hsep_grip(lower, &[tab(1, 18), tab(21, 18)]), None);
    }

    #[test]
    fn copy_rows_stays_on_the_canvas() {
        let mut prev = Buffer::empty(Rect::new(0, 0, 10, 4));
        prev.set_string(0, 0, "0123456789", Style::new());
        prev.set_string(0, 1, "abcdefghij", Style::new());
        prev.set_string(0, 3, "ZZZZZZZZZZ", Style::new());
        let mut buf = Buffer::empty(prev.area);
        let r = LRect {
            x: 6,
            y: 0,
            w: 30,
            h: 9,
        };
        copy_rows(&prev, &mut buf, r, 2);
        let row = |b: &Buffer, y: u16| -> String {
            (0..10).map(|x| b[(x, y)].symbol().to_string()).collect()
        };
        assert_eq!(row(&buf, 0), "      6789");
        assert_eq!(row(&buf, 1), "      ghij");
        assert_eq!(row(&buf, 3).trim(), "", "rows from n on are not copied");
        copy_rows(
            &prev,
            &mut buf,
            LRect {
                x: 12,
                y: 0,
                w: 3,
                h: 2,
            },
            2,
        ); // off the canvas: no-op
    }

    #[test]
    fn dragged_separator_is_restyled_in_place() {
        let layout = fleetmux_layout::Layout::new();
        let rects = layout.rects(240, 60);
        let mut buf = Buffer::empty(Rect::new(0, 0, 240, 60));
        for g in Group::ALL {
            frame(&mut buf, group_rect(&rects, g), PLAIN);
        }
        highlight_separator(&mut buf, &rects, Hit::VSep, 0);
        assert_eq!(buf[(119, 30)].symbol(), "│");
        assert_eq!(buf[(119, 30)].style().fg, DRAGGED.fg);
        assert_eq!(
            buf[(50, 35)].style().fg,
            PLAIN.fg,
            "HSep untouched by a VSep drag"
        );
        highlight_separator(&mut buf, &rects, Hit::HSep, 0);
        assert_eq!(buf[(50, 35)].symbol(), "─");
        assert_eq!(buf[(50, 35)].style().fg, DRAGGED.fg);
        highlight_separator(&mut buf, &rects, Hit::Status, 0); // not a separator: no-op
    }

    #[test]
    fn frame_and_label_stay_on_canvas_and_truncate() {
        let mut buf = Buffer::empty(Rect::new(0, 0, 20, 5));
        frame(
            &mut buf,
            LRect {
                x: 1,
                y: 1,
                w: 30,
                h: 10,
            },
            PLAIN,
        ); // larger than the canvas: cropped
        assert_eq!(buf[(0, 0)].symbol(), "┌");
        assert_eq!(buf[(0, 3)].symbol(), "│");
        label(
            &mut buf,
            LRect {
                x: 2,
                y: 0,
                w: 5,
                h: 1,
            },
            "abcdefgh",
            Style::new(),
        );
        let row: String = (0..20).map(|x| buf[(x, 0)].symbol().to_string()).collect();
        assert!(row.starts_with("┌─abcde─"), "{row:?}");
    }

    /// The column of `buf` at `x` from row `y0` up to (not including) `y1`, as symbols.
    fn column(buf: &Buffer, x: u16, y0: u16, y1: u16) -> String {
        (y0..y1).map(|y| buf[(x, y)].symbol().to_string()).collect()
    }

    /// A zoomed pane draws no frame, so the sidebar band's column next to the content carries a thin grey `│` (the
    /// unfocused border style) from the content's top row down to the row above the status line: the left band's
    /// last (gap) column, the mirrored band's first column. Unzoomed, the content's own left border is the line,
    /// so the band adds none; without a sidebar there is no line either. The zoomed pane keeps its full width.
    #[test]
    fn a_zoomed_pane_gets_a_divider_beside_the_sidebar() {
        let (cols, rows) = (240u16, 60u16);
        let mut st = state();
        st.layout.insert("z".into(), fleetmux_layout::PaneType::Worker);
        st.layout.select("z".into());
        st.layout.set_sidebar(34);

        let buf = fresh(&st, cols, rows);
        assert_eq!(
            column(&buf, 33, 0, rows - 1).trim(),
            "",
            "unzoomed: the band's gap column stays blank (the content border at column 34 is the line)"
        );

        st.layout.toggle_zoom("z".into());
        let z = st.layout.rects(cols, rows).zoom.expect("zoomed");
        assert_eq!((z.x, z.w), (34, cols - 34), "the zoomed pane keeps every content column");
        let buf = fresh(&st, cols, rows);
        let line = "│".repeat(usize::from(rows - 1 - z.y));
        assert_eq!(column(&buf, 33, z.y, rows - 1), line, "left band: its last column");
        let c = &buf[(33, z.y)];
        assert_eq!(c.fg, PLAIN.fg.unwrap(), "the divider has the unfocused border colour: {c:?}");
        assert_ne!(buf[(33, rows - 1)].symbol(), "│", "the status line is not part of the divider");

        st.layout.set_mirror(true);
        let buf = fresh(&st, cols, rows);
        assert_eq!(column(&buf, cols - 34, z.y, rows - 1), line, "mirrored band: its first column");
        assert_eq!(column(&buf, 33, z.y, rows - 1).matches('│').count(), 0);

        st.layout.set_mirror(false);
        st.layout.set_sidebar(0);
        let buf = fresh(&st, cols, rows);
        assert!(
            !(1..rows - 1).any(|y| buf[(0, y)].symbol() == "│" || buf[(33, y)].symbol() == "│"),
            "no sidebar, no divider"
        );
    }

    /// The sidebar's scrollbar (cmux): only while the cards overflow, a grey `┃` thumb in the band's column next to
    /// the content — the gap column `sb − 1`, the mirrored band's first column — on the rows the thumb covers. With
    /// a frame it stands alone in the blank gap; over frameless content it replaces those rows of the `│` divider,
    /// which keeps every other row. Same colour as the divider.
    #[test]
    fn the_sidebar_scrollbar_thumb_sits_on_the_divider_column() {
        let (cols, rows) = (240u16, 60u16);
        let fleets = |n: usize| -> String {
            let cards: Vec<String> = (0..n)
                .map(|i| {
                    format!(
                        r#"{{"id":"f{i}","title":"fleet {i}","state":"running","line1":"a","line2":"b"}}"#
                    )
                })
                .collect();
            format!(r#"{{"v":1,"current":"f0","fleets":[{}]}}"#, cards.join(","))
        };
        let mut st = state();
        st.layout.insert("z".into(), fleetmux_layout::PaneType::Worker);
        st.layout.select("z".into());
        st.layout.set_sidebar(34);
        st.layout.set_topbar(15);
        let top = st.layout.header_rows();
        st.side.data = Some(super::super::sidebar::parse(&fleets(13)).unwrap());
        let buf = fresh(&st, cols, rows);
        assert_eq!(column(&buf, 33, top, rows - 1).matches('┃').count(), 0, "13 cards fit: no scrollbar");

        // 15 cards, 13 fit: track rows 2..=52 (51 rows), thumb 51 × 13 / 15 = 44 rows from the top.
        st.side.data = Some(super::super::sidebar::parse(&fleets(15)).unwrap());
        let buf = fresh(&st, cols, rows);
        let thumb = "┃".repeat(44);
        assert_eq!(column(&buf, 33, top, top + 44), thumb, "framed: the thumb in the gap column");
        assert_eq!(buf[(33, top)].fg, PLAIN.fg.unwrap(), "the divider's grey");
        assert_eq!(column(&buf, 33, top + 44, rows - 1).trim(), "", "framed: no track below the thumb");

        st.layout.toggle_zoom("z".into());
        let buf = fresh(&st, cols, rows);
        assert_eq!(column(&buf, 33, top, top + 44), thumb, "frameless: the thumb replaces the divider's rows");
        assert_eq!(
            column(&buf, 33, top + 44, rows - 1),
            "│".repeat(usize::from(rows - 1 - top - 44)),
            "frameless: the divider keeps its other rows"
        );
        assert_eq!(buf[(33, 0)].symbol(), "│", "the divider still runs from row 0");
        assert_eq!(buf[(33, 1)].symbol(), "├", "and meets the topbar's line in ├");

        st.layout.set_mirror(true);
        let buf = fresh(&st, cols, rows);
        assert_eq!(column(&buf, cols - 34, top, top + 44), thumb, "mirrored: the band's first column");
        assert_eq!(column(&buf, 33, top, rows - 1).matches('┃').count(), 0);
    }

    /// cmux look for frameless content (a zoomed pane) under the in-grid topbar (macOS): the sidebar divider is one
    /// grey `│` from row 0 down to the row above the status line (column `sb − 1`; mirrored `cols − sb`), and the
    /// topbar's blank separation row (row `top − 1` = 1) carries a grey `─` across the content only, meeting the
    /// divider in `├` (mirrored `┤`). Without a sidebar the `─` spans the whole width. The zoomed pane keeps its
    /// rows; unzoomed, and without a topbar row, row 1 / the rows stay as they were.
    #[test]
    fn a_zoomed_pane_gets_a_divider_under_the_topbar() {
        let (cols, rows) = (240u16, 60u16);
        let row = |buf: &Buffer, y: u16| -> String {
            (0..cols).map(|x| buf[(x, y)].symbol().to_string()).collect()
        };
        let mut st = state();
        st.layout.insert("z".into(), fleetmux_layout::PaneType::Worker);
        st.layout.select("z".into());
        st.layout.set_sidebar(34);
        st.layout.set_topbar(15);
        assert_eq!(st.layout.header_rows(), 2, "topbar row 0 + its blank separation row 1");

        let buf = fresh(&st, cols, rows);
        assert_eq!(row(&buf, 1).trim(), "", "unzoomed: the groups' top borders are the line");

        st.layout.toggle_zoom("z".into());
        let z = st.layout.rects(cols, rows).zoom.expect("zoomed");
        assert_eq!(z.y, 2, "the zoomed pane keeps every content row");
        let buf = fresh(&st, cols, rows);
        let want = format!("{}├{}", " ".repeat(33), "─".repeat(usize::from(cols - 34)));
        assert_eq!(row(&buf, 1), want, "left band");
        assert_eq!(buf[(34, 1)].fg, PLAIN.fg.unwrap(), "the unfocused border colour");
        assert_eq!(buf[(33, 1)].fg, PLAIN.fg.unwrap(), "the unfocused border colour");
        for y in [0, 2, rows - 2] {
            assert_eq!(buf[(33, y)].symbol(), "│", "the divider runs through row {y}");
        }

        st.layout.set_mirror(true);
        let buf = fresh(&st, cols, rows);
        let want = format!("{}┤{}", "─".repeat(usize::from(cols - 34)), " ".repeat(33));
        assert_eq!(row(&buf, 1), want, "mirrored band");
        assert_eq!(buf[(cols - 34, 0)].symbol(), "│", "mirrored: the divider reaches row 0");

        st.layout.set_mirror(false);
        st.layout.set_sidebar(0);
        let buf = fresh(&st, cols, rows);
        assert_eq!(row(&buf, 1), "─".repeat(usize::from(cols)), "no sidebar: the whole width");

        st.layout.set_topbar(0);
        let buf = fresh(&st, cols, rows);
        assert!(
            !(0..rows).any(|y| row(&buf, y).contains('─')),
            "no topbar row (a bare server): no line"
        );
    }

    /// S1d (plan §16.2 拖动中; supervisor ruling [M2] 2026-10-30): while the sidebar's edge is dragged, *every*
    /// content box's left border column is Yellow + bold — the supervisor's and the lower group's — from the top
    /// border row down to the row above the status line. The status line itself is not painted.
    #[test]
    fn dragged_sidebar_edge_restyles_the_whole_left_border_column() {
        let mut layout = fleetmux_layout::Layout::new();
        layout.set_sidebar(34);
        let rects = layout.rects(240, 60);
        let x = rects.supervisor.x.saturating_sub(1);
        assert_eq!(x, 34, "the 34-column sidebar's edge is column 34: {rects:?}");
        let mut buf = Buffer::empty(Rect::new(0, 0, 240, 60));
        for g in Group::ALL {
            frame(&mut buf, group_rect(&rects, g), PLAIN);
        }
        let sup_bottom = rects.supervisor.y.saturating_add(rects.supervisor.h);
        let lower_bottom = rects.lower.y.saturating_add(rects.lower.h);
        assert_eq!(
            lower_bottom,
            rects.status.y.saturating_sub(1),
            "the lower box's bottom border is the row above the status line: {rects:?}"
        );

        highlight_separator(&mut buf, &rects, Hit::SidebarEdge, 34);

        let dragged = |c: &Cell| {
            c.style().fg == DRAGGED.fg && c.style().add_modifier.contains(Modifier::BOLD)
        };
        for y in 0..=lower_bottom {
            let c = &buf[(x, y)];
            let area = if y <= sup_bottom { "supervisor" } else { "lower" };
            assert!(
                dragged(c),
                "({x},{y}) on the {area} group's left border is not the dragged sidebar edge's Yellow+bold: {c:?}"
            );
        }
        for (y, want) in [(0, '┌'), (sup_bottom, '┌'), (lower_bottom, '└')] {
            assert_eq!(
                buf[(x, y)].symbol(),
                want.to_string(),
                "({x},{y}) is not the border cell {want:?}: {:?}",
                buf[(x, y)]
            );
        }
        let status = &buf[(x, rects.status.y)];
        assert!(
            !dragged(status),
            "({x},{}) on the status line was painted as the dragged edge: {status:?}",
            rects.status.y
        );
        let other = &buf[(x.saturating_add(1), 45)];
        assert!(
            !dragged(other),
            "({},45) inside the lower pane was painted as the dragged edge: {other:?}",
            x + 1
        );
    }
}
