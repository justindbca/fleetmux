//! Zoom, ratios, layout persistence, several viewers, unseen marks, colour depth (brief-m03 §3.4
//! `tests/views.rs`, PROTOCOL.md §10 / §14, plan.md §5 views-layout). M1: `#[ignore = "M1: views-multi"]`
//! until B16.
mod common;
use common::*;

const T: Duration = Duration::from_secs(10);

#[test]
#[ignore = "M1: views-multi"]
fn zoom_key_and_double_click() {
    let srv = Server::start();
    sh(&srv, "fx-supervisor-1", "supervisor", "exec sleep 1000");
    let w1 = sh(&srv, "fx-worker-1", "worker", "exec sleep 1000");
    let w2 = sh(&srv, "fx-worker-2", "worker", "exec sleep 1000");
    srv.ok("pane.focus", json!({"pane": w1}));
    let mut v = Viewer::attach(&srv, W, H);
    let (row, col) = v.wait_find("fx-worker-2", T);

    v.input(&prefixed(b"z"));
    srv.wait_layout("zoom on worker-1", T, |l| l["zoom"] == json!(w1));
    v.input(&prefixed(b"z"));
    srv.wait_layout("unzoomed", T, |l| l["zoom"].is_null());

    // Two clicks within 300 ms on a tab: select it, then toggle zoom on it.
    let mut double = sgr_click(col, row, 0);
    double.extend(sgr_click(col, row, 0));
    v.input(&double);
    srv.wait_layout("zoom on worker-2", T, |l| l["zoom"] == json!(w2));
}

#[test]
#[ignore = "M1: views-multi"]
fn ratio_keys_and_equals_reset() {
    let srv = Server::start();
    sh(&srv, "fx-supervisor-1", "supervisor", "exec sleep 1000");
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("fx-supervisor-1", T);
    let ratios = |h: u64, v: u64| move |l: &Value| l["h_ratio"] == json!(h) && l["v_ratio"] == json!(v);

    v.input(&prefixed(b">"));
    srv.wait_layout("55/60", T, ratios(55, 60));
    v.input(&prefixed(b"<"));
    v.input(&prefixed(b"<"));
    srv.wait_layout("45/60", T, ratios(45, 60));
    v.input(&prefixed(b"+"));
    srv.wait_layout("45/65", T, ratios(45, 65));
    v.input(&prefixed(b"-"));
    v.input(&prefixed(b"-"));
    srv.wait_layout("45/55", T, ratios(45, 55));
    v.input(&prefixed(b"="));
    srv.wait_layout("reset to 50/60", T, ratios(50, 60));
}

#[test]
#[ignore = "M1: views-multi"]
fn layout_json_survives_server_restart() {
    let mut srv = Server::start();
    sh(&srv, "fx-worker-1", "worker", "exec sleep 1000");
    srv.ok("layout.set", json!({"h_ratio": 65, "v_ratio": 40}));

    let old = srv.layout();
    srv.restart();
    let l = srv.layout();
    assert_eq!((l["h_ratio"].clone(), l["v_ratio"].clone()), (json!(65), json!(40)), "ratios lost: {l}");
    for g in ["supervisor", "lower", "workers"] {
        assert_eq!(l["groups"][g], json!([]), "{g} kept panes of the old epoch: {l} (before: {old})");
    }
}

#[test]
#[ignore = "M1: views-multi"]
fn two_viewers_share_selection_and_zoom() {
    let srv = Server::start();
    sh(&srv, "fx-supervisor-1", "supervisor", "echo SUP-BODY; exec sleep 1000");
    let w1 = sh(&srv, "fx-worker-1", "worker", "echo W1-BODY; exec sleep 1000");
    let w2 = sh(&srv, "fx-worker-2", "worker", "echo W2-BODY; exec sleep 1000");
    srv.ok("pane.focus", json!({"pane": w1}));
    let mut a = Viewer::attach(&srv, W, H);
    let mut b = Viewer::attach(&srv, 200, 50);
    a.wait_text("W1-BODY", T);
    b.wait_text("W1-BODY", T);

    a.input(&prefixed(b"2"));
    srv.wait_layout("worker-2 selected", T, |l| l["selected"]["workers"] == json!(w2));
    for v in [&mut a, &mut b] {
        v.wait_text("W2-BODY", T);
        v.wait_no_text("W1-BODY", T);
    }

    b.input(&prefixed(b"z"));
    srv.wait_layout("zoom on worker-2", T, |l| l["zoom"] == json!(w2));
    for v in [&mut a, &mut b] {
        v.wait_no_text("SUP-BODY", T);
        v.wait_status("the zoomed title", T, |s| s.contains("fx-worker-2"));
    }
}

