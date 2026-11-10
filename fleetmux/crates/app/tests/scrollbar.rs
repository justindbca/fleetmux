//! gui-scrollbar 段②: the `0x86` scrollbar state (PROTOCOL.md §6.1 `scroll` key, §6.3 `0x86`) and the `0x05`
//! scroll-to frame. The four pure-function tests pin `server::scrollbar::scroll_state_for`; the three relay tests
//! drive a real server through [`RawViewer`] (a raw view connection, as in `tests/overlay.rs`).
mod common;

use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use common::{H, Server, W};
use fleetmux::server::scrollbar::scroll_state_for;
use fleetmux_layout::{Layout, PaneType};
use fleetmux_proto::view::{CellRect, PaneScroll, ScrollState, ScrollTo};
use serde_json::{Value, json};

const HIST: &str = "k7q2-1";
const CONTROL: &str = "k7q2-2";
const SUP: &str = "k7q2-3";
const WORKER: &str = "k7q2-4";

/// Supervisor + history + one more lower tab + one worker, ratios 50 / 60.
fn layout() -> Layout {
    let mut l = Layout::new();
    l.insert(SUP.into(), PaneType::Supervisor);
    l.select(SUP.into());
    l.insert(HIST.into(), PaneType::History);
    l.insert(CONTROL.into(), PaneType::Tab);
    l.select(CONTROL.into());
    l.insert(WORKER.into(), PaneType::Worker);
    l.select(WORKER.into());
    l
}

/// The stand-in for the panes' parsers: supervisor at the bottom, the others scrolled.
fn scrolls(id: &str) -> Option<(usize, usize)> {
    match id {
        SUP => Some((0, 412)),
        HIST => Some((7, 900)),
        CONTROL => Some((0, 0)),
        WORKER => Some((40, 1000)),
        _ => None,
    }
}

fn cell(r: fleetmux_layout::Rect) -> CellRect {
    CellRect {
        x: r.x,
        y: r.y,
        w: r.w,
        h: r.h,
    }
}

// ---------------------------------------------------------------- pure function

#[test]
fn scroll_state_selected_pane_per_group() {
    let l = layout();
    let s = scroll_state_for(&l, (240, 60), (240, 60), &scrolls);
    let rects = l.rects(240, 60);
    // supervisor, lower, workers — the selected tab of each group.
    let panes: Vec<(&str, CellRect, u64, u64)> = s
        .panes
        .iter()
        .map(|p| (p.pane.as_str(), p.rect, p.offset, p.total))
        .collect();
    assert_eq!(
        panes,
        vec![
            (SUP, cell(rects.supervisor), 0, 412),
            (CONTROL, cell(rects.lower), 0, 0),
            (WORKER, cell(rects.workers), 40, 1000),
        ]
    );
    // A pane whose parser is gone (never in practice) is skipped, not invented.
    let s = scroll_state_for(&l, (240, 60), (240, 60), &|_| None);
    assert_eq!(s, ScrollState::default());
}

#[test]
fn scroll_state_omits_history_pane() {
    let mut l = layout();
    l.select(HIST.into());
    let s = scroll_state_for(&l, (240, 60), (240, 60), &scrolls);
    let ids: Vec<&str> = s.panes.iter().map(|p| p.pane.as_str()).collect();
    assert_eq!(ids, vec![SUP, WORKER], "the history cell got a scrollbar");
    // Zoomed history: nothing on screen to scroll.
    l.toggle_zoom(HIST.into());
    assert_eq!(
        scroll_state_for(&l, (240, 60), (240, 60), &scrolls),
        ScrollState::default()
    );
}

#[test]
fn scroll_state_zoom_only_that_pane() {
    let mut l = layout();
    l.toggle_zoom(WORKER.into());
    let s = scroll_state_for(&l, (240, 60), (240, 60), &scrolls);
    assert_eq!(
        s.panes,
        vec![PaneScroll {
            pane: WORKER.into(),
            rect: cell(l.rects(240, 60).zoom.unwrap()),
            offset: 40,
            total: 1000,
        }]
    );
}

