//! ui.language (docs/design.md §63): a server started with `FLEETMUX_UI_LANGUAGE=en` draws its topbar, status
//! line, sidebar cards and placeholder in English — no CJK character on the screen — and one started with
//! `zh-Hans` draws the texts it always did (render.rs / mouse.rs keep checking those).
mod common;
use common::*;

const T: Duration = Duration::from_secs(10);

/// What Python pushes in English (fleetmux_sidebar.payload with ui.language = en).
const ONE_EN: &str = r#"{"v":1,"current":"fleet-20261029-231820-ab12cd34","fleets":[{"id":"fleet-20261029-231820-ab12cd34","title":"fleetmux-demo","state":"running","line1":"running, workers 2, busy 1","line2":"~/.fleetmux/fleets/fmx-demo"}]}"#;
const CLOSED_EN: &str = r#"{"v":1,"current":"fleet-20261030-010101-ab12cd34","fleets":[{"id":"fleet-20261030-010101-ab12cd34","title":"fleetmux-demo","state":"closed","line1":"closed, restorable","line2":"~/.fleetmux/fleets/fmx-demo"}]}"#;

fn is_cjk(c: char) -> bool {
    matches!(c as u32, 0x3000..=0x303f | 0x3040..=0x30ff | 0x3400..=0x4dbf | 0x4e00..=0x9fff | 0xff00..=0xffef)
}

fn english(srv_env: &str) -> Server {
    Server::start_with_env(&[("FLEETMUX_NATIVE_HEADER", "0"), ("FLEETMUX_UI_LANGUAGE", srv_env)])
}

fn page(srv: &Server) {
    sh(srv, "fx-supervisor-1", "supervisor", "echo SUP-BODY; exec sleep 1000");
    sh(srv, "fx-worker-1", "worker", "echo W1-BODY; exec sleep 1000");
}

#[test]
fn english_status_line_and_sidebar_have_no_cjk() {
    let srv = english("en");
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    srv.ok("kv.set", json!({ "key": "sidebar", "value": ONE_EN }));
    let screen = v.wait_text("fleetmux-demo", T);
    let status = v.status_line();
    assert!(status.contains("^b s hide sidebar"), "status line: {status:?}\n{screen}");
    assert!(v.row_text(0).contains("running, workers 2, busy 1"), "topbar: {:?}", v.row_text(0));
    let cjk: Vec<char> = screen.chars().filter(|c| is_cjk(*c)).collect();
    assert!(cjk.is_empty(), "CJK on the English screen: {cjk:?}\n{screen}");
}

#[test]
fn english_closed_placeholder_has_no_cjk() {
    let srv = english("en");
    let mut v = Viewer::attach(&srv, W, H);
    srv.ok("kv.set", json!({ "key": "sidebar", "value": CLOSED_EN }));
    let screen = v.wait_text("fleet \"fleetmux-demo\" closed", T);
    assert!(screen.contains("restore button on the card"), "{screen}");
    let cjk: Vec<char> = screen.chars().filter(|c| is_cjk(*c)).collect();
    assert!(cjk.is_empty(), "CJK on the English placeholder: {cjk:?}\n{screen}");
}

#[test]
fn an_unknown_or_missing_value_is_english() {
    for value in ["", "fr", "zh"] {
        let srv = english(value);
        page(&srv);
        let mut v = Viewer::attach(&srv, W, H);
        v.wait_text("W1-BODY", T);
        srv.ok("kv.set", json!({ "key": "sidebar", "value": ONE_EN }));
        let screen = v.wait_text("fleetmux-demo", T);
        assert!(v.status_line().contains("hide sidebar"), "{value:?}: {screen}");
    }
}

#[test]
fn zh_hans_status_line_is_unchanged() {
    let srv = english("zh-Hans");
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    srv.ok("kv.set", json!({ "key": "sidebar", "value": ONE_EN }));
    let screen = v.wait_text("fleetmux-demo", T);
    assert!(v.status_line().contains("^b s 隐藏侧栏"), "{screen}");
}

#[test]
fn panes_do_not_inherit_the_server_language() {
    let srv = english("en");
    sh(&srv, "fx-env", "worker", "echo LANGVAR=[${FLEETMUX_UI_LANGUAGE:-unset}]; exec sleep 1000");
    let mut v = Viewer::attach(&srv, W, H);
    let screen = v.wait_text("LANGVAR=[", T);
    assert!(screen.contains("LANGVAR=[unset]"), "{screen}");
}

