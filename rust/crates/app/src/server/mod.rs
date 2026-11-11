//! `fleetmux server`: the per-run process that owns every pane's PTY and screen state (design.md §2.1).
//!
//! This file is the stage-B contract between the server packages (brief-m03 §3.3): it is written once in stage A
//! and not edited in stage B. Each submodule belongs to one package; the owner is noted next to it.

use std::collections::BTreeMap;
use std::path::PathBuf;

/// Command line, startup and the event loop (ctl-1).
pub mod boot;
/// `--daemonize`, `server.json`, `start.lock` (ctl-1).
pub mod daemonize;
/// One handler per control command (ctl-1).
pub mod handlers;
/// SGR mouse handling (attach-1).
pub mod mouse;
/// M2.6 history overlay geometry for `0x85` (ctl-1; brief-m26 W0 signature, W2 body).
pub mod overlay;
/// Pane ledger: PTY children, parsers, write queues, `panes.json` (server-1).
pub mod pane;
/// PTY spawn, process start time, process-group signals (server-1).
pub mod pty;
/// `fleetmux reap` and the logind warning (server-1).
pub mod reap;
/// `--record` PTY output recordings (server-1).
pub mod record;
/// Page (scrollback) mode (attach-1).
pub mod scroll;
/// gui-scrollbar: per-cell scrollbar geometry for `0x86`.
pub mod scrollbar;
/// Control socket bind / accept / dispatch (ctl-1).
pub mod socket;
/// `server.stats` counters (ctl-1).
pub mod stats;
/// View connections and viewer input (attach-1).
pub mod view;
/// Prefix-key table and `layout.json` (attach-1).
pub mod views;
/// M2.5 `web.*` relay and window bookkeeping (ctl-1; brief-m25 W0 signatures, W2 bodies).
pub mod web;

pub use boot::{Opts, parse_opts, run};

/// Everything the server owns, behind one `Arc<Mutex<State>>` shared by the control and view connections.
pub struct State {
    /// Every live pane.
    pub panes: pane::PaneStore,
    /// Groups, tab order, selection, focus, ratios, zoom.
    pub layout: fleetmux_layout::Layout,
    /// The fleet sidebar (S1): the last good kv `sidebar` and whether the user hid it. Shared by every viewer
    /// (its width changes every pane's PTY width); not part of `layout.json`.
    pub side: crate::render::sidebar::Side,
    /// `kv.set` / `kv.get` store (e.g. `history_url` for prefix `o`).
    pub kv: BTreeMap<String, String>,
    /// Attached view connections.
    pub viewers: view::ViewerHub,
    /// Page mode, when a viewer entered it with prefix `[`.
    pub page: Option<scroll::PageMode>,
    /// `server.stats` counters.
    pub counters: stats::Counters,
    /// The runtime directory (`--runtime`).
    pub runtime: PathBuf,
    /// This server generation's random code (PROTOCOL.md §1.4).
    pub epoch: String,
    /// The server's own start time, PROTOCOL.md §8.6.
    pub start_time: u64,
    /// The prefix key as bytes (`C-b` → `[0x02]`).
    pub prefix: Vec<u8>,
    /// M2.5: which viewers have a web window, the primary one, relays waiting for replies (`web.rs`).
    pub web: web::WebHub,
    /// The sidebar's restore attempt (A42-LINUX-RESTORE-FAILURE-04): the pane this server spawned for `^b r` / `↺`
    /// and, when it failed, the reason that stays on the status line until the operator retries.
    pub resume: view::ResumeAttempt,
    /// M4 fleet pages (design m4 §4.3 R2, PROTOCOL.md §2 addendum): the child pages and which page is current.
    /// `layout` above is always the layout of [`FleetPages::in_state`]; the other pages' layouts are parked here.
    /// `kv` above stays the root page's kv (reserved keys live only there).
    pub fleet_pages: FleetPages,
}

/// M4: every child fleet page of this tree and the current-page pointer (design m4 §4.3 R2, §4.4 ② / ③).
#[derive(Debug, Default)]
pub struct FleetPages {
    /// The page every viewer sees (`""` = the root page). Starts at the root; only `page.select` changes it.
    pub current: String,
    /// The page whose layout sits in `State::layout` right now: `current`, except while one control command runs
    /// on another page (`handlers::on_page` swaps that page's layout in and back out under the same lock).
    pub in_state: String,
    /// The root page's layout while it is not in `State::layout`.
    pub root_layout: Option<fleetmux_layout::Layout>,
    /// Child pages by id (`fleetmux_proto::valid_page_id`), at most `fleetmux_proto::MAX_PAGES`.
    pub pages: BTreeMap<String, FleetPage>,
    /// A layout change ran while another page's layout was swapped in: `Some(panes_changed)` until the swap ends
    /// and `handlers::after_layout_change` runs for real.
    pub pending: Option<bool>,
    /// The panes on screen at the last `view::apply_pty_sizes` (each group's selected tab of the current page, or the
    /// zoomed pane): a tmux client that comes into view is told so (`view::nudge_shown_tmux`).
    pub shown: Vec<fleetmux_proto::PaneId>,
    /// The panes whose foreground program was `tmux` at the last `view::tmux_watch_tick`, with their PTY size.
    pub tmux_seen: BTreeMap<fleetmux_proto::PaneId, (u16, u16)>,
    /// When `view::tmux_watch_tick` nudges the tmux clients in view again (a tmux client attached or was resized).
    pub nudge_at: Option<std::time::Instant>,
}

/// M4: one child fleet page — the same fixed layout type as the root (R1), its own kv, live or reclaimed.
#[derive(Debug)]
pub struct FleetPage {
    pub title: String,
    /// `reclaimed` = the child fleet was collected; the page stays as a read-only record (design m4 §5.4).
    pub reclaimed: bool,
    /// `None` while this page's layout is in `State::layout`.
    pub layout: Option<fleetmux_layout::Layout>,
    /// This page's kv (`run`, `history_url`, …); the reserved keys are root-only.
    pub kv: BTreeMap<String, String>,
}
