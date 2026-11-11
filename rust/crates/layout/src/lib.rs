//! fleetmux-layout: the fixed three-group layout as a pure model (design.md §3, plan.md §3.3).
//!
//! No I/O, no terminal: the server keeps one [`Layout`] per run, feeds it spawn / move / kill / select
//! events and asks it for rectangles (M0, `layout-impl`) and for mouse hit testing ([`Layout::hit_test`],
//! [`Layout::tab_rects`], [`ratio_from_x`], [`ratio_from_y`]; M1, `layout-hit`, in `src/hit.rs`).
//!
//! Rules the tests in `tests/model.rs` pin (design.md section in brackets):
//! - Groups: `Supervisor` = top left, `Lower` = bottom left (history / control / `tabs:`), `Workers` = right.
//! - Ratios: `h_ratio` = left column width in percent (default 50), `v_ratio` = supervisor share of the
//!   left column in percent (default 60); both clamp to 10..=90 [§3.1].
//! - Placement on insert by pane type [§3.4]: supervisor → Supervisor first; advisor → Supervisor last;
//!   worker → Workers last; daemon → Lower, right after the history pane (first when there is none);
//!   tab → Lower last; history → Lower first; placeholder / unknown → Workers last.
//! - Selection on remove [§3.2]: if the removed pane was the group's selected tab, the selection goes to
//!   the pane that was just moved in front of it (the successor of a rotation); otherwise to its left
//!   neighbour. Removing a pane that is not selected leaves the selection alone.
//! - Rectangles [§2.5, §3.1]: every [`Rect`] is the inner, PTY-sized area after borders and tab bars.
//!   The bottom row is the status line. At 240×60 with 50/60 the estimates are supervisor ≈118×34,
//!   lower ≈118×22, workers ≈119×57. Zoomed: the zoomed pane gets the whole content area, i.e. full
//!   width × (rows − 1), with only the status line left.
//! - JSON (proto-1 `protocol-draft.md` §4.14 / §8.3, ruled by the supervisor): the *layout object* is
//!   `h_ratio` int, `v_ratio` int, `zoom` pane id or `null`, `focus` group, `groups` object
//!   group → array of pane ids in tab order, `selected` object group → pane id or `null`; group names are
//!   `supervisor` / `lower` / `workers`. [`Layout::to_object`] returns exactly that object (the
//!   `layout.get` / `layout.set` reply body); [`Layout::to_json`] is the `layout.json` text = the same
//!   object plus `"version": 1`. These seven keys are fixed; pane type info may be kept as an extra key beside them (never instead of one), and `from_json` need not restore it.
//!   `from_json` accepts unknown extra keys, rejects a ratio outside 10..=90 with
//!   [`LayoutError::BadRatio`] (no clamping there; only `set_ratios` clamps) and wrong JSON / types with
//!   [`LayoutError::BadJson`].
//! - Not this crate: a new server reuses only `h_ratio` / `v_ratio` from an old `layout.json` and drops
//!   the pane references of an earlier epoch (protocol-draft §8.3); that filtering is the server's job.
//! - Sidebar (S1): [`Layout::sidebar`] is the number of columns the caller asks to reserve for the fleet
//!   sidebar's band (0 = none). [`Layout::sidebar_cols`] gives the width actually drawn: the asked width while
//!   `cols >= sidebar + SIDEBAR_MIN_CONTENT` (compared in `u32`, so the wire's maximum `sidebar` cannot overflow a
//!   `u16`), otherwise 0. [`Layout::rects`] computes the three groups in the
//!   remaining content columns and places them beside that band — right of it by default, left of it when the
//!   content is mirrored ([`Layout::set_mirror`]) — so every consumer of `rects` (drawing, mouse, PTY sizes,
//!   overlays) follows; the status row keeps the full width and a zoomed pane fills the content area.
//!   [`Layout::sidebar_rect`] is the band itself. The width is runtime state: it is not in `to_object` /
//!   `to_json` / `from_json`.
//! - Topbar (S1f): [`Layout::topbar`] is the width the topbar's left part keeps when the sidebar is not
//!   drawn (0 = no topbar). While it is set, the first [`TOPBAR_ROWS`] rows are the header: row 0 is the topbar
//!   itself and the rest is one blank separation row, so every rect moves down that band ([`Layout::rects`])
//!   and none of those rows is ever a tab row, the vsep column or a pane ([`Layout::hit_test`]).
//!   [`Layout::topbar_left`] is the width of the left part in a `cols`-wide viewer. Also runtime state only.
//! - Header band (A26 rev7, CURRENT_REQUIREMENTS rev 10/11): [`Layout::header`] is the rows the grid keeps blank
//!   under a header that lives outside it (the Linux GTK headerbar: [`HEADER_GAP`], 1). [`Layout::header_rows`]
//!   adds [`TOPBAR_ROWS`] while a topbar is set, so on both platforms the workspace's topmost tab row and the
//!   sidebar's first card start on the same row, one separation row below the header.
//! - Content mirror (A26 rev3, CURRENT_REQUIREMENTS rev 4 `content_layout`): [`Layout::set_mirror`] flips the
//!   content left/right — the fleet sidebar becomes the right band, `workers` the left column and the
//!   supervisor/lower stack the middle — while the macOS layout (left sidebar, stack next to it, workers on the
//!   right) is the default. It is applied by [`Layout::rects`], so drawing, mouse hit testing, both ratio drags,
//!   the history overlay, the scrollbars, the PTY sizes and the zoom rectangle all follow; the status row and
//!   row 0 (the topbar, whose toolbar stays on the left and whose fleet title/status stays on the right) do not
//!   move. Runtime state derived from the platform at boot, so it is not in `layout.json` and never restored
//!   from it; a resumed server sets it again.
//!
//! `layout.json` example:
//!
//! ```text
//! {"version":1,"h_ratio":50,"v_ratio":60,"zoom":null,"focus":"supervisor",
//!  "groups":{"supervisor":["k7q2-1"],"lower":["k7q2-2"],"workers":[]},
//!  "selected":{"supervisor":"k7q2-1","lower":"k7q2-2","workers":null}}
//! ```

