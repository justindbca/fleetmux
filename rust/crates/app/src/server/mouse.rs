//! Mouse handling on the server (PROTOCOL.md §11, design.md §2.6): parse the viewer's SGR reports and turn
//! them into layout actions, scroll-view moves, selections, or re-encoded reports for the inner program.
//!
//! B14 (mouse-input). Geometry is the layout at the PTY size (`ViewerHub::layout_size`, the same size
//! `draw` lays out with) in the viewer's own coordinates: the canvas is drawn from (0, 0), so a viewer cell
//! and a layout cell are the same point; anything past the layout is `Hit::Outside`.
//!
//! The decision table ([`decide`]):
//! - Shift held → [`Action::PassThrough`], `drag` untouched (the outer terminal's own selection takes it).
//! - Left press on a tab → [`Action::SelectTab`]; a second press on the same tab within [`DOUBLE_CLICK`] →
//!   [`Action::ToggleZoom`] (needs the per-connection [`ClickMemory`]; [`on_mouse`] has none).
//! - Left press in the fleet sidebar → [`Action::Sidebar`] (the cell; `view::mouse_input` decides whether it was
//!   the `◫` toggle, a toolbar button, a card or a card's `×`).
//! - Press on the vertical / horizontal separator starts a drag; motion and release give
//!   [`Action::DragH`] (`ratio_from_x_in`, over the content width right of the sidebar) / [`Action::DragV`]
//!   ([`Layout::ratio_from_y_in`], over the content height below the topbar row).
//! - Press in a pane sets `drag = Pane(group)`; while it is held, motion and release act on that pane even
//!   outside it. Inner mouse reporting on → [`Action::Forward`] (`encode_mouse` relative to the pane origin);
//!   off → left button [`Action::BeginSelect`] / [`Action::ExtendSelect`] (motion) / [`Action::EndSelect`] in
//!   pane-view coordinates (clamped to the pane), other buttons [`Action::PassThrough`]. A second left press on the
//!   same cell within [`DOUBLE_CLICK`] is [`Action::SelectWord`], a third on the same row within [`DOUBLE_CLICK`]
//!   of the second [`Action::SelectLine`]; the press after a triple click starts over.
//! - Wheel over a pane: inner mouse on → [`Action::Forward`]; off → [`Action::ScrollView`] ±[`WHEEL_ROWS`].
//! - Wheel over the fleet sidebar (its cards or its edge columns) → [`Action::ScrollSidebar`] ±1 card;
//!   `view::mouse_input` scrolls only when the cell is in the band's card rows.
//! - Status line, borders, outside → [`Action::PassThrough`].
use std::time::{Duration, Instant};

use fleetmux_layout::{Group, Hit, Layout, Rect};
use fleetmux_proto::PaneId;
use fleetmux_term::{Modes, MouseButton, MouseEvent, MouseKind, encode_mouse};

use super::State;
use super::view::Viewer;

/// Two presses on the same tab within this time toggle zoom; in a pane, two / three presses select a word / line
/// (PROTOCOL.md §11).
pub const DOUBLE_CLICK: Duration = Duration::from_millis(300);

/// Rows one wheel step moves a pane's view when the inner program does not report mouse.
pub const WHEEL_ROWS: i32 = 3;

