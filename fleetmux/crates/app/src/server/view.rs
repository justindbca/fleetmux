//! Viewer connections on the server side (design.md §2.1 / §2.5 / §2.6; PROTOCOL.md §6, §14): the hub of
//! connected `fleetmux attach` clients and the input path that splits their bytes into prefix keys, SGR mouse
//! reports, outer paste packets and plain bytes for the focused pane.
//!
//! B7 (attach-render). Seam with the event loop (boot.rs, ctl-1 B6, supervisor ruling M14):
//! - `socket.rs` reads a view handshake's first line, writes the `ok` line and calls [`serve_view`] with the
//!   **same** buffered reader (bytes the client sent right after the handshake are already in its buffer).
//! - The event loop waits on [`ViewerHub::dirty_notify`], paces with `render::draw::Throttle`, takes the
//!   `State` lock and calls [`render_all`]. Anything that changes what viewers see calls
//!   [`ViewerHub::mark_dirty`]; read-only control commands do not.
//! - Lock rule: nothing here awaits a channel send while holding the `State` lock. Frames are handed to a
//!   per-viewer bounded queue with `try_reserve_many` / `try_send`; only that viewer's writer task awaits the
//!   socket, without the lock.
//!
//! Queue / diff-base invariant: `Viewer::prev` always equals what the client shows once every frame already in
//! its queue is written. A screen update is reserved whole (all of its ≤ 1 MiB chunks) before it is queued: no
//! room before drawing → the viewer is skipped and `prev` is untouched (the next diff carries everything); the
//! reservation fails after drawing → nothing is queued and `prev = None` (the next frame is a full redraw). The
//! writer task wakes the renderer when the queue drains. An update needing more chunks than the queue can ever
//! hold, or a viewer stalled for [`STALL_LIMIT`], is disconnected with an `ERROR` line — never a silent loss.
//! Non-canvas frames (`0x81` OSC 52, `0x82`, `0x83`) that find the queue full wait in a bounded per-viewer
//! backlog the writer flushes when the queue drains; a full backlog disconnects the viewer with an `ERROR`.
//!
//! S1 adds the fleet sidebar: [`sync_sidebar`] turns kv `sidebar` (pushed by the fleet daemon) into
//! `State::side` and the layout's sidebar width, so every pane's PTY and every viewer's canvas follow.
use std::collections::{BTreeMap, HashMap, VecDeque};
use std::io;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use fleetmux_layout::{Group, Hit};
use fleetmux_proto::view::{self, Frame, MAX_FRAME};
use fleetmux_proto::{DEFAULT_KILL_TIMEOUT_MS, PaneId, Spawn, WriteMode};
use fleetmux_term::color::{Depth, depth_from_env};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::sync::mpsc::{self, Receiver, Sender};
use tokio::sync::{Mutex, Notify};

use super::State;
use super::mouse::{self, Action, ClickMemory};
use super::scroll::{self, PageAction};
use crate::attach::clipboard;
use crate::attach::input::{InputScanner, Token, split_keys};
use crate::render::draw::{self, DrawnPanes, draw_cached};
use crate::render::sidebar;

/// Server-local id of one view connection; never reused within a server's lifetime.
pub type ViewerId = u32;

/// Frames one viewer's queue holds (each ≤ 1 MiB).
pub const VIEW_QUEUE: usize = 32;
/// Non-canvas frames one viewer may have waiting while its queue is full.
pub const BACKLOG_MAX: usize = 16;
/// A viewer whose queue stays full this long is disconnected.
pub const STALL_LIMIT: Duration = Duration::from_secs(30);
/// An outer paste larger than this is passed on in pieces of this size.
pub const PASTE_MAX: usize = 4 << 20;

/// One view connection's per-viewer state (PROTOCOL.md §14: each viewer has its own canvas and diff base).
pub struct Viewer {
    /// This viewer supplies its own header; plain attach and old clients keep row-0 tools.
    pub native_header: bool,
    pub cols: u16,
    pub rows: u16,
    /// From the handshake's `term` / `colorterm` (`fleetmux_term::color::depth_from_env`).
    pub depth: Depth,
    /// Last time this viewer sent a `0x01` input frame; the PTY size follows the latest one (design.md §2.1).
    pub last_input_at: Option<Instant>,
    /// The canvas last sent to this viewer; `None` = next frame is a full redraw.
    pub prev: Option<ratatui::buffer::Buffer>,
    /// The selection this viewer made with the mouse, drawn reverse-video on its own canvas (PROTOCOL.md §11) until
    /// its next mouse press or key; change it only through [`select`], which drops the pane cache.
    pub selection: Option<Selection>,
}

/// A mouse selection in one pane's view: (row, col) ends as `TermParser::selection_text` takes them, and the view's
/// `display_offset` when it was made — scrolled to another offset, the highlight is not drawn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Selection {
    pub pane: PaneId,
    pub from: (u16, u16),
    pub to: (u16, u16),
    pub offset: usize,
}

/// State shared between the hub (under the `State` lock) and the connection's reader / writer tasks.
#[derive(Default)]
struct Shared {
    /// A screen update was skipped or failed for lack of room; the writer wakes the renderer once drained.
    stalled: AtomicBool,
    stalled_since: StdMutex<Option<Instant>>,
    /// Non-canvas frames waiting for room (≤ [`BACKLOG_MAX`]).
    backlog: StdMutex<VecDeque<Frame>>,
    /// The connection is going away (removed, writer failed, or disconnected by the hub).
    dead: AtomicBool,
    /// Backlog has frames / stall flag set: the writer should look.
    wake_writer: Notify,
    /// Stop writing now.
    kill: Notify,
    /// Stop reading now.
    wake_reader: Notify,
}

impl Shared {
    fn shut(&self) {
        self.dead.store(true, Ordering::SeqCst);
        self.kill.notify_one();
        self.wake_reader.notify_one();
    }
}

struct Slot {
    viewer: Viewer,
    tx: Sender<Frame>,
    shared: Arc<Shared>,
    scanner: InputScanner,
    scanner_prefix: Option<u8>,
    paste: Vec<u8>,
    /// B14: what the press that is still held hit (separator / pane), for motion and release.
    drag: Option<Hit>,
    /// B14: last tab press, for double clicks.
    clicks: ClickMemory,
    /// B14: selection start (pane, (row, col) in its view) while the left button is held with inner mouse off.
    select_from: Option<(PaneId, (u16, u16))>,
    /// B22: what `viewer.prev` holds from each pane, and (fix-m0-3) the outer cursor last queued; cleared
    /// wherever `prev` is reset to `None` (resize, an update that found no room), so the next update is a full
    /// redraw that places the cursor again.
    drawn: DrawnPanes,
    /// M2.6: the handshake said `"overlay":1` ([`ViewOpts::overlay`]): this connection gets `0x85`.
    overlay: bool,
    /// M2.6: the last `0x85` payload queued for this connection; a new one is sent only when it differs.
    last_overlay: Option<String>,
    /// gui-scrollbar: the handshake said `"scroll":1` ([`ViewOpts::scroll`]): this connection gets `0x86`.
    scroll: bool,
    /// gui-scrollbar: the last `0x86` payload queued for this connection; sent again only when it differs.
    last_scroll: Option<String>,
    /// S1e: the handshake's `inset` ([`ViewOpts::inset`]) or the last `0x06` line: the columns the toolbar makes
    /// room for on this viewer ([`State::side`]'s `inset` is the maximum over all slots, plan §17.2).
    inset: u16,
    /// A42-LINUX-SIDEBAR-ALIGN-03: the last `0x06` line's optional `sidebar_min` (0 = none): the narrowest sidebar
    /// band this viewer's window needs for its own header controls. [`State::side`]'s floor is the maximum over all
    /// slots, like `inset`.
    sidebar_min: u16,
}

/// All connected viewers. They share selection / ratios / zoom (held in `State::layout`), not canvases.
pub struct ViewerHub {
    next_id: ViewerId,
    viewers: BTreeMap<ViewerId, Slot>,
    /// `--default-size` (boot.rs sets it once): the virtual screen while nobody is attached.
    default_size: (u16, u16),
    /// `size_for_ptys(default_size)`, kept current on every add / remove / resize / input, so readers of the
    /// layout size (`draw`) stay right while `render_all` has the viewer map taken out.
    layout_size: (u16, u16),
    dirty: Arc<Notify>,
    /// B14: the drag a viewer is holding (latest press wins), kept here so `draw` can read it while
    /// `render_all` has the viewer map taken out.
    active_drag: Option<(ViewerId, Hit)>,
    /// B22: per layout pane, the generation of its parser content; bumped by `render_all` when the parser
    /// reports a change. Values come from one hub-wide counter, so they are never reused.
    pane_gens: HashMap<PaneId, u64>,
    last_gen: u64,
    /// M4 (design m4 §5.3): the `history_url` of a page a viewer selected (sidebar pick, `^b ( )`, a jump event)
    /// under the lock; [`handle_frame`] takes it afterwards and pushes `0x84 goto` to the web windows off the lock,
    /// like the `page.select` command does.
    pending_goto: Option<String>,
}

impl Default for ViewerHub {
    fn default() -> Self {
        ViewerHub {
            next_id: 1,
            viewers: BTreeMap::new(),
            default_size: (240, 60),
            layout_size: (240, 60),
            dirty: Arc::new(Notify::new()),
            active_drag: None,
            pane_gens: HashMap::new(),
            last_gen: 0,
            pending_goto: None,
        }
    }
}

/// PROTOCOL.md §14: the viewer's colour depth from its handshake (`fleetmux_term::color::depth_from_env`).
fn depth_for(hello: &view::Hello) -> Depth {
    depth_from_env(&hello.term, &hello.colorterm)
}

impl ViewerHub {
    /// Register a viewer after its handshake; `tx` feeds that connection's writer task.
    pub fn add(&mut self, hello: &view::Hello, tx: Sender<Frame>) -> ViewerId {
        let id = self.next_id;
        self.next_id += 1;
        let viewer = Viewer {
            native_header: false,
            cols: hello.cols.max(1),
            rows: hello.rows.max(1),
            depth: depth_for(hello),
            last_input_at: None,
            prev: None,
            selection: None,
        };
        let slot = Slot {
            viewer,
            tx,
            shared: Arc::new(Shared::default()),
            scanner: InputScanner::new(None),
            scanner_prefix: None,
            paste: Vec::new(),
            drag: None,
            clicks: ClickMemory::default(),
            select_from: None,
            drawn: DrawnPanes::default(),
            overlay: false,
            last_overlay: None,
            scroll: false,
            last_scroll: None,
            inset: 0,
            sidebar_min: 0,
        };
        self.viewers.insert(id, slot);
        self.refresh_size();
        self.mark_dirty();
        id
    }

    pub fn remove(&mut self, id: ViewerId) {
        if let Some(slot) = self.viewers.remove(&id) {
            slot.shared.shut();
        }
        if self.active_drag.is_some_and(|(v, _)| v == id) {
            self.active_drag = None;
            self.mark_dirty();
        }
        self.refresh_size();
    }

    /// The size the PTY layout is computed for: the viewer with the latest input, else the latest added, else
    /// `default` (`--default-size`, 240×60) when nobody is attached.
    pub fn size_for_ptys(&self, default: (u16, u16)) -> (u16, u16) {
        let latest_input = self
            .viewers
            .values()
            .filter_map(|s| s.viewer.last_input_at.map(|t| (t, &s.viewer)))
            .max_by_key(|(t, _)| *t)
            .map(|(_, v)| v);
        // Ids grow, so the last entry is the latest added.
        match latest_input.or_else(|| self.viewers.values().next_back().map(|s| &s.viewer)) {
            Some(v) => (v.cols, v.rows),
            None => default,
        }
    }

    /// Record that `id` just sent input (`last_input_at = now`).
    pub fn note_input(&mut self, id: ViewerId) {
        if let Some(s) = self.viewers.get_mut(&id) {
            s.viewer.last_input_at = Some(Instant::now());
        }
        self.refresh_size();
    }

    /// Send `frame` to every viewer (e.g. the `0x81` OSC 52 copy, `0x82` exit). Only for frames that do not
    /// change the canvas: screen updates go through [`render_all`]. The per-viewer queue is bounded, but no
    /// frame is silently dropped: a full queue parks it in a bounded backlog the writer flushes once the queue
    /// drains, and a full backlog disconnects that viewer with an `ERROR` line.
    pub fn broadcast(&self, frame: Frame) {
        for (id, slot) in &self.viewers {
            send_noncanvas(*id, slot, frame.clone());
        }
    }