use std::collections::{HashMap, HashSet};
use std::fmt;

use serde_json::{Map, Value, json};

/// Default `h_ratio` / `v_ratio` in percent (design §3.1).
const DEFAULT_RATIOS: (u8, u8) = (50, 60);
/// Both ratios clamp to this range.
const RATIO_MIN: u8 = 10;
const RATIO_MAX: u8 = 90;

/// Columns the content (the three groups) keeps to the right of the sidebar; with fewer, the sidebar is not
/// drawn at all and the groups use every column.
pub const SIDEBAR_MIN_CONTENT: u16 = 60;

/// Narrowest sidebar a drag may leave, in columns (plan §16.2 S1d).
pub const SIDEBAR_MIN_W: u16 = 24;
/// Widest requested sidebar width; a measured native-header floor may exceed it.
pub const SIDEBAR_MAX_W: u16 = 60;
/// Rows the in-grid topbar takes while [`Layout::topbar`] is set (A26 rev7): the controls row (row 0) and one
/// blank separation row under it, so the workspace's topmost tab row and the sidebar's first card start on the
/// same content line, with clear space between the global header and that line
/// (CURRENT_REQUIREMENTS rev 10 `macos_header_tab_separation` / rev 11 `workspace_tabs_align_first_fleet`).
pub const TOPBAR_ROWS: u16 = 2;

/// Rows the grid keeps blank under a header that is outside it (A26 rev7): the Linux GTK headerbar is the window's
/// titlebar, so the grid starts under it, and this one blank row is the same separation row [`TOPBAR_ROWS`]
/// carries under the macOS topbar — both platforms then start the tab row and the first card on the same line.
pub const HEADER_GAP: u16 = 1;

/// The widest sidebar a drag may leave in a `cols`-wide viewer: [`SIDEBAR_MAX_W`], but never so wide that the
/// content would have fewer than [`SIDEBAR_MIN_CONTENT`] columns (plan §16.2). `sidebar_max(240) == 60`,
/// `sidebar_max(100) == 40`, `sidebar_max(80) == 20`.
pub fn sidebar_max(cols: u16) -> u16 {
    SIDEBAR_MAX_W.min(cols.saturating_sub(SIDEBAR_MIN_CONTENT))
}

/// Pane id as the server hands it out: `<epoch>-<seq>`, e.g. `k7q2-12` (design §2.4).
pub type PaneId = String;

/// The three fixed tab groups.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Group {
    /// Top left: supervisor first, advisor after it.
    Supervisor,
    /// Bottom left: history first, control (daemon) next, roster `tabs:` after.
    Lower,
    /// Right: workers in roster order.
    Workers,
}

