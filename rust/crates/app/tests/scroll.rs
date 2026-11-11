//! Page mode and copy (brief-m03 §3.4 `tests/scroll.rs`, PROTOCOL.md §12 / §13, plan.md §5 copy-scroll).
//! M1: `#[ignore = "M1: copy-scroll"]` until B15. Panes are plain shells (inner mouse reporting off).
mod common;
use common::*;

const T: Duration = Duration::from_secs(10);
const PGUP: &[u8] = b"\x1b[5~";
const PGDN: &[u8] = b"\x1b[6~";

/// A focused worker that printed `L1` … `L200` (line 42 replaced by `NEEDLE-HERE`) and then echoes its input.
fn long_pane(srv: &Server) -> String {
    let id = sh(
        srv,
        "fx-worker-1",
        "worker",
        "for i in $(seq 1 200); do if [ $i = 42 ]; then echo NEEDLE-HERE; else echo L$i; fi; done; exec cat",
    );
    srv.ok("pane.focus", json!({"pane": id}));
    id
}

/// The offset shown as `[offset/scrollback]` on the status line, if any.
fn page_offset(status: &str) -> Option<u64> {
    let start = status.find('[')? + 1;
    let (num, rest) = status[start..].split_once('/')?;
    rest.contains(']').then_some(())?;
    num.trim().parse().ok()
}

/// Base64 payloads of every `ESC ] 52 ; c ; … BEL` in `raw`, decoded.
fn osc52(raw: &[u8]) -> Vec<String> {
    let text = String::from_utf8_lossy(raw);
    text.split("\x1b]52;c;")
        .skip(1)
        .filter_map(|s| s.split_once('\x07').map(|(b, _)| b.to_string()))
        .map(|b| String::from_utf8_lossy(&b64_decode(&b).unwrap_or_else(|| panic!("bad base64 {b:?}"))).into_owned())
        .collect()
}

/// A worker showing `SEL-ONE` / `SEL-TWO` on consecutive rows; returns the drag that selects both lines.
fn selection_pane(srv: &Server, v: &mut Viewer) -> ((u16, u16), (u16, u16)) {
    sh(srv, "fx-worker-1", "worker", "echo SEL-ONE; echo SEL-TWO; exec sleep 1000");
    let (r1, c1) = v.wait_find("SEL-ONE", T);
    let (r2, c2) = v.wait_find("SEL-TWO", T);
    assert_eq!(r2, r1 + 1, "fixture: lines not adjacent");
    ((c1, r1), (c2 + 6, r2))
}