    pub fn len(&self) -> usize {
        self.viewers.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    // ---- B7 appends (supervisor ruling M14) ----

    /// Something viewers see changed: wake the event loop's render step (a stored permit if it is busy).
    /// B14, for B16's separator highlight: what the drag being held by a viewer hit (`VSep` / `HSep` while a
    /// separator is dragged, `Pane(g)` while a press in a pane is held), `None` when no button is held. Shared
    /// by all viewers (the ratios are shared) and readable from `draw` during `render_all`.
    pub fn drag_hit(&self) -> Option<Hit> {
        self.active_drag.map(|(_, h)| h)
    }

    pub fn mark_dirty(&self) {
        self.dirty.notify_one();
    }

    /// S1e (plan §17.2): the biggest `inset` any connected viewer reported — the toolbar's room for the gui's
    /// traffic lights; `0` while no viewer asked for any.
    pub fn max_inset(&self) -> u16 {
        self.viewers.values().map(|s| s.inset).max().unwrap_or(0)
    }

    /// A42-LINUX-SIDEBAR-ALIGN-03: the biggest `sidebar_min` any connected viewer reported — the narrowest band a
    /// window's own header controls need. `0` while no viewer asked for one.
    pub fn max_sidebar_min(&self) -> u16 {
        self.viewers
            .values()
            .map(|s| s.sidebar_min)
            .max()
            .unwrap_or(0)
    }

    /// Drop every viewer's pane cache and ask for a full redraw (plan §11.3): the close box is drawn over the panes,
    /// and a pane copied from `prev` would keep the box's cells after it closes (plan §3.5).
    pub fn forget_drawn(&mut self) {
        for slot in self.viewers.values_mut() {
            slot.drawn.clear();
        }
        self.mark_dirty();
    }

    /// The notifier [`ViewerHub::mark_dirty`] signals; boot.rs's event loop waits on it.
    pub fn dirty_notify(&self) -> Arc<Notify> {
        self.dirty.clone()
    }

    /// A `0x02` from viewer `id`: new canvas size, full redraw next. Callers then run [`apply_pty_sizes`].
    pub fn resize(&mut self, id: ViewerId, cols: u16, rows: u16) {
        if let Some(s) = self.viewers.get_mut(&id) {
            s.viewer.cols = cols.max(1);
            s.viewer.rows = rows.max(1);
            s.viewer.prev = None;
            s.drawn.clear();
        }
        self.refresh_size();
        self.mark_dirty();
    }

    /// `--default-size`; boot.rs calls this once after building `State`.
    pub fn set_default_size(&mut self, size: (u16, u16)) {
        self.default_size = (size.0.max(1), size.1.max(1));
        self.refresh_size();
    }

    pub fn default_size(&self) -> (u16, u16) {
        self.default_size
    }

    /// The size the layout is drawn and the PTYs are sized for (= `size_for_ptys(default_size())`, cached).
    pub fn layout_size(&self) -> (u16, u16) {
        self.layout_size
    }

    /// Whether `id` is still connected.
    pub fn contains(&self, id: ViewerId) -> bool {
        self.viewers
            .get(&id)
            .is_some_and(|s| !s.shared.dead.load(Ordering::SeqCst))
    }

    /// Send a non-canvas frame (e.g. `0x83`) to one viewer; same no-silent-loss rule as `broadcast`.
    pub fn send_to(&self, id: ViewerId, frame: Frame) {
        if let Some(slot) = self.viewers.get(&id) {
            send_noncanvas(id, slot, frame);
        }
    }

    /// B22: the generation of pane `id`'s parser content (`None` = not seen by `render_all` yet). `draw` copies a
    /// pane from the previous canvas only when this and its rectangle are unchanged.
    pub fn pane_gen(&self, id: &str) -> Option<u64> {
        self.pane_gens.get(id).copied()
    }

    /// B22: `changes` = every pane of the layout with whether its parser reported a change since the last call
    /// (`TermParser::take_dirty`). Changed or new panes get a fresh generation; panes not listed are forgotten.
    pub(crate) fn note_pane_changes(&mut self, changes: impl IntoIterator<Item = (PaneId, bool)>) {
        let mut gens = HashMap::with_capacity(self.pane_gens.len());
        for (id, changed) in changes {
            let g = match self.pane_gens.get(&id) {
                Some(g) if !changed => *g,
                _ => {
                    self.last_gen += 1;
                    self.last_gen
                }
            };
            gens.insert(id, g);
        }
        self.pane_gens = gens;
    }

    /// Recompute the cached layout size; a change redraws every viewer (borders move even with no pane).
    fn refresh_size(&mut self) {
        let size = self.size_for_ptys(self.default_size);
        if size != self.layout_size {
            self.layout_size = size;
            self.mark_dirty();
        }
    }
}

/// Queue a non-canvas frame, or park it in the backlog when the queue is full (order kept: once a backlog
/// exists, later frames join it). A full backlog shuts the connection with an `ERROR`.
fn send_noncanvas(id: ViewerId, slot: &Slot, frame: Frame) {
    let sh = &slot.shared;
    if sh.dead.load(Ordering::SeqCst) {
        return;
    }
    let mut backlog = sh.backlog.lock().unwrap_or_else(|e| e.into_inner());
    let frame = if backlog.is_empty() {
        match slot.tx.try_send(frame) {
            Ok(()) => return,
            Err(mpsc::error::TrySendError::Closed(_)) => return,
            Err(mpsc::error::TrySendError::Full(f)) => f,
        }
    } else {
        frame
    };
    if backlog.len() >= BACKLOG_MAX {
        drop(backlog);
        log(
            "ERROR",
            &format!("viewer {id}: queue and backlog full, disconnecting the slow viewer"),
        );
        sh.shut();
        return;
    }
    backlog.push_back(frame);
    drop(backlog);
    sh.wake_writer.notify_one();
}

/// What became of one viewer's screen update.
#[derive(Debug, PartialEq, Eq)]
enum Commit {
    /// Queued whole.
    Sent,
    /// Nothing to send.
    Empty,
    /// No room: nothing queued; the caller already left or reset `prev` accordingly.
    Stalled,
    /// Can never fit or the viewer is gone: disconnect it.
    Drop,
}

/// Queue `bytes` (one drawn update) for `slot` whole, or not at all. On failure after drawing, `prev` is reset
/// so the next frame is a full redraw.
fn commit(id: ViewerId, slot: &mut Slot, bytes: Vec<u8>, now: Instant) -> Commit {
    if bytes.is_empty() {
        return Commit::Empty;
    }
    let chunks: Vec<&[u8]> = bytes.chunks(MAX_FRAME).collect();
    if chunks.len() > slot.tx.max_capacity() {
        log(
            "ERROR",
            &format!(
                "viewer {id}: a {}-byte screen update needs {} frames, more than its queue of {}; disconnecting",
                bytes.len(),
                chunks.len(),
                slot.tx.max_capacity()
            ),
        );
        return Commit::Drop;
    }
    match slot.tx.try_reserve_many(chunks.len()) {
        Ok(permits) => {
            for (p, c) in permits.zip(chunks) {
                p.send(Frame::Screen(c.to_vec()));
            }
            Commit::Sent
        }
        Err(mpsc::error::TrySendError::Full(())) => {
            slot.viewer.prev = None;
            slot.drawn.clear();
            stall(slot, now);
            Commit::Stalled
        }
        Err(mpsc::error::TrySendError::Closed(())) => Commit::Drop,
    }
}

fn stall(slot: &Slot, now: Instant) {
    let sh = &slot.shared;
    sh.stalled_since
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .get_or_insert(now);
    sh.stalled.store(true, Ordering::SeqCst);
    // The writer may already be idle on an empty queue: make it look again.
    sh.wake_writer.notify_one();
}

fn stalled_too_long(slot: &Slot, now: Instant) -> bool {
    let since = *slot
        .shared
        .stalled_since
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    since.is_some_and(|t| now.duration_since(t) > STALL_LIMIT)
}

/// Draw every viewer and queue its update; returns the number of screen updates queued (one per viewer that
/// had changes) and adds it to `counters.frames` — the only place `frames` is counted.
pub fn render_all(state: &mut State) -> u64 {
    let now = Instant::now();
    state.viewers.refresh_size();
    take_pane_changes(state);
    // `draw` takes `&State` while each `Viewer` lives in `state.viewers`: take the map out for the loop. The
    // hub's other fields (layout size, default size, notifier) stay in place, so nothing `draw` reads changes.
    let mut map = std::mem::take(&mut state.viewers.viewers);
    let mut sent = 0u64;
    let mut gone = Vec::new();
    for (id, slot) in map.iter_mut() {
        if slot.shared.dead.load(Ordering::SeqCst) || slot.tx.is_closed() {
            gone.push(*id);
            continue;
        }
        if slot.tx.capacity() == 0 {
            if stalled_too_long(slot, now) {
                log(
                    "ERROR",
                    &format!("viewer {id}: queue full for over {STALL_LIMIT:?}, disconnecting"),
                );
                gone.push(*id);
            } else {
                stall(slot, now); // `prev` untouched: nothing drawn, nothing sent
            }
            continue;
        }
        // A selection in a pane that has closed goes with it.
        if slot
            .viewer
            .selection
            .as_ref()
            .is_some_and(|s| state.panes.term(&s.pane).is_none())
        {
            slot.viewer.selection = None;
        }
        let bytes = draw_cached(state, &mut slot.viewer, &mut slot.drawn);
        match commit(*id, slot, bytes, now) {
            Commit::Sent => {
                sent += 1;
                overlay_after_screen(state, slot, now);
                scroll_after_screen(state, slot, now);
            }
            Commit::Empty => {
                overlay_after_screen(state, slot, now);
                scroll_after_screen(state, slot, now);
            }
            Commit::Stalled => {} // M2.6: no `0x85` / `0x86` either; the next round catches up
            Commit::Drop => gone.push(*id),
        }
    }
    state.viewers.viewers = map;
    if !gone.is_empty() {
        for id in gone {
            state.viewers.remove(id);
        }
        apply_pty_sizes(state);
    }
    state.counters.frames.fetch_add(sent, Ordering::Relaxed);
    sent
}

/// M2.6 (PROTOCOL.md §6.3): after this round's screen update for `slot` was queued (or there was none), queue its
/// `0x85` when the connection asked for it and the overlay differs from the last one queued. Same queue, right
/// behind the screen frames, so the order holds. Never into the backlog (a later round's screen frames could
/// overtake it there): with no room now, or frames parked, nothing is sent and the viewer counts as stalled, so
/// the writer wakes the renderer once drained and the next round sends it.
fn overlay_after_screen(state: &State, slot: &mut Slot, now: Instant) {
    // `prev` is `None` only before the first screen frame (or after a reset): the first `0x85` follows a screen.
    if !slot.overlay || slot.viewer.prev.is_none() {
        return;
    }
    let mut overlay = super::overlay::overlay_for(
        &state.layout,
        state.viewers.layout_size(),
        (slot.viewer.cols, slot.viewer.rows),
        super::handlers::current_page_kv(state, "history_url").as_deref(),
    );
    if state.side.confirm {
        // S1b (plan §2.5 / §3.5): the box sits in the middle of the screen, where the gui's history iframe would
        // cover it — the `0x85` carries no rect while it is open.
        overlay.rect = None;
    }
    // A26 rev5 (`CURRENT_REQUIREMENTS` rev 6 `utm_topbar`): the window that draws its own header row (the Linux
    // GTK headerbar) reads the topbar's title/status from here, so a mirrored layout's grid keeps no topbar row.
    // The band and its denominator both come from the layout size the screen is composed at, so the headerbar's
    // boundary is the grid's own divider column (the page fills the window, so `cols / layout cols` is its width).
    let (lc, _) = state.viewers.layout_size();
    overlay.topbar = if state.layout.native_header() {
        super::overlay::topbar_for(&state.side, &state.layout, lc)
    } else {
        None
    };
    let line = overlay.encode();
    if slot.last_overlay.as_deref() == Some(line.as_str()) {
        return;
    }
    let parked = !slot
        .shared
        .backlog
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .is_empty();
    if parked {
        stall(slot, now);
        return;
    }
    match slot.tx.try_send(Frame::Overlay(line.clone())) {
        Ok(()) => slot.last_overlay = Some(line),
        Err(mpsc::error::TrySendError::Full(_)) => stall(slot, now),
        Err(mpsc::error::TrySendError::Closed(_)) => {}
    }
}

/// gui-scrollbar (PROTOCOL.md §6.3): after this round's screen update for `slot` was queued (or there was none),
/// queue its `0x86` when the connection asked for it (`"scroll":1`) and the state differs from the last one
/// queued. Same queue and rules as [`overlay_after_screen`]: right behind the screen frames, never into the
/// backlog; with no room now, or frames parked, nothing is sent and the viewer counts as stalled so the next
/// round catches up.
fn scroll_after_screen(state: &State, slot: &mut Slot, now: Instant) {
    // `prev` is `None` only before the first screen frame (or after a reset): the first `0x86` follows a screen.
    if !slot.scroll || slot.viewer.prev.is_none() {
        return;
    }
    let line = super::scrollbar::scroll_state_for(
        &state.layout,
        state.viewers.layout_size(),
        (slot.viewer.cols, slot.viewer.rows),
        &|id| {
            state
                .panes
                .term(id)
                .map(|t| (t.display_offset(), t.scrollback_len()))
        },
    )
    .encode();
    if slot.last_scroll.as_deref() == Some(line.as_str()) {
        return;
    }
    let parked = !slot
        .shared
        .backlog
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .is_empty();
    if parked {
        stall(slot, now);
        return;
    }
    match slot.tx.try_send(Frame::Scroll(line.clone())) {
        Ok(()) => slot.last_scroll = Some(line),
        Err(mpsc::error::TrySendError::Full(_)) => stall(slot, now),
        Err(mpsc::error::TrySendError::Closed(_)) => {}
    }
}

/// B22: take each layout pane's parser change flag (`TermParser::take_dirty`) into the hub's generations. Once
/// per render for all viewers: a viewer skipped now keeps its old record and compares against the newer
/// generation later.
pub(crate) fn take_pane_changes(state: &mut State) {
    let ids: Vec<PaneId> = Group::ALL
        .iter()
        .flat_map(|g| state.layout.group(*g).iter().cloned())
        .collect();
    let changes: Vec<(PaneId, bool)> = ids
        .into_iter()
        .map(|id| {
            let changed = state.panes.term_mut(&id).is_none_or(|t| t.take_dirty());
            (id, changed)
        })
        .collect();
    state.viewers.note_pane_changes(changes);
}

/// The PTY size for a pane in `group` at the current layout size (`pane.spawn` in handlers.rs uses it before
/// `PaneStore::spawn`).
pub fn pty_size_for(state: &State, group: Group) -> (u16, u16) {
    let (c, r) = state.viewers.size_for_ptys(state.viewers.default_size());
    let rects = state.layout.rects(c, r);
    let rect = draw::group_rect(&rects, group);
    (rect.w.max(1), rect.h.max(1))
}

/// Resize every pane whose PTY size differs from its rectangle at the current layout size (the zoomed pane
/// gets the zoom rectangle); marks dirty when anything changed. Selecting another tab never resizes: a group's
/// tabs share one rectangle (design.md §2.5).
pub fn apply_pty_sizes(state: &mut State) {
    state.viewers.refresh_size();
    let (c, r) = state.viewers.layout_size();
    // M4 (design m4 R5): every page's panes follow the one shared size, each by its own page's rectangles, so a
    // page switch resizes nothing unless the two pages differ in zoom.
    super::handlers::spread_shared(state);
    let mut want = Vec::new();
    pty_rects(&state.layout, c, r, &mut want);
    for layout in super::handlers::parked_layouts(state) {
        pty_rects(layout, c, r, &mut want);
    }
    let mut changed = false;
    for (id, w, h) in want {
        let Some(info) = state.panes.info(&id) else {
            continue;
        };
        if (info.cols, info.rows) != (w, h) {
            state.panes.resize(&id, w, h);
            changed = true;
        }
    }
    if changed {
        state.viewers.mark_dirty();
    }
    nudge_shown_tmux(state, changed);
}

/// A tmux session shown in two tabs (the parent page's worker tab and the child page's view, fleetmux design §44
/// worker-area mapping) has one window size: tmux's `window-size latest` takes the client with the latest input or
/// resize. A page switch or a tab change resizes nothing, so the client that comes into view would keep the hidden
/// one's size (rows / columns filled with `·`). Every pane that comes into view, and after a resize every pane in
/// view, gets a `SIGWINCH` to its foreground group when that program is `tmux`: the client re-reads its size and
/// becomes the latest. Other programs are never signalled. A hidden client that attaches or is resized later (its
/// `tmux attach` starts after the layout change; `arrange` moves it to another group) takes the window back with its
/// own resize: [`tmux_watch_tick`] sees that and nudges the clients in view once more, after [`NUDGE_DELAY`].
fn nudge_shown_tmux(state: &mut State, resized: bool) {
    if state.fleet_pages.in_state != state.fleet_pages.current {
        return;
    }
    let shown = shown_panes(state);
    let nudge: Vec<PaneId> = shown
        .iter()
        .filter(|p| resized || !state.fleet_pages.shown.contains(p))
        .cloned()
        .collect();
    state.fleet_pages.shown = shown;
    for id in nudge {
        nudge_tmux(state, &id);
    }
}

/// The panes on screen: each group's selected tab of the page in `state.layout`, or the zoomed pane.
fn shown_panes(state: &State) -> Vec<PaneId> {
    match state.layout.zoom() {
        Some(z) => vec![z.clone()],
        None => Group::ALL
            .iter()
            .filter_map(|g| state.layout.selected(*g).cloned())
            .collect(),
    }
}

/// `SIGWINCH` to `pane`'s foreground group when its program is `tmux`; `true` when it was one.
fn nudge_tmux(state: &State, pane: &str) -> bool {
    let Some(pgid) = state.panes.foreground_pgid(pane) else {
        return false;
    };
    if super::pty::process_name(pgid).as_deref() != Some("tmux") {
        return false;
    }
    let _ = super::pty::signal_group(pgid, libc::SIGWINCH);
    true
}

/// How long after a tmux client attached or was resized (anywhere, any page) the clients in view are nudged again:
/// long enough for that client's own resize to have reached the tmux server first.
pub const NUDGE_DELAY: Duration = Duration::from_millis(300);
/// How often the server looks for tmux clients that attached or were resized ([`tmux_watch_tick`]).
pub const TMUX_WATCH_TICK: Duration = Duration::from_millis(100);

/// One look of the tmux watch (`boot::tmux_watch`): a pane whose foreground program became `tmux`, or a tmux pane
/// whose PTY size changed, schedules a nudge of the tmux clients in view [`NUDGE_DELAY`] later; a due nudge runs.
pub(crate) fn tmux_watch_tick(state: &mut State, now: Instant) {
    let mut seen = BTreeMap::new();
    for info in state.panes.list() {
        let tmux = state
            .panes
            .foreground_pgid(&info.pane)
            .and_then(super::pty::process_name)
            .is_some_and(|n| n == "tmux");
        if tmux {
            seen.insert(info.pane.clone(), (info.cols, info.rows));
        }
    }
    if seen
        .iter()
        .any(|(p, size)| state.fleet_pages.tmux_seen.get(p) != Some(size))
    {
        state.fleet_pages.nudge_at = Some(now + NUDGE_DELAY);
    }
    state.fleet_pages.tmux_seen = seen;
    let due = state.fleet_pages.nudge_at.is_some_and(|at| now >= at);
    if due && state.fleet_pages.in_state == state.fleet_pages.current {
        state.fleet_pages.nudge_at = None;
        for id in shown_panes(state) {
            nudge_tmux(state, &id);
        }
    }
}

/// Each pane of `layout` with the PTY size its rectangle gives at `c`×`r` (the zoomed pane: the zoom rectangle).
fn pty_rects(layout: &fleetmux_layout::Layout, c: u16, r: u16, out: &mut Vec<(PaneId, u16, u16)>) {
    let rects = layout.rects(c, r);
    let zoom = layout.zoom().cloned();
    for g in Group::ALL {
        for id in layout.group(g) {
            let rect = match (&zoom, rects.zoom) {
                (Some(z), Some(zr)) if z == id => zr,
                _ => draw::group_rect(&rects, g),
            };
            if rect.w == 0 || rect.h == 0 {
                continue;
            }
            out.push((id.clone(), rect.w, rect.h));
        }
    }
}

/// After the layout changed (selection, focus, zoom, ratios): mirror it into the pane ledger, resize what
/// needs it, mark dirty.
pub(crate) fn layout_changed(state: &mut State) {
    state.panes.sync_layout(&state.layout);
    sync_bash(state);
    apply_pty_sizes(state);
    state.viewers.mark_dirty();
}

/// The toolbar's one-shot notices (plan §11.1): each button that has no action yet says when its milestone opens.
const SWITCH_NOTICE: &str = "切换 fleet：S3 开放";
const CHILD_CLOSE_NOTICE: &str = "关闭子 fleet：S3 开放";
/// M4 (design m4 R8): a card names a page (`page`) the server does not have.
const NO_PAGE_NOTICE: &str = "这个 fleet 没有画面可看";
/// Plan §13.6-2: the one-shot answer of the topbar's `↺`, `^b r` and a card's `↺` when kv `restorable` names no
/// run for it (S2b replaces the S1f `REOPEN_NOTICE`).
const NOTHING_TO_RESTORE: &str = "没有可恢复的 fleet";
/// Plan §18.2 / §18.7-4: one fleet per session — while a card is still `running` / `paused` / `stopping` a second
/// `↺` only says this. The check comes after the nothing-to-restore one, which §18.7-4 fixes the order of.
const RESTORE_WHILE_RUNNING: &str = "先关闭正在运行的 fleet 再恢复";
/// Title of the pane a restore spawns (plan §13.3 / §13.5): the `start --resume` output stays visible in it.
const RESUME_TITLE: &str = "resume";

/// The sidebar's restore attempt (A42-LINUX-RESTORE-FAILURE-04). The **pane id** is the identity: a title is a
/// label the protocol and the user can change, so it never decides whether a restore failed. Only an explicit
/// nonzero exit clears `正在恢复…`; exit 0 (the daemon is starting asynchronously) and an unknown status leave it
/// up, and nothing here retries on its own — the operator's next `^b r` / `↺` is the retry.
#[derive(Debug, Default, PartialEq, Eq)]
pub enum ResumeAttempt {
    /// No restore in this server generation.
    #[default]
    Idle,
    /// [`restore_run`] spawned this pane; only that pane's own exit can end the attempt.
    Running { pane: String },
    /// The attempt's pane exited with an explicit nonzero status: the reason stays visible until a retry.
    Failed { reason: String },
}

impl ResumeAttempt {
    /// The pane id this attempt spawned, if it is still being watched.
    pub fn pane(&self) -> Option<&str> {
        match self {
            ResumeAttempt::Running { pane } => Some(pane),
            _ => None,
        }
    }
}

/// S1e (plan §17.2 / §17.4-5): [`State::side`]'s `inset` is the biggest one any connected viewer reported — the
/// gui's traffic lights push the toolbar right on every canvas alike. When it changes the toolbar moves, and a
/// shown sidebar's minimum width follows it too, so the width is re-derived through [`sync_sidebar`].
///
/// A42-LINUX-SIDEBAR-ALIGN-03: the window's own header floor (`sidebar_min`) is aggregated the same way. A change
/// widens or narrows the effective sidebar band, which is what keeps the header boundary and the grid's divider on
/// one line when the user drags below what the controls need; the requested width in kv is untouched.
fn sync_inset(state: &mut State) {
    let inset = state.viewers.max_inset();
    let sidebar_min = state.viewers.max_sidebar_min();
    if state.side.inset != inset || state.side.sidebar_min != sidebar_min {
        state.side.inset = inset;
        state.side.sidebar_min = sidebar_min;
        sync_sidebar(state);
    }
}

/// Re-read kv `sidebar` into [`State::side`] and ask the layout for the width it needs (S1, plan §4.3): a value
/// [`sidebar::parse`] rejects is a `WARN` in `server.log` and no sidebar; a width change goes through
/// [`layout_changed`] (panes get their new PTY width), an unchanged width only marks every viewer dirty.
pub(crate) fn sync_sidebar(state: &mut State) {
    let text = state.kv.get("sidebar").cloned();
    state.side.data = match text {
        None => None,
        Some(text) => match sidebar::parse(&text) {
            Ok(mut data) => {
                follow_current_page(&mut data, &state.fleet_pages.current);
                Some(data)
            }
            Err(e) => {
                log("WARN", &format!("kv sidebar ignored: {e}"));
                None
            }
        },
    };
    // S1d (plan §16.2 / §16.3-4): the width the user dragged to lives in kv `sidebar_width`; a value that does
    // not parse or is out of range is ignored, and a drag in progress owns the width until it is released.
    if state.viewers.drag_hit() != Some(Hit::SidebarEdge)
        && let Some(w) = state
            .kv
            .get("sidebar_width")
            .and_then(|t| parse_sidebar_width(t))
    {
        state.side.width = w;
    }
    // S1f (plan §20.4-5): the topbar exists exactly while kv `sidebar` parses; its width is the toolbar's when the
    // sidebar is collapsed. Both are runtime layout state, so either one changing needs a full redraw. A26 rev5
    // (`CURRENT_REQUIREMENTS` rev 6 `utm_topbar`): a mirrored layout (Linux) keeps no topbar row in the grid — its
    // one header row is the native GTK headerbar — so only the unmirrored (macOS) layout reserves row 0.
    sync_bash(state);
    let w = sidebar::width(&state.side);
    let t = sidebar::topbar_cols(&state.side, state.layout.native_header());
    if state.layout.sidebar() != w || state.layout.topbar() != t {
        // A new topbar row moves the first card's row: the `layout_changed` below scrolls the selection into view
        // again with that geometry (`sync_bash` above followed it with the old one).
        if state.layout.topbar() != t {
            state.side.followed = None;
        }
        state.layout.set_sidebar(w);
        state.layout.set_topbar(t);
        layout_changed(state);
    } else {
        state.viewers.mark_dirty();
    }
}

/// The width a kv `sidebar_width` asks for (S1d, plan §16.2 / §16.3-4): `Some` only for a number inside
/// `SIDEBAR_MIN_W`..=`SIDEBAR_MAX_W`; anything else is ignored and the sidebar keeps its width.
fn parse_sidebar_width(text: &str) -> Option<u16> {
    text.trim()
        .parse::<u16>()
        .ok()
        .filter(|w| (fleetmux_layout::SIDEBAR_MIN_W..=fleetmux_layout::SIDEBAR_MAX_W).contains(w))
}

/// The pane id's sequence number (PROTOCOL.md §1.4: `<epoch>-<seq>`): bash cards follow creation order, which the
/// store's `BTreeMap` order breaks from the tenth pane on (`-10` sorts before `-2`).
fn pane_seq(pane: &str) -> u32 {
    pane.rsplit_once('-')
        .and_then(|(_, n)| n.parse().ok())
        .unwrap_or(0)
}

/// S1c (plan §14.3): mirror the `type == "shell"` panes into [`Side::bash`] (creation order) and the bash tab the root
/// page shows into [`Side::bash_sel`] (user 2026-11-09: each bash is a tab of the main fleet's worker area, so the
/// selected bash card is the root page's zoomed pane or focused tab, when that is a bash).
pub(crate) fn sync_bash(state: &mut State) {
    let mut bash: Vec<PaneId> = state
        .panes
        .list()
        .into_iter()
        .filter(|p| p.ty == sidebar::SHELL_TYPE)
        .map(|p| p.pane)
        .collect();
    bash.sort_by_key(|p| pane_seq(p));
    let layout = &state.layout;
    let sel = state
        .fleet_pages
        .current
        .is_empty()
        .then(|| layout.zoom().or(layout.selected(layout.focus())).cloned())
        .flatten()
        .filter(|p| bash.contains(p));
    // Plan §14.2: the box about a bash page closes with the page (its shell exited while the box was open).
    if let Some(pane) = state.side.close_bash.as_ref()
        && !bash.contains(pane)
    {
        state.side.close_bash = None;
        state.side.confirm = false;
        state.viewers.forget_drawn();
    }
    if state.side.bash != bash || state.side.bash_sel != sel {
        state.side.bash = bash;
        state.side.bash_sel = sel;
        state.viewers.mark_dirty();
    }
    // Like cmux: a new or moved selection (a new bash page, a pick, a step, the daemon's `current`) or a card that
    // came or went scrolls the selected card into view; anything else keeps the user's scroll, clamped.
    let content_rows = state.viewers.layout_size().1.saturating_sub(1);
    let top = state.layout.header_rows();
    if sidebar::follow_selection(&mut state.side, content_rows, top) {
        state.viewers.mark_dirty();
    }
}

/// Open a bash (plan §14.2 / §14.5-2): `$SHELL -l` (or `/bin/bash -l`) in `$HOME`, `type "shell"`, titled `bash-<n>`
/// for the next card, as the last tab of the root page's `workers` group (user 2026-11-09: "在fleetmux侧边栏双击打开的
/// 任意新的bash窗口，都在主fleet的worker区域有映射，都纳入主fleet的管理"; the root daemon registers it), shown at once
/// ([`show_bash`]) — also when a child page was current.
pub(crate) fn new_bash(state: &mut State) {
    let argv = vec![
        std::env::var("SHELL")
            .ok()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "/bin/bash".to_string()),
        "-l".to_string(),
    ];
    let req = Spawn {
        argv,
        cwd: std::env::var("HOME").ok().filter(|h| !h.is_empty()),
        env: BTreeMap::new(),
        title: format!("{}{}", sidebar::BASH_TITLE, state.side.bash.len() + 1),
        ty: sidebar::SHELL_TYPE.to_string(),
        group: Some(fleetmux_proto::Group::Workers),
        anchor: None,
        before: false,
    };
    match super::handlers::spawn(state, &req) {
        Ok(reply) => {
            if let Some(pane) = reply.get("pane").and_then(|p| p.as_str()) {
                show_bash(state, pane.to_string());
            }
        }
        Err(fail) => log(
            "WARN",
            &format!("sidebar bash: {} {}", fail.0.as_str(), fail.1),
        ),
    }
}

