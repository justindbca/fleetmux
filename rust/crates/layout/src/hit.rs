//! Mouse hit testing and drag-to-ratio (brief-m03 §3.3; PROTOCOL.md §11; M1 `layout-hit`).
//!
//! Two tab geometries live here.
//!
//! **Titled blocks — the product path** (fix-m1-tabs, supervisor ruling [M49]; the user's tab requirement
//! overrides the [M30] marker-cell rule): [`Layout::tab_rects_titled`] / [`Layout::hit_test_titled`] are what
//! drawing and mouse code use. A tab is a block of a blank pad, its text (unread `• ` + title, whose display
//! width the caller passes as `label_cols`) and a blank pad. In a group whose inner area is `w` columns wide
//! and holds `n` tabs, a block is `min(label_cols + 2, w / n − 1, cap + 2)` columns, the first one starts at
//! the inner area's first column, the next one after a one-column gap; the caller truncates the text to
//! `block − 2` columns so both pads stay. Gaps and the rest of the row after the last block belong to no tab
//! (they stay border / separator); a block narrower than 3 columns is not drawn. Recomputed from
//! [`Layout::rects`] on every call, so a ratio drag moves the blocks.
//!
//! **Fixed slots — legacy, compatibility only**: [`Layout::tab_rects`] / [`Layout::hit_test`] keep the old
//! stage-A contract (rulings [M30] / [M31]) for their frozen tests and must not be used for new drawing or hit
//! testing. Every tab takes one marker cell followed by a title area of `min(w / n − 1, cap)` columns, the first
//! marker cell being the inner area's first column, whatever the title length.
//!
//! The cap depends on the row (ruling [M31], fix-m0-4), in both geometries: the lower group's labels sit on
//! the draggable separator row and keep 15 title columns, which is what lets (50,35) stay `HSep` with three
//! lower tabs; the supervisor and workers labels sit on the top border and may take 24, so a rotated
//! generation such as `fx-supervisor-1.1` (17) shows whole at 196 and 240 columns. With many tabs the equal
//! share (`w / n − 1`) still truncates.
//!
//! At 240×60 with 50 / 60: the first supervisor block at row 0 from x = 1 (legacy title area from x = 2), VSep = column 119, HSep = row 35 (left
//! column), status = row 59. With a topbar (S1f, [`Layout::topbar`]) every row moves down one and row 0
//! belongs to the topbar: its left part is `Sidebar`, the sidebar's edge columns stay `SidebarEdge`, the rest
//! is `Outside` — never a tab, the vsep column or a pane. With the content mirror (A26 rev3,
//! [`Layout::set_mirror`]) every test here uses the mirrored rects, so the workers' labels start at the content's
//! left border and the stack's at its right border; the sidebar band and its drag line move to the right.

use crate::{Group, Layout, PaneId, RATIO_MAX, RATIO_MIN, Rect};

/// Widest title area of one supervisor / workers tab label (top border row), in columns.
const TITLE_MAX_TOP: u16 = 24;
/// Widest title area of one lower-group tab label (the horizontal separator row), in columns.
const TITLE_MAX_LOWER: u16 = 15;
/// The blank columns left and right of the text in a titled tab block.
const BLOCK_PADS: u16 = 2;
/// The narrowest titled block that is drawn (pad, one column of text, pad).
const BLOCK_MIN: u16 = 3;

/// What a viewer cell is, for the mouse (PROTOCOL.md §11): a tab label, a group's pane area, one of the two
/// draggable separators, the status line, or nothing.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Hit {
    /// Tab label `index` (0-based, tab order) of `group`.
    Tab { group: Group, index: usize },
    /// Inside the selected pane of `group` (or the zoomed pane's group while zoomed).
    Pane(Group),
    /// The vertical separator between the left column and the workers group (drag = `h_ratio`).
    VSep,
    /// The horizontal separator between supervisor and lower in the left column (drag = `v_ratio`).
    HSep,
    /// The bottom status row.
    Status,
    /// A cell of the fleet sidebar: above the status row, in the leftmost [`Layout::sidebar_cols`] columns.
    Sidebar,
    /// The sidebar's right edge while it is shown with at least two columns: the gap column (`sb − 1`) and the
    /// content's left border column (`sb`). A drag here resizes the sidebar (plan §16.2 S1d).
    SidebarEdge,
    /// Any other cell (borders, corners, outside the `cols` × `rows` viewer).
    Outside,
}

impl Layout {
    /// Classify cell (`x`, `y`) of a `cols` × `rows` viewer, consistent with [`Layout::rects`] and
    /// [`Layout::tab_rects`]. At 240×60 with 50 / 60: (5,0) is supervisor tab 0, (60,10) the supervisor
    /// pane, (119,30) the vertical separator, (50,35) the horizontal separator, (10,59) the status line.
    pub fn hit_test(&self, cols: u16, rows: u16, x: u16, y: u16) -> Hit {
        self.hit_with_tabs(cols, rows, x, y, || self.tab_rects(cols, rows))
    }

