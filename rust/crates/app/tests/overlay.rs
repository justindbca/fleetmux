//! M2.6 history overlay (PROTOCOL.md §6.1 `overlay` key, §6.3 `0x85`; design.md §3.6.3). Red in W0 (`todo!()`):
//! the six pure-function tests are W2 (`server::overlay::overlay_for`), the four relay tests W3 (`view.rs` sends
//! `0x85` after each committed screen update to the connections that asked for it).
//!
//! The relay tests stand in for the GUI client with [`RawViewer`]: a raw view connection whose handshake carries
//! (or not) `"overlay":1` and whose reader thread forwards every frame, so they test the server, not a client.
mod common;

use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use common::{H, Server, W};
use fleetmux::render::sidebar;
use fleetmux::server::overlay::{overlay_for, topbar_for};
use fleetmux_layout::{Layout, PaneType};
use fleetmux_proto::view::{CellRect, Overlay, Topbar};
use serde_json::{Value, json};

const HIST: &str = "k7q2-1";
const CONTROL: &str = "k7q2-2";
const SUP: &str = "k7q2-3";
const URL: &str = "file:///r/run/history.html";

/// Supervisor + history (selected in the lower group) + one more lower tab, default ratios 50 / 60.
fn layout() -> Layout {
    let mut l = Layout::new();
    l.insert(SUP.into(), PaneType::Supervisor);
    l.select(SUP.into());
    l.insert(HIST.into(), PaneType::History);
    l.insert(CONTROL.into(), PaneType::Tab);
    l.select(HIST.into());
    l
}

fn cell(r: fleetmux_layout::Rect) -> CellRect {
    CellRect {
        x: r.x,
        y: r.y,
        w: r.w,
        h: r.h,
    }
}

/// A26 rev5/rev7 (`CURRENT_REQUIREMENTS` rev 6 `utm_topbar`, rev 10 `top_container_alignment`): the native
/// headerbar's content is the current fleet's `title`/`line1`, unclipped, plus the sidebar band geometry the
/// headerbar puts its workspace/sidebar container boundary on (the layout's own `sidebar_cols`). With no kv
/// `sidebar` data there is no headerbar content at all; with a `current` id that names no card the text is empty
/// but the geometry still goes out, so the boundary stays on the grid's divider.
#[test]
fn topbar_for_is_the_current_fleets_title_and_status() {
    let json = r#"{"v":1,"current":"fleet-a","fleets":[
        {"id":"fleet-a","title":"fleetmux-demo","state":"running","line1":"运行中 · 监工+2 worker（1忙）","line2":"~/.cmux-fleet"},
        {"id":"fleet-b","title":"other","state":"running","line1":"x","line2":"y"}]}"#;
    let side = sidebar::Side {
        data: Some(sidebar::parse(json).expect("kv `sidebar` parses")),
        ..Default::default()
    };
    let mut l = layout();
    l.set_sidebar(36);
    assert_eq!(
        topbar_for(&side, &l, 150),
        Some(Topbar {
            title: "fleetmux-demo".into(),
            status: "运行中 · 监工+2 worker（1忙）".into(),
            sidebar: 36,
            cols: 150,
        }),
        "the headerbar shows the current fleet, unclipped, and the 36-column band"
    );
    // A window too narrow for the band (`cols < sidebar + SIDEBAR_MIN_CONTENT`): the layout drops it, and the
    // headerbar must follow the drawn grid, not the requested width.
    assert_eq!(
        topbar_for(&side, &l, 90).map(|t| (t.sidebar, t.cols)),
        Some((0, 90)),
        "a dropped sidebar is sent as 0 columns"
    );
    let mut missing = side.clone();
    if let Some(data) = missing.data.as_mut() {
        data.current = "fleet-gone".into();
    }
    assert_eq!(
        topbar_for(&missing, &l, 150),
        Some(Topbar {
            title: String::new(),
            status: String::new(),
            sidebar: 36,
            cols: 150,
        }),
        "no current card: empty text, but the boundary geometry still goes out"
    );
    assert_eq!(
        topbar_for(&sidebar::Side::default(), &l, 150),
        None,
        "no kv `sidebar` means no fleet title in the headerbar"
    );
}

// ---------------------------------------------------------------- pure function (W2)