/// Parse one SGR mouse report (`ESC [ < b ; x ; y M|m`) at the start of `bytes`: the event in 0-based
/// outer-screen coordinates and the number of bytes consumed. `None` when `bytes` does not start with a whole
/// SGR report.
pub fn parse_sgr(bytes: &[u8]) -> Option<(MouseEvent, usize)> {
    let body = bytes.strip_prefix(b"\x1b[<")?;
    let end = body.iter().position(|&c| c == b'M' || c == b'm')?;
    let mut fields = body[..end].split(|&c| c == b';');
    let mut num = || -> Option<u32> {
        let f = fields.next()?;
        if f.is_empty() || f.len() > 5 || !f.iter().all(u8::is_ascii_digit) {
            return None;
        }
        std::str::from_utf8(f).ok()?.parse().ok()
    };
    let (b, x, y) = (num()?, num()?, num()?);
    if fields.next().is_some()
        || x == 0
        || y == 0
        || x > u32::from(u16::MAX)
        || y > u32::from(u16::MAX)
    {
        return None;
    }
    let press = body[end] == b'M';
    let button = |n: u32| match n & 3 {
        0 => Some(MouseButton::Left),
        1 => Some(MouseButton::Middle),
        2 => Some(MouseButton::Right),
        _ => None,
    };
    let kind = if b & 64 != 0 {
        match (b & 3, press) {
            (0, true) => MouseKind::ScrollUp,
            (1, true) => MouseKind::ScrollDown,
            _ => return None, // horizontal wheel, or a wheel "release"
        }
    } else if b & 32 != 0 {
        match button(b) {
            Some(btn) => MouseKind::Drag(btn),
            None => MouseKind::Move,
        }
    } else {
        let btn = button(b)?;
        if press {
            MouseKind::Press(btn)
        } else {
            MouseKind::Release(btn)
        }
    };
    let ev = MouseEvent {
        kind,
        col: (x - 1) as u16,
        row: (y - 1) as u16,
        shift: b & 4 != 0,
        alt: b & 8 != 0,
        ctrl: b & 16 != 0,
    };
    Some((ev, 3 + end + 1))
}

/// What the server does with one mouse event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action {
    /// Left click on a tab: select it and focus its group.
    SelectTab(PaneId),
    /// Click inside a pane's cell: focus that group.
    FocusGroup(Group),
    /// Dragging the vertical separator: new `h_ratio` (10..=90).
    DragH(u8),
    /// Left press in the fleet sidebar at this (col, row): the `◫` toggle, a toolbar button, a fleet card or a card's
    /// `×`.
    Sidebar((u16, u16)),
    /// Dragging the left column's horizontal separator: new `v_ratio` (10..=90).
    DragV(u8),
    /// Dragging the sidebar's right edge to this column (plan §16.2 S1d): `view::mouse_input` clamps it to
    /// `SIDEBAR_MIN_W`..=`sidebar_max(cols)` and saves it on release.
    DragSidebar(u16),
    /// Bytes for the focused pane's PTY (re-encoded with `encode_mouse` when the inner program reports mouse).
    Forward(Vec<u8>),
    /// Wheel with inner mouse off: move the pane's `display_offset` by this many rows (+ = older).
    ScrollView(i32),
    /// Wheel over the fleet sidebar at this (col, row): scroll its cards by this many (+ = later cards).
    /// `view::mouse_input` acts only on the card area.
    ScrollSidebar((u16, u16), i32),
    /// Left press with inner mouse off: selection starts at this (col, row) of the pane's view.
    BeginSelect((u16, u16)),
    /// Left motion while held after `BeginSelect`: the selection now ends here (drawn highlighted).
    ExtendSelect((u16, u16)),
    /// Left release after `BeginSelect`: selection ends here; the server copies it (PROTOCOL.md §12).
    EndSelect((u16, u16)),
    /// Double click in a pane with inner mouse off: select and copy the word at this (col, row) of its view.
    SelectWord((u16, u16)),
    /// Triple click in a pane with inner mouse off: select and copy the line at this (col, row) of its view.
    SelectLine((u16, u16)),
    /// Double click on a tab (two presses within 300 ms).
    ToggleZoom(PaneId),
    /// Not handled and not forwarded (Shift held, or outside every target).
    PassThrough,
}

/// Per-connection memory for double clicks: the tab pressed last and when, the sidebar cell of the empty card
/// area pressed last (S1c, plan §14.5-2: two of those open a bash page), and the pane cell pressed last with its
/// click count (1–2; a third press is a triple click and starts over).
#[derive(Debug, Clone, Default)]
pub struct ClickMemory {
    last_tab: Option<(PaneId, Instant)>,
    last_blank: Option<((u16, u16), Instant)>,
    last_cell: Option<(PaneId, (u16, u16), Instant, u8)>,
}

impl ClickMemory {
    /// Record a left press on the sidebar's empty card area at `cell` (S1c, plan §14.5-2) and answer whether it is
    /// the second press of a double click: the same cell within [`DOUBLE_CLICK`]. A double click starts over, so a
    /// third press counts as a first one again.
    pub fn blank_press(&mut self, cell: (u16, u16), now: Instant) -> bool {
        let double = self
            .last_blank
            .is_some_and(|(last, at)| last == cell && now.duration_since(at) <= DOUBLE_CLICK);
        self.last_blank = if double { None } else { Some((cell, now)) };
        double
    }
}