impl Group {
    /// All groups in storage order (index 0, 1, 2).
    pub const ALL: [Group; 3] = [Group::Supervisor, Group::Lower, Group::Workers];

    /// Protocol name: `"supervisor"`, `"lower"` or `"workers"` (protocol-draft §1.5).
    pub fn name(self) -> &'static str {
        match self {
            Group::Supervisor => "supervisor",
            Group::Lower => "lower",
            Group::Workers => "workers",
        }
    }

    /// Inverse of [`Group::name`]; `None` for any other string.
    pub fn from_name(s: &str) -> Option<Group> {
        Group::ALL.into_iter().find(|g| g.name() == s)
    }

    fn index(self) -> usize {
        self as usize
    }
}

/// `metadata['type']` of a spawned pane; decides where it lands first (design §3.4).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PaneType {
    Supervisor,
    Advisor,
    Worker,
    Daemon,
    Tab,
    History,
    Placeholder,
    Unknown,
}

/// Where `move_to` puts a pane inside the target group.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Pos {
    /// Directly in front of this pane (which must be in the target group).
    Before(PaneId),
    /// Directly behind this pane (which must be in the target group).
    After(PaneId),
    /// Last in the target group.
    End,
}

/// A cell rectangle in viewer coordinates (0-based, columns × rows).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub struct Rect {
    pub x: u16,
    pub y: u16,
    pub w: u16,
    pub h: u16,
}

/// Result of [`Layout::rects`]: the inner (PTY-sized) area of each group, the zoomed pane's area when a
/// pane is zoomed, and the one-row status line at the bottom.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Rects {
    pub supervisor: Rect,
    pub lower: Rect,
    pub workers: Rect,
    /// `Some` only while a pane is zoomed: full width × (rows − 1).
    pub zoom: Option<Rect>,
    /// Bottom row, full width, height 1.
    pub status: Rect,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LayoutError {
    /// The pane being moved is not in the layout.
    NoSuchPane(PaneId),
    /// `Pos::Before` / `Pos::After` names a pane that is not in the target group.
    AnchorNotInGroup(PaneId),
    /// `layout.json` carries a ratio outside 10..=90 (`which` = `"h_ratio"` or `"v_ratio"`).
    BadRatio { which: &'static str, value: i64 },
    /// `layout.json` is not valid JSON or misses a required key.
    BadJson(String),
}

impl fmt::Display for LayoutError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LayoutError::NoSuchPane(id) => write!(f, "no such pane: {id}"),
            LayoutError::AnchorNotInGroup(id) => {
                write!(f, "anchor pane {id} is not in the target group")
            }
            LayoutError::BadRatio { which, value } => {
                write!(f, "{which} {value} is outside {RATIO_MIN}..={RATIO_MAX}")
            }
            LayoutError::BadJson(why) => write!(f, "bad layout.json: {why}"),
        }
    }
}

impl std::error::Error for LayoutError {}

/// The layout of one run: three ordered tab groups, the selected tab of each, the focused group, the
/// two split ratios and the zoomed pane.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Layout {
    groups: [Vec<PaneId>; 3],
    selected: [Option<PaneId>; 3],
    focus: Group,
    h_ratio: u8,
    v_ratio: u8,
    zoom: Option<PaneId>,
    /// Columns reserved for the fleet sidebar's band — the left band on macOS, the right band when the content is
    /// mirrored (Linux); 0 = no sidebar. Runtime state only: not part of `layout.json` / [`Layout::to_object`].
    sidebar: u16,
    /// Columns the topbar's left part keeps when the sidebar is not drawn; 0 = no topbar. Runtime state
    /// only: not part of `layout.json` / [`Layout::to_object`].
    topbar: u16,
    /// A26 rev7: rows the grid keeps blank under a header outside it (the Linux GTK headerbar, [`HEADER_GAP`]);
    /// 0 on macOS, whose header is the in-grid topbar. Runtime state only: not part of `layout.json` /
    /// [`Layout::to_object`].
    header: u16,
    /// A26 rev3: the content mirror (Linux). `false` = the macOS layout: sidebar left, stack next to it, workers
    /// right. `true` = workers left, stack middle, sidebar right. Runtime state only: not part of `layout.json` /
    /// [`Layout::to_object`].
    mirror: bool,
    /// Sidebar placement and native header are independent of content ordering (runtime only).
    sidebar_left: bool,
    native_header: bool,
    /// Pane type of every pane in `groups` (daemon placement needs to find the history pane).
    types: HashMap<PaneId, PaneType>,
}