/// Show bash tab `pane` (user 2026-11-09): the root page becomes current, a zoom of another pane is cancelled (it would
/// hide the tab), and the tab is selected in its group, which gets the focus.
fn show_bash(state: &mut State, pane: PaneId) {
    if !state.fleet_pages.current.is_empty() {
        select_page(state, "");
    }
    if state.layout.zoom().is_some_and(|z| *z != pane) {
        unzoom(state);
    }
    let Some(group) = Group::ALL
        .into_iter()
        .find(|g| state.layout.group(*g).contains(&pane))
    else {
        return;
    };
    state.layout.select(pane);
    state.layout.focus_group(group);
    layout_changed(state);
}

/// The fleet card's view (plan §14.2; user 2026-11-09): while a bash card is selected the focus goes back to the
/// supervisor group (the bash stays a tab of the worker area), and a zoom is cancelled.
fn fleet_view(state: &mut State) {
    if state.side.bash_sel.is_some() {
        state.layout.focus_group(Group::Supervisor);
        layout_changed(state);
    }
    unzoom(state);
}

/// Close a bash page (plan §14.2): an idle shell goes right away; while a program runs in the foreground — the PTY's
/// foreground group is not the shell's own — the same confirmation box asks first, and anything but `y` keeps the
/// page. The kill itself is only requested through `kill` (see [`on_input`]'s caller), so it runs off the lock
/// through `handlers::kill` and the pane — with its card and its zoom — leaves once the process group is gone.
fn close_bash(state: &mut State, pane: PaneId, kill: &mut Option<PaneId>) {
    let shell = state.panes.info(&pane).map(|p| p.pgid);
    let foreground = state.panes.foreground_pgid(&pane);
    let busy = matches!((shell, foreground), (Some(pgid), Some(fg)) if fg != pgid);
    if !busy {
        *kill = Some(pane);
        return;
    }
    if state.side.confirm {
        return;
    }
    state.side.close_bash = Some(pane);
    state.side.confirm = true;
    state.viewers.forget_drawn();
}

