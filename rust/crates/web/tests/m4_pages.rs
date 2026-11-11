//! M4 fleet pages on the window side (design-1 `m4-design.md` v0.7 §4.2 "查看协议", §5.3, §8.1 `crates/web/tests/`).
//! Phase-A red tests: only existing public symbols and the existing `web.sock` / stdin frame interfaces are used,
//! so the bin compiles on `a88859ab`; all are `#[ignore = "m4"]`, run in the `m4` gate with `--include-ignored`,
//! and return early (printing `M4 not in gate`) unless `FLEETMUX_GATE=m4`.
//!
//! **Open interface (not fixed by the design, B2 decides; flagged in rust-1/m4-red-tests.md):** how the attach side
//! tells an M2.5 window that the server's view `ok` line carried `"pages":1`. This file assumes an extra key
//! `"pages": 1` on the existing `web.sock` `open` request (unknown keys are ignored today, so the test is red by
//! behaviour, not by a refused request), and that the window then reports the intercepted navigation as a
//! `web.sock` event line `{"event":"jump","url":…}` (same channel as today's `{"event":"window",…}`), which the
//! attach side forwards as `0x04`. If B2 picks another carrier, the change goes through `test-contract:`.
//!
//! The window tests open a real window like `sock.rs` / `gui.rs` (macOS; Linux with a display, or
//! `FLEETMUX_WEB_E2E=1`). Unlike those files they **fail** without a display instead of printing `SKIP` and passing,
//! so the `m4` gate cannot go green unchecked. Their directories are never deleted: `$FM4_TEST_ROOT/w<pid>-<n>`
//! (default `/tmp/fm4w`).
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use fleetmux_proto::view::{self, Frame};
use fleetmux_web::policy::{Decision, allow, allow_gui, is_fleet_jump};
use fleetmux_web::sock;
use serde_json::{Value, json};

/// Milestone gate, checked first in every test of this file: they run only when `FLEETMUX_GATE=m4`
/// (`scripts/fleetmux_gate.sh m4` exports it). The m3 / m25 / m26 gates and CI run the whole package with
/// `-- --include-ignored`; there these M4 red tests print `M4 not in gate <gate>` and return, like
/// `@milestone(4)` in the Python tests. Inside the m4 gate nothing else may skip them.
fn in_m4_gate() -> bool {
    match std::env::var("FLEETMUX_GATE") {
        Ok(g) if g == "m4" => true,
        g => {
            println!("M4 not in gate {}", g.as_deref().unwrap_or("<unset>"));
            false
        }
    }
}

/// The window tests need a display. Without one they fail (never print SKIP and pass): a skipped window test
/// would let the `m4` gate go green on a headless Linux box without checking anything.
fn require_display() {
    let display = cfg!(target_os = "macos")
        || std::env::var_os("DISPLAY").is_some_and(|v| !v.is_empty())
        || std::env::var_os("WAYLAND_DISPLAY").is_some_and(|v| !v.is_empty());
    if display || std::env::var("FLEETMUX_WEB_E2E").as_deref() == Ok("1") {
        return;
    }
    panic!(
        "no display: this M4 window test needs macOS, DISPLAY / WAYLAND_DISPLAY, or FLEETMUX_WEB_E2E=1; \
         the m4 gate does not skip it"
    );
}

/// A kept test directory (never removed: the run's evidence rule).
fn kept_dir(tag: &str) -> PathBuf {
    static N: AtomicU32 = AtomicU32::new(0);
    let root = std::env::var_os("FM4_TEST_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/tmp/fm4w"));
    std::fs::create_dir_all(&root).unwrap();
    let n = N.fetch_add(1, Ordering::SeqCst);
    let p = root.join(format!("w{}-{n}", std::process::id()));
    std::fs::create_dir(&p).unwrap_or_else(|e| panic!("create {}: {e}", p.display()));
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(root.join("index.txt"))
    {
        let _ = writeln!(f, "w{}-{n} web:{tag}", std::process::id());
    }
    p
}

// ---------------------------------------------------------------- allow-list (pure)

#[test]
#[ignore = "m4"]
fn fleet_jump_url_in_a_child_run_dir_is_allowed() {
    if !in_m4_gate() {
        return;
    }
    let run = "/r/u/.cmux-fleet/runs/fleet-1";
    let page = "/tmp/fleetmux-web/abc-42/page";
    let child = format!(
        "file://{run}/sub/M0/history.html?fleet-jump=k7q2x-ab12&rel=parent&to=file://{run}/history.html"
    );
    let grandchild = format!(
        "file://{run}/sub/M0/sub/M0.1/history.html?fleet-jump=k7q2x-cd34&rel=parent&to=file://{run}/sub/M0/history.html"
    );
    for u in [&child, &grandchild] {
        assert!(is_fleet_jump(u), "{u}");
        assert_eq!(
            allow(u, Path::new(run), Path::new(page)),
            Decision::Allow,
            "{u}"
        );
    }
    let gui =
        format!("fleetmux://page{run}/sub/M0/history.html?fleet-jump=k7q2x-ab12&rel=parent&to=x");
    assert!(is_fleet_jump(&gui), "{gui}");
    assert_eq!(
        allow_gui(&gui, Some(Path::new(run)), Path::new(page)),
        Decision::Allow,
        "{gui}"
    );
}