impl Default for Layout {
    fn default() -> Self {
        Layout::new()
    }
}

impl Layout {
    /// Empty groups, nothing selected, focus on `Supervisor`, ratios 50 / 60, not zoomed.
    pub fn new() -> Self {
        Layout {
            groups: Default::default(),
            selected: Default::default(),
            focus: Group::Supervisor,
            h_ratio: DEFAULT_RATIOS.0,
            v_ratio: DEFAULT_RATIOS.1,
            zoom: None,
            sidebar: 0,
            topbar: 0,
            header: 0,
            mirror: false,
            sidebar_left: false,
            native_header: false,
            types: HashMap::new(),
        }
    }

    /// Both platforms use sidebar left, supervisor/lower in the middle, workers right.
    /// Linux's native header and its gap are independent of content ordering; none is persisted.
    pub fn for_platform() -> Self {
        let mut l = Self::new();
        l.set_sidebar_left(cfg!(target_os = "linux"));
        l.set_native_header(cfg!(target_os = "linux"));
        // A26 rev7: the Linux window's header is the native GTK headerbar, outside the grid, so the grid keeps the
        // separation row under it; macOS's header is grid row 0 ([`Layout::topbar`]).
        if cfg!(target_os = "linux") {
            l.set_header(HEADER_GAP);
        }
        l
    }

    /// Ask for a `cols`-wide fleet sidebar band; 0 removes it. The width actually drawn is
    /// [`Layout::sidebar_cols`] (it is dropped while the content would be narrower than
    /// [`SIDEBAR_MIN_CONTENT`]); which side the band takes is [`Layout::set_mirror`].
    pub fn set_sidebar(&mut self, cols: u16) {
        self.sidebar = cols;
    }

    /// The sidebar width asked for (0 = none).
    pub fn sidebar(&self) -> u16 {
        self.sidebar
    }

    /// Reserve row 0 for the topbar; `cols` is the width its left part keeps while the sidebar is not drawn
    /// (0 removes the topbar). See [`Layout::topbar_left`].
    pub fn set_topbar(&mut self, cols: u16) {
        self.topbar = cols;
    }

    /// The width the topbar's left part keeps while the sidebar is not drawn (0 = no topbar).
    pub fn topbar(&self) -> u16 {
        self.topbar
    }

    /// A26 rev7: ask the grid to keep `rows` blank rows under a header that lives outside it (the Linux GTK
    /// headerbar); 0 = the header is the in-grid topbar or there is none. See [`Layout::header_rows`].
    pub fn set_header(&mut self, rows: u16) {
        self.header = rows;
    }

    /// The blank rows kept under an outside header ([`Layout::set_header`]).
    pub fn header(&self) -> u16 {
        self.header
    }

    /// A26 rev7: the rows above the content that the header band owns — [`Layout::header`] plus [`TOPBAR_ROWS`]
    /// while the in-grid topbar is set. [`Layout::rects`] starts the content below it and
    /// [`Layout::hit_test`] never reports a tab, separator or pane there. On macOS that is 2 while the sidebar is
    /// shown (topbar row 0 + one blank row) and 0 without a topbar; on Linux it is 1 (the native headerbar is
    /// outside the grid).
    pub fn header_rows(&self) -> u16 {
        if self.native_header {
            // A terminal viewer can use the GTK gap for row-0 tools without moving shared panes.
            self.header.max(u16::from(self.topbar > 0))
        } else {
            self.header + if self.topbar > 0 { TOPBAR_ROWS } else { 0 }
        }
    }

    /// A26 rev3: mirror the content left/right (Linux). See the crate docs; runtime state only.
    pub fn set_mirror(&mut self, on: bool) {
        self.mirror = on;
    }

    /// Keep the sidebar left even under an explicit legacy content mirror.
    pub fn set_sidebar_left(&mut self, on: bool) {
        self.sidebar_left = on;
    }

    pub fn sidebar_on_right(&self) -> bool {
        self.mirror && !self.sidebar_left
    }

    pub fn native_header(&self) -> bool {
        self.native_header
    }

    pub fn set_native_header(&mut self, on: bool) {
        self.native_header = on;
    }

    /// Whether the content is explicitly mirrored.
    pub fn mirror(&self) -> bool {
        self.mirror
    }