/// Write the one-shot toolbar notice into [`State::side`] and redraw (plan §11.1); the next key press or mouse
/// press clears it ([`clear_notice`]).
fn notice(state: &mut State, text: &str) {
    state.side.notice = Some(text.to_string());
    state.viewers.mark_dirty();
}

/// Drop the one-shot toolbar notice if there is one (plan §11.1: the next key press or mouse press clears it).
fn clear_notice(state: &mut State) {
    if state.side.notice.take().is_some() {
        state.viewers.mark_dirty();
    }
}

/// Step the sidebar selection back (`forward == false`) or forward: the `←` / `→` buttons and `^b (` / `^b )` both
/// come here (plan §11.1 / §11.2 / §14.2). S1c walks the merged card list — the fleet cards, then the bash cards —
/// so one step past the fleet card shows the bash tab and one step back hands the focus to the supervisor. A step onto
/// another fleet's card would switch fleets, which stays S3's job: it only says so (see [`select_card`]).
pub(crate) fn side_step(state: &mut State, forward: bool) {
    let (has_prev, has_next) = sidebar::merged_neighbours(&state.side);
    if !(if forward { has_next } else { has_prev }) {
        return;
    }
    let Some(i) = sidebar::selected_card(&state.side) else {
        return;
    };
    let target = if forward { i + 1 } else { i.saturating_sub(1) };
    select_card(state, target);
}

/// Select card `j` of the merged list (plan §14.2): a bash card shows its tab ([`show_bash`]), the current fleet's
/// card shows the fleet's grid ([`fleet_view`]). A *different* fleet's card would switch fleets, which is S3's job, so
/// S1c keeps the S1b notice (plan §11.1: "切换 fleet：S3 开放") and leaves the view alone. The selection is not stored
/// — it is the root page's zoom / focused tab ([`sidebar::selected_card`] reads it back), so this only moves those and
/// lets [`layout_changed`] mirror them into [`State::side`].
///
/// M4 (design m4 R8): a fleet card that carries `page` selects that page (`page.select`, the web windows follow);
/// a `page` the server does not have only says [`NO_PAGE_NOTICE`]. A card without `page` keeps the S1c behaviour
/// above. The bash tabs live in the root page, so picking one while a child page is current goes back to the
/// root first.
fn select_card(state: &mut State, j: usize) {
    if let Some(pane) = sidebar::bash_at(&state.side, j).cloned() {
        show_bash(state, pane);
        return;
    }
    let card = state
        .side
        .data
        .as_ref()
        .and_then(|d| d.fleets.get(j).map(|e| (e.page.clone(), e.id != d.current)));
    match card {
        Some((Some(page), _)) if page != state.fleet_pages.current => {
            if !select_page(state, &page) {
                notice(state, NO_PAGE_NOTICE);
            } else if state.side.bash_sel.is_some() {
                // The root page came back with a bash tab focused: the fleet card was picked, not the bash card.
                fleet_view(state);
            }
        }
        Some((Some(_), _)) => fleet_view(state),
        Some((None, true)) => notice(state, SWITCH_NOTICE),
        _ => fleet_view(state),
    }
}

/// M4: select `page` from the view side (sidebar, jump event) and remember its `history_url` for the web windows.
fn select_page(state: &mut State, page: &str) -> bool {
    match super::handlers::page_select(state, page) {
        Ok(url) => {
            if url.is_some() {
                state.viewers.pending_goto = url;
            }
            true
        }
        Err(_) => false,
    }
}

/// M4 (design m4 R9): when any card carries `page`, the highlighted card — and so the topbar — is the one whose
/// `page` is the current page, whatever kv `current` names (the root daemon keeps writing its own run there). No
/// card for the current page, or no card with `page` at all: kv `current` stays.
pub(crate) fn follow_current_page(data: &mut sidebar::Data, current: &str) {
    if let Some(e) = data
        .fleets
        .iter()
        .find(|e| e.page.as_deref() == Some(current))
    {
        data.current = e.id.clone();
    }
}

/// M4 (design m4 R13): the page whose `history_url` has the path of the `to` parameter of a `?fleet-jump=` URL
/// (query and fragment ignored, percent-decoded). The root page is `""`.
pub(crate) fn jump_target(state: &State, url: &str) -> Option<String> {
    let query = url.split_once('?')?.1.split('#').next().unwrap_or("");
    let to = query
        .split('&')
        .find_map(|kv| kv.strip_prefix("to="))
        .map(percent_decode)?;
    let path = |u: &str| u.split(['?', '#']).next().unwrap_or(u).to_string();
    let want = path(&to);
    std::iter::once(String::new())
        .chain(state.fleet_pages.pages.keys().cloned())
        .find(|p| {
            super::handlers::page_kv(state, p, "history_url").is_some_and(|u| path(&u) == want)
        })
}