    /// [`Layout::hit_test`] for the titled tab blocks of [`Layout::tab_rects_titled`] (same `label_cols`):
    /// a tab is hit only inside its block, never in a gap between blocks or in the row after the last one.
    pub fn hit_test_titled(
        &self,
        cols: u16,
        rows: u16,
        x: u16,
        y: u16,
        label_cols: impl Fn(&PaneId) -> u16,
    ) -> Hit {
        self.hit_with_tabs(cols, rows, x, y, || {
            self.tab_rects_titled(cols, rows, label_cols)
        })
    }

    /// The classification shared by both hit tests; `tabs` gives the tab rectangles to test first.
    fn hit_with_tabs(
        &self,
        cols: u16,
        rows: u16,
        x: u16,
        y: u16,
        tabs: impl FnOnce() -> Vec<(Group, usize, Rect)>,
    ) -> Hit {
        if x >= cols || y >= rows {
            return Hit::Outside;
        }
        let r = self.rects(cols, rows);
        if y == r.status.y {
            return Hit::Status;
        }
        let sb = self.sidebar_cols(cols);
        // A26 rev3: the sidebar band is on the left, or on the right while the content is mirrored. Its drag
        // line is the two columns at its inner edge: the gap column and the content's border column.
        let (edge_a, edge_b, in_sidebar) = if self.sidebar_on_right() {
            let inner = cols.saturating_sub(sb);
            (inner.saturating_sub(1), inner, x >= inner)
        } else {
            (sb.saturating_sub(1), sb, x < sb)
        };
        if sb >= 2 && (x == edge_a || x == edge_b) {
            // The sidebar's right (or, mirrored, left) edge: the gap column and the content's border column
            // (plan §16.2 S1d).
            return Hit::SidebarEdge;
        }
        if self.topbar > 0 && y == 0 {
            // S1f: row 0 is the topbar — its left part, and `Outside` for the divider gap and the right part.
            return if x < self.topbar_left(cols) {
                Hit::Sidebar
            } else {
                Hit::Outside
            };
        }
        if y < self.header_rows() {
            // A26 rev7: the rest of the header band (the topbar's separation row, or the Linux headerbar's blank
            // row under a grid that has no topbar) belongs to no tab, separator or pane.
            return Hit::Outside;
        }
        if in_sidebar {
            // The fleet sidebar keeps its columns while zoomed too.
            return Hit::Sidebar;
        }
        if let Some(zoom) = r.zoom {
            // Zoomed: the three groups are hidden; the whole content area is the zoomed pane.
            let group = self
                .zoom
                .as_deref()
                .and_then(|id| self.find(id))
                .map(|(g, _)| g);
            return match group {
                Some(g) if contains(zoom, x, y) => Hit::Pane(g),
                _ => Hit::Outside,
            };
        }
        if let Some((group, index, _)) = tabs().into_iter().find(|(_, _, t)| contains(*t, x, y)) {
            return Hit::Tab { group, index };
        }
        // A26 rev3: the separator is the column between the two columns of the content. Unmirrored it is the
        // stack's right border; mirrored the stack is on the right, so it is the stack's left neighbour.
        let stack_right = r.supervisor.x + r.supervisor.w;
        let vsep = if self.mirror() {
            r.supervisor.x.saturating_sub(1)
        } else {
            stack_right
        };
        let hsep = r.supervisor.y + r.supervisor.h;
        if x == vsep {
            return Hit::VSep;
        }
        if y == hsep && x >= r.supervisor.x && x < stack_right {
            return Hit::HSep;
        }
        for (g, rect) in [
            (Group::Supervisor, r.supervisor),
            (Group::Lower, r.lower),
            (Group::Workers, r.workers),
        ] {
            if contains(rect, x, y) {
                return Hit::Pane(g);
            }
        }
        Hit::Outside
    }

    /// `h_ratio` for a vertical-separator drag at column `x` of a `cols`-wide viewer while the fleet sidebar is
    /// shown: the same as [`ratio_from_x`] in content coordinates, so the drag does not jump when the sidebar
    /// appears (`x = 136` of 240 with a 34-column sidebar → 50). Mirrored, the separator is the content's mirror
    /// of that column (`inner − 1 − vsep`), so the ratio is measured from the content's right edge instead.
    pub fn ratio_from_x_in(&self, cols: u16, x: u16) -> u8 {
        let inner = cols.saturating_sub(self.sidebar_cols(cols));
        match self.content_x(cols, x) {
            Some(c) if self.mirror() => ratio_from_x_mirrored(inner, c),
            Some(c) => ratio_from_x(inner, c),
            None => ratio_from_x(inner, 0),
        }
    }