    /// The width of the topbar's left part in a `cols`-wide viewer: the drawn sidebar width when it is wider
    /// than the toolbar, otherwise the toolbar width; 0 when there is no topbar. Mirrored, the sidebar is the
    /// right band, so the left part is always just the toolbar.
    pub fn topbar_left(&self, cols: u16) -> u16 {
        if self.topbar == 0 {
            0
        } else if self.sidebar_on_right() {
            self.topbar.min(cols)
        } else {
            self.sidebar_cols(cols).max(self.topbar).min(cols)
        }
    }

    /// The band the fleet sidebar is drawn in (`None` while it is dropped): the left columns, or the right
    /// columns when the content is mirrored. Its height is the layout rows above the status line.
    pub fn sidebar_rect(&self, cols: u16, rows: u16) -> Option<Rect> {
        let sb = self.sidebar_cols(cols);
        (sb > 0).then(|| Rect {
            x: if self.sidebar_on_right() { cols - sb } else { 0 },
            y: 0,
            w: sb,
            h: rows.saturating_sub(1),
        })
    }

    /// A26 rev3: `x` (a viewer column) mapped into the content area's own coordinates, `None` when it is in the
    /// sidebar band instead. Unmirrored the content starts right of the sidebar; mirrored it starts at column 0.
    pub fn content_x(&self, cols: u16, x: u16) -> Option<u16> {
        let sb = self.sidebar_cols(cols);
        if self.sidebar_on_right() {
            (x < cols - sb).then_some(x)
        } else {
            (x >= sb && x < cols).then(|| x - sb)
        }
    }

    /// The sidebar width drawn in a `cols`-wide viewer: the asked width, or 0 when that would leave the
    /// content fewer than [`SIDEBAR_MIN_CONTENT`] columns.
    ///
    /// A42-LINUX-SIDEBAR-ALIGN-03 (M81 RB-01/RB-02): the asked width can be the wire's `u16::MAX`
    /// (`proto::view::SIDEBAR_MIN_MAX`), so the content check is done in `u32`. `sidebar + SIDEBAR_MIN_CONTENT`
    /// does not fit a `u16`: a debug build must not panic on it and a release build must not wrap into drawing a
    /// band wider than the viewer. The rule itself is unchanged — a band that cannot leave the content
    /// `SIDEBAR_MIN_CONTENT` columns is not drawn at all.
    pub fn sidebar_cols(&self, cols: u16) -> u16 {
        if self.sidebar > 0
            && u32::from(cols) >= u32::from(self.sidebar) + u32::from(SIDEBAR_MIN_CONTENT)
        {
            self.sidebar
        } else {
            0
        }
    }

    /// Put a new pane where its type says (design §3.4) and return the group it landed in.
    pub fn insert(&mut self, id: PaneId, ty: PaneType) -> Group {
        if self.find(&id).is_some() {
            self.remove(id.clone());
        }
        let (group, at) = match ty {
            PaneType::Supervisor => (Group::Supervisor, 0),
            PaneType::Advisor => (Group::Supervisor, self.group(Group::Supervisor).len()),
            PaneType::History => (Group::Lower, 0),
            PaneType::Daemon => {
                let lower = self.group(Group::Lower);
                let history = lower
                    .iter()
                    .position(|p| self.types.get(p) == Some(&PaneType::History));
                (Group::Lower, history.map_or(0, |i| i + 1))
            }
            PaneType::Tab => (Group::Lower, self.group(Group::Lower).len()),
            PaneType::Worker | PaneType::Placeholder | PaneType::Unknown => {
                (Group::Workers, self.group(Group::Workers).len())
            }
        };
        self.groups[group.index()].insert(at, id.clone());
        self.types.insert(id, ty);
        group
    }