/// `application/x-www-form-urlencoded` value decoding (`URLSearchParams`, which `history.html` builds the jump
/// link with): `+` is a space, `%XX` a byte; a bad escape stays as it is.
fn percent_decode(s: &str) -> String {
    let hex = |c: Option<&u8>| c.and_then(|&c| (c as char).to_digit(16)).map(|d| d as u8);
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        if c == b'+' {
            out.push(b' ');
        } else if let (b'%', Some(h), Some(l)) = (c, hex(b.get(i + 1)), hex(b.get(i + 2))) {
            out.push((h << 4) | l);
            i += 3;
            continue;
        } else {
            out.push(c);
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Cancel the zoom, so the fleet's own panes are shown again (plan §14.2: the fleet card's view is the normal grid).
fn unzoom(state: &mut State) {
    let Some(zoomed) = state.layout.zoom().cloned() else {
        return;
    };
    state.layout.toggle_zoom(zoomed);
    layout_changed(state);
}

/// Whether the root fleet's card (the first one, the only one in S1b) still gets its `×` (plan §11.1): a running /
/// paused fleet with no `stop_request` in flight.
fn root_closeable(state: &State) -> bool {
    state
        .side
        .data
        .as_ref()
        .and_then(|d| d.fleets.first())
        .is_some_and(|e| sidebar::closeable(&state.side, e))
}

/// Open the close confirmation box (plan §2.5 / §11.3): the root card's `×` and `^b X` both come here, and it only
/// opens while that card can still be closed. Opening drops every viewer's pane cache so the box leaves no ghost
/// when it closes (plan §3.5).
pub(crate) fn open_confirm(state: &mut State) {
    if state.side.confirm || !root_closeable(state) {
        return;
    }
    state.side.close_bash = None;
    state.side.confirm = true;
    state.viewers.forget_drawn();
}

/// Close the close confirmation box without writing anything (plan §2.5: any other key, a prefix key, a paste or a
/// press outside `[ 关闭 fleet ]`).
pub(crate) fn close_confirm(state: &mut State) {
    if !state.side.confirm {
        return;
    }
    state.side.confirm = false;
    state.side.close_bash = None;
    state.viewers.forget_drawn();
}

/// `y` in the close box (plan §2.5 / §11.3): write kv `stop_request` for this fleet's daemon, remember that the
/// request is out (the status line says so and the card's `×` goes away) and close the box.
fn confirm_stop(state: &mut State, kill: &mut Option<PaneId>) {
    // Plan §14.2: a bash page's box closes that page — the kill runs off the lock through `handlers::kill` (see
    // [`on_input`]'s caller), so the shell leaves and its card goes with it.
    if let Some(pane) = state.side.close_bash.clone() {
        close_confirm(state);
        *kill = Some(pane);
        return;
    }
    let at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    state.kv.insert(
        "stop_request".to_string(),
        format!(r#"{{"v":1,"state":"requested","at":{at}}}"#),
    );
    state.side.stop = sidebar::Stop::Requested;
    close_confirm(state);
}

/// kv `stop_request` as [`sidebar::Stop`] (plan §11.3): `requested` / `accepted` / `refused` + `reason`; anything
/// else (no key, not JSON, an unknown state) is no request at all.
fn parse_stop(text: &str) -> sidebar::Stop {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(text) else {
        return sidebar::Stop::None;
    };
    match value.get("state").and_then(|s| s.as_str()) {
        Some("requested") => sidebar::Stop::Requested,
        Some("accepted") => sidebar::Stop::Accepted,
        Some("refused") => sidebar::Stop::Refused(
            value
                .get("reason")
                .and_then(|r| r.as_str())
                .unwrap_or_default()
                .to_string(),
        ),
        _ => sidebar::Stop::None,
    }
}

/// Re-read kv `stop_request` into [`State::side`] (plan §11.3): the daemon answers a close request with `accepted`
/// or `refused` + `reason`, and the status line follows either way.
pub(crate) fn sync_stop(state: &mut State) {
    let stop = state
        .kv
        .get("stop_request")
        .map(|v| parse_stop(v))
        .unwrap_or(sidebar::Stop::None);
    if state.side.stop != stop {
        state.side.stop = stop;
        state.viewers.mark_dirty();
    }
}

/// Re-read kv `restorable` into [`State::side`] (plan §13.6-2 / §18.3): the closed fleets the topbar's `↺`, `^b r`
/// and the cards' `↺` bring back. A missing or empty value drops the list **and** `resuming` (the daemon's cards are
/// back); a value [`sidebar::parse_restorable`] rejects is a `WARN` in `server.log` and no list.
pub(crate) fn sync_restorable(state: &mut State) {
    let text = state.kv.get("restorable").cloned().unwrap_or_default();
    let restorable = if text.is_empty() {
        None
    } else {
        match sidebar::parse_restorable(&text) {
            Ok(r) => Some(r),
            Err(e) => {
                log("WARN", &format!("kv restorable ignored: {e}"));
                None
            }
        }
    };
    let resuming = state.side.resuming && restorable.is_some();
    if state.side.restorable != restorable || state.side.resuming != resuming {
        state.side.restorable = restorable;
        state.side.resuming = resuming;
        state.viewers.mark_dirty();
    }
    // A42-LINUX-RESTORE-FAILURE-04: the daemon's cards are back (kv `restorable` cleared), so the restore is over
    // and no attempt stays watched — a later, unrelated pane must never be matched to it.
    if state.side.restorable.is_none() && state.resume != ResumeAttempt::Idle {
        state.resume = ResumeAttempt::Idle;
        state.viewers.mark_dirty();
    }
}

/// The topbar's `↺` and `^b r` (plan §13.6-2): restore `runs[0]`, the newest closed fleet; with no run registered
/// they only say so (one-shot notice, cleared by the next key or mouse press).
pub(crate) fn reopen(state: &mut State) {
    let run = state
        .side
        .restorable
        .as_ref()
        .and_then(|r| r.runs.first().cloned());
    match run {
        Some(run) => restore_run(state, &run),
        None => notice(state, NOTHING_TO_RESTORE),
    }
}

/// A closed card's `↺` (plan §13.6-2): restore the run kv `restorable` registers under that card's `id` — never by
/// the card's position. No such run → the same notice as [`reopen`].
pub(crate) fn restore(state: &mut State, i: usize) {
    let id = state
        .side
        .data
        .as_ref()
        .and_then(|d| d.fleets.get(i))
        .map(|e| e.id.clone());
    let run = id.and_then(|id| {
        state
            .side
            .restorable
            .as_ref()
            .and_then(|r| r.runs.iter().find(|x| x.id == id).cloned())
    });
    match run {
        Some(run) => restore_run(state, &run),
        None => notice(state, NOTHING_TO_RESTORE),
    }
}

/// Spawn a registered `start --resume` argv as a `resume` tab of the `lower` group **in this server**, select and
/// zoom it, and say `正在恢复…` until kv `restorable` is cleared (plan §13.3 / §13.5). While a card is still up a
/// restore only says so (plan §18.2 / §18.7-4: one fleet per session, so a second `↺` must not act); while a restore
/// is running (or its pane still exists) another click spawns nothing: one restore at a time.
fn restore_run(state: &mut State, run: &sidebar::RestoreRun) {
    if state.side.data.as_ref().is_some_and(sidebar::has_live_card) {
        notice(state, RESTORE_WHILE_RUNNING);
        return;
    }
    if state.side.resuming || state.panes.list().iter().any(|p| p.title == RESUME_TITLE) {
        return;
    }
    let req = Spawn {
        argv: run.argv.clone(),
        cwd: Some(run.cwd.clone()),
        env: run.env.clone(),
        title: RESUME_TITLE.to_string(),
        ty: "tab".to_string(),
        group: Some(fleetmux_proto::Group::Lower),
        anchor: None,
        before: false,
    };
    match super::handlers::spawn(state, &req) {
        Ok(reply) => {
            let pane = reply.get("pane").and_then(|p| p.as_str()).map(str::to_string);
            if let Some(pane) = &pane {
                state.layout.select(pane.clone());
                state.layout.toggle_zoom(pane.clone());
                layout_changed(state);
            }
            // A42-LINUX-RESTORE-FAILURE-04: the pane id is the attempt's identity (never the title), and a new
            // attempt replaces the reason an earlier one left on the status line.
            state.resume = match pane {
                Some(pane) => ResumeAttempt::Running { pane },
                None => ResumeAttempt::Idle,
            };
            state.side.resuming = true;
            state.viewers.mark_dirty();
        }
        Err(fail) => log(
            "WARN",
            &format!("sidebar restore: {} {}", fail.0.as_str(), fail.1),
        ),
    }
}

/// A pane exited (B6 `reap_and_settle`, A42-LINUX-RESTORE-FAILURE-04). Only the pane this server spawned for the
/// restore decides: an explicit nonzero exit clears `正在恢复…` and keeps an actionable one-line reason on the
/// status line, with the pane's last output in `server.log` (so the original output stays traceable); exit 0 keeps
/// the status up while the daemon starts asynchronously; an unknown status changes nothing. The daemon's kv
/// `restorable` is never touched here and no retry is started.
pub(crate) fn resume_exited(state: &mut State, exited: &super::pane::Exited) {
    if state.resume.pane() != Some(exited.pane.as_str()) {
        return; // a different pane, or an older attempt: never clear a newer attempt's state
    }
    match &exited.status {
        Some(status) if status.success() => {
            // The daemon was launched; kv `restorable` decides when `正在恢复…` goes away.
            state.resume = ResumeAttempt::Idle;
            state.viewers.mark_dirty();
        }
        Some(status) => {
            let reason = exit_reason(status, &exited.tail, exited.pty_open);
            log(
                "WARN",
                &format!(
                    "sidebar restore failed: pane {} {}; its output:\n{}",
                    exited.pane,
                    exit_word(status),
                    exited.tail.trim_end()
                ),
            );
            state.side.resuming = false;
            state.resume = ResumeAttempt::Failed { reason };
            state.viewers.mark_dirty();
        }
        None => log(
            "INFO",
            &format!(
                "sidebar restore pane {} exited with an unknown status; keeping the restore state",
                exited.pane
            ),
        ),
    }
}

/// The operator killed the restore pane itself (A42-LINUX-RESTORE-FAILURE-04): that is a cancel, not a failure —
/// there is no exit code to judge — so the attempt is dropped and the status line says so instead of staying on a
/// `正在恢复…` that can never end. The daemon's kv `restorable` is untouched, so `^b r` / `↺` still works.
pub(crate) fn restore_cancelled(state: &mut State) {
    state.resume = ResumeAttempt::Idle;
    state.side.resuming = false;
    notice(state, "恢复已取消");
    state.viewers.mark_dirty();
}

/// How a child ended, for the log and the status line (`exit code 1`, `signal KILL`).
fn exit_word(status: &portable_pty::ExitStatus) -> String {
    match status.signal() {
        Some(signal) => format!("signal {signal}"),
        None => format!("exit code {}", status.exit_code()),
    }
}

/// The status line's one-line summary of a failed restore: the last non-empty line of the pane's output (what the
/// operator can act on), else the exit code. Whitespace is collapsed and the text is bounded.
fn exit_reason(status: &portable_pty::ExitStatus, tail: &str, pty_open: bool) -> String {
    let line = tail
        .lines()
        .rev()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .unwrap_or("");
    let text = if line.is_empty() {
        // `pty_open`: a background child still holds the PTY, so "no output" would be a claim we cannot make.
        if pty_open {
            format!(
                "恢复失败：恢复进程退出（{}），没有已捕获输出（PTY 未关闭）",
                exit_word(status)
            )
        } else {
            format!("恢复失败：恢复进程退出（{}），没有输出", exit_word(status))
        }
    } else {
        format!("恢复失败：{line}")
    };
    let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if text.chars().count() > 160 {
        let cut: String = text.chars().take(157).collect();
        format!("{cut}…")
    } else {
        text
    }
}

/// Write viewer input to the pane that has focus (the zoomed pane while zoomed).
fn write_focused(state: &mut State, bytes: &[u8], mode: WriteMode) {
    let target = match state.layout.zoom() {
        Some(z) => Some(z.clone()),
        None => state.layout.selected(state.layout.focus()).cloned(),
    };
    if let Some(id) = target {
        write_pane(state, &id, bytes, mode);
    }
}

/// Write viewer input (keys, paste, a forwarded mouse report) to pane `id`. A pane whose view is scrolled
/// back (wheel with inner mouse off) first returns to the live screen (PROTOCOL.md §11).
fn write_pane(state: &mut State, id: &str, bytes: &[u8], mode: WriteMode) {
    if bytes.is_empty() {
        return;
    }
    if let Some(t) = state.panes.term_mut(id)
        && t.display_offset() > 0
    {
        t.set_display_offset(0);
        state.viewers.mark_dirty();
    }
    if let Err(e) = state.panes.write(id, bytes, mode) {
        log(
            "WARN",
            &format!("viewer input to pane {id} not written: {e:?}"),
        );
    }
}

/// Page mode (PROTOCOL.md §13) takes ordinary input one key at a time (`scroll::on_key`, B15); nothing typed
/// in the mode reaches the pane. Leaves page mode when its pane is gone or the key says `Exit`.
fn page_input(state: &mut State, bytes: &[u8]) {
    for key in split_keys(bytes) {
        let Some(mut mode) = state.page.take() else {
            return;
        };
        let Some(term) = state.panes.term_mut(&mode.pane) else {
            state.viewers.mark_dirty(); // pane gone: out of page mode
            return;
        };
        match scroll::on_key(&mut mode, term, key) {
            PageAction::Exit => {
                state.viewers.mark_dirty();
                continue; // `state.page` stays `None`
            }
            PageAction::Redraw => state.viewers.mark_dirty(),
            PageAction::Stay => {}
        }
        state.page = Some(mode);
    }
}

/// One SGR mouse report from viewer `id` (PROTOCOL.md §11, §12; decisions in `mouse::decide`).
fn mouse_input(state: &mut State, id: ViewerId, report: &[u8], kill: &mut Option<PaneId>) {
    let Some((ev, _)) = mouse::parse_sgr(report) else {
        return;
    };
    // S1b (plan §11.1): a mouse press clears the one-shot toolbar notice (a release or a motion does not), and
    // this viewer's selection highlight (PROTOCOL.md §11).
    if matches!(ev.kind, fleetmux_term::MouseKind::Press(_)) {
        clear_notice(state);
        select(state, id, None);
    }
    let size = state.viewers.layout_size();
    // S1e (plan §17.2 / §17.4-5): while the sidebar is hidden its right-edge columns belong to the collapsed
    // toolbar (or are blank below it), not to a resize edge — an edge press is an ordinary sidebar click and
    // starts no drag.
    let hidden = state.side.hidden;
    let (action, before, after) = {
        let State {
            viewers,
            layout,
            panes,
            side,
            ..
        } = &mut *state;
        let Some(slot) = viewers.viewers.get_mut(&id) else {
            return;
        };
        // The native layout reserves row 0. Terminal-only viewers use that same row for tools,
        // without changing shared pane rectangles, PTYs, selection or other viewers.
        let mut terminal_layout;
        let layout = if layout.native_header() && !slot.viewer.native_header && side.data.is_some() {
            terminal_layout = layout.clone();
            terminal_layout.set_header(0);
            terminal_layout.set_topbar(sidebar::topbar_w(side));
            &terminal_layout
        } else {
            &*layout
        };
        let before = slot.drag;
        let modes = |p: &str| panes.term(p).map(|t| t.modes());
        let labels = |p: &PaneId| draw::label_cols(panes, p);
        let mut action = mouse::decide_titled(
            layout,
            size,
            &modes,
            &labels,
            &ev,
            &mut slot.drag,
            &mut slot.clicks,
            Instant::now(),
        );
        if hidden && slot.drag == Some(Hit::SidebarEdge) {
            slot.drag = None;
            action = Action::Sidebar((ev.col, ev.row));
        }
        (action, before, slot.drag)
    };
    if before != after {
        let hub = &mut state.viewers;
        match after {
            Some(h) => hub.active_drag = Some((id, h)),
            None if hub.active_drag.is_some_and(|(v, _)| v == id) => hub.active_drag = None,
            None => {}
        }
        hub.mark_dirty();
    }
    // A left press in a pane also gives that group the focus (the action itself forwards or selects).
    if let (fleetmux_term::MouseKind::Press(fleetmux_term::MouseButton::Left), Some(Hit::Pane(g))) =
        (ev.kind, after)
        && state.layout.focus() != g
    {
        state.layout.focus_group(g);
        layout_changed(state);
    }
    // The pane the event acts on: the held pane, else the one under the pointer.
    let target = || -> Option<PaneId> {
        let g = match before.or(after) {
            Some(Hit::Pane(g)) => g,
            _ => match state
                .layout
                .hit_test_titled(size.0, size.1, ev.col, ev.row, |p| {
                    draw::label_cols(&state.panes, p)
                }) {
                Hit::Pane(g) => g,
                _ => return None,
            },
        };
        mouse::pane_at(&state.layout, size, g).map(|(p, _)| p)
    };
    match action {
        Action::SelectTab(p) => {
            if let Some(g) = Group::ALL
                .into_iter()
                .find(|g| state.layout.group(*g).contains(&p))
            {
                state.layout.select(p);
                state.layout.focus_group(g);
                layout_changed(state);
            }
        }
        Action::ToggleZoom(p) => {
            state.layout.toggle_zoom(p);
            layout_changed(state);
        }
        Action::FocusGroup(g) => {
            state.layout.focus_group(g);
            layout_changed(state);
        }
        Action::DragH(h) | Action::DragV(h) => {
            let (cur_h, cur_v) = state.layout.ratios();
            let (nh, nv) = match action {
                Action::DragH(_) => (h, cur_v),
                _ => (cur_h, h),
            };
            if (nh, nv) != (cur_h, cur_v) {
                state.layout.set_ratios(nh, nv);
                layout_changed(state);
            }
            if after.is_none() {
                // The button was released: persist the ratios (PROTOCOL.md §11; `views::save_layout`, B16).
                // M4 (design m4 R11): the file holds the root page, with the tree's shared ratios.
                super::handlers::spread_shared(state);
                let path = state.runtime.join("layout.json");
                if let Err(e) = super::views::save_layout(&path, super::handlers::root_layout(state)) {
                    log("WARN", &format!("layout.json not written: {e}"));
                }
            }
        }
        Action::DragSidebar(c) if !state.side.hidden => {
            // S1d (plan §16.3-4): the new width is the dragged column clamped to `SIDEBAR_MIN_W`..=`sidebar_max`;
            // a window whose maximum is below the minimum cannot drag. Releasing saves the width to kv.
            // A26 rev3: the mirrored sidebar's drag line is on the right, so its width is measured from there.
            let (lc, _) = state.viewers.layout_size();
            let c = if state.layout.sidebar_on_right() {
                lc.saturating_sub(c)
            } else {
                c
            };
            let max = fleetmux_layout::sidebar_max(lc);
            if max >= fleetmux_layout::SIDEBAR_MIN_W {
                let w = c.clamp(fleetmux_layout::SIDEBAR_MIN_W, max);
                if after.is_none() {
                    // The button was released: persist the width first, so the `sync_sidebar` below reads it back.
                    state.kv.insert("sidebar_width".to_string(), w.to_string());
                }
                if w != state.side.width {
                    state.side.width = w;
                    sync_sidebar(state);
                }
            }
        }
        Action::Forward(bytes) => {
            if let Some(p) = target() {
                write_pane(state, &p, &bytes, WriteMode::Raw);
            }
        }
        Action::ScrollView(n) => {
            if let Some(p) = target()
                && let Some(t) = state.panes.term_mut(&p)
            {
                let cur = t.display_offset() as i64;
                let new = (cur + i64::from(n)).clamp(0, t.scrollback_len() as i64) as usize;
                if new as i64 != cur {
                    t.set_display_offset(new);
                    state.viewers.mark_dirty();
                }
            }
        }
        Action::BeginSelect((col, row)) => {
            let from = target().map(|p| (p, (row, col)));
            if let Some(slot) = state.viewers.viewers.get_mut(&id) {
                slot.select_from = from;
            }
        }
        Action::ExtendSelect((col, row)) => {
            let from = state
                .viewers
                .viewers
                .get(&id)
                .and_then(|s| s.select_from.clone());
            if let Some((p, from)) = from
                && let Some(t) = state.panes.term(&p)
            {
                let offset = t.display_offset();
                let sel = (from != (row, col)).then_some(Selection {
                    pane: p,
                    from,
                    to: (row, col),
                    offset,
                });
                select(state, id, sel);
            }
        }
        Action::EndSelect((col, row)) => {
            let from = state
                .viewers
                .viewers
                .get_mut(&id)
                .and_then(|s| s.select_from.take());
            // A click (press and release on one cell) is not a selection.
            if let Some((p, from)) = from
                && from != (row, col)
                && let Some(t) = state.panes.term(&p)
            {
                let text = t.selection_text(from, (row, col));
                let offset = t.display_offset();
                if !text.is_empty() {
                    copy(state, text);
                }
                // The highlight stays after the copy (cmux / Ghostty `selection-clear-on-copy = false`).
                let sel = Selection {
                    pane: p,
                    from,
                    to: (row, col),
                    offset,
                };
                select(state, id, Some(sel));
            }
        }
        Action::SelectWord((col, row)) | Action::SelectLine((col, row)) => {
            if let Some(slot) = state.viewers.viewers.get_mut(&id) {
                slot.select_from = None; // motion after a double / triple press extends nothing
            }
            let picked = target().and_then(|p| {
                let t = state.panes.term(&p)?;
                let (from, to) = match action {
                    Action::SelectWord(_) => t.word_at((row, col))?,
                    _ => t.line_at(row),
                };
                let text = t.selection_text(from, to);
                let sel = Selection {
                    pane: p,
                    from,
                    to,
                    offset: t.display_offset(),
                };
                (!text.is_empty()).then_some((text, sel))
            });
            if let Some((text, sel)) = picked {
                copy(state, text);
                select(state, id, Some(sel));
            }
        }
        Action::Sidebar((x, y)) => {
            let (lc, lr) = state.viewers.layout_size();
            let sb = state.layout.sidebar_cols(lc);
            // A26 rev3: the mirrored (Linux) sidebar is the right band, so the hit test runs in its own
            // `0..sb` columns; row 0's toolbar stays on the left and needs no shift.
            let x = if state.layout.sidebar_on_right() && y > 0 {
                x.saturating_sub(lc.saturating_sub(sb))
            } else {
                x
            };
            let top = if y == 0 && state.layout.native_header()
                && state.viewers.viewers.get(&id).is_some_and(|s| !s.viewer.native_header)
            {
                fleetmux_layout::TOPBAR_ROWS
            } else {
                state.layout.header_rows()
            };
            match sidebar::hit(&state.side, sb, lr.saturating_sub(1), top, x, y) {
                sidebar::SideHit::Hide | sidebar::SideHit::Show => {
                    state.side.hidden = !state.side.hidden;
                    sync_sidebar(state);
                }
                // S1c (plan §14.2 / §14.5-1): the toolbar's `+` opens a bash page (it was a notice until S1b).
                sidebar::SideHit::NewBash => new_bash(state),
                // S2b (plan §13.6-2): the topbar's `↺` and `^b r` restore the newest closed fleet.
                sidebar::SideHit::Reopen => reopen(state),
                // S2b (plan §13.6-2): a closed card's `↺` restores the run registered under its `id`.
                sidebar::SideHit::Restore(i) => restore(state, i),
                sidebar::SideHit::Prev => side_step(state, false),
                sidebar::SideHit::Next => side_step(state, true),
                // S1c (plan §14.2 / §14.5-3): `j ≥ fleets.len()` is a bash card — its `×` closes that page (asking
                // first while a program runs in it). The root fleet's `×` keeps the S2 close box; a child fleet's
                // still waits for S3.
                sidebar::SideHit::Close(j) => match sidebar::bash_at(&state.side, j).cloned() {
                    Some(pane) => close_bash(state, pane, kill),
                    None if j == 0 => open_confirm(state),
                    None => notice(state, CHILD_CLOSE_NOTICE),
                },
                // S1c (plan §14.2 / §14.5-2): two left presses within `mouse::DOUBLE_CLICK` on the sidebar's empty
                // card area open a bash page. Only the blank area counts: a card, the topbar and the status line are
                // not blank (`blank_at` asks `hit` too).
                sidebar::SideHit::Nothing => {
                    let content_rows = lr.saturating_sub(1);
                    if sidebar::blank_at(&state.side, sb, content_rows, top, x, y) {
                        let double =
                            state.viewers.viewers.get_mut(&id).is_some_and(|slot| {
                                slot.clicks.blank_press((x, y), Instant::now())
                            });
                        if double {
                            new_bash(state);
                        }
                    }
                }
                // S1c (plan §14.2): a card's body selects it — a bash card zooms its page, a fleet card cancels the
                // zoom. The selection is the zoom; nothing else is stored.
                sidebar::SideHit::Pick(j) => select_card(state, j),
            }
        }
        // Like cmux: the wheel over the sidebar's card rows scrolls the card list one card per step, stopping at
        // either end; the topbar row and the status line do not scroll it. The mirrored (Linux) band is the right one.
        Action::ScrollSidebar((x, y), step) => {
            let (lc, lr) = state.viewers.layout_size();
            let sb = state.layout.sidebar_cols(lc);
            let top = state.layout.header_rows();
            let content_rows = lr.saturating_sub(1);
            let in_band = if state.layout.sidebar_on_right() {
                x >= lc.saturating_sub(sb) && x < lc
            } else {
                x < sb
            };
            if sb > 0
                && in_band
                && (top..content_rows).contains(&y)
                && sidebar::scroll_cards(&mut state.side, step, content_rows, top)
            {
                state.viewers.mark_dirty();
            }
        }
        Action::PassThrough => {}
        // S1e (plan §17.2): a drag that started while the sidebar was shown, held across hiding it — the hidden
        // sidebar has no edge to drag.
        Action::DragSidebar(_) => {}
    }
}

/// One input token while the close confirmation box is open (plan §2.5 / §11.1): a lone `y` / `Y` confirms, every
/// other key, paste or prefix key cancels, and a mouse **press** confirms on `[ 关闭 fleet ]` and cancels anywhere
/// else (a release or a motion of the click that opened the box does nothing). Nothing is forwarded to a pane.
fn confirm_input(state: &mut State, tok: &Token, kill: &mut Option<PaneId>) {
    match tok {
        Token::Plain(b) if b.len() == 1 && (b[0] == b'y' || b[0] == b'Y') => {
            confirm_stop(state, kill)
        }
        Token::Mouse(b) => {
            let Some((ev, _)) = mouse::parse_sgr(b) else {
                return;
            };
            if !matches!(ev.kind, fleetmux_term::MouseKind::Press(_)) {
                return;
            }
            let (lc, lr) = state.viewers.layout_size();
            match sidebar::modal_hit(&state.side, lc, lr, ev.col, ev.row) {
                sidebar::ModalHit::Confirm => confirm_stop(state, kill),
                sidebar::ModalHit::Cancel => close_confirm(state),
            }
        }
        _ => close_confirm(state),
    }
}

/// Set viewer `id`'s selection highlight (PROTOCOL.md §11). A change drops that viewer's pane cache, so every pane
/// is read from its parser again and the highlight is drawn or removed on the next frame.
fn select(state: &mut State, id: ViewerId, sel: Option<Selection>) {
    if let Some(slot) = state.viewers.viewers.get_mut(&id)
        && slot.viewer.selection != sel
    {
        slot.viewer.selection = sel;
        slot.drawn.clear();
        state.viewers.mark_dirty();
    }
}

/// PROTOCOL.md §12: the selection goes to every viewer as OSC 52 and to this host's clipboard tool (B15's
/// `clipboard::{osc52, local_copy}`). The tool runs on its own thread, never under the `State` lock.
fn copy(state: &mut State, text: String) {
    state
        .viewers
        .broadcast(Frame::Screen(clipboard::osc52(&text)));
    std::thread::spawn(move || match clipboard::local_copy(&text) {
        Ok(_) => {}
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            log(
                "INFO",
                "no clipboard tool on this host; copied to viewers only",
            );
        }
        Err(e) => log("WARN", &format!("clipboard tool failed: {e}")),
    });
}

/// Handle one `0x01` input frame from viewer `id`: intercept the prefix key (PROTOCOL.md §10), SGR mouse
/// reports (§11) and the outer terminal's bracketed-paste packets (re-wrapped per the inner `?2004`); write
/// everything else unchanged to the focused pane. Returns frames to send back to that viewer.
///
/// B7: prefix + `d` has no effect here (the client recognises it and sends `0x03`, ruling D3); the doubled
/// prefix writes the prefix byte; other prefix keys go to `views::on_prefix_key` (M0 subset). B14: SGR mouse
/// reports go to `mouse::decide` and their action is applied here; in page mode ordinary keys go to
/// `scroll::on_key` (B15) one at a time and pastes are dropped.
///
/// Returns the frames for that viewer and, when this input closed a bash page (plan §14.2), that pane's id: the kill
/// is a `handlers::kill` escalation, which must run off the `State` lock, so the caller spawns it.
pub fn on_input(state: &mut State, id: ViewerId, bytes: &[u8]) -> (Vec<Frame>, Option<PaneId>) {
    let now = Instant::now();
    let prefix = state.prefix.first().copied();
    let tokens = {
        let Some(slot) = state.viewers.viewers.get_mut(&id) else {
            return (Vec::new(), None);
        };
        if slot.scanner_prefix != prefix {
            slot.scanner = InputScanner::new(prefix);
            slot.scanner_prefix = prefix;
        }
        slot.scanner.scan(bytes, now)
    };
    let before = state.viewers.layout_size();
    state.viewers.note_input(id);
    if state.viewers.layout_size() != before {
        apply_pty_sizes(state);
    }
    let mut out = Vec::new();
    let mut kill: Option<PaneId> = None;
    for (tok, _) in tokens {
        // S1b (plan §2.5 / §11.3): while the close box is open every token belongs to it — a lone `y` / `Y` confirms,
        // everything else cancels — and nothing reaches a pane or the page viewer, so no input leaks past it.
        if state.side.confirm {
            confirm_input(state, &tok, &mut kill);
            continue;
        }
        // S1b (plan §11.1): the one-shot toolbar notice lasts until the next key press, paste or prefix key; so
        // does this viewer's selection highlight (Ghostty `selection-clear-on-typing`).
        if matches!(
            &tok,
            Token::Plain(_) | Token::Paste(_) | Token::PrefixKey(_)
        ) {
            clear_notice(state);
            select(state, id, None);
        }
        match tok {
            Token::Plain(b) if state.page.is_some() => page_input(state, &b),
            Token::Plain(b) => write_focused(state, &b, WriteMode::Raw),
            Token::Paste(_) | Token::PasteEnd if state.page.is_some() => {
                // Page mode: nothing typed reaches the pane (PROTOCOL.md §13).
                if let Some(slot) = state.viewers.viewers.get_mut(&id) {
                    slot.paste.clear();
                }
            }
            Token::Paste(b) => {
                let full = match state.viewers.viewers.get_mut(&id) {
                    Some(slot) => {
                        slot.paste.extend_from_slice(&b);
                        (slot.paste.len() >= PASTE_MAX).then(|| std::mem::take(&mut slot.paste))
                    }
                    None => None,
                };
                if let Some(p) = full {
                    write_focused(state, &p, WriteMode::Paste);
                }
            }
            Token::PasteEnd => {
                let p = state
                    .viewers
                    .viewers
                    .get_mut(&id)
                    .map(|s| std::mem::take(&mut s.paste));
                if let Some(p) = p {
                    write_focused(state, &p, WriteMode::Paste);
                }
            }
            Token::Mouse(b) => mouse_input(state, id, &b, &mut kill),
            Token::PrefixKey(k) => {
                if prefix.is_some_and(|p| k == [p]) {
                    write_focused(state, &k, WriteMode::Raw);
                } else if k != b"d" {
                    out.extend(super::views::on_prefix_key(state, id, &k));
                }
            }
        }
    }
    (out, kill)
}

/// M2.6: per-connection options read from the handshake beside [`view::Hello`] (which gets no new field).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ViewOpts {
    pub native_header: bool,
    /// The handshake said `"overlay":1` (`view::wants_overlay`): this connection gets `0x85` (PROTOCOL.md §6.3).
    pub overlay: bool,
    /// gui-scrollbar: the handshake said `"scroll":1` (`view::wants_scroll`): this connection gets `0x86`
    /// (PROTOCOL.md §6.3).
    pub scroll: bool,
    /// S1e: the handshake's `inset` key (`view::wants_inset`, plan §17.3): the columns this viewer's row 0 leaves
    /// to the gui's traffic lights, `0` when it did not ask. A later `0x06` line updates it.
    pub inset: u16,
}

