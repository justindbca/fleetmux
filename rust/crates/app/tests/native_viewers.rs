//! A terminal attach and a native-header GUI share pane geometry, not window chrome.
mod common;
use common::*;
const T: Duration = Duration::from_secs(5);

#[test]
fn terminal_tools_survive_beside_a_native_gui_without_duplicate_gui_toolbar() {
    let srv = Server::start_with_env(&[("FLEETMUX_NATIVE_HEADER", "1")]);
    srv.spawn_id("supervisor-1", "supervisor", &["sh", "-c", "printf SUP-BODY; exec sleep 1000"]);
    srv.spawn_id("worker-1", "worker", &["sh", "-c", "printf WORK-BODY; exec sleep 1000"]);
    let side = json!({"v":1,"current":"test","fleets":[{
        "id":"test","title":"native-viewers","state":"running","line1":"ready","line2":"test-only"}]});
    srv.ok("kv.set", json!({"key":"sidebar","value":side.to_string()}));
    let mut terminal = Viewer::attach(&srv, 240, 60);
    let mut gui = Viewer::attach_native(&srv, 240, 60);
    terminal.wait_text("native-viewers · ready", T);
    gui.wait_text("native-viewers", T);
    assert!(terminal.row_text(0).starts_with(" ◫  +  ↺  ←  →"));
    assert!(gui.row_text(0).trim().is_empty(), "GUI has its own header");
    assert_eq!(terminal.wait_find("SUP-BODY", T), gui.wait_find("SUP-BODY", T));
    assert!(gui.row_text(1).contains("supervisor-1"));
    // A genuine terminal mouse press uses the existing toolbar action, not a synthetic prefix.
    terminal.input(b"\x1b[<0;2;1M\x1b[<0;2;1m");
    gui.wait_no_text("test-only", T);
    terminal.wait_no_text("test-only", T);
    assert!(terminal.row_text(0).starts_with(" ◫"));
    assert!(gui.row_text(0).trim().is_empty());
    terminal.input(b"\x1b[<0;2;1M\x1b[<0;2;1m");
    gui.wait_text("test-only", T);
    terminal.wait_text("test-only", T);
    terminal.input(b"\x02X");
    terminal.wait_text("关闭 fleet", T);
    gui.wait_text("关闭 fleet", T);
    terminal.input(b"n");
    terminal.wait_no_text("关闭 fleet", T);
    gui.wait_no_text("关闭 fleet", T);
    terminal.input(b"\x1b[<0;5;1M\x1b[<0;5;1m");
    terminal.wait_text("bash-1", T);
    gui.wait_text("bash-1", T);
    assert!(gui.row_text(0).trim().is_empty());
}

#[test]
fn terminal_without_sidebar_data_hits_the_drawn_tab_row() {
    let srv = Server::start_with_env(&[("FLEETMUX_NATIVE_HEADER", "1")]);
    let first = srv.spawn_id("sup-a", "supervisor", &["sh", "-c", "printf FIRST-BODY; exec sleep 1000"]);
    srv.spawn_id("sup-b", "supervisor", &["sh", "-c", "printf SECOND-BODY; exec sleep 1000"]);
    srv.ok("pane.focus", json!({"pane": first}));
    let mut terminal = Viewer::attach(&srv, 240, 60);
    let mut gui = Viewer::attach_native(&srv, 240, 60);
    terminal.wait_text("FIRST-BODY", T);
    gui.wait_text("FIRST-BODY", T);
    let (row, col) = terminal.wait_find("sup-b", T);
    assert_eq!(row, 1, "tabs are below the reserved header gap");
    let click = format!("\x1b[<0;{};{}M\x1b[<0;{};{}m", col + 1, row + 1, col + 1, row + 1);
    terminal.input(click.as_bytes());
    terminal.wait_text("SECOND-BODY", T);
    gui.wait_text("SECOND-BODY", T);
    assert!(terminal.row_text(0).trim().is_empty(), "no inert tools before sidebar metadata");
    assert!(gui.row_text(0).trim().is_empty());
}