    /// Move a pane into `group` at `pos` (`tab_into` / `place_right_of` / `dock_*`). On error the layout
    /// is unchanged.
    pub fn move_to(&mut self, id: PaneId, group: Group, pos: Pos) -> Result<(), LayoutError> {
        let Some((from, _)) = self.find(&id) else {
            return Err(LayoutError::NoSuchPane(id));
        };
        if let Pos::Before(anchor) | Pos::After(anchor) = &pos
            && (*anchor == id || !self.group(group).contains(anchor))
        {
            return Err(LayoutError::AnchorNotInGroup(anchor.clone()));
        }
        // Leaving a group hands its selection on like a removal; moving inside a group keeps it.
        let at = self.groups[from.index()]
            .iter()
            .position(|p| *p == id)
            .expect("found above");
        self.groups[from.index()].remove(at);
        if from != group && self.selected[from.index()].as_ref() == Some(&id) {
            self.selected[from.index()] = Self::heir(&self.groups[from.index()], at);
        }
        let target = &mut self.groups[group.index()];
        let at = match &pos {
            Pos::Before(anchor) => target
                .iter()
                .position(|p| p == anchor)
                .expect("checked above"),
            Pos::After(anchor) => {
                target
                    .iter()
                    .position(|p| p == anchor)
                    .expect("checked above")
                    + 1
            }
            Pos::End => target.len(),
        };
        target.insert(at, id);
        Ok(())
    }

    /// Take a pane out and return the selected tab of its group afterwards (design §3.2 rule); `None`
    /// when the group is now empty or the pane was unknown.
    pub fn remove(&mut self, id: PaneId) -> Option<PaneId> {
        let (g, at) = self.find(&id)?;
        self.groups[g.index()].remove(at);
        self.types.remove(&id);
        if self.zoom.as_ref() == Some(&id) {
            self.zoom = None;
        }
        if self.selected[g.index()].as_ref() == Some(&id) {
            // design §3.2: the successor of a rotation was moved directly in front of the old pane, so it
            // is the left neighbour; with no left neighbour the new first tab takes over.
            self.selected[g.index()] = Self::heir(&self.groups[g.index()], at);
        }
        self.selected[g.index()].clone()
    }

    /// Make `id` the selected tab of its group; `false` when the pane is unknown.
    pub fn select(&mut self, id: PaneId) -> bool {
        match self.find(&id) {
            Some((g, _)) => {
                self.selected[g.index()] = Some(id);
                true
            }
            None => false,
        }
    }

    /// Give keyboard focus to group `g`.
    pub fn focus_group(&mut self, g: Group) {
        self.focus = g;
    }

    /// Select the next tab of the focused group (wraps).
    pub fn next_tab(&mut self) {
        self.step_tab(1);
    }

    /// Select the previous tab of the focused group (wraps).
    pub fn prev_tab(&mut self) {
        self.step_tab(-1);
    }

    /// Select the `n`-th tab (0-based) of the focused group; out of range does nothing.
    pub fn select_nth(&mut self, n: usize) {
        let g = self.focus.index();
        if let Some(id) = self.groups[g].get(n) {
            self.selected[g] = Some(id.clone());
        }
    }

    /// Set left/right (`h`) and top/bottom (`v`) ratios in percent, each clamped to 10..=90.
    pub fn set_ratios(&mut self, h: u8, v: u8) {
        self.h_ratio = h.clamp(RATIO_MIN, RATIO_MAX);
        self.v_ratio = v.clamp(RATIO_MIN, RATIO_MAX);
    }

    /// Back to 50 / 60.
    pub fn reset_ratios(&mut self) {
        (self.h_ratio, self.v_ratio) = DEFAULT_RATIOS;
    }

    /// Zoom `id`; zooming the pane that is already zoomed restores the normal view.
    pub fn toggle_zoom(&mut self, id: PaneId) {
        if self.zoom.as_ref() == Some(&id) {
            self.zoom = None;
        } else if self.find(&id).is_some() {
            self.zoom = Some(id);
        }
    }