/// Decide what `ev` means for `viewer`'s screen. `drag` holds the separator / cell hit by the press that
/// started a drag, so motion and release keep acting on it.
///
/// Without per-connection click memory this never returns [`Action::ToggleZoom`]; the view connection uses
/// [`decide`] with its own [`ClickMemory`].
pub fn on_mouse(state: &State, viewer: &Viewer, ev: &MouseEvent, drag: &mut Option<Hit>) -> Action {
    let _ = viewer; // the layout is at the PTY size, in this viewer's coordinates (module docs)
    let (cols, rows) = state.viewers.layout_size();
    let modes = |id: &str| state.panes.term(id).map(|t| t.modes());
    let labels = |id: &PaneId| crate::render::draw::label_cols(&state.panes, id);
    decide_titled(
        &state.layout,
        (cols, rows),
        &modes,
        &labels,
        ev,
        drag,
        &mut ClickMemory::default(),
        Instant::now(),
    )
}

/// The pane a mouse event in `group` acts on, its content rectangle (outer coordinates), at `size`.
pub fn pane_at(layout: &Layout, size: (u16, u16), group: Group) -> Option<(PaneId, Rect)> {
    let rects = layout.rects(size.0, size.1);
    if let (Some(z), Some(r)) = (layout.zoom(), rects.zoom) {
        return Some((z.clone(), r));
    }
    let r = match group {
        Group::Supervisor => rects.supervisor,
        Group::Lower => rects.lower,
        Group::Workers => rects.workers,
    };
    layout.selected(group).map(|id| (id.clone(), r))
}

/// `ev` in pane-view coordinates of rectangle `r`, clamped into it.
fn view_pos(ev: &MouseEvent, r: Rect) -> (u16, u16) {
    let clamp = |v: u16, lo: u16, len: u16| v.clamp(lo, lo + len.saturating_sub(1)) - lo;
    (clamp(ev.col, r.x, r.w), clamp(ev.row, r.y, r.h))
}

/// [`on_mouse`] with the inputs spelled out: the layout and the size it is laid out at, the inner mode bits
/// of a pane (`None` = no such pane), the drag state, and this connection's click memory at time `now`.
///
/// Tab hits here use the layout's fixed-slot `hit_test` (titles unknown); the server uses [`decide_titled`],
/// whose tab blocks are the ones `draw` paints (fix-m1-tabs).
pub fn decide(
    layout: &Layout,
    size: (u16, u16),
    modes: &dyn Fn(&str) -> Option<Modes>,
    ev: &MouseEvent,
    drag: &mut Option<Hit>,
    clicks: &mut ClickMemory,
    now: Instant,
) -> Action {
    let hit = |x: u16, y: u16| layout.hit_test(size.0, size.1, x, y);
    decide_with(layout, size, modes, &hit, ev, drag, clicks, now)
}

/// fix-m1-tabs (supervisor ruling [M48], API B): [`decide`] with tab hits from `Layout::hit_test_titled`, the
/// block geometry `draw` paints; `label_cols` = `render::draw::label_cols` (block text width without padding).
/// A press on a gap or on the bar after the last block is not a tab.
#[allow(clippy::too_many_arguments)]
pub fn decide_titled(
    layout: &Layout,
    size: (u16, u16),
    modes: &dyn Fn(&str) -> Option<Modes>,
    label_cols: &dyn Fn(&PaneId) -> u16,
    ev: &MouseEvent,
    drag: &mut Option<Hit>,
    clicks: &mut ClickMemory,
    now: Instant,
) -> Action {
    let hit = |x: u16, y: u16| layout.hit_test_titled(size.0, size.1, x, y, label_cols);
    decide_with(layout, size, modes, &hit, ev, drag, clicks, now)
}