/// Serve one view connection after its handshake: `reader` is the buffered reader `socket.rs` read the
/// handshake line from (it may already hold the first frames), `writer` the write half; the `ok` line has been
/// written. Registers the viewer, runs its writer task, reads `0x01` / `0x02` / `0x03` until the client leaves
/// or the connection is shut, then unregisters it. Same as [`serve_view_opts`] with [`ViewOpts::default`].
pub async fn serve_view<R, W>(
    state: Arc<Mutex<State>>,
    hello: view::Hello,
    reader: R,
    writer: W,
) -> io::Result<()>
where
    R: AsyncRead + Unpin + Send,
    W: AsyncWrite + Unpin + Send + 'static,
{
    serve_view_opts(state, hello, ViewOpts::default(), reader, writer).await
}

/// M2.6: [`serve_view`] with the handshake's options. W3 (attach-1) keeps `opts.overlay` in the connection's
/// `Slot` (never in `Viewer`) and, for such a connection, sends `0x85` after each committed screen update when the
/// overlay changed (PROTOCOL.md §6.3).
pub async fn serve_view_opts<R, W>(
    state: Arc<Mutex<State>>,
    hello: view::Hello,
    opts: ViewOpts,
    mut reader: R,
    writer: W,
) -> io::Result<()>
where
    R: AsyncRead + Unpin + Send,
    W: AsyncWrite + Unpin + Send + 'static,
{
    let (tx, rx) = mpsc::channel(VIEW_QUEUE);
    let (id, shared, dirty) = {
        let mut st = state.lock().await;
        let id = st.viewers.add(&hello, tx);
        // M2.6: kept in the connection's `Slot` (not `Viewer`), set before the lock is released, so the first
        // render already knows.
        if let Some(slot) = st.viewers.viewers.get_mut(&id) {
            slot.overlay = opts.overlay;
            slot.viewer.native_header = opts.native_header;
            slot.scroll = opts.scroll;
            slot.inset = opts.inset;
        }
        // S1e (plan §17.2): a new viewer's inset may be the biggest one, and the toolbar's room follows it.
        sync_inset(&mut st);
        apply_pty_sizes(&mut st);
        let shared = st.viewers.viewers[&id].shared.clone();
        (id, shared, st.viewers.dirty_notify())
    };
    let writer_task = tokio::spawn(write_loop(id, rx, writer, shared.clone(), dirty));

    let mut buf: Vec<u8> = Vec::with_capacity(64 * 1024);
    let result = 'conn: loop {
        loop {
            match view::decode_frame(&buf) {
                Ok(Some((frame, used))) => {
                    buf.drain(..used);
                    if !handle_frame(&state, id, frame).await {
                        break 'conn Ok(());
                    }
                }
                Ok(None) => break,
                Err(e) => {
                    log("WARN", &format!("viewer {id}: bad frame, closing: {e:?}"));
                    break 'conn Ok(());
                }
            }
        }
        let read = tokio::select! {
            r = reader.read_buf(&mut buf) => r,
            _ = shared.wake_reader.notified() => break Ok(()),
        };
        match read {
            Ok(0) => break Ok(()),
            Ok(_) => {}
            Err(e) => break Err(e),
        }
    };
    {
        let mut st = state.lock().await;
        st.viewers.remove(id);
        super::web::viewer_gone(&mut st, id);
        // S1e (plan §17.2): the biggest inset may have left with this viewer.
        sync_inset(&mut st);
        apply_pty_sizes(&mut st);
    }
    shared.shut();
    let _ = writer_task.await;
    result
}