#[test]
fn scroll_state_clipped_to_viewer_canvas() {
    let l = layout();
    // Layout drawn at 240×60, this viewer is 100×50: its canvas is 100 × 49 cells above its own status row.
    let s = scroll_state_for(&l, (240, 60), (100, 50), &scrolls);
    let rects = l.rects(240, 60);
    // The supervisor and lower cells lose their right part; the worker cell starts at column 120 → gone.
    assert_eq!(
        s.panes
            .iter()
            .map(|p| (p.pane.as_str(), p.rect))
            .collect::<Vec<_>>(),
        vec![
            (
                SUP,
                CellRect {
                    x: 1,
                    y: 1,
                    w: 99,
                    h: rects.supervisor.h
                }
            ),
            (
                CONTROL,
                CellRect {
                    x: 1,
                    y: 36,
                    w: 99,
                    h: 13
                }
            ),
        ]
    );
    // A 37-row viewer has rows 0..=35 above its status row: the lower cell (row 36) is gone entirely, the
    // worker cell (row 1, 57 high) is cut to 35 rows.
    let s = scroll_state_for(&l, (240, 60), (240, 37), &scrolls);
    assert_eq!(
        s.panes
            .iter()
            .map(|p| (p.pane.as_str(), p.rect.h))
            .collect::<Vec<_>>(),
        vec![(SUP, 34), (WORKER, 35)]
    );
    // A viewer larger than the layout: rectangles are not stretched.
    let s = scroll_state_for(&l, (240, 60), (300, 80), &scrolls);
    assert_eq!(
        s.panes.iter().map(|p| p.rect).collect::<Vec<_>>(),
        vec![
            cell(rects.supervisor),
            cell(rects.lower),
            cell(rects.workers)
        ]
    );
}

// ---------------------------------------------------------------- relay through a real server

/// A raw view connection: handshake (with `"scroll":<n>` when given), then a reader thread that forwards every
/// frame as `(type, payload)` until the connection ends (`None` on the channel = closed).
struct RawViewer {
    stream: UnixStream,
    frames: mpsc::Receiver<Option<(u8, Vec<u8>)>>,
    /// Frames already taken off the channel, in arrival order.
    seen: Vec<(u8, Vec<u8>)>,
    closed: bool,
}

impl RawViewer {
    fn attach(srv: &Server, scroll: Option<u8>) -> RawViewer {
        let mut stream = UnixStream::connect(srv.sock()).expect("connect view");
        let mut hello = json!({"view": 1, "cols": W, "rows": H, "term": "xterm-256color", "colorterm": "truecolor"});
        if let Some(n) = scroll {
            hello["scroll"] = json!(n);
        }
        stream.write_all(format!("{hello}\n").as_bytes()).unwrap();
        let mut line = Vec::new();
        let mut byte = [0u8; 1];
        while byte[0] != b'\n' {
            stream.read_exact(&mut byte).expect("view handshake reply");
            line.push(byte[0]);
        }
        let reply: Value = serde_json::from_slice(&line).unwrap();
        assert_eq!(reply["ok"], json!(true), "view handshake refused: {reply}");
        let (tx, frames) = mpsc::channel();
        let mut reader = stream.try_clone().unwrap();
        std::thread::spawn(move || {
            loop {
                let mut hdr = [0u8; 5];
                if reader.read_exact(&mut hdr).is_err() {
                    let _ = tx.send(None);
                    return;
                }
                let len = u32::from_be_bytes([hdr[1], hdr[2], hdr[3], hdr[4]]) as usize;
                let mut payload = vec![0u8; len];
                if reader.read_exact(&mut payload).is_err() {
                    let _ = tx.send(None);
                    return;
                }
                if tx.send(Some((hdr[0], payload))).is_err() {
                    return;
                }
            }
        });
        RawViewer {
            stream,
            frames,
            seen: Vec::new(),
            closed: false,
        }
    }

    /// One client → server frame.
    fn send(&mut self, ty: u8, payload: &[u8]) {
        let mut hdr = vec![ty];
        hdr.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        hdr.extend_from_slice(payload);
        self.stream.write_all(&hdr).expect("write view frame");
    }

    /// Take every frame that arrives within `window`.
    fn drain(&mut self, window: Duration) {
        let end = Instant::now() + window;
        while let Some(left) = end.checked_duration_since(Instant::now()) {
            match self.frames.recv_timeout(left) {
                Ok(Some(f)) => self.seen.push(f),
                Ok(None) => {
                    self.closed = true;
                    return;
                }
                Err(_) => return,
            }
        }
    }

    /// The next `0x86` (decoded) within `timeout`; frames before it are kept in `seen`.
    fn next_scroll(&mut self, timeout: Duration) -> Option<ScrollState> {
        let end = Instant::now() + timeout;
        while let Some(left) = end.checked_duration_since(Instant::now()) {
            match self.frames.recv_timeout(left) {
                Ok(Some((ty, payload))) => {
                    self.seen.push((ty, payload.clone()));
                    if ty == 0x86 {
                        let s = String::from_utf8(payload).expect("0x86 is UTF-8");
                        return Some(ScrollState::decode(&s).unwrap_or_else(|e| {
                            panic!("0x86 is not a scroll state ({e:?}): {s}")
                        }));
                    }
                }
                Ok(None) => {
                    self.closed = true;
                    return None;
                }
                Err(_) => return None,
            }
        }
        None
    }

