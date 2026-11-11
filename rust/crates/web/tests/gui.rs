//! M2.6 GUI mode, `fleetmux-web --gui` (PROTOCOL.md §15.4; design.md §3.6). Red in W0 (`todo!()` / the W0 asset
//! placeholders), green in W1.
//!
//! The first seven are pure functions. The four window tests follow brief-m26 §5 (= brief-m25 §5): with a display
//! (macOS; Linux with `DISPLAY` / `WAYLAND_DISPLAY`) they really open a window (`--gui --test-hooks`) and read the
//! result back over `web.sock` (`text` / `overlay` / `js`); without one and without `FLEETMUX_WEB_E2E=1` they print
//! `SKIP: no display` and return; with `FLEETMUX_WEB_E2E=1` a window that does not come up is a failure. They are
//! never `#[ignore]`.
use std::collections::BTreeSet;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError, mpsc};
use std::time::{Duration, Instant};

use fleetmux_proto::view::{self, Frame};
use fleetmux_web::assets;
use fleetmux_web::gui::{IpcMsg, Token, check_ipc, pipe_frame_ok};
use fleetmux_web::policy::{Decision, allow_gui, page_path, to_file_url, to_page_url};
use fleetmux_web::sock::{self, Cmd, GuiCmd, parse_gui_line};
use fleetmux_web::window::{Invocation, parse_args};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

// ---------------------------------------------------------------- pure functions

#[test]
#[ignore = "M2.6: W1"]
fn gui_policy_allows_app_and_scoped_page_only() {
    let run = Path::new("/r/run");
    let page = Path::new("/w/dir/page");
    let d = |u: &str, r: Option<&Path>| allow_gui(u, r, page);
    assert_eq!(d("fleetmux://app/index.html", None), Decision::Allow);
    assert_eq!(d("about:blank", None), Decision::Allow);
    assert_eq!(
        d("fleetmux://page/r/run/history.html?x=1#h", Some(run)),
        Decision::Allow
    );
    assert_eq!(
        d("fleetmux://page/r/run/sub/history.html", Some(run)),
        Decision::Allow
    );
    assert_eq!(
        d("fleetmux://page/w/dir/page/history.html", None),
        Decision::Allow
    );
    // Outside both directories, or no scope yet, or climbing out with `..`.
    assert_eq!(
        d("fleetmux://page/r/other/history.html", Some(run)),
        Decision::Deny
    );
    assert_eq!(
        d("fleetmux://page/r/run/history.html", None),
        Decision::Deny
    );
    assert_eq!(
        d("fleetmux://page/r/run/../other/x.html", Some(run)),
        Decision::Deny
    );
    assert_eq!(
        d("fleetmux://page/r/run-other/x.html", Some(run)),
        Decision::Deny
    );
    // Only the terminal page itself navigates in the app scheme.
    assert_eq!(d("fleetmux://app/gui.js", None), Decision::Deny);
    assert_eq!(d("fleetmux://evil/index.html", None), Decision::Deny);
    // The web goes to the system browser; file: and everything else are refused in GUI mode.
    assert_eq!(d("https://example.com/x", Some(run)), Decision::External);
    assert_eq!(d("http://example.com/", Some(run)), Decision::External);
    assert_eq!(d("file:///r/run/history.html", Some(run)), Decision::Deny);
    assert_eq!(d("javascript:alert(1)", Some(run)), Decision::Deny);
    assert_eq!(d("data:text/html,x", Some(run)), Decision::Deny);
}

#[test]
#[ignore = "M2.6: W1"]
fn page_url_and_file_url_map_both_ways() {
    for (file, page) in [
        (
            "file:///a/b/history.html",
            "fleetmux://page/a/b/history.html",
        ),
        (
            "file:///a/b/history.html?fleet-jump=x&rel=child#w-3",
            "fleetmux://page/a/b/history.html?fleet-jump=x&rel=child#w-3",
        ),
        (
            "file:///a/my%20dir/h.html#%E4%B8%AD",
            "fleetmux://page/a/my%20dir/h.html#%E4%B8%AD",
        ),
    ] {
        assert_eq!(to_page_url(file).as_deref(), Some(page), "{file}");
        assert_eq!(to_file_url(page).as_deref(), Some(file), "{page}");
    }
    assert_eq!(
        to_page_url("file://localhost/a/b.html").as_deref(),
        Some("fleetmux://page/a/b.html")
    );
    for not_local in [
        "file://host/a/b.html",
        "http://x/a",
        "about:blank",
        "/a/b.html",
    ] {
        assert_eq!(to_page_url(not_local), None, "{not_local}");
    }
    for not_page in [
        "fleetmux://app/index.html",
        "file:///a/b.html",
        "about:blank",
    ] {
        assert_eq!(to_file_url(not_page), None, "{not_page}");
    }
    // What may be served: decoded, normalised, query / fragment dropped, under the scope or page/.
    let run = Path::new("/r/run");
    let pd = Path::new("/w/dir/page");
    assert_eq!(
        page_path(
            "fleetmux://page/r/run/sub/history.html?q=1#f",
            Some(run),
            pd
        ),
        Some(PathBuf::from("/r/run/sub/history.html"))
    );
    assert_eq!(
        page_path("fleetmux://page/r/run/my%20dir/h.html", Some(run), pd),
        Some(PathBuf::from("/r/run/my dir/h.html"))
    );
    assert_eq!(
        page_path("fleetmux://page/r/run/a/../h.html", Some(run), pd),
        Some(PathBuf::from("/r/run/h.html"))
    );
    assert_eq!(
        page_path("fleetmux://page/w/dir/page/history.html", None, pd),
        Some(PathBuf::from("/w/dir/page/history.html"))
    );
    assert_eq!(
        page_path("fleetmux://page/r/run/../etc/passwd", Some(run), pd),
        None
    );
    assert_eq!(
        page_path("fleetmux://page/r/run/%2E%2E/etc/passwd", Some(run), pd),
        None
    );
    assert_eq!(page_path("fleetmux://page/etc/passwd", Some(run), pd), None);
    assert_eq!(page_path("fleetmux://page/r/run/h.html", None, pd), None);
}

#[test]
#[ignore = "M2.6: W1"]
fn ipc_without_token_is_dropped() {
    let token = Token::random().unwrap();
    let hex = token.hex();
    assert_eq!(hex.len(), 32);
    assert!(
        hex.chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()),
        "{hex}"
    );
    assert_ne!(
        Token::random().unwrap().hex(),
        hex,
        "a fresh token per window"
    );
    let msg = |v: Value| v.to_string();
    let with = |mut v: Value| {
        v["token"] = json!(hex);
        v.to_string()
    };
    // Every message shape, with the right token.
    for (body, want) in [
        (
            json!({"type": "input", "data": "eA=="}),
            IpcMsg::Input(b"x".to_vec()),
        ),
        (
            json!({"type": "resize", "cols": 120, "rows": 40}),
            IpcMsg::Resize {
                cols: 120,
                rows: 40,
            },
        ),
        (json!({"type": "ack", "seq": 7}), IpcMsg::Ack(7)),
        (
            json!({"type": "copy", "text": "hi"}),
            IpcMsg::Copy("hi".into()),
        ),
        (json!({"type": "paste"}), IpcMsg::Paste),
        (json!({"type": "history_focus"}), IpcMsg::HistoryFocus),
        (
            json!({"type": "page_url", "url": "fleetmux://page/r/h.html#x"}),
            IpcMsg::PageUrl("fleetmux://page/r/h.html#x".into()),
        ),
        (
            json!({"type": "page_loaded", "ok": true}),
            IpcMsg::PageLoaded(true),
        ),
    ] {
        assert_eq!(check_ipc(&token, &with(body.clone())), Some(want), "{body}");
        // No token, a wrong one, another window's.
        assert_eq!(check_ipc(&token, &msg(body.clone())), None, "{body}");
        let mut wrong = body.clone();
        wrong["token"] = json!("0".repeat(32));
        assert_eq!(check_ipc(&token, &msg(wrong)), None, "{body}");
        let mut other = body.clone();
        other["token"] = json!(Token::random().unwrap().hex());
        assert_eq!(check_ipc(&token, &msg(other)), None, "{body}");
    }
    // Right token, bad shape.
    for bad in [
        json!({"type": "input", "data": "not base64!"}),
        json!({"type": "resize", "cols": 0, "rows": 40}),
        json!({"type": "resize", "cols": 120, "rows": 70000}),
        json!({"type": "ack", "seq": -1}),
        json!({"type": "eval", "js": "1"}),
        json!({"type": 1}),
        json!({}),
    ] {
        assert_eq!(check_ipc(&token, &with(bad.clone())), None, "{bad}");
    }
    assert_eq!(check_ipc(&token, "not json"), None);
    assert_eq!(check_ipc(&token, &hex), None);
}

#[test]
#[ignore = "M2.6: W1"]
fn ipc_scroll_parses_pane_and_offset() {
    let token = Token::random().unwrap();
    let hex = token.hex();
    let with = |body: Value| {
        let mut v = body;
        v["token"] = json!(hex);
        v.to_string()
    };
    assert_eq!(
        check_ipc(
            &token,
            &with(json!({"type": "scroll", "pane": "k7q2-7", "offset": 57}))
        ),
        Some(IpcMsg::Scroll {
            pane: "k7q2-7".into(),
            offset: 57,
        })
    );
    assert_eq!(
        check_ipc(
            &token,
            &with(json!({"type": "scroll", "pane": "k7q2-7", "offset": 0}))
        ),
        Some(IpcMsg::Scroll {
            pane: "k7q2-7".into(),
            offset: 0,
        })
    );
    // `pane` a string and `offset` a non-negative integer, both required; no token → dropped.
    for bad in [
        json!({"type": "scroll", "pane": "k7q2-7"}),
        json!({"type": "scroll", "offset": 3}),
        json!({"type": "scroll", "pane": 7, "offset": 3}),
        json!({"type": "scroll", "pane": "k7q2-7", "offset": -1}),
        json!({"type": "scroll", "pane": "k7q2-7", "offset": 1.5}),
        json!({"type": "scroll", "pane": "k7q2-7", "offset": "3"}),
        json!({"type": "scroll"}),
    ] {
        assert_eq!(check_ipc(&token, &with(bad.clone())), None, "{bad}");
        assert_eq!(check_ipc(&token, &bad.to_string()), None, "{bad}");
    }
}