#[test]
#[ignore = "M1: views-multi"]
fn pty_size_follows_latest_input_viewer() {
    let srv = Server::start();
    let id = sh(&srv, "fx-worker-1", "worker", "exec sleep 1000");
    let mut small = Viewer::attach(&srv, 80, 24);
    let mut big = Viewer::attach(&srv, 200, 50);
    let cols = |srv: &Server| pane_size(srv, &id).0;

    big.input(b"x");
    wait_until("worker width from the 200-column viewer", T, || {
        let c = cols(&srv);
        (90..=110).contains(&c).then_some(())
    });
    small.input(b"y");
    wait_until("worker width from the 80-column viewer", T, || {
        let c = cols(&srv);
        (30..=45).contains(&c).then_some(())
    });
}

#[test]
#[ignore = "M1: views-multi"]
fn unseen_output_marks_tab_with_dot() {
    let srv = Server::start();
    sh(&srv, "fx-worker-1", "worker", "exec sleep 1000");
    let w2 = sh(&srv, "fx-worker-2", "worker", "stty -echo; read x; echo LATE-OUT; exec sleep 1000");
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_find("fx-worker-2", T);
    assert_eq!(srv.pane(&w2)["unseen"], json!(false), "unseen before any output");

    // A `•` right before the title (within two cells).
    let dotted = |v: &mut Viewer| {
        let (row, col) = v.find("fx-worker-2")?;
        let lead: Vec<char> = (col.saturating_sub(2)..col).map(|c| v.cell(row, c).ch).collect();
        Some(lead.contains(&'•'))
    };
    srv.write(&w2, b"go\n", "raw");
    wait_until("a dot on the background tab", T, || (dotted(&mut v) == Some(true)).then_some(()));
    assert_eq!(srv.pane(&w2)["unseen"], json!(true));

    srv.ok("pane.focus", json!({"pane": w2}));
    wait_until("the dot to clear once shown", T, || (dotted(&mut v) == Some(false)).then_some(()));
    assert_eq!(srv.pane(&w2)["unseen"], json!(false));
}

#[test]
#[ignore = "M1: views-multi"]
fn rgb_downgraded_to_256_when_colorterm_absent() {
    let srv = Server::start();
    sh(&srv, "fx-worker-1", "worker", r"printf '\033[38;2;255;128;0mTRUECOLOR\033[0m\n'; exec sleep 1000");
    let mut plain = Viewer::attach_with(&srv, W, H, "");
    let mut rgb = Viewer::attach_with(&srv, W, H, "truecolor");
    let (row, col) = plain.wait_find("TRUECOLOR", T);
    rgb.wait_text("TRUECOLOR", T);

    assert!(
        rgb.raw().windows(14).any(|w| w == b"38;2;255;128;0"),
        "truecolor viewer did not get the 24-bit colour"
    );
    assert!(
        !plain.raw().windows(5).any(|w| w == b"38;2;"),
        "viewer without COLORTERM got 24-bit SGR"
    );
    assert_eq!(plain.cell(row, col).fg, Color::Indexed(214), "nearest 256-colour entry of (255,128,0)");
}

// ---- Coverage addition (fix-a-coverage-view, reviewer-1 coverage-a.md G6). ----

/// G6, design.md §7.3 M1: closing one of two viewers leaves the other one working: it still gets the panes'
/// later output and its input still reaches the focused pane.
#[test]
#[ignore = "M1: views-multi"]
fn closing_one_viewer_leaves_the_other_receiving_updates() {
    let srv = Server::start();
    let w = sh(
        &srv,
        "fx-worker-1",
        "worker",
        "stty -echo; echo W1-READY; read x; echo AFTER-CLOSE-1; read y; echo AFTER-CLOSE-2; exec sleep 1000",
    );
    srv.ok("pane.focus", json!({"pane": w}));
    let mut a = Viewer::attach(&srv, W, H);
    let mut b = Viewer::attach(&srv, 200, 50);
    a.wait_text("W1-READY", T);
    b.wait_text("W1-READY", T);

    a.detach();
    wait_until("the server to close the detached viewer", T, || a.closed().then_some(()));
    drop(a);

    srv.write(&w, b"go\n", "raw");
    b.wait_text("AFTER-CLOSE-1", T);
    b.input(b"more\r");
    b.wait_text("AFTER-CLOSE-2", T);
    assert!(!b.closed(), "the remaining viewer was closed too");
}