    /// The first `0x86` within `timeout` whose entry for `pane` satisfies `pred`; `None` on timeout / close.
    fn wait_pane(
        &mut self,
        pane: &str,
        timeout: Duration,
        pred: impl Fn(u64, u64) -> bool,
    ) -> Option<(u64, u64)> {
        let end = Instant::now() + timeout;
        while let Some(left) = end.checked_duration_since(Instant::now()) {
            let state = self.next_scroll(left)?;
            if let Some(p) = state.panes.iter().find(|p| p.pane == pane)
                && pred(p.offset, p.total)
            {
                return Some((p.offset, p.total));
            }
        }
        None
    }

    /// The first `0x86` within `timeout` whose whole state satisfies `pred`; `None` on timeout / close.
    fn wait_state(
        &mut self,
        timeout: Duration,
        pred: impl Fn(&ScrollState) -> bool,
    ) -> Option<ScrollState> {
        let end = Instant::now() + timeout;
        while let Some(left) = end.checked_duration_since(Instant::now()) {
            let state = self.next_scroll(left)?;
            if pred(&state) {
                return Some(state);
            }
        }
        None
    }

    fn count(&self, ty: u8) -> usize {
        self.seen.iter().filter(|(t, _)| *t == ty).count()
    }
}

const T: Duration = Duration::from_secs(5);
/// Long enough for a few render rounds (the renderer paces at ≤ 60 frames / s).
const QUIET: Duration = Duration::from_millis(800);

/// A server with a supervisor, a history pane in the lower group and a worker with 200 lines of scrollback.
fn server_with_panes() -> (Server, String, String, String) {
    let srv = Server::start();
    let sup = srv.spawn_id("sup", "supervisor", &["sleep", "1000"]);
    let hist = srv.spawn_id("history", "history", &["sleep", "1000"]);
    let worker = srv.spawn_id(
        "worker",
        "worker",
        &["sh", "-c", "seq 1 200; exec sleep 1000"],
    );
    (srv, sup, hist, worker)
}

#[test]
fn scroll_frame_only_to_opted_in_viewers() {
    let (srv, sup, hist, worker) = server_with_panes();
    let mut old = RawViewer::attach(&srv, None);
    let mut zero = RawViewer::attach(&srv, Some(0));
    let mut gui = RawViewer::attach(&srv, Some(1));
    let s = gui
        .next_scroll(T)
        .expect("a viewer that said \"scroll\":1 gets 0x86");
    let rects = srv_layout(&srv);
    let ids: Vec<&str> = s.panes.iter().map(|p| p.pane.as_str()).collect();
    assert_eq!(ids, vec![sup.as_str(), worker.as_str()], "panes: {s:?}");
    assert!(!ids.contains(&hist.as_str()), "the history pane got 0x86");
    let sup_entry = s.panes.iter().find(|p| p.pane == sup).unwrap();
    assert_eq!(sup_entry.rect, cell(rects.supervisor));
    let worker_entry = s.panes.iter().find(|p| p.pane == worker).unwrap();
    assert_eq!(worker_entry.rect, cell(rects.workers));
    assert_eq!(sup_entry.offset, 0);
    assert_eq!(sup_entry.total, 0);
    // The worker's `seq 1 200` may not be parsed yet when the first 0x86 goes out (a slow runner): its
    // scrollback arrives in a later 0x86, as a changed state.
    assert!(
        worker_entry.total >= 50 || gui.wait_pane(&worker, T, |_, total| total >= 50).is_some(),
        "worker 0x86 with scrollback, first was {worker_entry:?}"
    );
    // The first 0x86 comes after a screen frame.
    let first = gui.seen.iter().position(|(t, _)| *t == 0x86).unwrap();
    assert!(
        gui.seen[..first].iter().any(|(t, _)| *t == 0x81),
        "the first 0x86 came before any screen frame"
    );
    old.drain(QUIET);
    zero.drain(QUIET);
    for (name, v) in [("no scroll key", &old), ("\"scroll\":0", &zero)] {
        assert_eq!(v.count(0x86), 0, "{name}: got 0x86");
        assert!(v.count(0x81) > 0, "{name}: no screen frames at all");
        assert!(!v.closed, "{name}: connection closed");
    }
}