#[allow(clippy::too_many_arguments)]
fn decide_with(
    layout: &Layout,
    size: (u16, u16),
    modes: &dyn Fn(&str) -> Option<Modes>,
    hit_at: &dyn Fn(u16, u16) -> Hit,
    ev: &MouseEvent,
    drag: &mut Option<Hit>,
    clicks: &mut ClickMemory,
    now: Instant,
) -> Action {
    if ev.shift {
        return Action::PassThrough;
    }
    let (cols, rows) = size;
    let wheel = matches!(ev.kind, MouseKind::ScrollUp | MouseKind::ScrollDown);
    let press = matches!(ev.kind, MouseKind::Press(_));
    let release = matches!(ev.kind, MouseKind::Release(_));

    // A held drag owns motion and release, wherever they land.
    if !press
        && !wheel
        && let Some(held) = *drag
    {
        if release {
            *drag = None;
        }
        return match held {
            Hit::VSep => Action::DragH(layout.ratio_from_x_in(cols, ev.col)),
            // S1f (plan §20.4-6): with a topbar the content starts on row 1, so the release row is taken
            // relative to it; without one this is the old `ratio_from_y(rows − 1, row)`.
            Hit::HSep => Action::DragV(layout.ratio_from_y_in(rows, ev.row)),
            Hit::SidebarEdge => Action::DragSidebar(ev.col),
            Hit::Pane(g) => in_pane(layout, size, modes, g, ev, release),
            _ => Action::PassThrough,
        };
    }

    let hit = hit_at(ev.col, ev.row);
    if wheel {
        return match hit {
            Hit::Pane(g) => in_pane(layout, size, modes, g, ev, false),
            // The sidebar's cards, or its gap column where the scrollbar sits: one card per wheel step (cmux).
            Hit::Sidebar | Hit::SidebarEdge => {
                let step = if ev.kind == MouseKind::ScrollUp { -1 } else { 1 };
                Action::ScrollSidebar((ev.col, ev.row), step)
            }
            _ => Action::PassThrough,
        };
    }
    if !press {
        return Action::PassThrough; // motion / release with nothing held
    }
    let left = ev.kind == MouseKind::Press(MouseButton::Left);
    // Only consecutive selecting presses in one pane count as a double / triple click.
    let last_cell = clicks.last_cell.take();
    match hit {
        Hit::Tab { group, index } if left => {
            let Some(id) = layout.group(group).get(index).cloned() else {
                return Action::PassThrough;
            };
            let double = clicks
                .last_tab
                .as_ref()
                .is_some_and(|(last, at)| *last == id && now.duration_since(*at) <= DOUBLE_CLICK);
            if double {
                clicks.last_tab = None;
                Action::ToggleZoom(id)
            } else {
                clicks.last_tab = Some((id.clone(), now));
                Action::SelectTab(id)
            }
        }
        Hit::Sidebar if left => Action::Sidebar((ev.col, ev.row)),
        Hit::VSep | Hit::HSep | Hit::SidebarEdge if left => {
            *drag = Some(hit);
            Action::PassThrough
        }
        Hit::Pane(g) => {
            let mut action = in_pane(layout, size, modes, g, ev, false);
            if let Action::BeginSelect(at) = action
                && let Some((id, _)) = pane_at(layout, size, g)
            {
                let within = |last: &PaneId, t: Instant| {
                    *last == id && now.duration_since(t) <= DOUBLE_CLICK
                };
                let count = match last_cell {
                    Some((p, c, t, 1)) if c == at && within(&p, t) => 2,
                    Some((p, c, t, 2)) if c.1 == at.1 && within(&p, t) => 3,
                    _ => 1,
                };
                action = match count {
                    2 => Action::SelectWord(at),
                    3 => Action::SelectLine(at),
                    _ => action,
                };
                clicks.last_cell = (count < 3).then_some((id, at, now, count));
            }
            if action != Action::PassThrough {
                *drag = Some(Hit::Pane(g));
            }
            action
        }
        _ => Action::PassThrough,
    }
}