#[test]
#[ignore = "M2.6: W1"]
fn gui_sock_parses_gui_commands_and_js_only_with_test_hooks() {
    let p = |line: &str| parse_gui_line(line, false);
    assert_eq!(
        p(r#"{"cmd":"open","url":"file:///r/h.html"}"#),
        Ok(GuiCmd::Base(Cmd::Open("file:///r/h.html".into())))
    );
    assert_eq!(
        p(r#"{"cmd":"goto","url":"file:///r/h.html?x=1"}"#),
        Ok(GuiCmd::Base(Cmd::Goto("file:///r/h.html?x=1".into())))
    );
    assert_eq!(p(r#"{"cmd":"url"}"#), Ok(GuiCmd::Base(Cmd::Url)));
    assert_eq!(p(r#"{"cmd":"reload"}"#), Ok(GuiCmd::Base(Cmd::Reload)));
    assert_eq!(p(r#"{"cmd":"quit"}"#), Ok(GuiCmd::Base(Cmd::Quit)));
    assert_eq!(
        p(r#"{"cmd":"scope","run_dir":"/r/run"}"#),
        Ok(GuiCmd::Scope(PathBuf::from("/r/run")))
    );
    assert_eq!(p(r#"{"cmd":"focus"}"#), Ok(GuiCmd::Focus));
    assert_eq!(p(r#"{"cmd":"text"}"#), Ok(GuiCmd::Text));
    assert_eq!(p(r#"{"cmd":"overlay"}"#), Ok(GuiCmd::Overlay));
    // js only with --test-hooks; without it, an unknown command like any other.
    let js = r#"{"cmd":"js","code":"1+1"}"#;
    assert_eq!(parse_gui_line(js, true), Ok(GuiCmd::Js("1+1".into())));
    let err = p(js).expect_err("js without --test-hooks");
    assert!(err.contains("unknown command"), "{err}");
    // Bad requests.
    for bad in [
        r#"{"cmd":"scope"}"#,
        r#"{"cmd":"scope","run_dir":"relative/dir"}"#,
        r#"{"cmd":"open"}"#,
        r#"{"cmd":"eval","js":"1"}"#,
        r#"{"cmd":"nope"}"#,
        "not json",
    ] {
        assert!(p(bad).is_err(), "{bad}");
    }
    assert!(parse_gui_line(r#"{"cmd":"js"}"#, true).is_err());
    // The M2.5 parser is unchanged: GUI commands are not M2.5 commands.
    assert!(sock::parse_line(r#"{"cmd":"scope","run_dir":"/r"}"#).is_err());
    assert!(sock::parse_line(r#"{"cmd":"focus"}"#).is_err());
}

/// Every file `MANIFEST.json` lists, read from `crates/web/assets/xterm/`.
fn manifest() -> Value {
    serde_json::from_str(assets::MANIFEST).expect("MANIFEST.json is JSON")
}

/// The 16 ANSI colours of the cmux palette, as JSON strings (plan §2.1).
const CMUX_ANSI: [&str; 16] = [
    "#1a1a1a", "#cc372e", "#26a439", "#cdac08", "#0869cb", "#9647bf", "#479ec2", "#98989d",
    "#464646", "#ff453a", "#32d74b", "#ffd60a", "#0a84ff", "#bf5af2", "#76d6ff", "#ffffff",
];

/// A whole theme object as one `--theme` argument; `bg` is the only value the caller varies.
fn theme_arg(bg: &str) -> String {
    json!({"bg": bg, "fg": "#ffffff", "cursor": "#98989d", "ansi": CMUX_ANSI}).to_string()
}

#[test]
#[ignore = "M2.6: W1"]
fn gui_args_theme_must_be_a_valid_theme() {
    let args = |theme: &str| -> Vec<String> {
        ["--gui", "--dir", "/w", "--prefix", "C-b", "--theme", theme]
            .map(String::from)
            .to_vec()
    };
    // A whole theme becomes `GuiOpts.theme`.
    let good = theme_arg("#123456");
    match parse_args(&args(&good)).expect("a valid theme parses") {
        Invocation::Gui(o) => {
            let t = o.theme.as_ref().expect("GuiOpts.theme");
            assert_eq!(t.bg, "#123456");
            assert_eq!(t.fg, "#ffffff");
            assert_eq!(t.cursor, "#98989d");
            assert_eq!(t.ansi.len(), 16);
            assert_eq!(t.ansi[1], "#cc372e");
        }
        other => panic!("not Gui: {other:?}"),
    }
    // Uppercase in, lowercase out (the page gets one spelling).
    match parse_args(&args(&theme_arg("#ABCDEF"))).expect("uppercase parses") {
        Invocation::Gui(o) => assert_eq!(o.theme.expect("theme").bg, "#abcdef"),
        other => panic!("not Gui: {other:?}"),
    }
    // No `--theme`: no theme (xterm.js's own colours, as before).
    match parse_args(&["--gui", "--dir", "/w", "--prefix", "C-b"].map(String::from)).unwrap() {
        Invocation::Gui(o) => assert_eq!(o.theme, None),
        other => panic!("not Gui: {other:?}"),
    }
    // Bad values are usage errors (exit 2).
    for bad in [
        "not json",
        "{}",
        r##"{"bg":"blue","fg":"#ffffff","cursor":"#98989d","ansi":[]}"##,
        r##"{"bg":"#123456"}"##,
    ] {
        let e = parse_args(&args(bad)).expect_err("bad theme");
        assert!(e.contains("--theme"), "{e}");
    }
}

fn xterm_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("assets/xterm")
}

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// brief-m26 §1: the four pinned packages and their npm integrity values.
const PINNED: [(&str, &str, &str); 4] = [
    (
        "@xterm/xterm",
        "6.0.0",
        "sha512-TQwDdQGtwwDt+2cgKDLn0IRaSxYu1tSUjgKarSDkUM0ZNiSRXFpjxEsvc/Zgc5kq5omJ+V0a8/kIM2WD3sMOYg==",
    ),
    (
        "@xterm/addon-fit",
        "0.11.0",
        "sha512-jYcgT6xtVYhnhgxh3QgYDnnNMYTcf8ElbxxFzX0IZo+vabQqSPAjC3c1wJrKB5E19VwQei89QCiZZP86DCPF7g==",
    ),
    (
        "@xterm/addon-webgl",
        "0.19.0",
        "sha512-b3fMOsyLVuCeNJWxolACEUED0vm7qC0cy4wRvf3oURSzDTYVQiGPhTnhWZwIHdvC48Y+oLhvYXnY4XDXPoJo6A==",
    ),
    (
        "@xterm/addon-unicode11",
        "0.9.0",
        "sha512-FxDnYcyuXhNl+XSqGZL/t0U9eiNb/q3EWT5rYkQT/zuig8Gz/VagnQANKHdDWFM2lTMk9ly0EFQxxxtZUoRetw==",
    ),
];

#[test]
#[ignore = "M2.6: W1"]
fn assets_match_manifest_sha256() {
    let (mime, page) = assets::get("index.html").expect("the terminal page is an asset");
    assert!(mime.starts_with("text/html"), "{mime}");
    assert!(!page.is_empty());
    assert_eq!(assets::get("no-such-asset.js"), None);
    let m = manifest();
    let pkgs = m["packages"].as_array().expect("MANIFEST packages");
    let got: BTreeSet<(String, String, String)> = pkgs
        .iter()
        .map(|p| {
            (
                p["name"].as_str().unwrap().to_string(),
                p["version"].as_str().unwrap().to_string(),
                p["integrity"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    let want: BTreeSet<(String, String, String)> = PINNED
        .iter()
        .map(|(n, v, i)| (n.to_string(), v.to_string(), i.to_string()))
        .collect();
    assert_eq!(got, want, "pinned packages (brief-m26 §1)");
    let mut names = BTreeSet::new();
    for p in pkgs {
        let tarball = p["tarball"].as_str().unwrap();
        assert!(
            tarball.starts_with("https://registry.npmjs.org/@xterm/"),
            "{tarball}"
        );
        let files = p["files"].as_object().expect("files: name → sha256");
        assert!(
            files.keys().any(|f| f.starts_with("LICENSE")),
            "{}: its LICENSE is vendored",
            p["name"]
        );
        for (name, sha) in files {
            let bytes = std::fs::read(xterm_dir().join(name))
                .unwrap_or_else(|e| panic!("assets/xterm/{name}: {e}"));
            assert_eq!(
                sha256_hex(&bytes),
                sha.as_str().unwrap(),
                "assets/xterm/{name}"
            );
            if !name.starts_with("LICENSE") {
                let (_, served) = assets::get(&format!("xterm/{name}"))
                    .unwrap_or_else(|| panic!("fleetmux://app/xterm/{name} is not served"));
                assert_eq!(served, bytes.as_slice(), "served xterm/{name}");
            }
            names.insert(name.clone());
        }
    }
    for f in [
        "xterm.js",
        "xterm.css",
        "addon-fit.js",
        "addon-webgl.js",
        "addon-unicode11.js",
    ] {
        assert!(names.contains(f), "{f} is vendored (brief-m26 §1)");
    }
}

#[test]
#[ignore = "M2.6: W1"]
fn third_party_js_covers_every_vendored_package() {
    let notices = assets::THIRD_PARTY_JS;
    assert!(
        notices.contains("@xterm/xterm 6.0.0"),
        "release.yml greps this"
    );
    for p in manifest()["packages"].as_array().unwrap() {
        let name = p["name"].as_str().unwrap();
        let version = p["version"].as_str().unwrap();
        assert!(notices.contains(&format!("{name} {version}")), "{name}");
        assert!(notices.contains(p["tarball"].as_str().unwrap()), "{name}");
        for (file, _) in p["files"].as_object().unwrap() {
            if file.starts_with("LICENSE") {
                let text = std::fs::read_to_string(xterm_dir().join(file)).unwrap();
                assert!(
                    notices.contains(text.trim()),
                    "{name}: LICENSE text in full"
                );
            }
        }
    }
    assert_eq!(
        notices.matches("License: MIT").count(),
        PINNED.len(),
        "one `License: MIT` per package"
    );
}

#[test]
#[ignore = "M2.6: W1"]
fn pipe_accepts_only_screen_overlay_and_scroll_frames() {
    assert!(pipe_frame_ok(&Frame::Screen(b"x".to_vec())));
    assert!(pipe_frame_ok(&Frame::Overlay("{}".into())));
    assert!(pipe_frame_ok(&Frame::Scroll("{\"panes\":[]}".into())));
    for f in [
        Frame::Input(b"x".to_vec()),
        Frame::Resize { cols: 80, rows: 24 },
        Frame::Detach,
        Frame::Exit(String::new()),
        Frame::Open("/x".into()),
        Frame::Web("{}".into()),
        Frame::WebReply("{}".into()),
        Frame::ScrollTo("{\"pane\":\"p\",\"offset\":1}".into()),
    ] {
        assert!(!pipe_frame_ok(&f), "{f:?}");
    }
}

// ---------------------------------------------------------------- the window (graphical, brief-m26 §5)

/// brief-m26 §5: should a window test run here?
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

/// A fresh short directory (macOS `sun_path` is 103 bytes). Outside the m4 gate: under /tmp, removed on drop.
/// In the m4 gate (`FLEETMUX_GATE=m4`, brief-m4 M56): a new directory under `$FM4_TEST_ROOT`, kept as evidence:
/// never cleaned before, never removed after, and a path that already exists is a failure.
struct Tmp(PathBuf, bool);

impl Tmp {
    fn new(tag: &str) -> Tmp {
        static N: AtomicU32 = AtomicU32::new(0);
        let n = N.fetch_add(1, Ordering::SeqCst);
        if let Some(root) = m4_root() {
            // `<dir>/w2/web.sock` is the longest socket below it: 12 bytes
            let name = format!("wg-{tag}-{}-{n}", std::process::id());
            return Tmp(m4_new_dir(&root, name, 12), true);
        }
        let p = PathBuf::from(format!("/tmp/fmwg-{tag}-{}-{n}", std::process::id()));
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
fn m4_new_dir(root: &Path, name: String, sock_suffix: usize) -> PathBuf {
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

/// `<dir>.<what>`: an m4 evidence file kept next to a window's `--dir` (M56).
fn evidence(dir: &Path, what: &str) -> PathBuf {
    PathBuf::from(format!("{}.{what}", dir.display()))
}

/// m4 only: append one timestamped line (unix ms) to an evidence log.
fn note(log: &Option<PathBuf>, line: &str) {
    if let Some(p) = log {
        let ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(p)
        {
            let _ = writeln!(f, "{ms} {line}");
        }
    }
}

/// A `fleetmux-web --gui --test-hooks` child: frames go in on `stdin`, the `0x01` / `0x02` it writes come back on
/// `out`; killed and reaped on drop.
struct GuiWindow {
    child: Child,
    stdin: Option<ChildStdin>,
    out: mpsc::Receiver<Frame>,
    dir: PathBuf,
    /// Held from the spawn until [`GuiWindow::first_size`] returns (see [`STARTING`]).
    starting: Option<MutexGuard<'static, ()>>,
}

/// One window of this test binary starts at a time: spawn → `web.sock` → the page's first fit. libtest runs as many
/// tests at once as the machine has CPUs, so without this every window test started together (three on the 3-vCPU
/// hosted macOS runner, the first of them on a cold WebKit) and each start got a share of the CPU; the first fit then
/// took over 5 s and all three failed `first_size` (CI run 42711634447, both attempts). The 5 s is the window's own
/// start (the product allows 3 s, `READY_WAIT` in `crates/app/src/attach/gui.rs`), so the starts are serialized
/// instead of the bound being widened. The test bodies after the first fit still run in parallel.
static STARTING: Mutex<()> = Mutex::new(());

impl GuiWindow {
    fn start(dir: &Path) -> GuiWindow {
        GuiWindow::start_with(dir, &[])
    }

    /// As [`GuiWindow::start`], with extra command-line arguments (`--theme JSON`, plan §5 step ③).
    fn start_with(dir: &Path, extra: &[&str]) -> GuiWindow {
        // a test that panicked while starting its window poisons the lock; the next start is still valid
        let starting = STARTING.lock().unwrap_or_else(PoisonError::into_inner);
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_fleetmux-web"));
        cmd.env("FLEETMUX_WEB_QUIET", "1"); // quiet.rs: never shows or takes the focus
        cmd.args(["--gui", "--prefix", "C-b", "--test-hooks", "--dir"])
            .arg(dir)
            .args(extra)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped());
        if is_m4() {
            // M56: the child's own stderr is kept next to its --dir (the M55 mid-test EOF left none)
            let err = std::fs::File::create(evidence(dir, "stderr")).expect("create <dir>.stderr");
            cmd.stderr(err);
        }
        let mut child = cmd.spawn().expect("spawn fleetmux-web --gui");
        let stdin = child.stdin.take();
        let mut stdout = child.stdout.take().unwrap();
        let (tx, out) = mpsc::channel();
        // m4 only (M56, from M57 §4 3a): each stdout frame the child sent, and how and when this reader ended
        let reader_log = if is_m4() {
            Some(evidence(dir, "reader.log"))
        } else {
            None
        };
        std::thread::spawn(move || {
            loop {
                match view::read_frame(&mut stdout) {
                    Ok(Some(f)) => {
                        if reader_log.is_some() {
                            let shown: String = format!("{f:?}").chars().take(120).collect();
                            note(&reader_log, &format!("frame {shown}"));
                        }
                        if tx.send(f).is_err() {
                            note(&reader_log, "end: receiver dropped (tx.send failed)");
                            return;
                        }
                    }
                    Ok(None) => {
                        note(&reader_log, "end: Ok(None) (child stdout closed)");
                        return;
                    }
                    Err(e) => {
                        note(&reader_log, &format!("end: read_frame error: {e:?}"));
                        return;
                    }
                }
            }
        });
        GuiWindow {
            child,
            stdin,
            out,
            dir: dir.to_path_buf(),
            starting: Some(starting),
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
                panic!("fleetmux-web --gui exited before web.sock answered: {st}");
            }
            assert!(Instant::now() < end, "web.sock did not answer within 5 s");
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    /// The page's first fit: the first frame on stdout is a `0x02` (PROTOCOL.md §15.4). Lets the next window start.
    fn first_size(&mut self) -> (u16, u16) {
        let first = self.out.recv_timeout(Duration::from_secs(5));
        drop(self.starting.take());
        match first {
            Ok(Frame::Resize { cols, rows }) => (cols, rows),
            other => panic!("first stdout frame is not 0x02: {other:?}"),
        }
    }

    fn feed(&mut self, f: &Frame) {
        let w = self.stdin.as_mut().expect("stdin open");
        view::write_frame(w, f).unwrap();
        w.flush().unwrap();
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

impl Drop for GuiWindow {
    fn drop(&mut self) {
        let before = self.child.try_wait();
        // m4 only (M56, from M57 §4 1/2): web.sock and web.log as they were before the kill
        let state = if is_m4() {
            let log = std::fs::read_to_string(self.dir.join("web.log")).unwrap_or_default();
            let tail: Vec<&str> = log.lines().rev().take(3).collect();
            Some(format!(
                "test panicking: {}\nbefore kill: web.sock exists: {}\nbefore kill: web.log last lines (newest first): {tail:?}\n",
                std::thread::panicking(),
                sock::sock_path(&self.dir).exists(),
            ))
        } else {
            None
        };
        let _ = self.child.kill();
        let after = self.child.wait();
        if let Some(state) = state {
            // M56: had the child ended by itself before this drop, and with what status
            let shown = |r: &std::io::Result<Option<std::process::ExitStatus>>| match r {
                Ok(Some(st)) => format!("{st}"),
                Ok(None) => "still running".to_string(),
                Err(e) => format!("error: {e}"),
            };
            let _ = std::fs::write(
                evidence(&self.dir, "exit"),
                format!(
                    "pid {}\nbefore drop (try_wait): {before:?}\nafter kill + wait: {after:?}\nbefore drop: {}\n{state}",
                    self.child.id(),
                    shown(&before),
                ),
            );
        }
    }
}

/// One request line; the first reply line that is not an event.
fn ask(stream: &mut UnixStream, req: Value) -> Value {
    // m4 only (M56): every request and reply line, EOF and read errors included, in `<dir>.ask.log`
    let log = if is_m4() {
        let peer = stream.peer_addr().ok();
        let dir = peer
            .as_ref()
            .and_then(|a| a.as_pathname())
            .and_then(|p| p.parent());
        dir.map(|d| evidence(d, "ask.log"))
    } else {
        None
    };
    note(&log, &format!("> {req}"));
    stream.write_all(format!("{req}\n").as_bytes()).unwrap();
    let mut r = BufReader::new(stream.try_clone().unwrap());
    loop {
        let mut line = String::new();
        let got = r.read_line(&mut line);
        let seen = match &got {
            Ok(0) => "< EOF".to_string(),
            Ok(_) => format!("< {}", line.trim_end()),
            Err(e) => format!("< read error: {e}"),
        };
        note(&log, &seen);
        // m4 only (M56, from M57 §4 4): the request in flight and the call site, before the panic below
        if log.is_some()
            && !matches!(&got, Ok(n) if *n > 0 && serde_json::from_str::<Value>(line.trim_end()).is_ok())
        {
            note(&log, &format!("! failed request: {req}"));
            let bt = std::backtrace::Backtrace::force_capture();
            note(&log, &format!("! backtrace:\n{bt}"));
        }
        got.unwrap();
        let v: Value = serde_json::from_str(line.trim_end())
            .unwrap_or_else(|e| panic!("reply is not JSON ({e}): {line:?}"));
        if v.get("event").is_none() {
            return v;
        }
        note(&log, "  (event line: skipped by ask)");
    }
}

/// Ask until `f` accepts the reply (≤ 5 s).
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

/// A run directory with a history page in it.
fn run_with_page(t: &Tmp) -> (PathBuf, String) {
    let run = t.0.join("run");
    std::fs::create_dir(&run).unwrap();
    let page = run.join("history.html");
    std::fs::write(
        &page,
        "<html><head><title>h</title></head><body>history page</body></html>",
    )
    .unwrap();
    (run, format!("file://{}", page.display()))
}

#[test]
fn gui_renders_screen_bytes_into_xterm() {
    if !display_or_skip() {
        return;
    }
    let t = Tmp::new("txt");
    let mut w = GuiWindow::start(&t.0.join("w"));
    let mut s = w.connect();
    let (cols, handshake_rows) = w.first_size();
    assert!(cols >= 20 && handshake_rows >= 5, "{cols}×{handshake_rows}");
    w.feed(&Frame::Screen(
        b"\x1b[2J\x1b[Hhello gui\r\n\x1b[1;32msecond\x1b[0m line".to_vec(),
    ));
    // The text content, its row count and the live grid are read in ONE page execution (`fm.text()` +
    // `fm.snapshot()`), so a refit cannot interleave between the row count and the rows themselves.
    let snap = js_until(
        &mut s,
        "(() => ({snap: fm.snapshot(), text: fm.text()}))()",
        "screen text",
        |v| {
            v["text"][0]
                .as_str()
                .is_some_and(|r| r.starts_with("hello gui"))
        },
    );
    let rows = snap["snap"]["rows"].as_u64().unwrap_or(0);
    assert!(rows >= 5, "{snap}");
    assert_eq!(snap["snap"]["textRows"], json!(rows), "{snap}");
    let text = snap["text"].as_array().unwrap_or_else(|| panic!("{snap}"));
    assert_eq!(text.len(), usize::try_from(rows).unwrap(), "{snap}");
    assert!(
        text[1].as_str().unwrap().starts_with("second line"),
        "{snap}"
    );
    assert!(snap["snap"]["cols"].as_u64().unwrap_or(0) >= 20, "{snap}");
}

#[test]
fn gui_theme_sets_page_background() {
    if !display_or_skip() {
        return;
    }
    // `--theme` (plan §2.3, PROTOCOL.md §15.4): the page's own background, read back from the live window. The
    // body is the element that covers the viewport (gui.css sizes html and body 100%), so its computed colour is
    // the one a person sees beside / below the xterm grid.
    let t = Tmp::new("thm");
    let theme = theme_arg("#123456");
    let mut w = GuiWindow::start_with(&t.0.join("w"), &["--theme", &theme]);
    let mut s = w.connect();
    w.first_size();
    let r = ask_until(
        &mut s,
        json!({"cmd": "js", "code": "getComputedStyle(document.body).backgroundColor"}),
        "body background",
        |v| v["result"] == json!("rgb(18, 52, 86)"),
    );
    assert_eq!(r["result"], json!("rgb(18, 52, 86)"), "{r}");
    println!("body background from the live window: {}", r["result"]);
    let html = ask(
        &mut s,
        json!({"cmd": "js", "code": "getComputedStyle(document.documentElement).backgroundColor"}),
    );
    assert_eq!(html["result"], json!("rgb(18, 52, 86)"), "{html}");
    assert_eq!(ask(&mut s, json!({"cmd": "quit"}))["ok"], json!(true));
    assert!(
        w.wait_exit(Duration::from_secs(2)).is_some(),
        "still running after quit"
    );
}

#[test]
fn gui_overlay_follows_0x85_and_hides_on_null() {
    if !display_or_skip() {
        return;
    }
    let t = Tmp::new("ov");
    let (run, url) = run_with_page(&t);
    let mut w = GuiWindow::start(&t.0.join("w"));
    let mut s = w.connect();
    let (cols, rows) = w.first_size();
    assert!(cols >= 20 && rows >= 8, "{cols}×{rows}");
    assert_eq!(
        ask(&mut s, json!({"cmd": "scope", "run_dir": run}))["ok"],
        json!(true)
    );
    assert_eq!(
        ask(&mut s, json!({"cmd": "open", "url": url}))["ok"],
        json!(true)
    );
    let shown = json!({"pane": "k7q2-3", "rect": {"x": 2, "y": 3, "w": 10, "h": 4}, "url": url});
    w.feed(&Frame::Screen(b"\x1b[2J\x1b[Hbelow".to_vec()));
    w.feed(&Frame::Overlay(shown.to_string()));
    let o = ask_until(&mut s, json!({"cmd": "overlay"}), "overlay visible", |v| {
        v["visible"] == json!(true)
    });
    assert_eq!(o["cells"], json!({"x": 2, "y": 3, "w": 10, "h": 4}), "{o}");
    let src = o["src"].as_str().unwrap();
    assert!(
        src.starts_with("fleetmux://page/") && src.contains("history.html"),
        "{o}"
    );
    // Pixels = cells × the cell size (cell size = px.w / 10, px.h / 4), within 1 px.
    let px = |k: &str| o["px"][k].as_f64().unwrap_or_else(|| panic!("px.{k}: {o}"));
    let (cw, ch) = (px("w") / 10.0, px("h") / 4.0);
    assert!(cw > 2.0 && ch > 2.0, "{o}");
    assert!((px("x") - 2.0 * cw).abs() <= 1.0, "{o}");
    assert!((px("y") - 3.0 * ch).abs() <= 1.0, "{o}");
    // A null rect hides it; the page stays loaded.
    let hidden = json!({"pane": "k7q2-3", "rect": null, "url": url});
    w.feed(&Frame::Overlay(hidden.to_string()));
    let o = ask_until(&mut s, json!({"cmd": "overlay"}), "overlay hidden", |v| {
        v["visible"] == json!(false)
    });
    assert_eq!(o["cells"], Value::Null, "{o}");
    assert_eq!(ask(&mut s, json!({"cmd": "url"}))["url"], json!(url));
}

/// `js` in the terminal page: its completion value as JSON, or a panic with the reply.
fn js(s: &mut UnixStream, code: &str) -> Value {
    let r = ask(s, json!({"cmd": "js", "code": code}));
    assert_eq!(r["ok"], json!(true), "{code}: {r}");
    r["result"].clone()
}

/// The history iframe's inline `pointer-events` (`""` when it is left to the stylesheet).
fn hist_pointer_events(s: &mut UnixStream) -> String {
    js(s, "document.getElementById('history').style.pointerEvents")
        .as_str()
        .unwrap_or_default()
        .to_string()
}

/// The overlay's lower-left cells lie over the screen, so a selection / mouse report that starts on the terminal
/// and moves down into the history iframe must not be delivered to the page: from a mousedown on the screen until
/// the button is up again the iframe ignores the pointer. A direct click on the page keeps working.
#[test]
fn gui_history_iframe_does_not_swallow_a_terminal_drag() {
    if !display_or_skip() {
        return;
    }
    let t = Tmp::new("drag");
    let (run, url) = run_with_page(&t);
    let mut w = GuiWindow::start(&t.0.join("w"));
    let mut s = w.connect();
    let (cols, rows) = w.first_size();
    assert!(cols >= 20 && rows >= 8, "{cols}×{rows}");
    assert_eq!(
        ask(&mut s, json!({"cmd": "scope", "run_dir": run}))["ok"],
        json!(true)
    );
    assert_eq!(
        ask(&mut s, json!({"cmd": "open", "url": url}))["ok"],
        json!(true)
    );
    let shown = json!({"pane": "k7q2-3", "rect": {"x": 2, "y": 3, "w": 10, "h": 4}, "url": url});
    w.feed(&Frame::Screen(b"\x1b[2J\x1b[Hbelow".to_vec()));
    w.feed(&Frame::Overlay(shown.to_string()));
    ask_until(&mut s, json!({"cmd": "overlay"}), "overlay visible", |v| {
        v["visible"] == json!(true)
    });
    let down = concat!(
        "document.querySelector('.xterm-screen').dispatchEvent(",
        "new MouseEvent('mousedown', {bubbles: true, button: 0, buttons: 1}))"
    );
    let up =
        "window.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, button: 0, buttons: 0}))";

    assert_eq!(
        hist_pointer_events(&mut s),
        "",
        "idle: the stylesheet decides"
    );
    // A drag that starts on the screen: the iframe must not take the pointer.
    js(&mut s, &down);
    assert_eq!(
        hist_pointer_events(&mut s),
        "none",
        "mousedown on the terminal must hand the pointer to the terminal, not the page"
    );
    // The button comes up inside the window: back to normal at once.
    js(&mut s, up);
    assert_eq!(
        hist_pointer_events(&mut s),
        "",
        "mouseup must restore the iframe"
    );
    // Released outside the window (no mouseup reaches the page) and the window lost focus: blur restores it.
    js(&mut s, &down);
    assert_eq!(hist_pointer_events(&mut s), "none");
    js(&mut s, "window.dispatchEvent(new Event('blur'))");
    assert_eq!(
        hist_pointer_events(&mut s),
        "",
        "blur must restore the iframe"
    );
    // Released outside the window without a blur: a later mousemove with no button down means we missed it.
    js(&mut s, &down);
    assert_eq!(hist_pointer_events(&mut s), "none");
    js(
        &mut s,
        "window.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, buttons: 0}))",
    );
    assert_eq!(
        hist_pointer_events(&mut s),
        "",
        "a button-less mousemove must restore the iframe"
    );
    // A click that starts on the page itself is not a terminal drag: the page stays clickable.
    js(
        &mut s,
        concat!(
            "document.getElementById('history').dispatchEvent(",
            "new MouseEvent('mousedown', {bubbles: true, button: 0, buttons: 1}))"
        ),
    );
    assert_eq!(
        hist_pointer_events(&mut s),
        "",
        "a mousedown on the page must not disable it"
    );
    // Nothing above moved or unloaded the overlay.
    let o = ask(&mut s, json!({"cmd": "overlay"}));
    assert_eq!(o["visible"], json!(true), "{o}");
    assert_eq!(o["cells"], json!({"x": 2, "y": 3, "w": 10, "h": 4}), "{o}");
}

#[test]
fn gui_embedded_page_open_goto_url_reload() {
    if !display_or_skip() {
        return;
    }
    let t = Tmp::new("pg");
    let (run, url) = run_with_page(&t);
    let mut w = GuiWindow::start(&t.0.join("w"));
    let mut s = w.connect();
    w.first_size();
    assert_eq!(
        ask(&mut s, json!({"cmd": "url"}))["url"],
        json!("about:blank")
    );
    assert_eq!(
        ask(&mut s, json!({"cmd": "scope", "run_dir": run}))["ok"],
        json!(true)
    );
    assert_eq!(
        ask(&mut s, json!({"cmd": "open", "url": url}))["ok"],
        json!(true)
    );
    assert_eq!(ask(&mut s, json!({"cmd": "url"}))["url"], json!(url));
    let o = ask(&mut s, json!({"cmd": "overlay"}));
    assert_eq!(o["src"].as_str(), to_page_url(&url).as_deref(), "{o}");
    let with_query = format!("{url}?x=1");
    assert_eq!(
        ask(&mut s, json!({"cmd": "goto", "url": with_query}))["ok"],
        json!(true)
    );
    assert_eq!(ask(&mut s, json!({"cmd": "url"}))["url"], json!(with_query));
    // The page sits in the sandboxed iframe (design.md §3.6.7), read through the test hook.
    let r = ask(
        &mut s,
        json!({"cmd": "js", "code": "document.querySelector('iframe').getAttribute('sandbox')"}),
    );
    assert_eq!(r["result"], json!("allow-scripts allow-popups"), "{r}");
    assert_eq!(ask(&mut s, json!({"cmd": "reload"}))["ok"], json!(true));
    // Outside the scope / not a file: URL: refused, nothing changes.
    for bad in ["file:///etc/passwd", "https://example.com/"] {
        assert_eq!(
            ask(&mut s, json!({"cmd": "goto", "url": bad}))["ok"],
            json!(false),
            "{bad}"
        );
    }
    assert_eq!(ask(&mut s, json!({"cmd": "url"}))["url"], json!(with_query));
    assert_eq!(ask(&mut s, json!({"cmd": "quit"}))["ok"], json!(true));
    assert!(
        w.wait_exit(Duration::from_secs(2)).is_some(),
        "still running after quit"
    );
}

#[test]
fn gui_close_reports_closed_and_stdin_eof_exits_within_1s() {
    if !display_or_skip() {
        return;
    }
    // The person closes the window (the test hook stands in: `window.close()` in the terminal page, PROTOCOL.md
    // §15.4): every web.sock connection gets {"event":"window","state":"closed"}, then the process exits 0.
    let t = Tmp::new("cl");
    let mut w = GuiWindow::start(&t.0.join("w"));
    let s = w.connect();
    w.first_size();
    let mut watcher = BufReader::new(s.try_clone().unwrap());
    let mut s2 = s;
    s2.write_all(format!("{}\n", json!({"cmd": "js", "code": "window.close()"})).as_bytes())
        .unwrap();
    let mut closed = false;
    for _ in 0..4 {
        let mut line = String::new();
        if watcher.read_line(&mut line).unwrap_or(0) == 0 {
            break;
        }
        if serde_json::from_str::<Value>(line.trim_end()).ok()
            == Some(json!({"event": "window", "state": "closed"}))
        {
            closed = true;
            break;
        }
    }
    assert!(closed, "no window closed event");
    let st = w
        .wait_exit(Duration::from_secs(2))
        .expect("still running after the window closed");
    assert_eq!(st.code(), Some(0));

    // stdin EOF (the client is gone): exit within 1 s.
    let mut w = GuiWindow::start(&t.0.join("w2"));
    let _s = w.connect();
    w.first_size();
    drop(w.stdin.take());
    let started = Instant::now();
    assert!(
        w.wait_exit(Duration::from_secs(1)).is_some(),
        "fleetmux-web --gui still running {:?} after stdin EOF",
        started.elapsed()
    );
}

/// `js` in the terminal page, retried until `f` accepts the completion value (≤ 5 s).
fn js_until(s: &mut UnixStream, code: &str, what: &str, f: impl Fn(&Value) -> bool) -> Value {
    let end = Instant::now() + Duration::from_secs(5);
    loop {
        let v = ask(s, json!({"cmd": "js", "code": code}));
        if v["ok"] == json!(true) && f(&v["result"]) {
            return v["result"].clone();
        }
        assert!(Instant::now() < end, "{what}: last reply {v}");
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// One read-only page execution: the live grid (`term.cols` / `term.rows`), the `.xterm-screen` rect and the bars
/// (`fm.snapshot()`, gui.js; `--test-hooks` only). A refit cannot interleave inside it, unlike separate `js` calls or
/// a quiet window over `0x02` frames.
fn snapshot(s: &mut UnixStream) -> Value {
    let v = js(s, "fm.snapshot()");
    assert!(v["cols"].as_u64().unwrap_or(0) >= 2, "{v}");
    assert!(v["rows"].as_u64().unwrap_or(0) >= 1, "{v}");
    v
}

/// One entry of a snapshot's `scrollbars` array, by pane id.
fn bar<'a>(snap: &'a Value, pane: &str) -> &'a Value {
    snap["scrollbars"]
        .as_array()
        .unwrap_or_else(|| panic!("no scrollbars array: {snap}"))
        .iter()
        .find(|b| b["pane"] == json!(pane))
        .unwrap_or_else(|| panic!("no {pane} in {snap}"))
}

/// The scrollbars exist only after the first `0x86` (gui.js): an old server never sends one, so the page never
/// creates an element and never sends `0x05` (which an old server would refuse).
#[test]
fn gui_no_scrollbar_before_first_0x86() {
    if !display_or_skip() {
        return;
    }
    let t = Tmp::new("sb0");
    let mut w = GuiWindow::start(&t.0.join("w"));
    let mut s = w.connect();
    let (cols, rows) = w.first_size();
    assert!(cols >= 20 && rows >= 5, "{cols}×{rows}");
    w.feed(&Frame::Screen(b"\x1b[2J\x1b[Hplain".to_vec()));
    let n = js_until(
        &mut s,
        "fm.scrollbars().length",
        "no scrollbar state",
        |v| v == &json!(0),
    );
    assert_eq!(n, json!(0));
    assert_eq!(
        js(&mut s, "document.querySelectorAll('.sb').length"),
        json!(0),
        "no scrollbar element before the first 0x86"
    );
}

/// `0x86` draws one scrollbar per visible cell; a drag on the thumb is local first and sends `0x05` once per
/// animation frame (plus the final value), and the next `0x86` is authoritative again.
#[test]
fn gui_scrollbars_follow_0x86_and_drag_sends_0x05() {
    if !display_or_skip() {
        return;
    }
    let t = Tmp::new("sb1");
    let mut w = GuiWindow::start(&t.0.join("w"));
    let mut s = w.connect();
    let (handshake_cols, handshake_rows) = w.first_size();
    assert!(
        handshake_cols >= 20 && handshake_rows >= 10,
        "{handshake_cols}×{handshake_rows}"
    );
    w.feed(&Frame::Screen(b"\x1b[2J\x1b[Hplain".to_vec()));
    let state = json!({"panes": [
        {"pane": "p1", "rect": {"x": 0, "y": 0, "w": 10, "h": 4}, "offset": 0, "total": 20},
        {"pane": "p2", "rect": {"x": 0, "y": 5, "w": 10, "h": 2}, "offset": 3, "total": 3}
    ]});
    w.feed(&Frame::Scroll(state.to_string()));
    js_until(&mut s, "fm.scrollbars().length", "two scrollbars", |v| {
        v == &json!(2)
    });
    assert_eq!(
        js(&mut s, "document.querySelectorAll('.sb').length"),
        json!(2)
    );
    // Grid, rect and bars from ONE page execution (`fm.snapshot()`): the page's own live `term.cols`/`term.rows`,
    // so a refit cannot leave the measurement with a stale size (the old 200 ms quiet window could).
    let snap = snapshot(&mut s);
    let cols = snap["cols"].as_u64().unwrap();
    let rows = snap["rows"].as_u64().unwrap();
    assert!(cols >= 20 && rows >= 10, "{snap}");
    assert_eq!(snap["textRows"], json!(rows), "{snap}");
    let sc = &snap["screen"];
    let cw = sc["w"].as_f64().unwrap() / cols as f64;
    let ch = sc["h"].as_f64().unwrap() / rows as f64;
    assert!(cw > 2.0 && ch > 2.0, "{sc} for {cols}×{rows}");
    let p1 = bar(&snap, "p1");
    assert_eq!(
        p1["cells"],
        json!({"x": 0, "y": 0, "w": 10, "h": 4}),
        "{p1}"
    );
    assert_eq!(
        (p1["offset"].clone(), p1["total"].clone()),
        (json!(0), json!(20))
    );
    let px = &p1["px"];
    let f = |k: &str| px[k].as_f64().unwrap_or_else(|| panic!("px.{k}: {p1}"));
    assert!((f("x") - (10.0 * cw - 12.0)).abs() <= 1.0, "{p1}");
    assert!(f("y").abs() <= 1.0, "{p1}");
    assert!((f("h") - 4.0 * ch).abs() <= 1.0, "{p1}");
    // offset 0 with history: the thumb sits at the bottom of its track.
    let th = &p1["thumb"];
    let (top, h) = (th["top"].as_f64().unwrap(), th["height"].as_f64().unwrap());
    assert!(h >= 16.0, "{p1}");
    assert!(
        (top + h - f("h")).abs() <= 1.0,
        "p1 thumb at the bottom: {p1}"
    );
    // p2 has as many history rows as its screen (3): its thumb is at the top of the track.
    let p2 = bar(&snap, "p2");
    assert_eq!(p2["thumb"]["top"], json!(0), "p2: {p2}");
    let (p2h, p2px) = (
        p2["thumb"]["height"].as_f64().unwrap(),
        p2["px"]["h"].as_f64().unwrap(),
    );
    assert!(p2h >= 16.0 && p2h <= p2px, "p2: {p2}");

    // Drag p1's thumb to the top of its track: local position first, then one 0x05 with the final value.
    let g = js(
        &mut s,
        concat!(
            "(() => {",
            " const el = document.querySelector('.sb[data-pane=\"p1\"]');",
            " const th = el.querySelector('.sb-thumb');",
            " const tr = el.getBoundingClientRect(), r = th.getBoundingClientRect();",
            " const mid = (r.top + r.bottom) / 2;",
            " th.dispatchEvent(new MouseEvent('mousedown',",
            "   {bubbles: true, cancelable: true, button: 0, buttons: 1, clientY: mid}));",
            " window.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, buttons: 1, clientY: tr.top}));",
            " window.dispatchEvent(new MouseEvent('mouseup',",
            "   {bubbles: true, button: 0, buttons: 0, clientY: tr.top}));",
            " return {mid: mid, trackTop: tr.top, trackH: tr.height};",
            "})()"
        ),
    );
    let want = json!({"pane": "p1", "offset": 20});
    let end = Instant::now() + Duration::from_secs(5);
    loop {
        match w.out.recv_timeout(Duration::from_millis(200)) {
            Ok(Frame::ScrollTo(line)) => {
                let v: Value = serde_json::from_str(&line).unwrap_or(Value::Null);
                if v == want {
                    break;
                }
            }
            Ok(_) => {} // other frame types: skip
            Err(mpsc::RecvTimeoutError::Timeout) => assert!(
                Instant::now() < end,
                "no 0x05 {want} within 5 s after the drag {g}"
            ),
            Err(mpsc::RecvTimeoutError::Disconnected) => panic!("stdout closed before the 0x05"),
        }
    }
    // The next 0x86 is authoritative: offset 20 puts the thumb at the top of the track.
    let moved = json!({"panes": [
        {"pane": "p1", "rect": {"x": 0, "y": 0, "w": 10, "h": 4}, "offset": 20, "total": 20},
        {"pane": "p2", "rect": {"x": 0, "y": 5, "w": 10, "h": 2}, "offset": 0, "total": 3}
    ]});
    w.feed(&Frame::Scroll(moved.to_string()));
    js_until(
        &mut s,
        "fm.scrollbars().find(b => b.pane === \"p1\").thumb.top",
        "p1 at the top",
        |v| v == &json!(0),
    );
    // An empty 0x86 removes every bar (a zoomed history pane, or no visible cell).
    w.feed(&Frame::Scroll(json!({"panes": []}).to_string()));
    js_until(
        &mut s,
        "document.querySelectorAll('.sb').length",
        "no scrollbar elements",
        |v| v == &json!(0),
    );
    js_until(
        &mut s,
        "fm.scrollbars().length",
        "no scrollbar state",
        |v| v == &json!(0),
    );
    // total == 0 (alt screen, or no history yet): the track is still drawn and the thumb fills it.
    let empty_hist = json!({"panes": [
        {"pane": "p0", "rect": {"x": 0, "y": 0, "w": 10, "h": 3}, "offset": 0, "total": 0}
    ]});
    w.feed(&Frame::Scroll(empty_hist.to_string()));
    js_until(
        &mut s,
        "fm.scrollbars().length",
        "one total-0 scrollbar",
        |v| v == &json!(1),
    );
    let snap0 = snapshot(&mut s);
    let p0 = bar(&snap0, "p0");
    assert_eq!(p0["thumb"]["top"], json!(0), "{p0}");
    assert_eq!(
        p0["thumb"]["height"], p0["px"]["h"],
        "total 0: the thumb fills the track {p0}"
    );
}

/// The measurement must use the page's live grid even when a refit arrives after the old 200 ms quiet window: a
/// delayed (300 ms) real refit through the page's own ResizeObserver changes the grid, and the one-execution
/// snapshot still matches the re-placed bars. The stale-grid counterfactual is what would fail if the test ever
/// measured with a pre-refit size again.
#[test]
fn gui_scrollbar_measurement_uses_the_live_grid_after_a_delayed_refit() {
    if !display_or_skip() {
        return;
    }
    let t = Tmp::new("sb2");
    let mut w = GuiWindow::start(&t.0.join("w"));
    let mut s = w.connect();
    let (cols, rows) = w.first_size();
    assert!(cols >= 20 && rows >= 10, "{cols}×{rows}");
    w.feed(&Frame::Screen(b"\x1b[2J\x1b[Hplain".to_vec()));
    let state = json!({"panes": [
        {"pane": "p1", "rect": {"x": 0, "y": 0, "w": 10, "h": 4}, "offset": 0, "total": 20},
        {"pane": "p2", "rect": {"x": 0, "y": 5, "w": 10, "h": 2}, "offset": 3, "total": 3}
    ]});
    w.feed(&Frame::Scroll(state.to_string()));
    js_until(&mut s, "fm.scrollbars().length", "two scrollbars", |v| {
        v == &json!(2)
    });
    let before = snapshot(&mut s);
    let bcols = before["cols"].as_u64().unwrap();
    let brows = before["rows"].as_u64().unwrap();
    assert!(bcols >= 20 && brows >= 10, "{before}");

    // The delayed stimulus: 300 ms after this call the page changes its own geometry, so the page's own
    // ResizeObserver -> refit() -> term.resize -> reportSize() runs *after* the old 200 ms quiet window. The
    // width change is what a startup refit cannot produce (showing the window does not change its width).
    let scheduled = Instant::now();
    js(
        &mut s,
        concat!(
            "(() => { setTimeout(() => {",
            " const termEl = document.getElementById('term');",
            " document.querySelector('.xterm').style.paddingTop = '24px';",
            " termEl.style.width = 'calc(100% - 40px)';",
            " }, 300); return true; })()"
        ),
    );

    // Wait for the page's own `0x02` whose *columns* shrank. A startup refit only changes rows (the window's width
    // is the same hidden and shown), so a column shrink identifies this delayed stimulus, not a startup resize.
    let end = Instant::now() + Duration::from_secs(10);
    loop {
        match w.out.recv_timeout(Duration::from_millis(200)) {
            Ok(Frame::Resize { cols, .. }) if u64::from(cols) < bcols => break,
            Ok(_) => {}
            Err(mpsc::RecvTimeoutError::Timeout) => {
                assert!(
                    Instant::now() < end,
                    "no shrunken 0x02 within 10 s after the delayed stimulus"
                );
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                panic!("stdout closed before the delayed refit")
            }
        }
    }
    assert!(
        scheduled.elapsed() >= Duration::from_millis(200),
        "the refit arrived after {} ms; it must cross the old 200 ms quiet window",
        scheduled.elapsed().as_millis()
    );

    let after = snapshot(&mut s);
    let acols = after["cols"].as_u64().unwrap();
    let arows = after["rows"].as_u64().unwrap();
    assert!(
        acols < bcols,
        "the grid did not take the delayed stimulus: before {before}, after {after}"
    );
    assert!(arows <= brows, "{before} → {after}");
    assert_eq!(after["textRows"], json!(arows), "{after}");
    let sc = &after["screen"];
    let cw = sc["w"].as_f64().unwrap() / acols as f64;
    let ch = sc["h"].as_f64().unwrap() / arows as f64;
    assert!(cw > 2.0 && ch > 2.0, "{sc} for {acols}×{arows}");
    let p1 = bar(&after, "p1");
    assert_eq!(
        p1["cells"],
        json!({"x": 0, "y": 0, "w": 10, "h": 4}),
        "{p1}"
    );
    assert_eq!(
        (p1["offset"].clone(), p1["total"].clone()),
        (json!(0), json!(20))
    );
    let px = &p1["px"];
    let f = |k: &str| px[k].as_f64().unwrap_or_else(|| panic!("px.{k}: {p1}"));
    assert!((f("x") - (10.0 * cw - 12.0)).abs() <= 1.0, "{p1}");
    assert!(f("y").abs() <= 1.0, "{p1}");
    assert!((f("h") - 4.0 * ch).abs() <= 1.0, "{p1}");
    let th = &p1["thumb"];
    let (top, h) = (th["top"].as_f64().unwrap(), th["height"].as_f64().unwrap());
    assert!(h >= 16.0, "{p1}");
    assert!(
        (top + h - f("h")).abs() <= 1.0,
        "p1 thumb at the bottom: {p1}"
    );
    let p2 = bar(&after, "p2");
    assert_eq!(p2["thumb"]["top"], json!(0), "p2: {p2}");
    let (p2h, p2px) = (
        p2["thumb"]["height"].as_f64().unwrap(),
        p2["px"]["h"].as_f64().unwrap(),
    );
    assert!(p2h >= 16.0 && p2h <= p2px, "p2: {p2}");

    // Counterfactual teeth: the pre-stimulus grid against the post-stimulus rect/bars violates the same tolerance,
    // so this test would be red if it measured with a stale size again.
    let stale_cw = sc["w"].as_f64().unwrap() / bcols as f64;
    let stale_ch = sc["h"].as_f64().unwrap() / brows as f64;
    assert!(
        (f("x") - (10.0 * stale_cw - 12.0)).abs() > 1.0
            || (f("h") - 4.0 * stale_ch).abs() > 1.0,
        "stale-grid counterfactual did not bite: {p1}, before {bcols}×{brows}, after {acols}×{arows}"
    );

    // Drag p1's thumb to the top of its track at the new grid: local position first, then one 0x05 with the final
    // value; the next 0x86 is authoritative again, and an empty 0x86 removes every bar.
    let g = js(
        &mut s,
        concat!(
            "(() => {",
            " const el = document.querySelector('.sb[data-pane=\"p1\"]');",
            " const th = el.querySelector('.sb-thumb');",
            " const tr = el.getBoundingClientRect(), r = th.getBoundingClientRect();",
            " const mid = (r.top + r.bottom) / 2;",
            " th.dispatchEvent(new MouseEvent('mousedown',",
            "   {bubbles: true, cancelable: true, button: 0, buttons: 1, clientY: mid}));",
            " window.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, buttons: 1, clientY: tr.top}));",
            " window.dispatchEvent(new MouseEvent('mouseup',",
            "   {bubbles: true, button: 0, buttons: 0, clientY: tr.top}));",
            " return {mid: mid, trackTop: tr.top, trackH: tr.height};",
            "})()"
        ),
    );
    let want = json!({"pane": "p1", "offset": 20});
    let end = Instant::now() + Duration::from_secs(5);
    loop {
        match w.out.recv_timeout(Duration::from_millis(200)) {
            Ok(Frame::ScrollTo(line)) => {
                let v: Value = serde_json::from_str(&line).unwrap_or(Value::Null);
                if v == want {
                    break;
                }
            }
            Ok(_) => {}
            Err(mpsc::RecvTimeoutError::Timeout) => assert!(
                Instant::now() < end,
                "no 0x05 {want} within 5 s after the drag {g}"
            ),
            Err(mpsc::RecvTimeoutError::Disconnected) => panic!("stdout closed before the 0x05"),
        }
    }
    let moved = json!({"panes": [
        {"pane": "p1", "rect": {"x": 0, "y": 0, "w": 10, "h": 4}, "offset": 20, "total": 20},
        {"pane": "p2", "rect": {"x": 0, "y": 5, "w": 10, "h": 2}, "offset": 0, "total": 3}
    ]});
    w.feed(&Frame::Scroll(moved.to_string()));
    js_until(
        &mut s,
        "fm.scrollbars().find(b => b.pane === \"p1\").thumb.top",
        "p1 at the top",
        |v| v == &json!(0),
    );
    w.feed(&Frame::Scroll(json!({"panes": []}).to_string()));
    js_until(
        &mut s,
        "document.querySelectorAll('.sb').length",
        "no scrollbar elements",
        |v| v == &json!(0),
    );
    js_until(
        &mut s,
        "fm.scrollbars().length",
        "no scrollbar state",
        |v| v == &json!(0),
    );
}

/// The computed `opacity` of one pane's scrollbar (`0` = invisible, `1` = fully shown); `-1` when the element is
/// missing. The fade is a CSS transition on `.sb` driven by gui.js, so the computed value is what a user sees.
fn sb_opacity(s: &mut UnixStream, pane: &str) -> f64 {
    let code = format!(
        "(() => {{ const el = document.querySelector('.sb[data-pane=\"{pane}\"]');\
         return el ? parseFloat(getComputedStyle(el).opacity) : -1; }})()"
    );
    js(s, &code).as_f64().unwrap_or(-1.0)
}

/// Poll `sb_opacity` until `f` accepts it (bounded by `within`); the value and the time it took.
fn sb_opacity_until(
    s: &mut UnixStream,
    pane: &str,
    what: &str,
    within: Duration,
    f: impl Fn(f64) -> bool,
) -> (f64, Duration) {
    let start = Instant::now();
    let end = start + within;
    loop {
        let o = sb_opacity(s, pane);
        if f(o) {
            return (o, start.elapsed());
        }
        assert!(
            Instant::now() < end,
            "{what}: opacity {o} after {:?}",
            start.elapsed()
        );
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// The scrollbar is a transient affordance: invisible at rest, shown by a `0x86` that moves the offset, gone again
/// after ~1.5 s, and held visible for as long as its thumb is being dragged (even past that idle timeout).
#[test]
fn gui_scrollbar_fades_when_idle_and_stays_visible_while_dragging() {
    if !display_or_skip() {
        return;
    }
    let t = Tmp::new("sbfade");
    let mut w = GuiWindow::start(&t.0.join("w"));
    let mut s = w.connect();
    let (cols, rows) = w.first_size();
    assert!(cols >= 20 && rows >= 10, "{cols}×{rows}");
    w.feed(&Frame::Screen(b"\x1b[2J\x1b[Hplain".to_vec()));
    // The bar is placed at the bottom-right of the canvas: a trusted mousemove that the machine delivers when the
    // window appears (at the real pointer, near the top-left) must not hover it and postpone the idle fade.
    let (bx, by) = (cols - 14, rows - 6);
    let state = move |offset: u64| {
        json!({"panes": [
            {"pane": "p1", "rect": {"x": bx, "y": by, "w": 10, "h": 4}, "offset": offset, "total": 20}
        ]})
        .to_string()
    };
    w.feed(&Frame::Scroll(state(0)));
    js_until(
        &mut s,
        "document.querySelectorAll('.sb').length",
        "one scrollbar",
        |v| v == &json!(1),
    );

    // 1. At rest the bar is fully transparent (not just dim).
    let (o, waited) = sb_opacity_until(
        &mut s,
        "p1",
        "idle scrollbar is invisible (opacity 0)",
        Duration::from_secs(5),
        |o| o == 0.0,
    );
    assert_eq!(o, 0.0, "idle opacity {o} after {waited:?}");

    // 2. A 0x86 that moves the offset shows it; it is still shown ~0.9 s later and fades to 0 within ~1.5 s
    //    (1.0 s idle hold + 0.3 s fade, plus scheduling margin).
    w.feed(&Frame::Scroll(state(5)));
    let changed_at = Instant::now();
    let (o, shown) = sb_opacity_until(
        &mut s,
        "p1",
        "a moved offset shows the scrollbar",
        Duration::from_secs(2), // generous: the delivery gate runs this under load
        |o| o > 0.5,
    );
    assert!(o > 0.5, "opacity {o} {shown:?} after the offset change");
    // Mid-hold check, timed from the offset change (not from when we saw it shown: on a slow CI runner
    // seeing it can take most of the 1 s hold). Skipped when the runner is already too late to tell.
    if let Some(rest) = Duration::from_millis(500).checked_sub(changed_at.elapsed()) {
        std::thread::sleep(rest);
    }
    let o = sb_opacity(&mut s, "p1");
    if changed_at.elapsed() < Duration::from_millis(800) {
        assert!(
            o > 0.5,
            "still visible ~0.5 s after the offset change (the idle hold is ~1 s), got {o}"
        );
    }
    let left = Duration::from_millis(4000)
        .checked_sub(changed_at.elapsed())
        .unwrap_or(Duration::from_millis(1));
    let (o, faded) = sb_opacity_until(
        &mut s,
        "p1",
        "the scrollbar fades out after the ~1 s idle hold (4 s deadline) — offset change",
        left,
        |o| o == 0.0,
    );
    assert_eq!(o, 0.0, "opacity {o} after {faded:?}");

    // 3. Holding the thumb still — no mousemove at all — keeps it visible well past the idle fade window.
    let g = js(
        &mut s,
        concat!(
            "(() => {",
            " const el = document.querySelector('.sb[data-pane=\"p1\"]');",
            " const th = el.querySelector('.sb-thumb');",
            " const r = th.getBoundingClientRect();",
            " const mid = (r.top + r.bottom) / 2;",
            " th.dispatchEvent(new MouseEvent('mousedown',",
            "   {bubbles: true, cancelable: true, button: 0, buttons: 1, clientY: mid}));",
            " return {mid: mid, top: r.top, bottom: r.bottom};",
            "})()"
        ),
    );
    assert_eq!(
        js(
            &mut s,
            "(() => { const l = document.querySelector('.sb[data-pane=\"p1\"] .sb-label'); return l ? !l.hidden : 'missing'; })()"
        ),
        json!(true),
        "the offset/total label appears while dragging {g}"
    );
    let start = Instant::now();
    let mut samples = 0;
    let mut last_at = Duration::ZERO;
    while start.elapsed() < Duration::from_millis(1600) {
        let at = start.elapsed();
        let o = sb_opacity(&mut s, "p1");
        assert!(o > 0.5, "visible while the thumb is held still at {at:?}: opacity {o} {g}");
        samples += 1;
        last_at = at;
        std::thread::sleep(Duration::from_millis(200));
    }
    // What matters is a visible sample past the 1.0 s hold + 0.3 s fade, not how many fit in (each sample is
    // a JS round trip; a slow CI runner fits ~5).
    assert!(
        samples >= 2 && last_at >= Duration::from_millis(1300),
        "held samples: {samples}, last at {last_at:?}"
    );
    js(
        &mut s,
        "window.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, button: 0, buttons: 0, clientY: 120})); true",
    );
    let released_at = Instant::now();
    // Released away from the bar: the label goes, it is idle again and fades away.
    assert_eq!(
        js(
            &mut s,
            "(() => { const l = document.querySelector('.sb[data-pane=\"p1\"] .sb-label'); return l ? !l.hidden : 'missing'; })()"
        ),
        json!(false),
        "no label once the drag ends"
    );
    let left = Duration::from_millis(4000)
        .checked_sub(released_at.elapsed())
        .unwrap_or(Duration::from_millis(1));
    let (o, faded) = sb_opacity_until(
        &mut s,
        "p1",
        "the scrollbar fades out after the ~1 s idle hold (4 s deadline) — release",
        left,
        |o| o == 0.0,
    );
    assert_eq!(o, 0.0, "opacity {o} after {faded:?}");
}

/// Only a *changed* offset of that very pane is activity: the first `0x86` of a pane (whatever its offset) and a
/// repeated state never flash a bar, and another pane's offset never touches it. The affordance is the right 12 px
/// of the cell only: invisible and `pointer-events: none` at rest (so the terminal keeps every click there), shown
/// and grabbable while hovered, a 6 px thumb 2 px from the edge widening to 10 px, and the `offset/total` label
/// only while the thumb is dragged (held still included).
#[test]
fn gui_scrollbar_activity_is_only_that_panes_changed_offset() {
    if !display_or_skip() {
        return;
    }
    let t = Tmp::new("sbact");
    let mut w = GuiWindow::start(&t.0.join("w"));
    let mut s = w.connect();
    let (cols, rows) = w.first_size();
    assert!(cols >= 20 && rows >= 10, "{cols}×{rows}");
    w.feed(&Frame::Screen(b"\x1b[2J\x1b[Hplain".to_vec()));
    // Both bars sit at the bottom-right of the canvas: a trusted mousemove the machine delivers when the window
    // appears (at the real pointer, near the top-left) must not hover them and show them by accident.
    let (bx, by) = (cols - 14, rows - 6);
    let state = move |a: u64, b: u64| {
        json!({"panes": [
            {"pane": "p1", "rect": {"x": bx, "y": by, "w": 10, "h": 4}, "offset": a, "total": 100},
            {"pane": "p2", "rect": {"x": bx, "y": by - 6, "w": 10, "h": 2}, "offset": b, "total": 5}
        ]})
        .to_string()
    };
    let pe = |s: &mut UnixStream, pane: &str| {
        js(
            s,
            &format!(
                "(() => {{ const el = document.querySelector('.sb[data-pane=\"{pane}\"]');\
                 return el ? getComputedStyle(el).pointerEvents : 'missing'; }})()"
            ),
        )
    };
    let label_shown = |s: &mut UnixStream| {
        js(
            s,
            "(() => { const l = document.querySelector('.sb[data-pane=\"p1\"] .sb-label');\
             return l ? !l.hidden : 'missing'; })()",
        )
    };

    // The first 0x86 (p1 already scrolled to 40, p2 at the bottom) is not activity: nothing flashes, and while
    // invisible the bar takes no pointer at all, so a click at the right edge still belongs to the terminal.
    w.feed(&Frame::Scroll(state(40, 0)));
    js_until(
        &mut s,
        "document.querySelectorAll('.sb').length",
        "two bars",
        |v| v == &json!(2),
    );
    for pane in ["p1", "p2"] {
        let (o, at) = sb_opacity_until(
            &mut s,
            pane,
            "the first 0x86 of a pane never flashes it",
            Duration::from_secs(2),
            |o| o == 0.0,
        );
        assert_eq!(o, 0.0, "{pane} after {at:?}");
        assert_eq!(pe(&mut s, pane), json!("none"), "{pane} at rest");
    }

    // Re-sending the same state is not activity either.
    w.feed(&Frame::Scroll(state(40, 0)));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(
        sb_opacity(&mut s, "p1"),
        0.0,
        "an unchanged offset stays idle"
    );
    assert_eq!(sb_opacity(&mut s, "p2"), 0.0);

    // p1's offset moves: only p1 shows; p2 (unchanged) stays invisible.
    w.feed(&Frame::Scroll(state(45, 0)));
    sb_opacity_until(
        &mut s,
        "p1",
        "the pane whose offset moved shows",
        Duration::from_secs(2), // generous: the delivery gate runs this under load
        |o| o > 0.5,
    );
    assert_eq!(
        sb_opacity(&mut s, "p2"),
        0.0,
        "another pane's offset is not activity"
    );
    assert_eq!(label_shown(&mut s), json!(false), "no label without a drag");
    assert_eq!(pe(&mut s, "p1"), json!("auto"), "a shown bar is grabbable");

    // Geometry while shown but not hovered: a 6 px thumb, 2 px from the right edge of the 12 px zone.
    let geo = js(
        &mut s,
        "(() => { const el = document.querySelector('.sb[data-pane=\"p1\"]');\
         const th = el.querySelector('.sb-thumb');\
         const r = el.getBoundingClientRect(), t = th.getBoundingClientRect();\
         return {zone: r.width, thumb: t.width, gap: r.right - t.right}; })()",
    );
    let near = |v: &Value, want: f64| (v.as_f64().unwrap_or(-99.0) - want).abs() <= 0.6;
    assert!(near(&geo["zone"], 12.0), "{geo}");
    assert!(near(&geo["thumb"], 6.0), "{geo}");
    assert!(near(&geo["gap"], 2.0), "{geo}");

    // Hovering the right 12 px shows it, keeps it grabbable and widens the thumb to 10 px. Everything is read in the
    // same evaluation as the synthetic mousemove (a stray real pointer event later in the run must not matter).
    let hover = js(
        &mut s,
        "(() => { const el = document.querySelector('.sb[data-pane=\"p1\"]');\
         const r = el.getBoundingClientRect();\
         const x = r.left + r.width / 2, y = r.top + r.height / 2;\
         window.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, clientX: x, clientY: y}));\
         const th = el.querySelector('.sb-thumb').getBoundingClientRect();\
         const inz = document.elementFromPoint(x, y);\
         const outz = document.elementFromPoint(r.left - 4, y);\
         return {cls: el.className, pe: getComputedStyle(el).pointerEvents, op: getComputedStyle(el).opacity,\
                 tw: th.width, gap: el.getBoundingClientRect().right - th.right,\
                 inZone: inz && inz.closest('.sb') ? inz.closest('.sb').dataset.pane : null,\
                 outZone: outz && outz.closest('.sb') ? 'bar' : 'page'}; })()",
    );
    assert_eq!(hover["op"], json!("1"), "hover shows it: {hover}");
    assert_eq!(
        hover["pe"],
        json!("auto"),
        "a hovered bar is grabbable: {hover}"
    );
    assert!(
        hover["cls"]
            .as_str()
            .unwrap_or("")
            .split(' ')
            .any(|c| c == "hover"),
        "the hover class: {hover}"
    );
    assert!(
        near(&hover["tw"], 10.0),
        "hover widens the thumb to 10 px: {hover}"
    );
    assert!(
        near(&hover["gap"], 2.0),
        "2 px from the edge while hovered: {hover}"
    );
    assert_eq!(
        hover["inZone"],
        json!("p1"),
        "the right 12 px is the bar: {hover}"
    );
    assert_eq!(
        hover["outZone"],
        json!("page"),
        "just left of the 12 px zone is the page: {hover}"
    );

    // A drag shows the label and holds the bar up even while the thumb is held still past the fade window.
    js(
        &mut s,
        "(() => { const el = document.querySelector('.sb[data-pane=\"p1\"]');\
         const th = el.querySelector('.sb-thumb');\
         const r = th.getBoundingClientRect();\
         th.dispatchEvent(new MouseEvent('mousedown',\
           {bubbles: true, cancelable: true, button: 0, buttons: 1, clientX: r.left + r.width / 2,\
            clientY: (r.top + r.bottom) / 2})); return true; })()",
    );
    assert_eq!(
        label_shown(&mut s),
        json!(true),
        "the label appears while dragging"
    );
    let start = Instant::now();
    while start.elapsed() < Duration::from_millis(1400) {
        let o = sb_opacity(&mut s, "p1");
        assert!(
            o > 0.5,
            "visible while the thumb is held still at {:?}: {o}",
            start.elapsed()
        );
        std::thread::sleep(Duration::from_millis(200));
    }
    js(
        &mut s,
        "window.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, button: 0, buttons: 0, clientX: 0, clientY: 0})); true",
    );
    let released_at = Instant::now();
    assert_eq!(
        label_shown(&mut s),
        json!(false),
        "no label once the drag ends"
    );
    let left = Duration::from_millis(4000)
        .checked_sub(released_at.elapsed())
        .unwrap_or(Duration::from_millis(1));
    let (o, faded) = sb_opacity_until(
        &mut s,
        "p1",
        "the scrollbar fades out after the ~1 s idle hold (4 s deadline) — release",
        left,
        |o| o == 0.0,
    );
    assert_eq!(o, 0.0, "opacity {o} after {faded:?}");
}

// ---------------------------------------------------------------- A26 rev5 Linux headerbar

/// A26 rev5/rev7 (`CURRENT_REQUIREMENTS` rev 6 `utm_topbar`, rev 10 `top_container_alignment`): the Linux window
/// has **one** header row — a `GtkHeaderBar` set as the window's titlebar — split into the approved layout's two
/// containers. GTK's own close/minimize/maximize stay native widgets at the OS-default top-right anchor (the
/// `gtk-decoration-layout` setting, not a literal in our code); the sidebar header (right, above the mirrored
/// layout's sidebar band) holds a `│` boundary separator and the toolbar `◫ + ↺ ← →` in that order, right-aligned
/// beside those buttons; the workspace header (left) holds the fleet title/status, left-aligned. Both fill the row,
/// and the boundary is recomputed from the headerbar's allocation plus the `0x85` `topbar` geometry, so it follows
/// the grid's sidebar drag line. The grid keeps no topbar row (server side: `render::sidebar::topbar_cols` is 0
/// while mirrored), so there is no second band.
///
/// Source check only (no window is opened here): the GTK code is `#[cfg(target_os = "linux")]`, so it cannot be
/// compiled into a macOS test run; the native run is the parent's.
/// A42 rev12: the `term.onData` diagnostic tap (contract: `workspace/impl-1/keyboard-hook.md`) is a passive,
/// default-OFF hook on the **real** `term.onData` callback, exists only in a `--test-hooks` window, records nothing
/// until the isolated window's own `js` request enables it against a declared target, and never changes the bytes the
/// input path sends. Source check only (no window is opened here); the runtime behaviour is covered by
/// [`ondata_diagnostic_hook_is_test_hooks_only_default_off_and_passive`].
#[test]
fn gui_js_ondata_diagnostic_is_a_test_hooks_only_passive_tap() {
    let (_, js) = assets::get("gui.js").expect("the terminal page script is an asset");
    let js = std::str::from_utf8(js).expect("gui.js is UTF-8");
    // The tap lives in the term.onData callback, between the encode and the unchanged send of the same bytes.
    let ondata = js
        .find("term.onData(d => {")
        .expect("the term.onData callback is where input leaves the page");
    let tap = js
        .find("onDataDiag.record(d, bytes)")
        .expect("the tap records the callback's own string and bytes");
    let send = js
        .find("sendInput(bytes)")
        .expect("the input path still sends the same bytes");
    assert!(
        ondata < tap && tap < send,
        "the tap must sit inside the term.onData callback, before the unchanged send"
    );
    // Test-hooks only: the whole hook (including the global it exposes) is the false branch of `boot.testHooks ?`.
    let gate = js.find("const onDataDiag = boot.testHooks ?").expect("the hook is gated by --test-hooks");
    let global = js
        .find("window.__fmOnDataDiag = api;")
        .expect("the hook is reachable for a test-hooks `js` request");
    let off = js.find(": null;\n  term.onData(d => {").expect("the hook is null without --test-hooks");
    assert!(
        gate < global && global < off,
        "the diagnostic global must be created inside the --test-hooks branch only"
    );
    // Default OFF, explicit target, bounded buffer, dump/state: the contract build-1 adapts to.
    for needle in [
        "const SCHEMA = 'fm-ondata-diag/1', MAX = 500;",
        "let enabled = false, target = null, seq = 0, dropped = 0, t0 = 0, records = [];",
        "window.__FM_ON_DATA_DIAG_TARGET__",
        "fm-ondata-diag: refusing to record: no target window selected",
        "if (!enabled) return;",
        "source: 'term.onData'",
        "dump() {",
    ] {
        assert!(js.contains(needle), "gui.js must contain {needle:?}");
    }
    // The hook block itself: no event synthesis, no input writing, no prevent/stop, no ipc secret.
    let block = &js[gate..off];
    for gone in [
        "dispatchEvent",
        "KeyboardEvent",
        "preventDefault",
        "stopPropagation",
        "execCommand",
        "insertText",
        "sendInput(",
        "postMessage(",
        "TOKEN",
        "token",
    ] {
        assert!(
            !block.contains(gone),
            "the onData diagnostic must not contain {gone:?}: it only reads what the callback already produced"
        );
    }
}

#[test]
fn ondata_diagnostic_hook_is_test_hooks_only_default_off_and_passive() {
    if !display_or_skip() {
        return;
    }
    let t = Tmp::new("ondata");
    let mut w = GuiWindow::start(&t.0.join("w"));
    let mut s = w.connect();
    let (cols, rows) = w.first_size();
    assert!(cols >= 20 && rows >= 5, "{cols}×{rows}");
    // Present only because this window was launched with --test-hooks; default OFF.
    assert_eq!(js(&mut s, "typeof window.__fmOnDataDiag"), json!("object"));
    assert_eq!(js(&mut s, "window.__fmOnDataDiag.schema"), json!("fm-ondata-diag/1"));
    let st = js(&mut s, "window.__fmOnDataDiag.state()");
    assert_eq!(st["enabled"], json!(false), "{st}");
    assert_eq!(st["records"], json!(0), "{st}");
    assert_eq!(st["maxRecords"], json!(500), "{st}");
    // While OFF the tap's entry point records nothing (silently).
    js(&mut s, "window.__fmOnDataDiag.record('x')");
    assert_eq!(
        js(&mut s, "window.__fmOnDataDiag.state()")["records"],
        json!(0),
        "the diagnostic recorded while disabled"
    );
    // enable() refuses without the explicitly declared target of this isolated window.
    let refused = ask(&mut s, json!({"cmd": "js", "code": "window.__fmOnDataDiag.enable()"}));
    assert_eq!(refused["ok"], json!(false), "{refused}");
    assert!(
        refused["error"]
            .as_str()
            .unwrap_or_default()
            .contains("refusing to record"),
        "{refused}"
    );
    assert_eq!(
        js(&mut s, "window.__fmOnDataDiag.state()")["enabled"],
        json!(false),
        "a refused enable() left the diagnostic on"
    );
    // Declared target: enable() starts an empty buffer, and the recorder keeps the exact bytes it is handed.
    let on = js(
        &mut s,
        "window.__FM_ON_DATA_DIAG_TARGET__ = 'a42:iso-1'; window.__fmOnDataDiag.enable()",
    );
    assert_eq!(on["enabled"], json!(true), "{on}");
    assert_eq!(on["target"], json!("a42:iso-1"), "{on}");
    js(&mut s, "window.__fmOnDataDiag.record('\\r')");
    js(&mut s, "window.__fmOnDataDiag.record('\\u00e9')");
    let raw = js(&mut s, "JSON.stringify(window.__fmOnDataDiag.dump())");
    let raw = raw.as_str().expect("dump() stringifies to a JSON string").to_string();
    assert!(
        !raw.contains("token"),
        "the diagnostic dump must not carry the ipc secret: {raw}"
    );
    let dump: Value = serde_json::from_str(&raw).expect("the dump parses");
    assert_eq!(dump["schema"], json!("fm-ondata-diag/1"), "{dump}");
    assert_eq!(dump["target"], json!("a42:iso-1"), "{dump}");
    let recs = dump["records"].as_array().expect("records array");
    assert_eq!(recs.len(), 2, "{dump}");
    assert_eq!(recs[0]["seq"], json!(1), "{dump}");
    assert_eq!(recs[0]["hex"], json!("0d"), "Return is one 0x0d byte: {dump}");
    assert_eq!(recs[0]["bytes"], json!(1), "{dump}");
    assert_eq!(recs[0]["source"], json!("term.onData"), "{dump}");
    assert_eq!(recs[1]["hex"], json!("c3a9"), "é is UTF-8 c3 a9: {dump}");
    assert_eq!(recs[1]["bytes"], json!(2), "{dump}");
    // disable() stops recording; the buffer keeps what it had.
    assert_eq!(
        js(&mut s, "window.__fmOnDataDiag.disable()")["enabled"],
        json!(false)
    );
    js(&mut s, "window.__fmOnDataDiag.record('y')");
    assert_eq!(
        js(&mut s, "window.__fmOnDataDiag.state()")["records"],
        json!(2),
        "the diagnostic recorded after disable()"
    );
    assert_eq!(ask(&mut s, json!({"cmd": "quit"}))["ok"], json!(true));
}

/// A42-LINUX-IME-CANDIDATE-05: the caret message is window-local and event-driven.
///
/// The page sends one small message of its own (never a field on `inset`), guarded by real state — the terminal's
/// own textarea is the focused element, the page has the focus, the history iframe does not, and the caret is a
/// cell of the live grid whose rect is not xterm's offscreen default. The window process pushes it to WebKit's own
/// input method context on Linux only, with no frame to the server, no new dispatcher/thread and no timer.
///
/// Source check only (no window is opened here); the real GTK call is measured by the isolated probe in
/// `workspace/impl-1` (product binary + an interposer on `gtk_im_context_set_cursor_location`).
#[test]
fn gui_linux_caret_push_is_window_local_and_event_driven() {
    let src = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/gui.rs"))
        .expect("the gui module source");
    let (_, js) = assets::get("gui.js").expect("the terminal page script is an asset");
    let js = std::str::from_utf8(js).expect("gui.js is UTF-8");

    // The page: its own message type, its own reporter, real guards, no timer.
    assert!(
        js.contains("send('caret', c ?"),
        "the caret travels in its own small ipc message"
    );
    assert!(
        !js.contains("send('inset', { cols, cell, caret") && !js.contains("caret: c"),
        "the caret must not ride the inset message"
    );
    let block_start = js.find("const caretCell = () => {").expect("the caret reporter");
    let block_end = js
        .find("// A26: push row 0 down")
        .expect("the caret block ends before the topbar placement");
    let block = &js[block_start..block_end];
    for needle in [
        "IS_MAC || !ta",
        "if (IS_MAC) return;",
        "document.hasFocus()",
        "document.visibilityState !== 'visible'",
        "document.activeElement !== ta || inPage",
        "b.cursorY >= 0 && b.cursorY < term.rows",
        "b.cursorX >= 0 && b.cursorX < term.cols",
        "g.width > 0 && g.height > 0",
        "g.left + b.cursorX * cw",
        "g.top + b.cursorY * ch",
        "const agree =",
        "if (!force && key === lastCaret) return;",
    ] {
        assert!(block.contains(needle), "the caret guard must contain {needle:?}");
    }
    // macOS sends nothing at all: the reporter returns before it reads the state, caches a key or sends anything.
    let reporter = js
        .find("const reportCaret = (force) => {\n    if (IS_MAC) return;")
        .expect("reportCaret must return on macOS before anything is sent");
    let read_at = js[reporter..]
        .find("const c = caretCell();")
        .expect("the reporter reads the cell");
    let send_at = js[reporter..]
        .find("send('caret'")
        .expect("the caret send exists");
    assert!(
        read_at < send_at,
        "the macOS early return must come before the read and the send"
    );
    // A context change (font/resize/window state) re-reports even when the page rect is unchanged: the window
    // re-converts it against the current layout and decides whether the IM needs a new rect.
    assert!(
        js.contains("reportCaret(true); // a font change or a resize moves the caret under the IM"),
        "refit must force a caret report"
    );
    // The whole cell is kept inside the grid (the last row/column can round past its edge).
    for needle in ["gx + gw - x", "gy + gh - y", "x + w > gx + gw || y + h > gy + gh"] {
        assert!(block.contains(needle), "the cell must be clamped to the grid: {needle:?}");
    }
    for gone in ["setTimeout", "setInterval", "requestAnimationFrame", "dispatchEvent"] {
        assert!(
            !block.contains(gone),
            "the caret reporter must not use {gone:?}: it is event-driven and deduped"
        );
    }
    for site in [
        "term.onCursorMove(reportCaret)",
        "term.textarea.addEventListener('blur', () => reportCaret());",
        "window.addEventListener('focus', () => reportCaret(true));",
        "window.addEventListener('blur', () => reportCaret());",
        "reportCaret(); // a fed screen moves the cursor",
        "reportCaret(true); // a font change or a resize moves the caret under the IM",
    ] {
        assert!(js.contains(site), "a caret event is missing: {site:?}");
    }

    // The window: Linux only, one push, no wire frame, no second dispatcher.
    let cfg = src
        .find("#[cfg(target_os = \"linux\")]\n    fn push_caret_area")
        .expect("the push is Linux only");
    // The slice ends at the next method's doc block, so shortening the push (M100: the page rect is pushed
    // unchanged) cannot pull the neighbouring functions into these assertions.
    let push_end = src[cfg..]
        .find("\n\n    /// A42-LINUX-IME-CANDIDATE-05: re-assert the retained caret")
        .map(|i| cfg + i)
        .expect("the push is followed by the re-assert method");
    let push = &src[cfg..push_end];
    for needle in [
        "view.input_method_context()",
        "notify_cursor_area(rect.0, rect.1, rect.2, rect.3)",
        "let same = self.last_caret == Some(rect);",
        "if !self.focused || !view.is_mapped()",
        // M100: the client window is the webview's own GdkWindow and the page's CSS origin coincides with it, so
        // the page cell is the whole conversion — no toplevel/window origin is added on top (measured: IBus got the
        // caret plus a duplicated (5, 51)).
        "caret_to_widget(caret, (alloc.width(), alloc.height()))",
        // M105: gdk 0.18's `origin()` is `(ret, x, y)`; the cache key reads that order (M99 read `(x, y, _)`, i.e.
        // `ret` as x and the real y dropped) and keeps the scale beside the root. The key is a cache only: the
        // payload above stays the page's raw logical cell.
        "let (ret, x, y) = w.origin();",
        "caret_origin_key(ret, x, y, w.scale_factor())",
        "if let Some(inv) = caret_invalidator(rect)",
    ] {
        assert!(push.contains(needle), "the push must contain {needle:?}");
    }
    for gone in [
        "out.send",
        "Frame::",
        "set_enable_preedit",
        "spawn",
        "std::thread",
        "sleep",
        "set_interval",
        // No space conversion is re-added by hand: the rect stays client-window-relative (M100).
        "translate_coordinates",
        "allocation origin",
    ] {
        assert!(
            !push.contains(gone),
            "the caret push must not contain {gone:?}: window-local, no new dispatcher, no timer"
        );
    }
    // The ipc arm itself forwards nothing.
    let arm = src
        .find("IpcMsg::Caret(caret) =>")
        .expect("the caret ipc arm");    let arm = &src[arm..arm + 400];
    assert!(
        !arm.contains("out.send") && !arm.contains("Frame::"),
        "the caret never reaches the server line"
    );
    // A focus loss drops the cache, so a refocus re-pushes the same cell — including a report that arrived before
    // tao's `Focused(true)` (two independent notifications of the same focus-in, not ordered).
    assert!(
        src.contains("fn focus_changed(&mut self, focused: bool)"),
        "the window tracks its own focus state"
    );
    assert!(
        src.contains("self.pending_caret = Some(caret)"),
        "the latest reported caret is retained while it cannot be pushed"
    );
    assert!(
        src.contains("fn reassert_caret(&mut self)"),
        "one re-assert path is shared by the focus, geometry and composition events"
    );
    assert!(
        src.contains("if focused {\n            self.reassert_caret();\n        }"),
        "a focus regain re-pushes the retained caret instead of waiting for a report the page will not repeat"
    );
    assert!(
        src.contains("fn geometry_changed(&mut self) {\n        self.reassert_caret();"),
        "a resize/maximize/restore re-converts the retained caret (the webview's origin can move)"
    );
    // M99: a pure window move changes the client window's root origin without a page report, so the retained caret
    // is re-asserted from tao's own Moved event (no timer, no poll).
    assert!(
        src.contains("WindowEvent::Moved(_)"),
        "a window move must reach the caret cache (the root origin moves, the page cell does not)"
    );
    let moved = src
        .find("WindowEvent::Moved(_)")
        .expect("the move arm");
    assert!(
        src[moved..moved + 400].contains("ui.geometry_changed();"),
        "the move arm must use the existing geometry path, not a second dispatcher"
    );
    // M99/M105: the GTK3 ibus client drops an identical rect *before* it converts it to root coordinates, so after a
    // move the same client-relative cell never refreshes IBus. A strictly smaller, non-empty sub-rect of the cell is
    // written and immediately superseded by the real rect inside the same handler; a 1x1 cell has no sub-rect and is
    // never widened to fake one.
    assert!(
        push.contains("caret_invalidator("),
        "the push must use the invalidator helper when the root moved"
    );
    assert!(
        push.contains("self.last_root"),
        "the push tracks the client window's root origin, not just the rect"
    );
    let helper = src
        .find("fn caret_invalidator(")
        .expect("the invalidator helper");
    let helper = &src[helper..helper + 400];
    assert!(
        helper.contains("if w > 1 {")
            && helper.contains("Some((x, y, w - 1, h))")
            && helper.contains("} else if h > 1 {")
            && helper.contains("Some((x, y, w, h - 1))"),
        "the invalidator is a smaller non-empty sub-rect: width, else height"
    );
    assert!(
        helper.contains("None") && !helper.contains("w + 1"),
        "a 1x1 cell has no sub-rect: the box is never widened past its own edge"
    );
    for gone in ["GtkIMContext", "gtk_im_context", "unsafe", "set_interval"] {
        assert!(
            !helper.contains(gone),
            "the invalidator must not reach the private GTK context, use unsafe or a timer: {gone:?}"
        );
    }
    // A preedit update re-asserts the retained caret: WebKit re-sends its own cursor area at composition time from an
    // editor state that can be one font step behind the grid (measured on the product binary).
    assert!(
        src.contains("connect_preedit_changed") && src.contains("UiEvent::CaretRefresh"),
        "the GTK preedit update must reach the caret cache"
    );
    assert!(
        !src.contains("connect_preedit_started"),
        "one composition hook, not two pushes for the same emit"
    );
    assert!(
        src.contains("UiEvent::CaretRefresh => self.reassert_caret()"),
        "the composition event re-asserts the retained caret"
    );
    assert!(
        src.contains("WindowEvent::Resized(_)"),
        "the resize event must reach the caret cache"
    );
    // The conversion checks the whole extent, not only the top-left corner.
    assert!(
        src.contains("if css.x.checked_add(css.w)? > vw || css.y.checked_add(css.h)? > vh {"),
        "a rect that ends outside the webview is dropped"
    );
    assert!(
        src.contains("WindowEvent::Focused(focused)"),
        "a focus event must reach the caret cache"
    );
}

#[test]
fn gui_linux_headerbar_is_the_one_header_row() {
    let src = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/gui.rs"))
        .expect("the gui module source");
    for (needle, why) in [
        ("gtk::HeaderBar::new()", "the row is a GTK headerbar"),
        (
            "set_show_close_button(true)",
            "GTK's own close/minimize/maximize are the native window buttons",
        ),
        (
            "set_titlebar(Some(&header.bar))",
            "the headerbar is the window's titlebar, so the WM draws no second one (GTK3 docs)",
        ),
        (
            "bar.set_custom_title(Some(&row))",
            "the two containers live in that same row",
        ),
        (
            "row.set_parts(&heading, &divider, &tools)",
            "the workspace header, the separator and the sidebar toolbar are the one row's children",
        ),
        (
            "header_row::HeaderRow::new()",
            "the row is its own container: it places the boundary from the allocation, not from a size request",
        ),
        (
            "gtk::Separator::new(gtk::Orientation::Vertical)",
            "the container boundary is a real widget separator, not styling",
        ),
        (
            "buttons.pack_start(&button, false, false, 0)",
            "the toolbar is packed left to right in its declared order",
        ),
        (
            "title.set_xalign(0.0)",
            "the workspace title is left-aligned in its own container",
        ),
        (
            "status.set_xalign(0.0)",
            "the workspace status is left-aligned beside the title",
        ),
        (
            "fn size_allocate(&self, alloc: &gtk::Allocation)",
            "the boundary is recomputed on every row allocation (resize, font change, sidebar drag)",
        ),
        (
            "let row_in_bar = x0 - bar_x;",
            "the boundary is measured in the headerbar's own coordinates (the grid fills the window)",
        ),
        (
            "tools.size_allocate(&gtk::Allocation::new(x0, y0, want.max(1), height))",
            "A42-LINUX-HEADER-DRIFT-02: the row's children are placed at the row's own origin — a windowless \
             container's child allocations are in the window's coordinates, so `(0, 0)` drew the header content \
             at the window's corner (up/left of the native buttons, and it moved when the window was restored)",
        ),
        (
            "heading.size_allocate(&gtk::Allocation::new(x0 + want + 1, y0, rest, height))",
            "the toolbar starts on the boundary and spans the row's height, on the native buttons' centre",
        ),
        (
            "fn preferred_width(&self) -> (i32, i32)",
            "the row's floor is the toolbar's intrinsic width (A42-LINUX-MINWIDTH-01)",
        ),
        (
            "tools.preferred_width().0",
            "the toolbar's minimum width is respected, never squeezed away",
        ),
        (
            "header_split(",
            "the boundary math lives in one tested function",
        ),
        (
            "fn sidebar_floor(",
            "A42-LINUX-SIDEBAR-ALIGN-03: the shared floor is one tested function, not a clamp inside the header",
        ),
        (
            "sidebar_floor(row_x, tools_min, band.cols, band.cell)",
            "the left sidebar floor includes the real left offset, not right-native controls",
        ),
        (
            ".translate_coordinates(&self.bar, 0, 0)",
            "native controls on the left are accounted for by their actual allocation",
        ),
        (
            "header_css(",
            "the headerbar is painted from the fleetmux palette (rev 12 `linux_header_theme`)",
        ),
        (
            "add_provider(&provider, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION)",
            "the palette is scoped to this headerbar's style context, not the GTK/GNOME theme",
        ),
        (
            "Header::attach(&window, prefix.byte, &proxy, opts.theme.as_ref())",
            "the window hands the resolved palette to the headerbar",
        ),
        (
            "fn set_band",
            "the 0x85 `topbar` geometry drives the boundary",
        ),
        (
            "self.row.queue_resize()",
            "a new band re-runs the allocation that moves the separator",
        ),
        (
            "wrap.set_halign(gtk::Align::Fill)",
            "the row fills the headerbar, so the containers reach the window edges",
        ),
        (
            "with_visible(false)",
            "the titlebar is attached before the window is shown",
        ),
        (
            "window.set_visible(true)",
            "the window is shown once the headerbar is attached",
        ),
        (
            "fn set_topbar",
            "the 0x85 `topbar` key fills the title/status and the geometry",
        ),
        (
            "zip(*b\"scr()\")",
            "the five tools send ^b s, ^b c, ^b r, ^b (, ^b ) — the same prefix keys the row-0 cells call",
        ),
    ] {
        assert!(
            src.contains(needle),
            "gui.rs must contain {needle:?}: {why}"
        );
    }
    // The toolbar's order is the approved `◫ + ↺ ← →`, in that order in the source and packed from the left.
    let at = |needle: &str| {
        src.find(needle)
            .unwrap_or_else(|| panic!("gui.rs must contain {needle:?}"))
    };
    let order = [
        "(\"◫\", \"Fleet sidebar (^b s)\")",
        "(\"+\", \"New bash (^b c)\")",
        "(\"↺\", \"Reopen fleet (^b r)\")",
        "(\"←\", \"Previous (^b ()\")",
        "(\"→\", \"Next (^b ))\")",
    ];
    let mut last = 0;
    for needle in order {
        let i = at(needle);
        assert!(
            i >= last,
            "the toolbar order is ◫ + ↺ ← →: {needle:?} is out of order"
        );
        last = i;
    }
    for (gone, why) in [
        (
            "with_decorations(false)",
            "the native buttons are never dropped for HTML lookalikes",
        ),
        (
            "with_decorations(true)\n    let builder = builder.with_visible",
            "stale: the Linux builder has both flags on one line",
        ),
        (
            "set_xalign(1.0)",
            "the workspace title/status are left-aligned now, not squeezed right",
        ),
        (
            "row.pack_end(&button, false, false, 0)",
            "the toolbar order is explicit `pack_start`, not a reversed `pack_end` loop",
        ),
        (
            "heading.set_width_request(",
            "A42-LINUX-MINWIDTH-01: the workspace width must never become a size request — that fed the \
             headerbar's minimum width and ratcheted `WM_NORMAL_HINTS` with every resize",
        ),
        (
            "heading.set_margin_end(",
            "A42-LINUX-MINWIDTH-01: the boundary is placed by allocation, not by margins written during \
             allocation (GTK3 only applies those to a later pass)",
        ),
        (
            "size_allocate(&gtk::Allocation::new(0, 0,",
            "A42-LINUX-HEADER-DRIFT-02: never place a child at the window's corner — the row's origin is \
             (alloc.x(), alloc.y()), which is not (0, 0) in a decorated window",
        ),
    ] {
        assert!(
            !src.contains(gone),
            "gui.rs must not contain {gone:?}: {why}"
        );
    }
    // No literal decoration layout: the OS setting decides where the native buttons sit.
    assert!(
        !src.contains("set_decoration_layout"),
        "the window buttons keep the OS-default place (`gtk-decoration-layout`), never a literal layout"
    );
}

// ---------------------------------------------------------------- S1e titlebar (plan §17.2, §17.4-2 / §17.4-3)

/// S1e (plan §17.2 / §17.3 / §17.4-2 / §17.4-3): the page computes the titlebar inset from the cell width, reports
/// it to the window process (`inset`, the handshake key and the `0x06` frame) and turns row 0's blank and `─` cells
/// into window drag / double-click zoom, on macOS only (the transparent titlebar the window builder asks for).
/// Asset check only: no window is started, nothing is dragged.
#[test]
fn gui_js_has_the_titlebar_drag_zone() {
    let (mime, js) = assets::get("gui.js").expect("the terminal page script is an asset");
    assert!(mime.starts_with("text/javascript"), "{mime}");
    let js = std::str::from_utf8(js).expect("gui.js is UTF-8");
    for (needle, why) in [
        (
            "drag_window",
            "the row-0 drag zone asks the window process to drag (plan §17.2)",
        ),
        (
            "zoom_window",
            "a double click on that zone zooms the window (plan §17.2)",
        ),
        (
            "'inset'",
            "the page reports its inset to the window process (plan §17.3)",
        ),
        (
            "controlCenter",
            "the page pads row 0 to the measured window-control centre (A26)",
        ),
        (
            "81",
            "the toolbar starts one cell past 81 px of native controls + gap (A26)",
        ),
        (
            "IS_MAC",
            "the drag zone is macOS only, Linux keeps its native titlebar (plan §17.2)",
        ),
        (
            "getChars()",
            "only a blank or ─ cell is the drag zone; a button or label cell goes to the server (plan §17.2)",
        ),
    ] {
        assert!(js.contains(needle), "gui.js must contain {needle:?}: {why}");
    }
}

/// PROTOCOL.md §12: a selection the page itself makes (Option-click / forced selection, xterm's own double click) is
/// copied on mouseup, like the server's copy-on-select; Cmd-C / Ctrl-Shift-C still copies it too. Asset check only.
#[test]
fn gui_js_copies_its_own_selection_on_mouseup() {
    let (_, js) = assets::get("gui.js").expect("the terminal page script is an asset");
    let js = std::str::from_utf8(js).expect("gui.js is UTF-8");
    let at = js
        .find("copy-on-select")
        .expect("gui.js must mark its copy-on-select handler");
    let block = &js[at..js.len().min(at + 600)];
    for needle in ["'mouseup'", "hasSelection()", "getSelection()", "send('copy'"] {
        assert!(block.contains(needle), "the copy-on-select block must contain {needle:?}:\n{block}");
    }
}
