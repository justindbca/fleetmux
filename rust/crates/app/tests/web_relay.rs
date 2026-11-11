//! M2.5 `web.*` relay (PROTOCOL.md §4.18–§4.21, §6.3 `0x84` / `0x04`, §15; design.md §3.5.3–§3.5.6). Red in W0
//! (`todo!()`): the first five are the server side (W2), the last three the attach side (W3).
//!
//! The server-side tests stand in for the attach client with [`WebViewer`]: a raw view connection that writes
//! `0x04` frames by hand and collects every `0x84` payload, so they test the server, not a client implementation.
mod common;

use std::io::{Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::mpsc;
use std::time::{Duration, Instant, UNIX_EPOCH};

use common::{Server, TempDir};
use fleetmux::attach::web::{self as aweb, OpenEnv, OpenStep};
use fleetmux::server::web::{self as sweb, PageStat};
use serde_json::{Value, json};

/// A view connection that answers for a web window (PROTOCOL.md §6): handshake, then a reader thread that keeps
/// draining the socket and forwards each `0x84` payload; `0x04` frames are written by hand.
struct WebViewer {
    stream: UnixStream,
    web: mpsc::Receiver<String>,
}

impl WebViewer {
    fn attach(srv: &Server) -> WebViewer {
        let mut stream = UnixStream::connect(srv.sock()).expect("connect view");
        let hello = json!({"view": 1, "cols": 80, "rows": 24, "term": "xterm-256color", "colorterm": "truecolor"});
        stream.write_all(format!("{hello}\n").as_bytes()).unwrap();
        let mut line = Vec::new();
        let mut byte = [0u8; 1];
        while byte[0] != b'\n' {
            stream.read_exact(&mut byte).expect("view handshake reply");
            line.push(byte[0]);
        }
        let reply: Value = serde_json::from_slice(&line).unwrap();
        assert_eq!(reply["ok"], json!(true), "view handshake refused: {reply}");
        let (tx, web) = mpsc::channel();
        let mut reader = stream.try_clone().unwrap();
        std::thread::spawn(move || {
            loop {
                let mut hdr = [0u8; 5];
                if reader.read_exact(&mut hdr).is_err() {
                    return;
                }
                let len = u32::from_be_bytes([hdr[1], hdr[2], hdr[3], hdr[4]]) as usize;
                let mut payload = vec![0u8; len];
                if reader.read_exact(&mut payload).is_err() {
                    return;
                }
                if hdr[0] == 0x84
                    && tx
                        .send(String::from_utf8_lossy(&payload).into_owned())
                        .is_err()
                {
                    return;
                }
            }
        });
        WebViewer { stream, web }
    }

    /// Send one `0x04` line.
    fn send(&mut self, v: Value) {
        let payload = v.to_string();
        let mut frame = vec![0x04];
        frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        frame.extend_from_slice(payload.as_bytes());
        self.stream.write_all(&frame).unwrap();
    }

    /// Report that this client's window is open, and give the server a moment to read it before the control
    /// request that depends on it (the two arrive on different connections).
    fn window_open(&mut self) {
        self.send(json!({"event": "window", "state": "open"}));
        std::thread::sleep(Duration::from_millis(300));
    }

    /// The next `0x84` as JSON, or `None` after `timeout`.
    fn next(&self, timeout: Duration) -> Option<Value> {
        let line = self.web.recv_timeout(timeout).ok()?;
        Some(
            serde_json::from_str(&line)
                .unwrap_or_else(|e| panic!("0x84 is not JSON ({e}): {line}")),
        )
    }
}

/// Run one control request on its own thread (the server holds the reply until the windows answer).
fn request_async(srv: &Server, cmd: &'static str, args: Value) -> std::thread::JoinHandle<Value> {
    let mut c = srv.client();
    std::thread::spawn(move || c.request(cmd, args))
}

#[test]
#[ignore = "M2.5: web-relay"]
fn web_goto_broadcasts_to_viewers_with_window() {
    let srv = Server::start();
    let mut a = WebViewer::attach(&srv);
    let mut b = WebViewer::attach(&srv);
    let none = WebViewer::attach(&srv); // a viewer that never reports a window
    a.window_open();
    b.window_open();
    let url = "file:///r/history.html?x=1";
    let pending = request_async(&srv, "web.goto", json!({"url": url}));
    for v in [&mut a, &mut b] {
        let cmd = v
            .next(Duration::from_secs(3))
            .expect("a viewer with a window gets 0x84 goto");
        assert_eq!(
            (cmd["op"].as_str(), cmd["url"].as_str()),
            (Some("goto"), Some(url)),
            "{cmd}"
        );
        v.send(json!({"seq": cmd["seq"], "ok": true}));
    }
    let reply = pending.join().unwrap();
    assert_eq!(reply["ok"], json!(true), "{reply}");
    assert_eq!(reply["windows"], json!(2), "{reply}");
    assert!(
        none.next(Duration::from_millis(300)).is_none(),
        "a viewer without a window got a 0x84"
    );
}

#[test]
#[ignore = "M2.5: web-relay"]
fn web_url_from_primary_window() {
    let srv = Server::start();
    let mut first = WebViewer::attach(&srv);
    let mut last = WebViewer::attach(&srv);
    first.window_open();
    last.window_open(); // most recent `open` = primary
    let pending = request_async(&srv, "web.url", json!({}));
    let cmd = last
        .next(Duration::from_secs(3))
        .expect("the primary window is asked");
    assert_eq!(cmd["op"], json!("url"), "{cmd}");
    last.send(json!({"seq": cmd["seq"], "ok": true, "url": "file:///b/history.html?x=2"}));
    let reply = pending.join().unwrap();
    assert_eq!(reply["url"], json!("file:///b/history.html?x=2"), "{reply}");
    assert!(
        first.next(Duration::from_millis(300)).is_none(),
        "only the primary window is asked"
    );
}

#[test]
#[ignore = "M2.5: web-relay"]
fn no_window_is_error() {
    let srv = Server::start();
    assert_eq!(
        srv.err("web.goto", json!({"url": "file:///r/history.html"})),
        "no_window"
    );
    assert_eq!(srv.err("web.url", json!({})), "no_window");
    let _plain = WebViewer::attach(&srv); // attached, but no window
    assert_eq!(srv.err("web.url", json!({})), "no_window");
    assert_eq!(srv.ok("web.reload", json!({}))["windows"], json!(0)); // reload with no window is not an error
    assert_eq!(srv.err("web.eval", json!({"js": "1"})), "not_implemented");
}

#[test]
#[ignore = "M2.5: web-relay"]
fn web_timeout_after_3s() {
    let srv = Server::start();
    let mut mute = WebViewer::attach(&srv);
    mute.window_open();
    let started = Instant::now();
    assert_eq!(srv.err("web.url", json!({})), "web_timeout");
    let took = started.elapsed();
    assert!(
        (Duration::from_millis(2900)..Duration::from_millis(4500)).contains(&took),
        "web_timeout after {took:?}, want about 3 s"
    );
    assert!(
        mute.next(Duration::from_millis(10)).is_some(),
        "the request was relayed before timing out"
    );
}

#[test]
#[ignore = "M2.5: web-relay"]
fn reload_pushes_page_only_when_changed() {
    let tmp = TempDir::new();
    let page = tmp.path().join("history.html");
    std::fs::write(&page, "<html>v1</html>").unwrap();
    let have = sweb::stat_page(&page).unwrap();
    assert_eq!(have.size, 15);
    // The client's copy matches: no bytes.
    let msg = sweb::page_for(&page, Some(have)).unwrap();
    assert_eq!(
        (msg.size, msg.mtime, msg.b64.as_deref()),
        (have.size, have.mtime, None)
    );
    assert_eq!(msg.path, page.to_str().unwrap());
    // No copy, or a different one: the whole file.
    assert_eq!(
        sweb::page_for(&page, None).unwrap().b64.as_deref(),
        Some(common::b64(b"<html>v1</html>").as_str())
    );
    let stale = PageStat {
        size: have.size,
        mtime: have.mtime - 1,
    };
    assert!(sweb::page_for(&page, Some(stale)).unwrap().b64.is_some());
    std::fs::write(&page, "<html>version 2</html>").unwrap();
    assert!(sweb::page_for(&page, Some(have)).unwrap().b64.is_some());
    assert!(sweb::page_for(&tmp.path().join("missing.html"), None).is_err());
}

fn mtime_ns(p: &std::path::Path) -> i64 {
    let m = std::fs::metadata(p).unwrap().modified().unwrap();
    m.duration_since(UNIX_EPOCH).unwrap().as_nanos() as i64
}

#[test]
#[ignore = "M2.5: web-attach"]
fn same_host_detection_uses_size_and_mtime() {
    let tmp = TempDir::new();
    let page = tmp.path().join("history.html");
    std::fs::write(&page, "<html>x</html>").unwrap();
    let (size, mtime) = (14, mtime_ns(&page));
    assert!(aweb::same_host(&page, size, mtime));
    assert!(!aweb::same_host(&page, size + 1, mtime));
    assert!(!aweb::same_host(&page, size, mtime + 1));
    assert!(!aweb::same_host(&tmp.path().join("nope.html"), size, mtime));
}

#[test]
#[ignore = "M2.5: web-attach"]
fn cross_host_page_written_0600_under_web_dir() {
    let tmp = TempDir::new();
    let dir = aweb::web_dir(tmp.path(), "0123456789abcdef0123456789abcdef01234567", 4242);
    assert_eq!(
        dir,
        tmp.path()
            .join("fleetmux-web")
            .join("0123456789abcdef01234567-4242")
    );
    std::fs::create_dir_all(&dir).unwrap();
    let path = aweb::write_page(&dir, b"<html>pushed</html>").unwrap();
    assert_eq!(path, dir.join("page").join("history.html"));
    assert_eq!(std::fs::read(&path).unwrap(), b"<html>pushed</html>");
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    // A newer push replaces it in place.
    assert_eq!(aweb::write_page(&dir, b"v2").unwrap(), path);
    assert_eq!(std::fs::read(&path).unwrap(), b"v2");
}

#[test]
#[ignore = "M2.5: web-attach"]
fn prefix_o_tries_window_then_opener_then_path() {
    let bin = Some(PathBuf::from("/opt/fleetmux/fleetmux-web"));
    let env = |display, web_bin: Option<PathBuf>, version_ok| OpenEnv {
        display,
        web_bin,
        version_ok,
    };
    use OpenStep::{Opener, ShowPath, Window};
    assert_eq!(
        aweb::open_order(&env(true, bin.clone(), true)),
        [Window, Opener, ShowPath]
    );
    assert_eq!(
        aweb::open_order(&env(false, bin.clone(), true)),
        [Opener, ShowPath]
    );
    assert_eq!(aweb::open_order(&env(true, None, true)), [Opener, ShowPath]);
    assert_eq!(aweb::open_order(&env(true, bin, false)), [Opener, ShowPath]);
    let vars = |set: &'static [(&'static str, &'static str)]| {
        move |k: &str| {
            set.iter()
                .find(|(n, _)| *n == k)
                .map(|(_, v)| v.to_string())
        }
    };
    assert!(aweb::has_display("macos", vars(&[])));
    assert!(aweb::has_display("linux", vars(&[("DISPLAY", ":0")])));
    assert!(aweb::has_display(
        "linux",
        vars(&[("WAYLAND_DISPLAY", "wayland-0")])
    ));
    assert!(!aweb::has_display("linux", vars(&[])));
    assert!(!aweb::has_display("linux", vars(&[("DISPLAY", "")])));
}
