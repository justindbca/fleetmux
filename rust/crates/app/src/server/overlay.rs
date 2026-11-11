//! M2.6 history overlay (design.md §3.6.3 ②, PROTOCOL.md §6.3 `0x85`): where the history pane is visible on one
//! viewer's canvas. A pure function of the layout, so the view code (`view.rs`, W3) only calls it after each
//! committed screen update and sends the result when it differs from the last one that viewer got.
//! Signature from W0 (ctl-1, brief-m26 §3.1); W2 (ctl-1) implements it.
use fleetmux_layout::{Group, Layout, PaneType, Rect};
use fleetmux_proto::view::{CellRect, Overlay, Topbar};

use crate::render::sidebar::Side;

/// The overlay for one viewer.
///
/// - `pane`: the pane whose type is `History` (`layout.pane_type(id) == Some(PaneType::History)`), else `None`.
/// - `rect`: only while that pane is visible: the zoom rectangle when it is the zoomed pane; the lower group's
///   inner rectangle when nothing is zoomed and it is the lower group's selected tab; otherwise `None`. Computed
///   with `layout.rects(layout_size)` (the PTY size the layout is drawn at), then clipped to this viewer's own
///   canvas `viewer_size.0 × (viewer_size.1 − 1)` (its bottom row is its status line); a clip with no width or
///   no height → `None`.
/// - `url`: `history_url` as given (kv `history_url`), `None` when unset.
pub fn overlay_for(
    layout: &Layout,
    layout_size: (u16, u16),
    viewer_size: (u16, u16),
    history_url: Option<&str>,
) -> Overlay {
    let pane = Group::ALL
        .into_iter()
        .flat_map(|g| layout.group(g))
        .find(|id| layout.pane_type(id) == Some(PaneType::History))
        .cloned();
    let rect = pane.as_ref().and_then(|id| {
        let rects = layout.rects(layout_size.0, layout_size.1);
        let shown = match layout.zoom() {
            Some(z) if z == id => rects.zoom,
            Some(_) => None,
            None if layout.selected(Group::Lower) == Some(id) => Some(rects.lower),
            None => None,
        }?;
        clip(shown, viewer_size)
    });
    Overlay {
        pane,
        rect,
        url: history_url.map(str::to_string),
        // A26 rev5: the native headerbar's title/status is filled by the caller (`view.rs`, which has the sidebar
        // data); this pure function only owns the history page's geometry.
        topbar: None,
    }
}

/// A26 rev5/rev7 (CURRENT_REQUIREMENTS rev 6 `utm_topbar`, rev 10 `top_container_alignment`): the topbar's
/// content — the current fleet's `title`/`line1` plus the sidebar band geometry — for a window that draws its own
/// header row (the Linux GTK headerbar, `crates/web/src/gui.rs`), which is why the grid there keeps no topbar row.
/// `None` while there is no kv `sidebar` data at all, and `None` for a macOS viewer: its topbar is grid row 0
/// ([`crate::render::sidebar::draw_topbar`]).
///
/// `cols` is the viewer's own canvas width: [`Layout::sidebar_cols`] turns it into the drawn band width, which is
/// what the headerbar's workspace/sidebar container boundary follows (the grid's own drag line, since the page
/// fills the window). Only the text is sent unclipped: the headerbar ellipsizes it to its own width, so a
/// font-size change never moves the native window buttons (`CURRENT_REQUIREMENTS` rev 6 `topbar`).
pub fn topbar_for(side: &Side, layout: &Layout, cols: u16) -> Option<Topbar> {
    side.data.as_ref()?;
    // No current card: empty text, but the band geometry still goes out — the headerbar's container boundary must
    // stay on the grid's sidebar divider even while no fleet is selected. A selected bash card names itself.
    let (title, status) = crate::render::sidebar::current_title(side).unwrap_or_default();
    Some(Topbar {
        title,
        status,
        sidebar: layout.sidebar_cols(cols),
        cols,
    })
}

/// `r` cut to the viewer's canvas above its status row (`cols × (rows − 1)`); `None` when nothing is left.
/// gui-scrollbar: shared with `scrollbar.rs`, which clips each cell's rectangle the same way.
pub(crate) fn clip(r: Rect, (cols, rows): (u16, u16)) -> Option<CellRect> {
    let right = r.x.saturating_add(r.w).min(cols);
    let bottom = r.y.saturating_add(r.h).min(rows.saturating_sub(1));
    let (w, h) = (right.saturating_sub(r.x), bottom.saturating_sub(r.y));
    (w > 0 && h > 0).then_some(CellRect {
        x: r.x,
        y: r.y,
        w,
        h,
    })
}