// ---------------------------------------------------------------- M2.5 window: ?fleet-jump= interception

struct Window {
    child: Child,
    dir: PathBuf,
}

impl Window {
    fn start(dir: &Path, run_dir: &Path) -> Window {
        let child = Command::new(env!("CARGO_BIN_EXE_fleetmux-web"))
            .env("FLEETMUX_WEB_QUIET", "1") // quiet.rs: never shows or takes the focus
            .arg("--dir")
            .arg(dir)
            .arg("--run-dir")
            .arg(run_dir)
            .stdin(Stdio::piped())
            .spawn()
            .expect("spawn fleetmux-web");
        Window {
            child,
            dir: dir.to_path_buf(),
        }
    }

    fn connect(&mut self) -> UnixStream {
        let end = Instant::now() + Duration::from_secs(5);
        loop {
            if let Ok(s) = UnixStream::connect(sock::sock_path(&self.dir)) {
                s.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
                return s;
            }
            if let Some(st) = self.child.try_wait().unwrap() {
                panic!("fleetmux-web exited before web.sock answered: {st}");
            }
            assert!(Instant::now() < end, "web.sock did not answer within 5 s");
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}

impl Drop for Window {
    fn drop(&mut self) {
        // Only the window process this test started.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// One request line; the first reply line that is not an event.
fn ask(stream: &mut UnixStream, req: Value) -> Value {
    stream.write_all(format!("{req}\n").as_bytes()).unwrap();
    let mut r = BufReader::new(stream.try_clone().unwrap());
    loop {
        let mut line = String::new();
        r.read_line(&mut line).unwrap();
        let v: Value = serde_json::from_str(line.trim_end())
            .unwrap_or_else(|e| panic!("reply is not JSON ({e}): {line:?}"));
        if v.get("event").is_none() {
            return v;
        }
    }
}

/// Every event line on `ev` until one has `event == want` or `within` passes.
fn wait_event(ev: &UnixStream, want: &str, within: Duration) -> (Option<Value>, Vec<String>) {
    ev.set_read_timeout(Some(Duration::from_millis(200)))
        .unwrap();
    let mut r = BufReader::new(ev.try_clone().unwrap());
    let mut lines = Vec::new();
    let end = Instant::now() + within;
    while Instant::now() < end {
        let mut line = String::new();
        match r.read_line(&mut line) {
            Ok(0) => break,
            Ok(_) => {
                lines.push(line.trim_end().to_string());
                if let Ok(v) = serde_json::from_str::<Value>(line.trim_end()) {
                    if v["event"] == json!(want) {
                        return (Some(v), lines);
                    }
                }
            }
            Err(_) => continue,
        }
    }
    (None, lines)
}

/// `<dir>/run/history.html` that navigates itself to a `?fleet-jump=` URL shortly after loading — the same
/// `location.href = …` a click on a sub-fleet link does in the real `history.html` (fleetmux/history.py:179).
fn jumping_page(dir: &Path) -> (PathBuf, String, String) {
    let run = dir.join("run");
    std::fs::create_dir_all(run.join("sub/M0")).unwrap();
    std::fs::write(
        run.join("sub/M0/history.html"),
        "<html><body>child history</body></html>",
    )
    .unwrap();
    let page = run.join("history.html");
    let url = format!("file://{}", page.display());
    let to = format!("file://{}/sub/M0/history.html", run.display());
    let jump = format!("{url}?fleet-jump=k7q2x-ab12&rel=child&to={to}");
    std::fs::write(
        &page,
        format!(
            "<html><head><title>h</title></head><body>history page\
             <script>setTimeout(function(){{location.href={jump:?}}},400)</script></body></html>"
        ),
    )
    .unwrap();
    (run, url, jump)
}

#[test]
#[ignore = "m4"]
fn navigation_reports_fleet_jump_when_the_server_has_pages() {
    if !in_m4_gate() {
        return;
    }
    require_display();
    let d = kept_dir("navigation_reports_fleet_jump_when_the_server_has_pages");
    let (run, url, jump) = jumping_page(&d);
    let mut w = Window::start(&d.join("w"), &run);
    let mut s = w.connect();
    let ev = w.connect();
    let r = ask(&mut s, json!({"cmd": "open", "url": url, "pages": 1}));
    assert_eq!(r["ok"], json!(true), "{r}");
    let (got, lines) = wait_event(&ev, "jump", Duration::from_secs(4));
    let now = ask(&mut s, json!({"cmd": "url"}));
    let got = got.unwrap_or_else(|| {
        panic!(
            "with pages the window must report the ?fleet-jump= navigation as {{\"event\":\"jump\"}}; \
             events seen {lines:?}; window url now {now} [evidence {}]",
            d.display()
        )
    });
    assert_eq!(got["url"], json!(jump), "{got}");
    assert_eq!(
        now["url"],
        json!(url),
        "with pages the marked URL must not load: {now} [evidence {}]",
        d.display()
    );
}

/// Pins today's behaviour for a server without pages (design §4.2: "没有 pages:1 时照旧加载").
#[test]
#[ignore = "m4"]
fn navigation_loads_fleet_jump_as_before_without_pages() {
    if !in_m4_gate() {
        return;
    }
    require_display();
    let d = kept_dir("navigation_loads_fleet_jump_as_before_without_pages");
    let (run, url, jump) = jumping_page(&d);
    let mut w = Window::start(&d.join("w"), &run);
    let mut s = w.connect();
    let r = ask(&mut s, json!({"cmd": "open", "url": url}));
    assert_eq!(r["ok"], json!(true), "{r}");
    let end = Instant::now() + Duration::from_secs(4);
    loop {
        let now = ask(&mut s, json!({"cmd": "url"}));
        if now["url"] == json!(jump) {
            break;
        }
        assert!(
            Instant::now() < end,
            "without pages the ?fleet-jump= URL must load as before; url {now} [evidence {}]",
            d.display()
        );
        std::thread::sleep(Duration::from_millis(100));
    }
}

// ---------------------------------------------------------------- GUI: iframe follows the selected page's url

struct GuiWindow {
    child: Child,
    stdin: Option<ChildStdin>,
    out: mpsc::Receiver<Frame>,
    dir: PathBuf,
}

impl GuiWindow {
    fn start(dir: &Path) -> GuiWindow {
        let mut child = Command::new(env!("CARGO_BIN_EXE_fleetmux-web"))
            .env("FLEETMUX_WEB_QUIET", "1") // quiet.rs: never shows or takes the focus
            .args(["--gui", "--prefix", "C-b", "--test-hooks", "--dir"])
            .arg(dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .expect("spawn fleetmux-web --gui");
        let stdin = child.stdin.take();
        let mut stdout = child.stdout.take().unwrap();
        let (tx, out) = mpsc::channel();
        std::thread::spawn(move || {
            while let Ok(Some(f)) = view::read_frame(&mut stdout) {
                if tx.send(f).is_err() {
                    return;
                }
            }
        });
        GuiWindow {
            child,
            stdin,
            out,
            dir: dir.to_path_buf(),
        }
    }

    fn connect(&mut self) -> UnixStream {
        let end = Instant::now() + Duration::from_secs(5);
        loop {
            if let Ok(s) = UnixStream::connect(sock::sock_path(&self.dir)) {
                s.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
                return s;
            }
            if let Some(st) = self.child.try_wait().unwrap() {
                panic!("fleetmux-web --gui exited before web.sock answered: {st}");
            }
            assert!(Instant::now() < end, "web.sock did not answer within 5 s");
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn first_size(&self) -> (u16, u16) {
        match self.out.recv_timeout(Duration::from_secs(5)) {
            Ok(Frame::Resize { cols, rows }) => (cols, rows),
            other => panic!("first stdout frame is not 0x02: {other:?}"),
        }
    }

    fn feed(&mut self, f: &Frame) {
        let w = self.stdin.as_mut().expect("stdin open");
        view::write_frame(w, f).unwrap();
        w.flush().unwrap();
    }
}

impl Drop for GuiWindow {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn ask_until(s: &mut UnixStream, req: Value, what: &str, f: impl Fn(&Value) -> bool) -> Value {
    let end = Instant::now() + Duration::from_secs(5);
    loop {
        let v = ask(s, req.clone());
        if f(&v) {
            return v;
        }
        assert!(Instant::now() < end, "{what}: last reply {v}");
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// After `page.select` the server's `0x85` carries the new page's `history_url` (design §5.3); the GUI's history
/// iframe must follow it to the child run's page.
#[test]
#[ignore = "m4"]
fn gui_iframe_follows_the_overlay_url_of_the_selected_page() {
    if !in_m4_gate() {
        return;
    }
    require_display();
    let d = kept_dir("gui_iframe_follows_the_overlay_url_of_the_selected_page");
    let run = d.join("run");
    std::fs::create_dir_all(run.join("sub/M0")).unwrap();
    std::fs::write(
        run.join("history.html"),
        "<html><body>root history</body></html>",
    )
    .unwrap();
    std::fs::write(
        run.join("sub/M0/history.html"),
        "<html><body>child history</body></html>",
    )
    .unwrap();
    let root_url = format!("file://{}/history.html", run.display());
    let child_url = format!("file://{}/sub/M0/history.html", run.display());
    let mut w = GuiWindow::start(&d.join("w"));
    let mut s = w.connect();
    w.first_size();
    assert_eq!(
        ask(&mut s, json!({"cmd": "scope", "run_dir": run}))["ok"],
        json!(true)
    );
    assert_eq!(
        ask(&mut s, json!({"cmd": "open", "url": root_url}))["ok"],
        json!(true)
    );
    w.feed(&Frame::Screen(b"\x1b[2J\x1b[Hbelow".to_vec()));
    let rect = json!({"x": 2, "y": 3, "w": 10, "h": 4});
    w.feed(&Frame::Overlay(
        json!({"pane": "k7q2-3", "rect": rect, "url": root_url}).to_string(),
    ));
    ask_until(
        &mut s,
        json!({"cmd": "overlay"}),
        "root page in the iframe",
        |v| {
            v["visible"] == json!(true)
                && v["src"]
                    .as_str()
                    .is_some_and(|s| s.ends_with("/run/history.html"))
        },
    );
    // page.select M0: the next 0x85 names the child page.
    w.feed(&Frame::Overlay(
        json!({"pane": "k7q2-9", "rect": rect, "url": child_url}).to_string(),
    ));
    let o = ask_until(
        &mut s,
        json!({"cmd": "overlay"}),
        "child page in the iframe",
        |v| {
            v["src"]
                .as_str()
                .is_some_and(|s| s.contains("/run/sub/M0/history.html"))
        },
    );
    assert_eq!(o["visible"], json!(true), "{o} [evidence {}]", d.display());
}

/// Cross host (supervisor M26): the `0x85` urls name the server's paths, which do not exist on the window's host;
/// the embedded page shows the client's pushed copy in `page/`. When the server switches to a child page the window
/// must **not** load the server path itself (it would be a 404 here); the client's page exchange brings the copy
/// (`attach::web` tests `m4_cross_host_*`). The iframe keeps the copy.
#[test]
#[ignore = "m4"]
fn gui_does_not_load_a_server_path_that_is_not_on_this_host() {
    if !in_m4_gate() {
        return;
    }
    require_display();
    let d = kept_dir("gui_does_not_load_a_server_path_that_is_not_on_this_host");
    let srv = format!("/m4-srv-w-{}/run", std::process::id());
    assert!(!Path::new(&srv).exists(), "{srv} must not exist on this host");
    let root_url = format!("file://{srv}/history.html");
    let child_url = format!("file://{srv}/sub/M0/history.html");
    let wdir = d.join("w");
    let mut w = GuiWindow::start(&wdir);
    let mut s = w.connect();
    w.first_size();
    assert_eq!(
        ask(&mut s, json!({"cmd": "scope", "run_dir": srv}))["ok"],
        json!(true)
    );
    std::fs::create_dir_all(wdir.join("page")).unwrap();
    let copy = wdir.join("page/history.html");
    std::fs::write(&copy, "<html><body>pushed root copy</body></html>").unwrap();
    let copy_url = format!("file://{}", copy.display());
    assert_eq!(
        ask(&mut s, json!({"cmd": "open", "url": copy_url, "pages": 1}))["ok"],
        json!(true)
    );
    w.feed(&Frame::Screen(b"\x1b[2J\x1b[Hbelow".to_vec()));
    let rect = json!({"x": 2, "y": 3, "w": 10, "h": 4});
    w.feed(&Frame::Overlay(
        json!({"pane": "k7q2-3", "rect": rect, "url": root_url}).to_string(),
    ));
    ask_until(&mut s, json!({"cmd": "overlay"}), "the pushed copy in the iframe", |v| {
        v["visible"] == json!(true)
            && v["src"]
                .as_str()
                .is_some_and(|x| x.ends_with("/w/page/history.html"))
    });
    w.feed(&Frame::Overlay(
        json!({"pane": "k7q2-9", "rect": rect, "url": child_url}).to_string(),
    ));
    std::thread::sleep(Duration::from_millis(800));
    let o = ask(&mut s, json!({"cmd": "overlay"}));
    let src = o["src"].as_str().unwrap_or_default().to_string();
    assert!(
        src.ends_with("/w/page/history.html") && !src.contains("/m4-srv-w-"),
        "the window loaded the server path itself: {o} [evidence {}]",
        d.display()
    );
    assert_eq!(o["visible"], json!(true), "{o} [evidence {}]", d.display());
}