#[test]
#[ignore = "M2.6: W2"]
fn overlay_rect_when_history_tab_selected() {
    let l = layout();
    let o = overlay_for(&l, (240, 60), (240, 60), Some(URL));
    assert_eq!(
        o,
        Overlay {
            pane: Some(HIST.into()),
            rect: Some(CellRect {
                x: 1,
                y: 36,
                w: 118,
                h: 22
            }),
            url: Some(URL.into()),
            topbar: None,
        }
    );
    // Any ratio: the lower group's inner rectangle of the layout at that size.
    let mut l = layout();
    l.set_ratios(65, 40);
    let o = overlay_for(&l, (200, 50), (200, 50), Some(URL));
    assert_eq!(o.rect, Some(cell(l.rects(200, 50).lower)));
}

#[test]
#[ignore = "M2.6: W2"]
fn overlay_null_rect_when_other_lower_tab_selected() {
    let mut l = layout();
    l.select(CONTROL.into());
    let o = overlay_for(&l, (240, 60), (240, 60), Some(URL));
    assert_eq!(
        o,
        Overlay {
            pane: Some(HIST.into()),
            rect: None,
            url: Some(URL.into()),
            topbar: None,
        }
    );
}

#[test]
#[ignore = "M2.6: W2"]
fn overlay_zoomed_history_uses_zoom_rect() {
    let mut l = layout();
    l.toggle_zoom(HIST.into());
    let o = overlay_for(&l, (240, 60), (240, 60), Some(URL));
    assert_eq!(
        o.rect,
        Some(CellRect {
            x: 0,
            y: 0,
            w: 240,
            h: 59
        })
    );
    // Zoomed even while another lower tab is selected: the zoomed pane is what is on screen.
    l.select(CONTROL.into());
    let o = overlay_for(&l, (240, 60), (240, 60), Some(URL));
    assert_eq!(o.rect, Some(cell(l.rects(240, 60).zoom.unwrap())));
}

#[test]
#[ignore = "M2.6: W2"]
fn overlay_null_rect_when_other_pane_zoomed() {
    let mut l = layout();
    l.toggle_zoom(SUP.into());
    let o = overlay_for(&l, (240, 60), (240, 60), Some(URL));
    assert_eq!(o.pane.as_deref(), Some(HIST));
    assert_eq!(o.rect, None);
}

#[test]
#[ignore = "M2.6: W2"]
fn overlay_clipped_to_viewer_canvas_above_status_row() {
    let l = layout();
    // Layout drawn at 240×60, this viewer is 100×50: its canvas is 100 × 49 cells above its own status row.
    let o = overlay_for(&l, (240, 60), (100, 50), Some(URL));
    assert_eq!(
        o.rect,
        Some(CellRect {
            x: 1,
            y: 36,
            w: 99,
            h: 13
        })
    );
    // The lower cell starts at row 36: a 37-row viewer has rows 0..=35 above its status row → nothing left.
    let o = overlay_for(&l, (240, 60), (240, 37), Some(URL));
    assert_eq!(o.rect, None);
    // A viewer larger than the layout: the rectangle is not stretched.
    let o = overlay_for(&l, (240, 60), (300, 80), Some(URL));
    assert_eq!(
        o.rect,
        Some(CellRect {
            x: 1,
            y: 36,
            w: 118,
            h: 22
        })
    );
    // Zoomed history at 240×60 seen in 100×50: clipped to 100 × 49.
    let mut z = layout();
    z.toggle_zoom(HIST.into());
    let o = overlay_for(&z, (240, 60), (100, 50), None);
    assert_eq!(
        o.rect,
        Some(CellRect {
            x: 0,
            y: 0,
            w: 100,
            h: 49
        })
    );
}

#[test]
#[ignore = "M2.6: W2"]
fn overlay_null_pane_without_history() {
    let mut l = Layout::new();
    l.insert(SUP.into(), PaneType::Supervisor);
    l.insert(CONTROL.into(), PaneType::Tab);
    l.select(CONTROL.into());
    assert_eq!(
        overlay_for(&l, (240, 60), (240, 60), Some(URL)),
        Overlay {
            pane: None,
            rect: None,
            url: Some(URL.into()),
            topbar: None,
        }
    );
    assert_eq!(
        overlay_for(&l, (240, 60), (240, 60), None),
        Overlay::default()
    );
}

// ---------------------------------------------------------------- relay through a real server (W3)

/// A raw view connection: handshake (with `"overlay":<n>` when given), then a reader thread that forwards every
/// frame as `(type, payload)` until the connection ends (`None` on the channel = closed).
struct RawViewer {
    _stream: UnixStream,
    frames: mpsc::Receiver<Option<(u8, Vec<u8>)>>,
    /// Frames already taken off the channel, in arrival order.
    seen: Vec<(u8, Vec<u8>)>,
    closed: bool,
}