/// M4 (design m4 R13, PROTOCOL.md §6.3): a `0x04 {"event":"jump","url"}` from a window that intercepted a
/// `?fleet-jump=` link: select the page whose `history_url` has the path of the link's `to`. No such page → an
/// INFO line and nothing else (the window did not load the marked URL, so it still shows the page it had).
/// `false` = not a jump event (the line goes on to `web::on_client_line`).
fn on_jump(state: &mut State, id: ViewerId, line: &str) -> bool {
    let Ok(serde_json::Value::Object(o)) = serde_json::from_str::<serde_json::Value>(line) else {
        return false;
    };
    if o.get("event").and_then(|v| v.as_str()) != Some("jump") {
        return false;
    }
    let url = o.get("url").and_then(|v| v.as_str()).unwrap_or("");
    match jump_target(state, url) {
        Some(page) => {
            log("INFO", &format!("viewer {id}: fleet jump to page {page:?}"));
            select_page(state, &page);
        }
        None => log(
            "INFO",
            &format!("viewer {id}: fleet jump to no page of this tree, ignored: {url}"),
        ),
    }
    true
}

/// M4 (§4.25): push `0x84 goto <url>` to every viewer with a web window, off the lock (they answer on their time).
fn push_goto(state: &Arc<Mutex<State>>, url: Option<String>) {
    if let Some(url) = url {
        let state = state.clone();
        tokio::spawn(async move { super::web::push_goto(&state, url).await });
    }
}

