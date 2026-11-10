//! gui-scrollbar (PROTOCOL.md §6.3 `0x86`): where every visible cell is on this viewer's canvas and how far it is
//! scrolled, so the gui page can draw one scrollbar per cell. A pure function of the layout, like `overlay.rs`:
//! `view.rs` calls it after each committed screen update and sends the result when it differs from the last one
//! that viewer got.
use fleetmux_layout::{Group, Layout, PaneType, Rect};
use fleetmux_proto::view::{PaneScroll, ScrollState};

/// The scrollbar state for one viewer.
///
/// - Panes: the zoomed pane while something is zoomed, else each group's selected tab, in [`Group::ALL`] order
///   (supervisor, lower, workers); empty groups are skipped.
/// - `rect`: that pane's inner rectangle from `layout.rects(layout_size)` (the PTY size the layout is drawn at),
///   clipped to this viewer's own canvas `viewer_size.0 × (viewer_size.1 − 1)` (its bottom row is its status
///   line); a clip with no width or no height → the pane is left out.
/// - `offset` / `total`: `scroll(id)` — the pane's `display_offset()` / `scrollback_len()`; `None` (no parser,
///   which should not happen) → the pane is left out.
/// - A `PaneType::History` pane never appears (the gui covers it with the history iframe); a zoomed history pane
///   makes `panes` empty.
pub fn scroll_state_for(
    layout: &Layout,
    layout_size: (u16, u16),
    viewer_size: (u16, u16),
    scroll: &dyn Fn(&str) -> Option<(usize, usize)>,
) -> ScrollState {
    let rects = layout.rects(layout_size.0, layout_size.1);
    let shown: Vec<(String, Option<Rect>)> = match layout.zoom() {
        Some(z) => vec![(z.clone(), rects.zoom)],
        None => Group::ALL
            .iter()
            .filter_map(|g| {
                let id = layout.selected(*g)?;
                let rect = match g {
                    Group::Supervisor => rects.supervisor,
                    Group::Lower => rects.lower,
                    Group::Workers => rects.workers,
                };
                Some((id.clone(), Some(rect)))
            })
            .collect(),
    };
    let mut panes = Vec::new();
    for (id, rect) in shown {
        if layout.pane_type(&id) == Some(PaneType::History) {
            continue;
        }
        let Some(rect) = rect.and_then(|r| super::overlay::clip(r, viewer_size)) else {
            continue;
        };
        let Some((offset, total)) = scroll(&id) else {
            continue;
        };
        panes.push(PaneScroll {
            pane: id,
            rect,
            offset: offset as u64,
            total: total as u64,
        });
    }
    ScrollState { panes }
}