/// A press / motion / release / wheel acting on the pane shown in group `g`.
fn in_pane(
    layout: &Layout,
    size: (u16, u16),
    modes: &dyn Fn(&str) -> Option<Modes>,
    g: Group,
    ev: &MouseEvent,
    release: bool,
) -> Action {
    let Some((id, r)) = pane_at(layout, size, g) else {
        return Action::PassThrough;
    };
    let m = modes(&id).unwrap_or_default();
    if m.mouse.is_some() {
        return match encode_mouse(ev, &m, (r.x, r.y)) {
            Some(bytes) => Action::Forward(bytes),
            None => Action::PassThrough,
        };
    }
    match ev.kind {
        MouseKind::ScrollUp => Action::ScrollView(WHEEL_ROWS),
        MouseKind::ScrollDown => Action::ScrollView(-WHEEL_ROWS),
        MouseKind::Press(MouseButton::Left) => Action::BeginSelect(view_pos(ev, r)),
        MouseKind::Drag(MouseButton::Left) if !release => Action::ExtendSelect(view_pos(ev, r)),
        MouseKind::Release(MouseButton::Left) if release => Action::EndSelect(view_pos(ev, r)),
        _ => Action::PassThrough,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use fleetmux_layout::PaneType;
    use fleetmux_term::MouseMode;

    const SIZE: (u16, u16) = (240, 60);

    fn ev(kind: MouseKind, col: u16, row: u16) -> MouseEvent {
        MouseEvent {
            kind,
            col,
            row,
            shift: false,
            alt: false,
            ctrl: false,
        }
    }
    const LP: MouseKind = MouseKind::Press(MouseButton::Left);
    const LR: MouseKind = MouseKind::Release(MouseButton::Left);
    const LD: MouseKind = MouseKind::Drag(MouseButton::Left);

    fn layout() -> Layout {
        let mut l = Layout::new();
        l.insert("sup".into(), PaneType::Supervisor);
        l.insert("adv".into(), PaneType::Advisor);
        l.insert("hist".into(), PaneType::History);
        l.insert("w1".into(), PaneType::Worker);
        // Selecting the first pane of an empty group is the spawn handler's job (B6), not `insert`'s.
        for id in ["sup", "hist", "w1"] {
            l.select(id.into());
        }
        l
    }
    fn off(_: &str) -> Option<Modes> {
        Some(Modes::default())
    }
    fn on(_: &str) -> Option<Modes> {
        Some(Modes {
            mouse: Some(MouseMode::Drag),
            sgr_mouse: true,
            ..Modes::default()
        })
    }
    fn run(
        l: &Layout,
        m: &dyn Fn(&str) -> Option<Modes>,
        e: MouseEvent,
        drag: &mut Option<Hit>,
    ) -> Action {
        decide(
            l,
            SIZE,
            m,
            &e,
            drag,
            &mut ClickMemory::default(),
            Instant::now(),
        )
    }

    #[test]
    fn parse_sgr_press_release_motion_wheel_and_mods() {
        let (e, n) = parse_sgr(b"\x1b[<0;120;31Mrest").unwrap();
        assert_eq!((e.kind, e.col, e.row, n), (LP, 119, 30, 12));
        assert_eq!(parse_sgr(b"\x1b[<0;1;1m").unwrap().0.kind, LR);
        assert_eq!(parse_sgr(b"\x1b[<32;5;5M").unwrap().0.kind, LD);
        assert_eq!(parse_sgr(b"\x1b[<35;5;5M").unwrap().0.kind, MouseKind::Move);
        assert_eq!(
            parse_sgr(b"\x1b[<64;5;5M").unwrap().0.kind,
            MouseKind::ScrollUp
        );
        assert_eq!(
            parse_sgr(b"\x1b[<65;5;5M").unwrap().0.kind,
            MouseKind::ScrollDown
        );
        let e = parse_sgr(b"\x1b[<30;5;5M").unwrap().0; // right + shift + alt + ctrl
        assert_eq!(
            (e.kind, e.shift, e.alt, e.ctrl),
            (MouseKind::Press(MouseButton::Right), true, true, true)
        );
        for bad in [
            &b"\x1b[<0;0;5M"[..],
            b"\x1b[<0;5M",
            b"\x1b[<0;5;5",
            b"\x1b[<66;5;5M",
            b"\x1b[0;5;5M",
            b"\x1b[<3;5;5M",
        ] {
            assert_eq!(parse_sgr(bad), None, "{bad:?}");
        }
    }

    #[test]
    fn tab_press_selects_and_second_press_within_300ms_toggles_zoom() {
        let l = layout();
        let tabs = l.tab_rects(SIZE.0, SIZE.1);
        let (_, _, r) = tabs
            .iter()
            .find(|(g, i, _)| *g == Group::Supervisor && *i == 1)
            .unwrap();
        let mut clicks = ClickMemory::default();
        let t0 = Instant::now();
        let mut drag = None;
        let e = ev(LP, r.x, r.y);
        assert_eq!(
            decide(&l, SIZE, &off, &e, &mut drag, &mut clicks, t0),
            Action::SelectTab("adv".into())
        );
        let t1 = t0 + Duration::from_millis(200);
        assert_eq!(
            decide(&l, SIZE, &off, &e, &mut drag, &mut clicks, t1),
            Action::ToggleZoom("adv".into())
        );
        // A third press starts over; a slow second press is a plain select.
        assert_eq!(
            decide(&l, SIZE, &off, &e, &mut drag, &mut clicks, t1),
            Action::SelectTab("adv".into())
        );
        let t2 = t1 + Duration::from_millis(400);
        assert_eq!(
            decide(&l, SIZE, &off, &e, &mut drag, &mut clicks, t2),
            Action::SelectTab("adv".into())
        );
        assert_eq!(drag, None);
    }

    #[test]
    fn separator_drags_map_to_ratios_and_release_ends_the_drag() {
        let l = layout();
        let mut drag = None;
        assert_eq!(
            run(&l, &off, ev(LP, 119, 30), &mut drag),
            Action::PassThrough
        );
        assert_eq!(drag, Some(Hit::VSep));
        assert_eq!(run(&l, &off, ev(LD, 167, 30), &mut drag), Action::DragH(70));
        assert_eq!(run(&l, &off, ev(LR, 167, 30), &mut drag), Action::DragH(70));
        assert_eq!(drag, None);

        assert_eq!(
            run(&l, &off, ev(LP, 50, 35), &mut drag),
            Action::PassThrough
        );
        assert_eq!(drag, Some(Hit::HSep));
        assert_eq!(run(&l, &off, ev(LD, 50, 23), &mut drag), Action::DragV(39));
        assert_eq!(run(&l, &off, ev(LR, 50, 23), &mut drag), Action::DragV(39));
        assert_eq!(drag, None);
    }

    #[test]
    fn pane_with_inner_mouse_gets_reports_relative_to_its_origin() {
        let l = layout();
        let (_, r) = pane_at(&l, SIZE, Group::Workers).unwrap();
        let mut drag = None;
        let a = run(&l, &on, ev(LP, 180, 30), &mut drag);
        let want = format!("\x1b[<0;{};{}M", 180 - r.x + 1, 30 - r.y + 1).into_bytes();
        assert_eq!(a, Action::Forward(want));
        assert_eq!(drag, Some(Hit::Pane(Group::Workers)));
        // Release outside the pane still goes to it (the drag owns it) and ends the drag.
        let a = run(&l, &on, ev(LR, 183, 32), &mut drag);
        let want = format!("\x1b[<0;{};{}m", 183 - r.x + 1, 32 - r.y + 1).into_bytes();
        assert_eq!(a, Action::Forward(want));
        assert_eq!(drag, None);
        // Wheel: button 64, no drag.
        let a = run(&l, &on, ev(MouseKind::ScrollUp, 180, 30), &mut drag);
        assert_eq!(
            a,
            Action::Forward(format!("\x1b[<64;{};{}M", 180 - r.x + 1, 30 - r.y + 1).into_bytes())
        );
        assert_eq!(drag, None);
    }

    /// Wheel over the sidebar (its cards, or its gap column where the scrollbar sits) scrolls the card list, one
    /// card per step (cmux); it never reaches a pane and starts no drag.
    #[test]
    fn wheel_over_the_sidebar_scrolls_its_cards() {
        let mut l = layout();
        l.set_sidebar(34);
        let mut drag = None;
        assert_eq!(
            run(&l, &off, ev(MouseKind::ScrollDown, 5, 10), &mut drag),
            Action::ScrollSidebar((5, 10), 1)
        );
        assert_eq!(
            run(&l, &off, ev(MouseKind::ScrollUp, 5, 10), &mut drag),
            Action::ScrollSidebar((5, 10), -1)
        );
        assert_eq!(
            run(&l, &on, ev(MouseKind::ScrollDown, 33, 10), &mut drag),
            Action::ScrollSidebar((33, 10), 1),
            "the gap column (the scrollbar's) scrolls the cards too"
        );
        assert_eq!(drag, None);
    }

    #[test]
    fn pane_without_inner_mouse_scrolls_and_selects() {
        let l = layout();
        let (_, r) = pane_at(&l, SIZE, Group::Supervisor).unwrap();
        let mut drag = None;
        assert_eq!(
            run(&l, &off, ev(MouseKind::ScrollUp, 60, 10), &mut drag),
            Action::ScrollView(3)
        );
        assert_eq!(
            run(&l, &off, ev(MouseKind::ScrollDown, 60, 10), &mut drag),
            Action::ScrollView(-3)
        );
        assert_eq!(drag, None);
        assert_eq!(
            run(&l, &off, ev(LP, 60, 10), &mut drag),
            Action::BeginSelect((60 - r.x, 10 - r.y))
        );
        // Motion while held extends the selection (drawn highlighted, PROTOCOL.md §11).
        assert_eq!(
            run(&l, &off, ev(LD, 70, 11), &mut drag),
            Action::ExtendSelect((70 - r.x, 11 - r.y))
        );
        // Released past the pane's right edge: clamped to its last column.
        assert_eq!(
            run(&l, &off, ev(LR, 200, 11), &mut drag),
            Action::EndSelect((r.w - 1, 11 - r.y))
        );
        assert_eq!(drag, None);
        // Right button with mouse off: nothing.
        let rp = ev(MouseKind::Press(MouseButton::Right), 60, 10);
        assert_eq!(run(&l, &off, rp, &mut drag), Action::PassThrough);
        assert_eq!(drag, None);
    }

    #[test]
    fn shift_status_and_borders_pass_through_without_touching_drag() {
        let l = layout();
        let mut drag = None;
        let mut e = ev(LP, 119, 30);
        e.shift = true;
        assert_eq!(run(&l, &on, e, &mut drag), Action::PassThrough);
        assert_eq!(drag, None);
        let mut e = ev(LP, 180, 30);
        e.shift = true;
        assert_eq!(run(&l, &on, e, &mut drag), Action::PassThrough);
        assert_eq!(drag, None);
        assert_eq!(run(&l, &on, ev(LP, 10, 59), &mut drag), Action::PassThrough); // status
        assert_eq!(run(&l, &on, ev(LP, 0, 0), &mut drag), Action::PassThrough); // corner
        assert_eq!(
            run(&l, &on, ev(LP, 250, 10), &mut drag),
            Action::PassThrough
        ); // past the layout
        assert_eq!(
            run(&l, &on, ev(LR, 180, 30), &mut drag),
            Action::PassThrough
        ); // release, nothing held
        assert_eq!(drag, None);
    }

    #[test]
    fn zoomed_pane_takes_the_content_area() {
        let mut l = layout();
        l.toggle_zoom("w1".into());
        let (id, r) = pane_at(&l, SIZE, Group::Workers).unwrap();
        assert_eq!((id.as_str(), r.x, r.y), ("w1", 0, 0));
        let mut drag = None;
        let a = run(&l, &on, ev(LP, 10, 5), &mut drag);
        assert_eq!(a, Action::Forward(b"\x1b[<0;11;6M".to_vec()));
    }

    #[test]
    fn blank_presses_need_the_same_cell_within_the_double_click_window() {
        let t0 = Instant::now();
        let mut clicks = ClickMemory::default();
        assert!(
            !clicks.blank_press((5, 10), t0),
            "a first press on the blank card area is not a double click"
        );
        assert!(
            clicks.blank_press((5, 10), t0 + Duration::from_millis(200)),
            "a second press on the same cell after 200 ms (< DOUBLE_CLICK = 300 ms) is a double click"
        );
        assert!(
            !clicks.blank_press((5, 10), t0 + Duration::from_millis(400)),
            "the press after a double click starts over: (5,10) at +400 ms is not a triple click"
        );

        let mut clicks = ClickMemory::default();
        assert!(!clicks.blank_press((5, 10), t0));
        assert!(
            !clicks.blank_press((6, 10), t0 + Duration::from_millis(100)),
            "a second press 100 ms later on another cell (6,10) is not a double click"
        );
        assert!(
            !clicks.blank_press((5, 10), t0 + Duration::from_millis(700)),
            "a second press on (5,10) 700 ms later (> DOUBLE_CLICK) is not a double click"
        );
    }

    #[test]
    fn pane_presses_count_double_and_triple_clicks() {
        let l = layout();
        let (_, r) = pane_at(&l, SIZE, Group::Supervisor).unwrap();
        let t0 = Instant::now();
        let at = |ms: u64| t0 + Duration::from_millis(ms);
        let pos = |c: u16, w: u16| (c - r.x, w - r.y);
        let mut clicks = ClickMemory::default();
        let mut drag = None;
        let mut click = |m: &dyn Fn(&str) -> Option<Modes>, e: MouseEvent, ms: u64| {
            let a = decide(&l, SIZE, m, &e, &mut drag, &mut clicks, at(ms));
            let mut up = e;
            up.kind = LR;
            decide(&l, SIZE, m, &up, &mut drag, &mut clicks, at(ms));
            a
        };
        assert_eq!(
            click(&off, ev(LP, 60, 10), 0),
            Action::BeginSelect(pos(60, 10))
        );
        assert_eq!(
            click(&off, ev(LP, 60, 10), 200),
            Action::SelectWord(pos(60, 10)),
            "a second press on the same cell within DOUBLE_CLICK selects the word"
        );
        assert_eq!(
            click(&off, ev(LP, 65, 10), 400),
            Action::SelectLine(pos(65, 10)),
            "a third press on the same row within DOUBLE_CLICK of the second selects the line"
        );
        assert_eq!(
            click(&off, ev(LP, 65, 10), 500),
            Action::BeginSelect(pos(65, 10)),
            "after a triple click the count starts over"
        );
        // Too slow, or another cell: a plain press.
        assert_eq!(
            click(&off, ev(LP, 60, 10), 1000),
            Action::BeginSelect(pos(60, 10))
        );
        assert_eq!(
            click(&off, ev(LP, 60, 10), 1400),
            Action::BeginSelect(pos(60, 10))
        );
        assert_eq!(
            click(&off, ev(LP, 61, 10), 1500),
            Action::BeginSelect(pos(61, 10))
        );
        // A double click, then a third press on another row: a plain press.
        assert_eq!(
            click(&off, ev(LP, 61, 10), 1600),
            Action::SelectWord(pos(61, 10))
        );
        assert_eq!(
            click(&off, ev(LP, 61, 11), 1700),
            Action::BeginSelect(pos(61, 11))
        );
        // Shift still passes through, inner mouse on still forwards.
        let mut sh = ev(LP, 61, 11);
        sh.shift = true;
        assert_eq!(click(&off, sh, 1750), Action::PassThrough);
        assert!(matches!(
            click(&on, ev(LP, 70, 12), 3000),
            Action::Forward(_)
        ));
        assert!(matches!(
            click(&on, ev(LP, 70, 12), 3100),
            Action::Forward(_)
        ));
    }

    #[test]
    fn a_press_elsewhere_breaks_a_pane_double_click() {
        let l = layout();
        let (_, r) = pane_at(&l, SIZE, Group::Supervisor).unwrap();
        let tabs = l.tab_rects(SIZE.0, SIZE.1);
        let (_, _, tab) = tabs.iter().find(|(g, _, _)| *g == Group::Workers).unwrap();
        let t0 = Instant::now();
        let mut clicks = ClickMemory::default();
        let mut drag = None;
        let mut press = |e: MouseEvent, ms: u64| {
            let a = decide(
                &l,
                SIZE,
                &off,
                &e,
                &mut drag,
                &mut clicks,
                t0 + Duration::from_millis(ms),
            );
            drag = None;
            a
        };
        assert_eq!(
            press(ev(LP, 60, 10), 0),
            Action::BeginSelect((60 - r.x, 10 - r.y))
        );
        assert_eq!(
            press(ev(LP, tab.x, tab.y), 50),
            Action::SelectTab("w1".into())
        );
        assert_eq!(
            press(ev(LP, 60, 10), 100),
            Action::BeginSelect((60 - r.x, 10 - r.y))
        );
    }
}