/// One client frame; `false` = close the connection.
async fn handle_frame(state: &Arc<Mutex<State>>, id: ViewerId, frame: Frame) -> bool {
    match frame {
        Frame::Input(bytes) => {
            let (kill, goto) = {
                let mut st = state.lock().await;
                if !st.viewers.contains(id) {
                    return false;
                }
                let (frames, kill) = on_input(&mut st, id, &bytes);
                for f in frames {
                    st.viewers.send_to(id, f);
                }
                (kill, st.viewers.pending_goto.take())
            };
            push_goto(state, goto);
            // S1c (plan §14.2): closing a bash page is the same escalation `pane.kill` uses, one lock at a time.
            if let Some(pane) = kill {
                let state = state.clone();
                tokio::spawn(async move {
                    if let Err(fail) =
                        super::handlers::kill(&state, &pane, DEFAULT_KILL_TIMEOUT_MS).await
                    {
                        log(
                            "WARN",
                            &format!("closing bash page {pane}: {} {}", fail.0.as_str(), fail.1),
                        );
                    }
                });
            }
            true
        }
        Frame::Resize { cols, rows } => {
            let mut st = state.lock().await;
            if !st.viewers.contains(id) {
                return false;
            }
            st.viewers.resize(id, cols, rows);
            apply_pty_sizes(&mut st);
            true
        }
        Frame::Detach => false,
        // gui-scrollbar: a `0x05` from the client: scroll that pane there (PROTOCOL.md §6.3). A bad line, or a
        // pane that does not exist, is a WARN and nothing else — never a closed connection. No reply either: the
        // next `0x86` is the acknowledgement.
        Frame::ScrollTo(line) => {
            let mut st = state.lock().await;
            if !st.viewers.contains(id) {
                return false;
            }
            match view::ScrollTo::decode(&line) {
                Ok(to) => {
                    let changed = match st.panes.term_mut(&to.pane) {
                        Some(t) => {
                            let before = t.display_offset();
                            // The parser clamps to its own `scrollback_len()`.
                            t.set_display_offset(to.offset as usize);
                            t.display_offset() != before
                        }
                        None => {
                            log(
                                "WARN",
                                &format!("viewer {id}: scroll to unknown pane {:?}", to.pane),
                            );
                            false
                        }
                    };
                    // Without this there is no next round: neither the screen nor the `0x86` would move.
                    if changed {
                        st.viewers.mark_dirty();
                    }
                }
                Err(e) => log("WARN", &format!("viewer {id}: bad 0x05: {e:?}")),
            }
            true
        }
        // M2.5: a `0x04` from the client (a reply, or a window / page event; never fatal).
        Frame::WebReply(line) => {
            let goto = {
                let mut st = state.lock().await;
                if !st.viewers.contains(id) {
                    return false;
                }
                if !on_jump(&mut st, id, &line) {
                    for f in super::web::on_client_line(&mut st, id, &line) {
                        st.viewers.send_to(id, f);
                    }
                }
                st.viewers.pending_goto.take()
            };
            push_goto(state, goto);
            true
        }
        // S1e (plan §17.3): a `0x06` from the client, this viewer's titlebar inset after a font-size change. A bad
        // line is a WARN and nothing else — never a closed connection.
        Frame::Inset(line) => {
            let mut st = state.lock().await;
            if !st.viewers.contains(id) {
                return false;
            }
            match view::Inset::decode(&line) {
                Ok(inset) => {
                    if let Some(slot) = st.viewers.viewers.get_mut(&id) {
                        slot.inset = inset.cols;
                        slot.sidebar_min = inset.sidebar_min;
                    }
                    sync_inset(&mut st);
                }
                Err(e) => log("WARN", &format!("viewer {id}: bad 0x06: {e:?}")),
            }
            true
        }
        Frame::Screen(_)
        | Frame::Exit(_)
        | Frame::Open(_)
        | Frame::Web(_)
        | Frame::Overlay(_)
        | Frame::Scroll(_) => {
            log(
                "WARN",
                &format!("viewer {id}: server-to-client frame from the client, closing"),
            );
            false
        }
    }
}

enum WriterEvent {
    Frame(Option<Frame>),
    Look,
    Kill,
}

/// The connection's writer: queued frames → socket; when the queue drains, flush the non-canvas backlog and,
/// if a screen update was skipped for lack of room, wake the renderer. Never holds the `State` lock.
async fn write_loop<W: AsyncWrite + Unpin>(
    id: ViewerId,
    mut rx: Receiver<Frame>,
    mut w: W,
    sh: Arc<Shared>,
    dirty: Arc<Notify>,
) {
    'run: loop {
        let ev = tokio::select! {
            biased;
            _ = sh.kill.notified() => WriterEvent::Kill,
            f = rx.recv() => WriterEvent::Frame(f),
            _ = sh.wake_writer.notified() => WriterEvent::Look,
        };
        match ev {
            WriterEvent::Kill => break,
            WriterEvent::Frame(None) => {
                // Every sender is gone (viewer removed): write what is parked, then stop.
                let _ = flush_backlog(id, &mut w, &sh).await;
                break;
            }
            WriterEvent::Frame(Some(f)) => {
                if !write_one(id, &mut w, &sh, &f).await {
                    break;
                }
            }
            WriterEvent::Look => {}
        }
        if rx.is_empty() {
            if !flush_backlog(id, &mut w, &sh).await {
                break 'run;
            }
            if sh.stalled.swap(false, Ordering::SeqCst) {
                *sh.stalled_since.lock().unwrap_or_else(|e| e.into_inner()) = None;
                dirty.notify_one();
            }
        }
    }
    sh.dead.store(true, Ordering::SeqCst);
    sh.wake_reader.notify_one();
    let _ = w.shutdown().await;
}

async fn flush_backlog<W: AsyncWrite + Unpin>(id: ViewerId, w: &mut W, sh: &Shared) -> bool {
    loop {
        let next = sh
            .backlog
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .pop_front();
        match next {
            Some(f) => {
                if !write_one(id, w, sh, &f).await {
                    return false;
                }
            }
            None => return true,
        }
    }
}

/// Write one frame; `false` = the connection is finished (write error or shut down).
async fn write_one<W: AsyncWrite + Unpin>(id: ViewerId, w: &mut W, sh: &Shared, f: &Frame) -> bool {
    let bytes = match view::encode_frame(f) {
        Ok(b) => b,
        Err(e) => {
            log(
                "ERROR",
                &format!("viewer {id}: cannot encode a frame: {e:?}"),
            );
            return true;
        }
    };
    tokio::select! {
        biased;
        _ = sh.kill.notified() => false,
        r = async { w.write_all(&bytes).await?; w.flush().await } => r.is_ok(),
    }
}

/// One `server.log` line (PROTOCOL.md §8.5): `<RFC 3339 UTC> <LEVEL> <message>` on stderr, which
/// `--daemonize` points at `server.log`.
fn log(level: &str, msg: &str) {
    eprintln!("{} {level} {msg}", rfc3339(SystemTime::now()));
}

fn rfc3339(t: SystemTime) -> String {
    let secs = t
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let (days, rem) = (secs / 86_400, secs % 86_400);
    // Civil date from days since 1970-01-01 (H. Hinnant's algorithm).
    let z = days as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        rem / 3600,
        rem / 60 % 60,
        rem % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hello(cols: u16, rows: u16, colorterm: &str) -> view::Hello {
        view::Hello {
            view: 1,
            cols,
            rows,
            term: "xterm-256color".into(),
            colorterm: colorterm.into(),
        }
    }

    #[test]
    fn pty_size_follows_latest_input_else_latest_added_else_default() {
        let mut hub = ViewerHub::default();
        hub.set_default_size((100, 30));
        assert_eq!(hub.size_for_ptys(hub.default_size()), (100, 30));
        assert_eq!(hub.layout_size(), (100, 30));
        let (a, _ra) = mpsc::channel(4);
        let (b, _rb) = mpsc::channel(4);
        let v1 = hub.add(&hello(80, 24, ""), a);
        let v2 = hub.add(&hello(200, 50, "truecolor"), b);
        assert_ne!(v1, v2);
        assert_eq!(hub.layout_size(), (200, 50), "latest added");
        hub.note_input(v1);
        assert_eq!(hub.layout_size(), (80, 24), "latest input");
        hub.resize(v1, 90, 20);
        assert_eq!(hub.layout_size(), (90, 20));
        hub.remove(v1);
        assert_eq!(hub.layout_size(), (200, 50));
        assert_eq!(hub.len(), 1);
        hub.remove(v2);
        assert!(hub.is_empty());
        assert_eq!(hub.layout_size(), (100, 30));
    }

    #[test]
    fn depth_from_handshake() {
        let mut hub = ViewerHub::default();
        let (a, _ra) = mpsc::channel(4);
        let id = hub.add(&hello(80, 24, "24bit"), a);
        assert_eq!(hub.viewers[&id].viewer.depth, Depth::Rgb);
        let (b, _rb) = mpsc::channel(4);
        let id = hub.add(&hello(80, 24, ""), b);
        assert_eq!(hub.viewers[&id].viewer.depth, Depth::Indexed256);
    }

    fn slot_with(cap: usize) -> (ViewerHub, ViewerId, Receiver<Frame>) {
        let mut hub = ViewerHub::default();
        let (tx, rx) = mpsc::channel(cap);
        let id = hub.add(&hello(10, 2, ""), tx);
        (hub, id, rx)
    }

    #[test]
    fn commit_reserves_whole_updates_and_resets_prev_when_full() {
        let now = Instant::now();
        let (mut hub, id, mut rx) = slot_with(2);
        let slot = hub.viewers.get_mut(&id).unwrap();
        slot.viewer.prev = Some(ratatui::buffer::Buffer::empty(ratatui::layout::Rect::new(
            0, 0, 10, 2,
        )));
        assert_eq!(commit(id, slot, Vec::new(), now), Commit::Empty);
        assert!(slot.viewer.prev.is_some());
        // Two chunks fit exactly.
        assert_eq!(
            commit(id, slot, vec![b'x'; MAX_FRAME + 1], now),
            Commit::Sent
        );
        assert!(
            slot.viewer.prev.is_some(),
            "a queued update keeps the new base"
        );
        // Queue full: nothing queued, base reset, stall flagged.
        assert_eq!(commit(id, slot, b"y".to_vec(), now), Commit::Stalled);
        assert!(slot.viewer.prev.is_none());
        assert!(slot.shared.stalled.load(Ordering::SeqCst));
        assert!(matches!(rx.try_recv(), Ok(Frame::Screen(b)) if b.len() == MAX_FRAME));
        assert!(matches!(rx.try_recv(), Ok(Frame::Screen(b)) if b.len() == 1));
        assert!(rx.try_recv().is_err(), "the failed update queued nothing");
        // More chunks than the queue can ever hold: bounded — disconnect, never retry forever.
        assert_eq!(
            commit(id, slot, vec![b'z'; 2 * MAX_FRAME + 1], now),
            Commit::Drop
        );
    }

    #[test]
    fn noncanvas_frames_wait_in_a_bounded_backlog_then_disconnect() {
        let (hub, id, mut rx) = slot_with(1);
        hub.send_to(id, Frame::Exit("a".into()));
        hub.broadcast(Frame::Exit("b".into())); // queue full → backlog
        let sh = hub.viewers[&id].shared.clone();
        assert_eq!(sh.backlog.lock().unwrap().len(), 1);
        for k in 0..BACKLOG_MAX - 1 {
            hub.send_to(id, Frame::Exit(format!("{k}")));
        }
        assert!(!sh.dead.load(Ordering::SeqCst));
        hub.send_to(id, Frame::Exit("overflow".into()));
        assert!(
            sh.dead.load(Ordering::SeqCst),
            "a full backlog disconnects, it does not drop silently"
        );
        assert!(!hub.contains(id));
        assert_eq!(rx.try_recv().unwrap(), Frame::Exit("a".into()));
    }

    #[test]
    fn pane_generations_bump_on_change_and_are_never_reused() {
        let mut hub = ViewerHub::default();
        assert_eq!(hub.pane_gen("a"), None);
        hub.note_pane_changes([("a".to_string(), true), ("b".to_string(), false)]);
        let (a1, b1) = (hub.pane_gen("a").unwrap(), hub.pane_gen("b").unwrap());
        assert_ne!(
            a1, b1,
            "a pane seen for the first time gets a fresh generation"
        );
        hub.note_pane_changes([("a".to_string(), false), ("b".to_string(), true)]);
        assert_eq!(
            hub.pane_gen("a"),
            Some(a1),
            "unchanged keeps its generation"
        );
        let b2 = hub.pane_gen("b").unwrap();
        assert!(b2 > a1.max(b1));
        hub.note_pane_changes([("a".to_string(), false)]);
        assert_eq!(
            hub.pane_gen("b"),
            None,
            "panes no longer in the layout are forgotten"
        );
        hub.note_pane_changes([("a".to_string(), false), ("b".to_string(), false)]);
        let b3 = hub.pane_gen("b").unwrap();
        assert!(
            b3 > b2,
            "a returning id never matches an old record: {b3} vs {b2}"
        );
    }

    #[test]
    fn rfc3339_formats_utc() {
        let t = UNIX_EPOCH + Duration::from_secs(1_761_609_600 + 3661); // 2025-10-28T01:01:01Z
        assert_eq!(rfc3339(t), "2025-10-28T01:01:01Z");
    }

    /// M4 R9: the card whose `page` is the current page becomes `current`; none with that page, or no `page` at all,
    /// leaves kv `current` as it is.
    #[test]
    fn m4_highlight_follows_the_current_page() {
        let card = |id: &str, page: Option<&str>| sidebar::Entry {
            id: id.into(),
            title: id.into(),
            state: "running".into(),
            line1: String::new(),
            line2: String::new(),
            page: page.map(str::to_string),
        };
        let mut d = sidebar::Data {
            current: "root".into(),
            fleets: vec![card("root", Some("")), card("m0", Some("M0")), card("old", None)],
            close: None,
        };
        follow_current_page(&mut d, "M0");
        assert_eq!(d.current, "m0");
        follow_current_page(&mut d, "");
        assert_eq!(d.current, "root");
        follow_current_page(&mut d, "M9");
        assert_eq!(d.current, "root", "no card for the page: unchanged");
    }

    #[test]
    fn m4_jump_to_parameter_is_percent_decoded() {
        assert_eq!(percent_decode("file%3A%2F%2F%2Fr%2Fsub%2FM0%2Fhistory.html"), "file:///r/sub/M0/history.html");
        assert_eq!(percent_decode("a+b%2"), "a b%2");
        assert_eq!(percent_decode("%e5%8f%aa"), "只");
    }
}