#[test]
fn scroll_to_sets_offset_and_next_0x86_reports_it() {
    let (srv, _sup, _hist, worker) = server_with_panes();
    let mut gui = RawViewer::attach(&srv, Some(1));
    let (_, mut total) = gui
        .wait_pane(&worker, T, |_, total| total >= 50)
        .expect("worker 0x86 with scrollback");
    // `seq 1 200` can still be arriving (a slow runner): scroll only once its scrollback stopped growing, or the
    // offset set below moves with the new lines and `total` is stale.
    while let Some((_, t)) = gui.wait_pane(&worker, QUIET, move |_, t| t != total) {
        total = t;
    }
    // Scroll the worker to 50.
    let to = ScrollTo {
        pane: worker.clone(),
        offset: 50,
    };
    gui.send(0x05, to.encode().as_bytes());
    assert_eq!(
        gui.wait_pane(&worker, T, |offset, _| offset == 50),
        Some((50, total)),
        "0x05 did not set the offset"
    );
    // Clamped to the scrollback, not past it.
    let to = ScrollTo {
        pane: worker.clone(),
        offset: 10_000_000,
    };
    gui.send(0x05, to.encode().as_bytes());
    assert_eq!(
        gui.wait_pane(&worker, T, |offset, t| offset == t),
        Some((total, total)),
        "the offset was not clamped to the scrollback"
    );
    // A pane that does not exist: ignored, the connection stays.
    let to = ScrollTo {
        pane: "nope".into(),
        offset: 3,
    };
    gui.send(0x05, to.encode().as_bytes());
    gui.drain(QUIET);
    assert!(!gui.closed, "an unknown pane closed the connection");
    // Viewer input into that pane puts it back at the bottom (PROTOCOL.md §11; the reset lives in
    // `view::write_pane`, so this goes through the view connection, not `pane.write`).
    srv.ok("pane.focus", json!({"pane": &worker}));
    gui.send(0x01, b"\r");
    let (offset, _) = gui
        .wait_pane(&worker, T, |offset, _| offset == 0)
        .expect("input did not reset the offset");
    assert_eq!(offset, 0);
}

#[test]
fn old_viewer_never_sees_0x86_and_stays_connected() {
    let (srv, sup, _hist, worker) = server_with_panes();
    let mut old = RawViewer::attach(&srv, None);
    // Positive control: a viewer that asked gets 0x86 whenever the state changes (zoom changes the cell list).
    let mut gui = RawViewer::attach(&srv, Some(1));
    gui.next_scroll(T).expect("first 0x86 to the GUI viewer");
    // The server sends 0x86 per state, not per request (view.rs `scroll_after_screen` skips a state equal to the
    // last one queued): an earlier 0x86 (the worker's `seq` output parsed after the first one, on a slow runner)
    // may still be queued, and taking it as this change's frame puts the test a frame behind, where the next two
    // changes can share a render round and cancel out. Wait for the 0x86 that shows this change instead.
    for i in 0..4 {
        let zoomed = i % 2 == 0;
        let zoom = if zoomed { json!(sup) } else { Value::Null };
        srv.ok("layout.set", json!({"zoom": zoom}));
        gui.wait_state(T, |s| {
            if zoomed {
                s.panes.len() == 1 && s.panes[0].pane == sup
            } else {
                s.panes.iter().any(|p| p.pane == worker)
            }
        })
        .unwrap_or_else(|| panic!("GUI viewer: no 0x86 after zoom change {i}"));
    }
    old.drain(QUIET);
    assert_eq!(old.count(0x86), 0, "a viewer without \"scroll\" got 0x86");
    assert!(!old.closed, "the old viewer was disconnected");
    // Still served: one more change still reaches it as a screen frame.
    let screens = old.count(0x81);
    srv.ok("layout.set", json!({"h_ratio": 60}));
    old.drain(QUIET);
    assert!(
        old.count(0x81) > screens,
        "no screen frame after the last switch"
    );
    assert_eq!(old.count(0x86), 0);
    assert!(!old.closed);
}

/// The server's layout rectangles at the viewer size (`W × H`), from its own ratios.
fn srv_layout(srv: &Server) -> fleetmux_layout::Rects {
    let v = srv.layout();
    let mut l = Layout::new();
    l.set_ratios(
        v["h_ratio"].as_u64().unwrap() as u8,
        v["v_ratio"].as_u64().unwrap() as u8,
    );
    l.rects(W, H)
}