    /// Rectangles for a `cols` × `rows` viewer (see the crate docs for what each one covers). A shown sidebar
    /// ([`Layout::sidebar_cols`]) takes a band of columns: the left columns, or the right columns when the
    /// content is mirrored ([`Layout::set_mirror`]); the groups are laid out in the remaining content width and
    /// placed after it, the status row spans `cols`, and a zoomed pane fills the content area.
    pub fn rects(&self, cols: u16, rows: u16) -> Rects {
        // design §3.1: the tab labels sit on the border rows, so the supervisor box starts with its tab
        // row, the left column's horizontal separator doubles as the lower group's tab row, and the
        // vertical separator column is shared by both columns. At 240×60, 50/60 this gives VSep = column
        // 119, HSep = row 35, supervisor 118×34, lower 118×22, workers 119×57; with a 34-column sidebar the
        // same picture sits in the 206 content columns, moved right by 34 (VSep = 136, supervisor 101 wide).
        // A26 rev3: the mirror maps every content column `x` to `inner − x − w`, so the same picture has the
        // workers on the left and the stack (its 50% width kept) on the right.
        let sb = self.sidebar_cols(cols);
        let inner = cols.saturating_sub(sb);
        let left = if self.sidebar_on_right() { 0 } else { sb };
        // S1f + A26 rev7: the header band (topbar row 0 plus its separation row, or the Linux headerbar's
        // separation row) is above the content; the tab rows start on its last row, so the groups start one row
        // below `top`.
        let top = self.header_rows();
        let content = rows.saturating_sub(1 + top);
        let status = Rect {
            x: 0,
            y: content + top,
            w: cols,
            h: rows.min(1),
        };
        // Clamped so every group keeps at least one column / row while the viewer is big enough.
        let vsep = Self::keep_inside(Self::split(inner, self.h_ratio).saturating_sub(1), inner);
        let hsep = top + Self::keep_inside(Self::split(content, self.v_ratio), content);
        let bottom = top + content.saturating_sub(1);
        let span = |from: u16, to: u16| to.saturating_sub(from);
        // A rectangle in content coordinates (0..inner), placed in the viewer.
        let at = |x: u16, y: u16, w: u16, h: u16| Rect {
            x: left + if self.mirror { inner.saturating_sub(x + w) } else { x },
            y,
            w,
            h,
        };
        Rects {
            supervisor: at(1, top + 1, span(1, vsep), span(top + 1, hsep)),
            lower: at(1, hsep + 1, span(1, vsep), span(hsep + 1, bottom)),
            workers: at(
                vsep + 1,
                top + 1,
                span(vsep + 1, inner.saturating_sub(1)),
                span(top + 1, bottom),
            ),
            zoom: self
                .zoom
                .as_ref()
                .map(|_| at(0, top, inner, content)),
            status,
        }
    }

    /// The protocol layout object (protocol-draft §4.14): `h_ratio`, `v_ratio`, `zoom`, `focus`,
    /// `groups`, `selected` — and nothing else.
    pub fn to_object(&self) -> serde_json::Value {
        let mut groups = Map::new();
        let mut selected = Map::new();
        for g in Group::ALL {
            groups.insert(g.name().into(), json!(self.group(g)));
            selected.insert(g.name().into(), json!(self.selected(g)));
        }
        json!({
            "h_ratio": self.h_ratio,
            "v_ratio": self.v_ratio,
            "zoom": self.zoom,
            "focus": self.focus.name(),
            "groups": groups,
            "selected": selected,
        })
    }

    /// Serialise to the `layout.json` text: [`Layout::to_object`] plus `"version": 1` (protocol-draft §8.3).
    pub fn to_json(&self) -> String {
        let mut object = self.to_object();
        object["version"] = json!(1);
        object.to_string()
    }

    /// Parse `layout.json`; rejects ratios outside 10..=90 with [`LayoutError::BadRatio`] and malformed
    /// JSON or wrong value types with [`LayoutError::BadJson`]; unknown extra keys are ignored.
    pub fn from_json(s: &str) -> Result<Self, LayoutError> {
        let bad = |why: String| LayoutError::BadJson(why);
        let v: Value = serde_json::from_str(s).map_err(|e| bad(e.to_string()))?;
        let obj = v.as_object().ok_or_else(|| bad("not an object".into()))?;
        let key = |k: &str| obj.get(k).ok_or_else(|| bad(format!("missing {k}")));

        let ratio = |which: &'static str| -> Result<u8, LayoutError> {
            let value = key(which)?
                .as_i64()
                .ok_or_else(|| bad(format!("{which} is not an integer")))?;
            if !(i64::from(RATIO_MIN)..=i64::from(RATIO_MAX)).contains(&value) {
                return Err(LayoutError::BadRatio { which, value });
            }
            Ok(value as u8)
        };
        let pane_or_null = |v: &Value, what: &str| -> Result<Option<PaneId>, LayoutError> {
            match v {
                Value::Null => Ok(None),
                Value::String(id) => Ok(Some(id.clone())),
                _ => Err(bad(format!("{what} is neither a pane id nor null"))),
            }
        };

        let mut l = Layout::new();
        l.h_ratio = ratio("h_ratio")?;
        l.v_ratio = ratio("v_ratio")?;
        l.focus = key("focus")?
            .as_str()
            .and_then(Group::from_name)
            .ok_or_else(|| bad("focus is not a group name".into()))?;

