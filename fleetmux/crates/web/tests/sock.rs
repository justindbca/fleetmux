//! `web.sock` and the window process (PROTOCOL.md §15). Red in W0 (`todo!()`), green in W1.
//!
//! The two window tests follow brief-m25 §5: with a display (macOS; Linux with `DISPLAY` / `WAYLAND_DISPLAY`)
//! they really open a window; without one and without `FLEETMUX_WEB_E2E=1` they print `SKIP: no display` and
//! return; with `FLEETMUX_WEB_E2E=1` a window that does not come up is a failure. They are never `#[ignore]`.
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

use fleetmux_web::sock::{self, Cmd};

/// A fresh short directory (macOS `sun_path` is 103 bytes). Outside the m4 gate: under /tmp, removed on drop.
/// In the m4 gate (`FLEETMUX_GATE=m4`, brief-m4 M56): a new directory under `$FM4_TEST_ROOT`, kept as evidence:
/// never cleaned before, never removed after, and a path that already exists is a failure.
struct Tmp(PathBuf, bool);

impl Tmp {
    fn new(tag: &str) -> Tmp {
        static N: AtomicU32 = AtomicU32::new(0);
        let n = N.fetch_add(1, Ordering::SeqCst);
        if let Some(root) = m4_root() {
            // `<dir>/w/web.sock` is the longest socket below it: 11 bytes
            let name = format!("w-{tag}-{}-{n}", std::process::id());
            return Tmp(m4_new_dir(&root, name, 11), true);
        }
        let p = PathBuf::from(format!("/tmp/fmw-{tag}-{}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir(&p).unwrap();
        Tmp(p, false)
    }
}

impl Drop for Tmp {
    fn drop(&mut self) {
        if !self.1 {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
}

/// The m4 gate runs this test (`FLEETMUX_GATE=m4`).
fn is_m4() -> bool {
    std::env::var("FLEETMUX_GATE").as_deref() == Ok("m4")
}

/// `$FM4_TEST_ROOT` in the m4 gate (which always sets it), `None` otherwise.
fn m4_root() -> Option<PathBuf> {
    if !is_m4() {
        return None;
    }
    let root = std::env::var_os("FM4_TEST_ROOT").filter(|v| !v.is_empty());
    Some(PathBuf::from(root.expect(
        "FLEETMUX_GATE=m4 needs FM4_TEST_ROOT (scripts/fleetmux_gate.sh sets it)",
    )))
}

/// The m4 gate's socket budget (M58, `scripts/fleetmux_gate.sh`): `M4E_ROOT` is at most 103 - `M4_SUFFIX_MAX` (64)
/// = 39 bytes and `FM4_TEST_ROOT` is `$M4E_ROOT/r.XXXXXX` (9 more), so whatever root a run has, `/<name>` plus
/// the longest socket below it must fit 103 - 39 - 9 = 55 bytes.
const M4_BELOW_TEST_ROOT: usize = 103 - (103 - 64) - "/r.XXXXXX".len();

/// Digits of a pid on this OS (macOS pids stay below 100000; Linux `pid_max` can reach 4194304).
const PID_DIGITS: usize = if cfg!(target_os = "macos") { 5 } else { 7 };

/// A new directory `<root>/<name>` whose sockets (`sock_suffix` bytes below it) fit `sun_path`; it must not exist.
fn m4_new_dir(root: &std::path::Path, name: String, sock_suffix: usize) -> PathBuf {
    // M58: the name holds this process's pid; count it at the most digits this OS allows
    let pid_len = std::process::id().to_string().len();
    let worst = 1 + name.len() + PID_DIGITS.saturating_sub(pid_len) + sock_suffix;
    assert!(
        worst <= M4_BELOW_TEST_ROOT,
        "m4 evidence name {name} gives up to {worst} bytes below FM4_TEST_ROOT (> {M4_BELOW_TEST_ROOT})"
    );
    let p = root.join(name);
    let longest = p.as_os_str().len() + sock_suffix;
    assert!(
        longest <= 103,
        "m4 evidence dir {} gives {longest}-byte socket paths (> 103)",
        p.display()
    );
    std::fs::create_dir(&p)
        .unwrap_or_else(|e| panic!("m4 evidence dir {} is not new: {e}", p.display()));
    println!("m4 evidence dir: {}", p.display());
    p
}

fn mode(p: &Path) -> u32 {
    std::fs::metadata(p).unwrap().permissions().mode() & 0o777
}

/// brief-m25 §5: should a window test run here?
fn display_or_skip() -> bool {
    let display = cfg!(target_os = "macos")
        || std::env::var_os("DISPLAY").is_some_and(|v| !v.is_empty())
        || std::env::var_os("WAYLAND_DISPLAY").is_some_and(|v| !v.is_empty());
    if display || std::env::var("FLEETMUX_WEB_E2E").as_deref() == Ok("1") {
        return true;
    }
    println!("SKIP: no display");
    false
}

/// A `fleetmux-web` child for one window test; killed and reaped on drop.
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

    /// Connected to `web.sock` within 5 s, or panic with how the child ended.
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

    fn wait_exit(&mut self, within: Duration) -> Option<std::process::ExitStatus> {
        let end = Instant::now() + within;
        while Instant::now() < end {
            if let Some(st) = self.child.try_wait().unwrap() {
                return Some(st);
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        None
    }
}

impl Drop for Window {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// One request line, one reply line (PROTOCOL.md §15.3).
fn ask(stream: &mut UnixStream, line: &str) -> String {
    stream.write_all(format!("{line}\n").as_bytes()).unwrap();
    let mut reply = String::new();
    BufReader::new(stream.try_clone().unwrap())
        .read_line(&mut reply)
        .unwrap();
    reply.trim_end().to_string()
}

/// The string value of `"key":"…"` in a one-line JSON object (no escapes in the URLs used here).
fn json_str(line: &str, key: &str) -> Option<String> {
    let pat = format!("\"{key}\":\"");
    let start = line.find(&pat)? + pat.len();
    Some(line[start..].split('"').next()?.to_string())
}

fn ok(line: &str) -> bool {
    line.replace(' ', "").contains("\"ok\":true")
}

#[test]
#[ignore = "M2.5: web-window"]
fn sock_dir_0700_sock_0600() {
    let t = Tmp::new("perm");
    let dir = t.0.join("w");
    let listener = sock::bind(&dir).expect("bind web.sock");
    assert_eq!(mode(&dir), 0o700);
    assert_eq!(mode(&sock::sock_path(&dir)), 0o600);
    sock::check_dir(&dir).expect("its own directory passes the check");
    drop(listener);
    // A group/other-accessible directory is refused.
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
    assert!(sock::check_dir(&dir).is_err());
    assert_eq!(
        sock::parse_line(r#"{"cmd":"open","url":"about:blank"}"#),
        Ok(Cmd::Open("about:blank".into()))
    );
    assert_eq!(sock::parse_line(r#"{"cmd":"url"}"#), Ok(Cmd::Url));
    assert!(sock::parse_line(r#"{"cmd":"eval","js":"1"}"#).is_err());
}

#[test]
#[ignore = "M2.5: web-window"]
fn sock_rejects_other_uid() {
    // The pure half (brief-m25 §3.1): a real second uid is checked on the VM (brief-m03 risk #10).
    let t = Tmp::new("uid");
    let own = std::fs::metadata(&t.0).unwrap().uid();
    assert!(sock::peer_allowed(own, own));
    assert!(!sock::peer_allowed(own.wrapping_add(1), own));
    assert!(!sock::peer_allowed(0, own.max(1)));
}

#[test]
#[ignore = "M2.5: web-window"]
fn sock_path_too_long_fails() {
    let t = Tmp::new("long");
    let room = sock::MAX_SOCK_PATH - t.0.as_os_str().len();
    let dir = t.0.join("d".repeat(room)); // `<dir>/web.sock` is then over the limit
    assert!(sock::sock_path(&dir).as_os_str().len() > sock::MAX_SOCK_PATH);
    assert!(sock::bind(&dir).is_err());
    assert!(!sock::sock_path(&dir).exists());
}

#[test]
fn pipe_eof_exits_within_1s() {
    if !display_or_skip() {
        return;
    }
    let t = Tmp::new("eof");
    let run = t.0.join("run");
    std::fs::create_dir(&run).unwrap();
    let mut w = Window::start(&t.0.join("w"), &run);
    let _s = w.connect();
    drop(w.child.stdin.take()); // the client's end of the lifeline goes away
    let started = Instant::now();
    let st = w.wait_exit(Duration::from_secs(1));
    assert!(
        st.is_some(),
        "fleetmux-web still running {:?} after stdin EOF",
        started.elapsed()
    );
}

#[test]
fn open_goto_url_reload_quit_roundtrip() {
    if !display_or_skip() {
        return;
    }
    let t = Tmp::new("rt");
    let run = t.0.join("run");
    std::fs::create_dir(&run).unwrap();
    let page = run.join("history.html");
    std::fs::write(&page, "<html><title>h</title><body>history</body></html>").unwrap();
    let url = format!("file://{}", page.display());
    let mut w = Window::start(&t.0.join("w"), &run);
    let mut s = w.connect();
    assert!(ok(&ask(
        &mut s,
        &format!(r#"{{"cmd":"open","url":"{url}"}}"#)
    )));
    let got = ask(&mut s, r#"{"cmd":"url"}"#);
    assert_eq!(
        json_str(&got, "url").as_deref(),
        Some(url.as_str()),
        "{got}"
    );
    let with_query = format!("{url}?x=1");
    assert!(ok(&ask(
        &mut s,
        &format!(r#"{{"cmd":"goto","url":"{with_query}"}}"#)
    )));
    let got = ask(&mut s, r#"{"cmd":"url"}"#);
    assert_eq!(
        json_str(&got, "url").as_deref(),
        Some(with_query.as_str()),
        "{got}"
    );
    assert!(ok(&ask(&mut s, r#"{"cmd":"reload"}"#)));
    // A URL the allow-list refuses is answered ok:false and changes nothing.
    assert!(!ok(&ask(
        &mut s,
        r#"{"cmd":"goto","url":"file:///etc/passwd"}"#
    )));
    assert!(ok(&ask(&mut s, r#"{"cmd":"quit"}"#)));
    assert!(
        w.wait_exit(Duration::from_secs(2)).is_some(),
        "fleetmux-web still running after quit"
    );
    assert!(
        UnixStream::connect(sock::sock_path(&w.dir)).is_err(),
        "web.sock still answers after quit"
    );
}

/// fix-m25-1 D1: a page that rewrites its own URL while it loads (the real `history.html` does
/// `location.replace('#…')`; a page may also `history.replaceState` its query). `open` / `goto` must still be
/// answered as soon as their page has loaded — well inside the client's 2 s wait — and `url` must then read the
/// page's real current URL (query and fragment as the page left them, not as requested). A fragment-only `goto`
/// on the loaded page (a same-document navigation, no load events) is answered promptly too, and a `goto` sent
/// while an earlier `open` is still loading is answered for its own page, not the earlier one's.
#[test]
fn open_goto_answer_promptly_when_page_rewrites_its_url() {
    if !display_or_skip() {
        return;
    }
    const PROMPT: Duration = Duration::from_millis(1500);
    let t = Tmp::new("rw");
    let run = t.0.join("run");
    std::fs::create_dir(&run).unwrap();
    let page = run.join("history.html");
    std::fs::write(
        &page,
        "<html><head><title>rw</title><script>\
         var q = location.search ? location.search + '&loaded=1' : '?loaded=1';\
         history.replaceState(null, '', location.pathname + q + location.hash);\
         location.replace('#view=note');\
         </script></head><body>rewrites its URL while loading</body></html>",
    )
    .unwrap();
    let url = format!("file://{}", page.display());
    let mut w = Window::start(&t.0.join("w"), &run);
    let mut s = w.connect();

    let timed = |s: &mut UnixStream, line: String| {
        let started = Instant::now();
        let reply = ask(s, &line);
        (reply, started.elapsed())
    };

    let (r, took) = timed(&mut s, format!(r#"{{"cmd":"open","url":"{url}"}}"#));
    assert!(ok(&r), "{r}");
    assert!(took < PROMPT, "open answered after {took:?}");
    let got = ask(&mut s, r#"{"cmd":"url"}"#);
    assert_eq!(
        json_str(&got, "url").as_deref(),
        Some(format!("{url}?loaded=1#view=note").as_str()),
        "{got}"
    );

    let (r, took) = timed(
        &mut s,
        format!(r#"{{"cmd":"goto","url":"{url}?x=1#agent=a&view=log"}}"#),
    );
    assert!(ok(&r), "{r}");
    assert!(took < PROMPT, "goto answered after {took:?}");
    let got = ask(&mut s, r#"{"cmd":"url"}"#);
    let loaded = format!("{url}?x=1&loaded=1#view=note");
    assert_eq!(
        json_str(&got, "url").as_deref(),
        Some(loaded.as_str()),
        "{got}"
    );

    // Fragment-only goto on the loaded page: same document, no page load.
    let same_doc = format!("{url}?x=1&loaded=1#agent=z");
    let (r, took) = timed(&mut s, format!(r#"{{"cmd":"goto","url":"{same_doc}"}}"#));
    assert!(ok(&r), "{r}");
    assert!(took < PROMPT, "fragment goto answered after {took:?}");
    let got = ask(&mut s, r#"{"cmd":"url"}"#);
    assert_eq!(
        json_str(&got, "url").as_deref(),
        Some(same_doc.as_str()),
        "{got}"
    );

    // An `open` still loading when a `goto` arrives (second connection): the goto's answer is for its own page.
    let mut s2 = w.connect();
    s.write_all(format!("{{\"cmd\":\"open\",\"url\":\"{url}?x=1\"}}\n").as_bytes())
        .unwrap();
    let (r, took) = timed(&mut s2, format!(r#"{{"cmd":"goto","url":"{url}?x=2"}}"#));
    assert!(ok(&r), "{r}");
    assert!(took < PROMPT, "goto after open answered after {took:?}");
    let got = ask(&mut s2, r#"{"cmd":"url"}"#);
    let second = format!("{url}?x=2&loaded=1#view=note");
    assert_eq!(
        json_str(&got, "url").as_deref(),
        Some(second.as_str()),
        "{got}"
    );
    let mut first = String::new();
    BufReader::new(s.try_clone().unwrap())
        .read_line(&mut first)
        .unwrap();
    assert!(ok(&first), "the superseded open is still answered: {first}");

    assert!(ok(&ask(&mut s2, r#"{"cmd":"quit"}"#)));
    assert!(w.wait_exit(Duration::from_secs(2)).is_some());
}