    /// The label rectangle (height 1) of every tab, as `(group, index, rect)` in group order and tab order
    /// within a group. Labels stay inside their group's width: long titles are truncated so that a group's
    /// labels never run into the next group or the separator.
    pub fn tab_rects(&self, cols: u16, rows: u16) -> Vec<(Group, usize, Rect)> {
        let r = self.rects(cols, rows);
        if r.zoom.is_some() {
            return Vec::new(); // zoomed: no tab rows are drawn (design §3.1)
        }
        let hsep = r.supervisor.y + r.supervisor.h;
        let top = r.supervisor.y.saturating_sub(1);
        let mut out = Vec::new();
        for (g, inner, row, cap) in [
            (Group::Supervisor, r.supervisor, top, TITLE_MAX_TOP),
            (Group::Lower, r.lower, hsep, TITLE_MAX_LOWER),
            (Group::Workers, r.workers, top, TITLE_MAX_TOP),
        ] {
            let n = self.group(g).len();
            if n == 0 || inner.w < 2 {
                continue;
            }
            // `n` × (marker + title) never exceeds the inner width, because the title area is at most share − 1.
            let share = (inner.w as usize / n) as u16;
            let title = share.saturating_sub(1).min(cap);
            if title == 0 {
                continue; // too narrow to show any label
            }
            for i in 0..n {
                let x = inner.x + 1 + i as u16 * (title + 1);
                out.push((
                    g,
                    i,
                    Rect {
                        x,
                        y: row,
                        w: title,
                        h: 1,
                    },
                ));
            }
        }
        out
    }

    /// The titled tab blocks (fix-m1-tabs, the geometry new drawing and mouse code use), as `(group, index,
    /// rect)` in group order and tab order. `label_cols(id)` is the display width of the text inside the block
    /// (an unread `• ` plus the title), without the pads. See the module doc for the geometry.
    pub fn tab_rects_titled(
        &self,
        cols: u16,
        rows: u16,
        label_cols: impl Fn(&PaneId) -> u16,
    ) -> Vec<(Group, usize, Rect)> {
        let r = self.rects(cols, rows);
        if r.zoom.is_some() {
            return Vec::new(); // zoomed: no tab rows are drawn (design §3.1)
        }
        let hsep = r.supervisor.y + r.supervisor.h;
        let top = r.supervisor.y.saturating_sub(1);
        let mut out = Vec::new();
        for (g, inner, row, cap) in [
            (Group::Supervisor, r.supervisor, top, TITLE_MAX_TOP),
            (Group::Lower, r.lower, hsep, TITLE_MAX_LOWER),
            (Group::Workers, r.workers, top, TITLE_MAX_TOP),
        ] {
            let panes = self.group(g);
            if panes.is_empty() {
                continue;
            }
            // Every block plus its gap fits in one share, so the group's blocks never leave its inner width.
            let share = (inner.w as usize / panes.len()) as u16;
            let max = share.saturating_sub(1).min(cap.saturating_add(BLOCK_PADS));
            let mut x = inner.x;
            for (i, id) in panes.iter().enumerate() {
                let w = label_cols(id).saturating_add(BLOCK_PADS).min(max);
                if w < BLOCK_MIN {
                    continue; // no room for a pad, one column of text and a pad
                }
                out.push((g, i, Rect { x, y: row, w, h: 1 }));
                x = x.saturating_add(w + 1); // the block, then one gap column
            }
        }
        out
    }
}

/// `h_ratio` for a vertical-separator drag released at column `x` of a `cols`-wide viewer, in percent,
/// clamped to 10..=90 (x = 120 of 240 → 50).
pub fn ratio_from_x(cols: u16, x: u16) -> u8 {
    // Inverse of `rects`: the separator sits on column round(cols · h / 100) − 1.
    percent(u32::from(x) + 1, cols)
}

/// `h_ratio` for a vertical-separator drag at content column `x` of a mirrored `inner`-wide content area: the
/// separator sits on column `inner − round(inner · h / 100)`, so `x = 120` of 240 → 50 as well.
pub fn ratio_from_x_mirrored(inner: u16, x: u16) -> u8 {
    percent(u32::from(inner.saturating_sub(x)), inner)
}

/// `v_ratio` for a horizontal-separator drag released at row `y` of a left column `left_rows` rows tall,
/// in percent, clamped to 10..=90.
pub fn ratio_from_y(left_rows: u16, y: u16) -> u8 {
    // Inverse of `rects` when `left_rows` is the content height (`rows − 1`): the separator sits on row
    // round(left_rows · v / 100).
    percent(u32::from(y), left_rows)
}

impl Layout {
    /// [`ratio_from_y`] for a `rows`-tall viewer whose top rows may be the header band (S1f + A26 rev7): the drag
    /// row is taken relative to the content, so the separator row gives the same ratio with and without a header.
    /// Without a header this is `ratio_from_y(rows − 1, y)`.
    pub fn ratio_from_y_in(&self, rows: u16, y: u16) -> u8 {
        let top = self.header_rows();
        ratio_from_y(rows.saturating_sub(1 + top), y.saturating_sub(top))
    }
}

/// round(`part` · 100 / `whole`), clamped to the ratio range; a zero-sized axis gives the middle.
fn percent(part: u32, whole: u16) -> u8 {
    if whole == 0 {
        return 50;
    }
    let whole = u32::from(whole);
    let p = (part * 100 + whole / 2) / whole;
    p.clamp(u32::from(RATIO_MIN), u32::from(RATIO_MAX)) as u8
}

fn contains(r: Rect, x: u16, y: u16) -> bool {
    x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h
}