#[test]
#[ignore = "M1: copy-scroll"]
fn prefix_bracket_enters_page_mode() {
    let srv = Server::start();
    long_pane(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("L200", T);
    v.input(&prefixed(b"["));
    v.wait_status("[0/<scrollback>]", T, |s| page_offset(s) == Some(0));
}

#[test]
#[ignore = "M1: copy-scroll"]
fn pgup_pgdn_and_q() {
    let srv = Server::start();
    let id = long_pane(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("L200", T);
    let (_, rows) = pane_size(&srv, &id);

    v.input(&prefixed(b"["));
    v.wait_status("page mode", T, |s| page_offset(s) == Some(0));
    v.input(PGUP);
    v.wait_status("offset rows-1 after PgUp", T, |s| page_offset(s) == Some(rows - 1));
    v.wait_no_text("L200", T);
    v.input(PGDN);
    v.wait_status("offset 0 after PgDn", T, |s| page_offset(s) == Some(0));
    v.input(PGUP);
    v.wait_status("offset rows-1 again", T, |s| page_offset(s) == Some(rows - 1));
    v.input(b"q");
    v.wait_status("page mode to end", T, |s| page_offset(s).is_none());
    v.wait_text("L200", T);
    // Keys inside page mode (including the `q` that left it) never reach the pane.
    std::thread::sleep(Duration::from_millis(300));
    let tail = srv.read(&id, 3);
    assert!(!tail.lines().any(|l| l.trim() == "q" || l.contains("[5~")), "page-mode keys leaked into the pane:\n{tail}");
}

#[test]
#[ignore = "M1: copy-scroll"]
fn slash_search_jumps() {
    let srv = Server::start();
    long_pane(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    let before = v.wait_text("L200", T);
    assert!(!before.contains("NEEDLE-HERE"), "fixture: the needle must start off screen");

    v.input(&prefixed(b"["));
    v.wait_status("page mode", T, |s| page_offset(s) == Some(0));
    v.input(b"/NEEDLE-HERE\r");
    v.wait_text("NEEDLE-HERE", T);
    let status = v.status_line();
    assert!(page_offset(&status).is_some_and(|o| o > 0), "search did not scroll back: {status:?}");
}

#[test]
#[ignore = "M1: copy-scroll"]
fn drag_select_copies_text() {
    let srv = Server::start();
    let mut v = Viewer::attach(&srv, W, H);
    let (from, to) = selection_pane(&srv, &mut v);
    v.input(&sgr_drag(from, to, 0));
    v.wait_raw(b"\x1b]52;c;", T);
    let copies = osc52(&v.raw());
    assert_eq!(copies.last().map(String::as_str), Some("SEL-ONE\nSEL-TWO"), "{copies:?}");
}

#[test]
#[ignore = "M1: copy-scroll"]
fn osc52_emitted_and_local_clipboard_called() {
    // Fake clipboard tools first on the server's PATH; whichever one the platform picks writes the file.
    let fake = TempDir::new();
    let clip = fake.path().join("clip.txt");
    for tool in ["pbcopy", "wl-copy", "xclip"] {
        let path = fake.path().join(tool);
        std::fs::write(&path, format!("#!/bin/sh\ncat > '{}'\n", clip.display())).unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    let path = format!("{}:{}", fake.path().display(), std::env::var("PATH").unwrap_or_default());
    let srv = Server::start_with_env(&[("PATH", &path), ("DISPLAY", ":0")]);
    let mut v = Viewer::attach(&srv, W, H);
    let (from, to) = selection_pane(&srv, &mut v);
    v.input(&sgr_drag(from, to, 0));
    v.wait_raw(b"\x1b]52;c;", T);
    assert_eq!(osc52(&v.raw()).last().map(String::as_str), Some("SEL-ONE\nSEL-TWO"));
    wait_file_eq(&clip, b"SEL-ONE\nSEL-TWO", T);
}

/// Waits for the `n`-th OSC 52 copy and returns it.
fn wait_copy(v: &Viewer, n: usize) -> String {
    wait_until(&format!("OSC 52 copy #{n}"), T, || osc52(&v.raw()).get(n - 1).cloned())
}

/// Waits until the viewer's cell (`row`, `col`) is drawn reverse-video (`on`) or not.
fn wait_inverse(v: &mut Viewer, (row, col): (u16, u16), on: bool, what: &str) {
    wait_until(what, T, || (v.cell(row, col).attrs.inverse == on).then_some(()));
}

/// A worker showing `pick me:now ok`; returns the viewer cell of its `p`.
fn word_pane(srv: &Server, v: &mut Viewer) -> (u16, u16) {
    sh(srv, "fx-worker-1", "worker", "echo 'pick me:now ok'; exec sleep 1000");
    v.wait_find("pick me:now ok", T)
}

#[test]
#[ignore = "M1: copy-scroll"]
fn double_click_copies_and_highlights_the_word() {
    let srv = Server::start();
    let mut v = Viewer::attach(&srv, W, H);
    let (row, col) = word_pane(&srv, &mut v);
    // `me` sits at columns col+5..col+6; ':' ends it (Ghostty's word boundaries).
    let mut twice = sgr_click(col + 6, row, 0);
    twice.extend(sgr_click(col + 6, row, 0));
    v.input(&twice);
    assert_eq!(wait_copy(&v, 1), "me");
    wait_inverse(&mut v, (row, col + 5), true, "the word `me` highlighted");
    assert!(!v.cell(row, col + 4).attrs.inverse, "the space before the word is highlighted too");
    assert!(!v.cell(row, col + 7).attrs.inverse, "the ':' after the word is highlighted too");
    // A single click anywhere drops the highlight (and copies nothing). After DOUBLE_CLICK, or a press on this
    // row would be the third of a triple click.
    std::thread::sleep(Duration::from_millis(400));
    v.input(&sgr_click(col + 12, row, 0));
    wait_inverse(&mut v, (row, col + 5), false, "the highlight cleared by the next click");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(osc52(&v.raw()).len(), 1, "a single click copied something");
}

#[test]
#[ignore = "M1: copy-scroll"]
fn triple_click_copies_the_line_and_typing_clears_it() {
    let srv = Server::start();
    let mut v = Viewer::attach(&srv, W, H);
    let (row, col) = word_pane(&srv, &mut v);
    let mut thrice = sgr_click(col + 1, row, 0);
    thrice.extend(sgr_click(col + 1, row, 0));
    thrice.extend(sgr_click(col + 9, row, 0));
    v.input(&thrice);
    assert_eq!(wait_copy(&v, 2), "pick me:now ok");
    wait_inverse(&mut v, (row, col + 13), true, "the whole line highlighted");
    v.input(b"x");
    wait_inverse(&mut v, (row, col + 13), false, "the highlight cleared by typing");
}

#[test]
#[ignore = "M1: copy-scroll"]
fn drag_select_stays_highlighted_after_release() {
    let srv = Server::start();
    let mut v = Viewer::attach(&srv, W, H);
    let (from, to) = selection_pane(&srv, &mut v);
    v.input(&sgr_drag(from, to, 0));
    assert_eq!(wait_copy(&v, 1), "SEL-ONE\nSEL-TWO");
    wait_inverse(&mut v, (from.1, from.0), true, "the drag selection highlighted");
    wait_inverse(&mut v, (to.1, to.0), true, "the drag selection's last cell highlighted");
}