impl RawViewer {
    fn attach(srv: &Server, overlay: Option<u8>) -> RawViewer {
        let mut stream = UnixStream::connect(srv.sock()).expect("connect view");
        let mut hello = json!({"view": 1, "cols": W, "rows": H, "term": "xterm-256color", "colorterm": "truecolor"});
        if let Some(n) = overlay {
            hello["overlay"] = json!(n);
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
            _stream: stream,
            frames,
            seen: Vec::new(),
            closed: false,
        }
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

    /// The next `0x85` (decoded) within `timeout`; frames before it are kept in `seen`.
    fn next_overlay(&mut self, timeout: Duration) -> Option<Overlay> {
        let end = Instant::now() + timeout;
        while let Some(left) = end.checked_duration_since(Instant::now()) {
            match self.frames.recv_timeout(left) {
                Ok(Some((ty, payload))) => {
                    self.seen.push((ty, payload.clone()));
                    if ty == 0x85 {
                        let s = String::from_utf8(payload).expect("0x85 is UTF-8");
                        return Some(
                            Overlay::decode(&s)
                                .unwrap_or_else(|e| panic!("0x85 is not an overlay ({e:?}): {s}")),
                        );
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

    fn count(&self, ty: u8) -> usize {
        self.seen.iter().filter(|(t, _)| *t == ty).count()
    }
}

const T: Duration = Duration::from_secs(3);
/// Long enough for a few render rounds (the renderer paces at ≤ 60 frames / s).
const QUIET: Duration = Duration::from_millis(800);

/// A server with a history pane (selected in the lower group, as the first lower pane) and one more lower tab,
/// and kv `history_url` set.
fn server_with_history() -> (Server, String, String) {
    let srv = Server::start();
    let hist = srv.spawn_id("history", "history", &["sleep", "1000"]);
    let tab = srv.spawn_id("control", "tab", &["sleep", "1000"]);
    srv.ok("kv.set", json!({"key": "history_url", "value": URL}));
    (srv, hist, tab)
}

fn lower_at(h_ratio: u8, v_ratio: u8) -> CellRect {
    let mut l = Layout::new();
    l.set_ratios(h_ratio, v_ratio);
    cell(l.rects(W, H).lower)
}

#[test]
#[ignore = "M2.6: W3"]
fn overlay_frame_only_to_opted_in_viewers() {
    let (srv, hist, tab) = server_with_history();
    let mut old = RawViewer::attach(&srv, None);
    let mut zero = RawViewer::attach(&srv, Some(0));
    let mut gui = RawViewer::attach(&srv, Some(1));
    let o = gui
        .next_overlay(T)
        .expect("a viewer that said \"overlay\":1 gets 0x85");
    assert_eq!(
        o,
        Overlay {
            pane: Some(hist.clone()),
            rect: Some(lower_at(50, 60)),
            url: Some(URL.into()),
            topbar: None,
        }
    );
    srv.ok("pane.focus", json!({"pane": tab}));
    assert_eq!(
        gui.next_overlay(T).expect("0x85 after the tab switch").rect,
        None
    );
    old.drain(QUIET);
    zero.drain(QUIET);
    for (name, v) in [("no overlay key", &old), ("\"overlay\":0", &zero)] {
        assert_eq!(v.count(0x85), 0, "{name}: got 0x85");
        assert!(v.count(0x81) > 0, "{name}: no screen frames at all");
        assert!(!v.closed, "{name}: connection closed");
    }
}

#[test]
#[ignore = "M2.6: W3"]
fn overlay_sent_once_after_handshake_then_only_on_change() {
    let (srv, hist, tab) = server_with_history();
    // A worker that keeps the screen changing while the overlay stays the same.
    srv.spawn_id(
        "ticker",
        "worker",
        &["sh", "-c", "while :; do date +%N; sleep 0.05; done"],
    );
    let mut gui = RawViewer::attach(&srv, Some(1));
    let first = gui.next_overlay(T).expect("0x85 after the handshake");
    assert_eq!(first.pane.as_deref(), Some(hist.as_str()));
    let first_overlay = gui.seen.iter().position(|(t, _)| *t == 0x85).unwrap();
    assert!(
        gui.seen[..first_overlay].iter().any(|(t, _)| *t == 0x81),
        "the first 0x85 comes after the first screen frame"
    );
    gui.drain(QUIET);
    let screens_before = gui.count(0x81);
    gui.drain(QUIET);
    assert!(
        gui.count(0x81) > screens_before,
        "the ticker keeps the screen changing"
    );
    assert_eq!(gui.count(0x85), 1, "unchanged overlay sent again");
    srv.ok("pane.focus", json!({"pane": tab}));
    let changed = gui.next_overlay(T).expect("0x85 once the overlay changes");
    assert_eq!(changed.rect, None);
    gui.drain(QUIET);
    assert_eq!(gui.count(0x85), 2, "one 0x85 per change");
}

#[test]
#[ignore = "M2.6: W3"]
fn overlay_follows_tab_switch_ratio_and_history_url() {
    let (srv, hist, tab) = server_with_history();
    let mut gui = RawViewer::attach(&srv, Some(1));
    let o = gui.next_overlay(T).expect("first 0x85");
    assert_eq!(o.rect, Some(lower_at(50, 60)));
    // Tab switch away and back.
    srv.ok("pane.focus", json!({"pane": tab}));
    assert_eq!(gui.next_overlay(T).expect("switch away").rect, None);
    srv.ok("pane.focus", json!({"pane": hist}));
    assert_eq!(
        gui.next_overlay(T).expect("switch back").rect,
        Some(lower_at(50, 60))
    );
    // Ratios.
    srv.ok("layout.set", json!({"h_ratio": 65, "v_ratio": 40}));
    assert_eq!(
        gui.next_overlay(T).expect("after layout.set").rect,
        Some(lower_at(65, 40))
    );
    // Zoom the history pane, then unzoom.
    srv.ok("layout.set", json!({"zoom": hist}));
    assert_eq!(
        gui.next_overlay(T).expect("zoomed").rect,
        Some(CellRect {
            x: 0,
            y: 0,
            w: W,
            h: H - 1
        })
    );
    srv.ok("layout.set", json!({"zoom": null}));
    assert_eq!(
        gui.next_overlay(T).expect("unzoomed").rect,
        Some(lower_at(65, 40))
    );
    // history_url: a kv.set alone (nothing else on screen changes) still sends the new url.
    let url2 = "file:///r/run2/history.html?x=1#h";
    srv.ok("kv.set", json!({"key": "history_url", "value": url2}));
    let o = gui.next_overlay(T).expect("after kv.set history_url");
    assert_eq!(o.url.as_deref(), Some(url2));
    assert_eq!(o.pane.as_deref(), Some(hist.as_str()));
}

#[test]
#[ignore = "M2.6: W3"]
fn old_viewer_never_sees_0x85_and_stays_connected() {
    let (srv, hist, tab) = server_with_history();
    let mut old = RawViewer::attach(&srv, None);
    // Positive control: a viewer that asked gets one 0x85 per switch, so the old one is really being tested.
    let mut gui = RawViewer::attach(&srv, Some(1));
    gui.next_overlay(T).expect("first 0x85 to the GUI viewer");
    for i in 0..4 {
        let pane = if i % 2 == 0 { &tab } else { &hist };
        srv.ok("pane.focus", json!({"pane": pane}));
        gui.next_overlay(T)
            .unwrap_or_else(|| panic!("GUI viewer: no 0x85 after switch {i}"));
    }
    old.drain(QUIET);
    assert_eq!(old.count(0x85), 0, "a viewer without \"overlay\" got 0x85");
    assert!(!old.closed, "the old viewer was disconnected");
    // Still served: one more change still reaches it as a screen frame.
    let screens = old.count(0x81);
    srv.ok("pane.focus", json!({"pane": tab}));
    old.drain(QUIET);
    assert!(
        old.count(0x81) > screens,
        "no screen frame after the last switch"
    );
    assert_eq!(old.count(0x85), 0);
    assert!(!old.closed);
}

#[test]
fn overlay_rect_moves_right_with_the_sidebar() {
    let mut l = layout();
    l.set_sidebar(34);
    let lower = l.rects(240, 60).lower;
    let o = overlay_for(&l, (240, 60), (240, 60), None);
    assert_eq!(
        o.rect,
        Some(cell(lower)),
        "the overlay rect with the sidebar: {:?}, lower group {lower:?}",
        o.rect
    );
    assert_eq!(
        o.rect.map(|r| r.x),
        Some(35),
        "the overlay x with the sidebar: {:?}, lower group {lower:?}",
        o.rect
    );
}

// ------------------------------ fleet sidebar close box (S1b-④, plan §2.5 / §3.5) -------------------------

/// The kv `sidebar` value the fleet daemon pushes: one running fleet, the current one (plan §4.5).
const SIDEBAR_ONE: &str = r#"{"v":1,"current":"fleet-20261029-231820-ab12cd34","fleets":[{"id":"fleet-20261029-231820-ab12cd34","title":"fleetmux-demo","state":"running","line1":"运行中 · 监工+2 worker（1忙）","line2":"~/.cmux-fleet-fmx-demo"}]}"#;

#[cfg(target_os = "linux")]
#[test]
fn linux_default_keeps_native_header_metadata_and_history_in_the_middle_column() {
    let srv = Server::start_with_env(&[]);
    let hist = srv.spawn_id("history", "history", &["sleep", "1000"]);
    srv.spawn_id("control", "tab", &["sleep", "1000"]);
    srv.ok("kv.set", json!({"key": "history_url", "value": URL}));
    srv.ok("kv.set", json!({"key": "sidebar", "value": SIDEBAR_ONE}));
    let mut gui = RawViewer::attach(&srv, Some(1));
    let first = gui.next_overlay(T).expect("native header metadata after handshake");
    let top = first.topbar.expect("native header must survive turning off the content mirror");
    assert_eq!(top.title, "fleetmux-demo");
    assert_eq!((top.sidebar, top.cols), (34, W));
    assert_eq!(first.pane.as_deref(), Some(hist.as_str()));
    let mut expected = Layout::for_platform();
    expected.set_sidebar(34);
    let lower = expected.rects(W, H).lower;
    assert_eq!(lower.x, 35, "history is beside the left sidebar, not on the right");
    assert_eq!(first.rect, Some(cell(lower)));
}

/// The gui's history iframe covers the xterm.js screen, so it would hide the centered close box: while the box is
/// open the `0x85` carries no rect (plan §2.5 / §3.5). The pane name and the url stay.
#[test]
fn overlay_rect_is_null_while_the_close_dialog_is_open() {
    let (srv, hist, _tab) = server_with_history();
    srv.ok("kv.set", json!({"key": "sidebar", "value": SIDEBAR_ONE}));
    let mut l = Layout::new();
    l.set_sidebar(34);
    l.set_topbar(15);
    let lower = l.rects(W, H).lower;
    let mut gui = RawViewer::attach(&srv, Some(1));
    let first = gui.next_overlay(T).expect("0x85 after the handshake");
    assert_eq!(first.pane.as_deref(), Some(hist.as_str()));
    assert_eq!(
        first.rect,
        Some(cell(lower)),
        "the overlay rect with the sidebar: {:?}, lower group {lower:?}",
        first.rect
    );

    // `^b X` from an ordinary viewer opens the box: the state is shared by every viewer.
    let mut v = common::Viewer::attach(&srv, W, H);
    v.input(&common::prefixed(b"X"));
    let open = gui.next_overlay(T).expect("0x85 when the box opens");
    assert_eq!(
        open.rect, None,
        "the history iframe keeps a rect while the close box is open: {:?}",
        open.rect
    );
    assert_eq!(
        open.pane.as_deref(),
        Some(hist.as_str()),
        "the box hid the pane name too: {:?}",
        open.pane
    );
}

/// A wider sidebar moves the `0x85` rectangle with it (plan §16.2 / §16.5-④): the lower group starts at the
/// content's first column, `sidebar + 1`.
#[test]
fn overlay_rect_follows_a_wider_sidebar() {
    let mut l = layout();
    l.set_sidebar(44);
    let lower = l.rects(240, 60).lower;
    let o = overlay_for(&l, (240, 60), (240, 60), None);
    assert_eq!(
        o.rect,
        Some(cell(lower)),
        "the overlay rect with a 44-column sidebar: {:?}, lower group {lower:?}",
        o.rect
    );
    assert_eq!(
        o.rect.map(|r| r.x),
        Some(45),
        "the overlay x with a 44-column sidebar: {:?}, lower group {lower:?}",
        o.rect
    );
}

// ------------------------------ S1f topbar (plan §20.2 / §20.6) ------------------------------

/// The topbar takes row 0, so the groups — and the overlay rect that follows the lower group — move down one
/// row: `y` is 37 (the lower group's first row), not 36.
#[test]
fn overlay_rect_moves_down_with_the_topbar() {
    let mut l = layout();
    l.set_sidebar(34);
    l.set_topbar(15);
    let lower = l.rects(240, 60).lower;
    let o = overlay_for(&l, (240, 60), (240, 60), None);
    assert_eq!(
        o.rect,
        Some(cell(lower)),
        "the overlay rect with the topbar: {:?}, lower group {lower:?}",
        o.rect
    );
    assert_eq!(
        o.rect.map(|r| r.y),
        Some(37),
        "the overlay y with the topbar is the lower group's first row (37), not the tab row: {:?}, lower group {lower:?}",
        o.rect
    );
}