        let groups = key("groups")?
            .as_object()
            .ok_or_else(|| bad("groups is not an object".into()))?;
        let mut seen = HashSet::new();
        for g in Group::ALL {
            let list = groups
                .get(g.name())
                .and_then(Value::as_array)
                .ok_or_else(|| bad(format!("groups.{} is not an array", g.name())))?;
            for p in list {
                let id = p
                    .as_str()
                    .ok_or_else(|| bad(format!("groups.{} holds a non-string", g.name())))?;
                if !seen.insert(id.to_string()) {
                    return Err(bad(format!("pane {id} appears twice")));
                }
                l.groups[g.index()].push(id.to_string());
            }
        }

        let selected = key("selected")?
            .as_object()
            .ok_or_else(|| bad("selected is not an object".into()))?;
        for g in Group::ALL {
            let what = format!("selected.{}", g.name());
            let sel = pane_or_null(selected.get(g.name()).unwrap_or(&Value::Null), &what)?;
            if let Some(id) = &sel
                && !l.group(g).contains(id)
            {
                return Err(bad(format!("{what} = {id} is not in that group")));
            }
            l.selected[g.index()] = sel;
        }

        l.zoom = pane_or_null(key("zoom")?, "zoom")?;
        if let Some(id) = &l.zoom
            && !seen.contains(id)
        {
            return Err(bad(format!("zoom = {id} is not in any group")));
        }
        Ok(l)
    }

    // Read-only accessors.

    /// Panes of group `g`, in tab order.
    pub fn group(&self, g: Group) -> &[PaneId] {
        &self.groups[g.index()]
    }

    /// Selected tab of group `g`.
    pub fn selected(&self, g: Group) -> Option<&PaneId> {
        self.selected[g.index()].as_ref()
    }

    /// The group that has keyboard focus.
    pub fn focus(&self) -> Group {
        self.focus
    }

    /// `(h_ratio, v_ratio)` in percent.
    pub fn ratios(&self) -> (u8, u8) {
        (self.h_ratio, self.v_ratio)
    }

    /// The zoomed pane, if any.
    pub fn zoom(&self) -> Option<&PaneId> {
        self.zoom.as_ref()
    }

    /// Recorded type of pane `id`.
    pub fn pane_type(&self, id: &str) -> Option<PaneType> {
        self.types.get(id).copied()
    }

    // Internal helpers.

    /// Group and index of pane `id`.
    fn find(&self, id: &str) -> Option<(Group, usize)> {
        Group::ALL
            .into_iter()
            .find_map(|g| self.group(g).iter().position(|p| p == id).map(|i| (g, i)))
    }

    /// Who is selected after the pane at `at` left `group` (already removed): its left neighbour, else the
    /// new first tab, else nobody.
    fn heir(group: &[PaneId], at: usize) -> Option<PaneId> {
        group.get(at.saturating_sub(1)).cloned()
    }

    /// Move the focused group's selection by `delta` tabs, wrapping; with nothing selected, forward
    /// starts at the first tab and backward at the last.
    fn step_tab(&mut self, delta: isize) {
        let g = self.focus.index();
        let n = self.groups[g].len();
        if n == 0 {
            return;
        }
        let current = self.selected[g]
            .as_ref()
            .and_then(|s| self.groups[g].iter().position(|p| p == s));
        let next = match current {
            Some(i) => (i as isize + delta).rem_euclid(n as isize) as usize,
            None if delta >= 0 => 0,
            None => n - 1,
        };
        self.selected[g] = Some(self.groups[g][next].clone());
    }

    /// Clamp a separator position on a `len`-cell axis (border, inner, separator, inner, border) to
    /// 2..=len-3, so both sides keep an inner cell; tiny viewers degrade without underflow.
    fn keep_inside(pos: u16, len: u16) -> u16 {
        let hi = len.saturating_sub(3).max(1);
        pos.clamp(2.min(hi), hi)
    }

    /// Cell coordinate `percent`% of the way along `len` cells, rounded to nearest.
    fn split(len: u16, percent: u8) -> u16 {
        ((u32::from(len) * u32::from(percent) + 50) / 100) as u16
    }
}

// Mouse hit testing and drag-to-ratio (M1, `layout-hit`), API unchanged from stage A.
mod hit;
pub use hit::{Hit, ratio_from_x, ratio_from_x_mirrored, ratio_from_y};
