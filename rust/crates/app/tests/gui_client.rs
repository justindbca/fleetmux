//! M2.6 `fleetmux gui` client (PROTOCOL.md §9.5, §15.4; design.md §3.6.2 / §3.6.4). Red in W0 (`todo!()`), green
//! in W3. No graphics: the window is a Python stand-in (as `attach/web.rs`'s
//! `started_window_answers_on_web_sock_and_is_reaped`) that speaks the GUI side of the contract:
//!
//! - `--version` prints `attach::web::expected_version()`; otherwise it records its argv and pid in
//!   `<dir>/args.json`, binds `<dir>/web.sock`, then writes one `0x02` (120×40, "the page's first fit") on stdout;
//! - every frame on its stdin is appended to `<dir>/pipe.jsonl` as `{"t": type, "p": base64 payload}`; stdin EOF →
//!   exit 0;
//! - every `web.sock` request is appended to `<dir>/cmds.jsonl` and answered `{"ok":true}` (`url` answers the last
//!   `open` / `goto` URL, else `about:blank`); `quit` → exit 0;
//! - a test-only request `{"cmd":"_test", …}` (never recorded) makes it act like the real window: `input` (base64)
//!   / `resize` ([cols, rows]) → a `0x01` / `0x02` frame on stdout; `event` → that line to every `web.sock`
//!   connection; `close` → `{"event":"window","state":"closed"}` to every connection, then exit 0 (the person closed
//!   the window).
//!
//! `fleetmux gui` runs as a copy of the binary in a temp directory next to the stand-in, so the stand-in is the
//! `fleetmux-web` it finds first (PROTOCOL.md §9.4 order), whatever else is built in `target/`.
mod common;

use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

use common::{BIN, Server, b64};
use fleetmux::attach::gui::gui_possible;
use fleetmux::attach::web::{OpenEnv, expected_version};
use fleetmux_layout::Layout;
use fleetmux_proto::view::{Overlay, ScrollState};
use serde_json::{Value, json};

const T: Duration = Duration::from_secs(5);

const FAKE_WEB: &str = r#"#!/usr/bin/env python3
import base64, json, os, socket, struct, sys, threading
a = sys.argv[1:]
if '--version' in a:
    print(VERSION); sys.exit(0)
d = a[a.index('--dir') + 1]
lock = threading.Lock(); conns = []; last = ['about:blank']
def log(name, obj):
    with lock:
        with open(os.path.join(d, name), 'a') as f: f.write(json.dumps(obj) + '\n')
def frame(t, payload):
    with lock:
        sys.stdout.buffer.write(struct.pack('>BI', t, len(payload)) + payload); sys.stdout.buffer.flush()
def to_all(line):
    with lock:
        for c in list(conns):
            try: c.sendall((line + '\n').encode())
            except OSError: pass
with open(os.path.join(d, 'args.json'), 'w') as f: json.dump({'argv': a, 'pid': os.getpid()}, f)
def pipe():
    r = sys.stdin.buffer
    while True:
        h = r.read(5)
        if len(h) < 5: os._exit(0)
        t, n = struct.unpack('>BI', h); p = r.read(n)
        log('pipe.jsonl', {'t': t, 'p': base64.b64encode(p).decode()})
threading.Thread(target=pipe, daemon=True).start()
s = socket.socket(socket.AF_UNIX); s.bind(os.path.join(d, 'web.sock')); s.listen(8)
frame(0x02, struct.pack('>HH', 120, 40))
def serve(c):
    with lock: conns.append(c)
    for line in c.makefile('r'):
        req = json.loads(line); cmd = req.get('cmd')
        if cmd == '_test':
            if 'input' in req: frame(0x01, base64.b64decode(req['input']))
            if 'resize' in req: frame(0x02, struct.pack('>HH', *req['resize']))
            if 'scroll' in req: frame(0x05, json.dumps(req['scroll']).encode())
            if 'event' in req: to_all(json.dumps(req['event']))
            if req.get('close'):
                to_all(json.dumps({'event': 'window', 'state': 'closed'})); sys.stdout.close(); os._exit(0)
            c.sendall(b'{"ok":true}\n'); continue
        log('cmds.jsonl', req)
        if cmd in ('open', 'goto'): last[0] = req['url']
        reply = {'ok': True, 'url': last[0]} if cmd == 'url' else {'ok': True}
        c.sendall((json.dumps(reply) + '\n').encode())
        if cmd == 'quit': os._exit(0)
while True:
    c, _ = s.accept(); threading.Thread(target=serve, args=(c,), daemon=True).start()
"#;

fn have_python() -> bool {
    Command::new("python3")
        .arg("-c")
        .arg("1")
        .status()
        .is_ok_and(|s| s.success())
}

/// A short directory (`web.sock` must fit `sun_path`). Outside the m4 gate: under /tmp, removed on drop.
/// In the m4 gate (`FLEETMUX_GATE=m4`, brief-m4 M56): a new directory under `$FM4_TEST_ROOT`, kept as evidence:
/// never cleaned before, never removed after, and a path that already exists is a failure.
struct Tmp(PathBuf, bool);

/// Digits of a pid on this OS (macOS pids stay below 100000; Linux `pid_max` can reach 4194304).
const PID_DIGITS: usize = if cfg!(target_os = "macos") { 5 } else { 7 };

impl Tmp {
    fn new(tag: &str) -> Tmp {
        static N: AtomicU32 = AtomicU32::new(0);
        let n = N.fetch_add(1, Ordering::SeqCst);
        if let Some(root) = m4_root() {
            // the longest socket below it is the one `fleetmux gui` makes (M58: the real path, not a 24-hex
            // estimate): `x/fleetmux-web/<token>-<gui pid>/web.sock`, the token being `rt` (`common::Server`'s
            // runtime dir, checked in `Gui::start`), so 28 bytes + the pid; the name stays short (the tag goes to stdout)
            let p = m4_new_dir(
                &root,
                format!("g{}-{n}", std::process::id()),
                "/x/fleetmux-web/rt-".len() + PID_DIGITS + "/web.sock".len(),
            );
            println!("m4 evidence dir for {tag}: {}", p.display());
            std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o700)).unwrap();
            return Tmp(p, true);
        }
        let p = PathBuf::from(format!("/tmp/fmg-{tag}-{}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir(&p).unwrap();
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o700)).unwrap();
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

/// One `fleetmux gui` run against a server with a supervisor shell, a history pane (selected in the lower group)
/// and one more lower tab; kv `history_url` points at a real `history.html` in a run directory.
struct Gui {
    srv: Server,
    tmp: Tmp,
    child: Child,
    sup: String,
    hist: String,
    tab: String,
    run_dir: PathBuf,
    url: String,
}

impl Gui {
    fn start(tag: &str) -> Gui {
        let srv = Server::start();
        if is_m4() {
            // M58: the m4 socket budget in `Tmp::new` counts this token (`<runtime>`'s last component)
            assert!(srv.runtime.ends_with("rt"), "runtime {}", srv.runtime.display());
        }
        let sup = srv.spawn_id("supervisor", "supervisor", &["sh"]);
        let hist = srv.spawn_id("history", "history", &["sleep", "1000"]);
        let tab = srv.spawn_id("control", "tab", &["sleep", "1000"]);
        let tmp = Tmp::new(tag);
        let run_dir = tmp.0.join("run");
        std::fs::create_dir(&run_dir).unwrap();
        let page = run_dir.join("history.html");
        std::fs::write(&page, "<html><body>history</body></html>").unwrap();
        let url = format!("file://{}", page.display());
        srv.ok("kv.set", json!({"key": "history_url", "value": url}));
        srv.ok("pane.focus", json!({"pane": sup}));

        let bin = tmp.0.join("bin");
        std::fs::create_dir(&bin).unwrap();
        let exe = bin.join("fleetmux");
        if std::fs::hard_link(BIN, &exe).is_err() {
            std::fs::copy(BIN, &exe).unwrap();
        }
        let fake = bin.join("fleetmux-web");
        std::fs::write(
            &fake,
            FAKE_WEB.replace("VERSION", &format!("{:?}", expected_version())),
        )
        .unwrap();
        std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755)).unwrap();
        let root = tmp.0.join("x");
        std::fs::create_dir(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
        let out = std::fs::File::create(tmp.0.join("gui.out")).unwrap();
        let child = Command::new(&exe)
            .arg("gui")
            .arg("--runtime")
            .arg(&srv.runtime)
            .env("XDG_RUNTIME_DIR", &root)
            .env("DISPLAY", ":0")
            .env_remove("FLEETMUX_WEB_BIN")
            .env_remove(fleetmux::attach::web::WEB_BIN_ENV_LEGACY)
            .stdin(Stdio::null())
            .stdout(out.try_clone().unwrap())
            .stderr(out)
            .spawn()
            .expect("spawn fleetmux gui");
        Gui {
            srv,
            tmp,
            child,
            sup,
            hist,
            tab,
            run_dir,
            url,
        }
    }

    fn output(&self) -> String {
        std::fs::read_to_string(self.tmp.0.join("gui.out")).unwrap_or_default()
    }

    /// The stand-in window's directory once it has started (`args.json` written), within `T`.
    fn web_dir(&mut self) -> PathBuf {
        let parent = self.tmp.0.join("x").join("fleetmux-web");
        let end = Instant::now() + T;
        loop {
            if let Some(d) = std::fs::read_dir(&parent).ok().and_then(|it| {
                it.flatten()
                    .map(|e| e.path())
                    .find(|p| p.join("args.json").exists())
            }) {
                return d;
            }
            if let Some(st) = self.child.try_wait().unwrap() {
                panic!(
                    "fleetmux gui exited ({st}) before starting its window:\n{}",
                    self.output()
                );
            }
            assert!(
                Instant::now() < end,
                "no window started within {T:?}:\n{}",
                self.output()
            );
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn lines(&mut self, name: &str) -> Vec<Value> {
        let p = self.web_dir().join(name);
        std::fs::read_to_string(p)
            .unwrap_or_default()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    }

    /// Frames the client wrote into the window's stdin: `(type, payload)`.
    fn pipe(&mut self) -> Vec<(u8, Vec<u8>)> {
        self.lines("pipe.jsonl")
            .iter()
            .map(|v| {
                (
                    v["t"].as_u64().unwrap() as u8,
                    common_b64_decode(v["p"].as_str().unwrap()),
                )
            })
            .collect()
    }

    /// `web.sock` commands the client sent, in order.
    fn cmds(&mut self) -> Vec<Value> {
        self.lines("cmds.jsonl")
    }

    fn wait_for<R>(&mut self, what: &str, mut f: impl FnMut(&mut Gui) -> Option<R>) -> R {
        let end = Instant::now() + T;
        loop {
            if let Some(r) = f(self) {
                return r;
            }
            assert!(
                Instant::now() < end,
                "{what}: not within {T:?}; client output:\n{}",
                self.output()
            );
            std::thread::sleep(Duration::from_millis(25));
        }
    }

    /// Make the stand-in act (`_test` request on its `web.sock`, as a second connection).
    fn act(&mut self, req: Value) {
        let sock = self.web_dir().join("web.sock");
        let mut s = UnixStream::connect(sock).expect("connect stand-in web.sock");
        s.set_read_timeout(Some(T)).unwrap();
        let mut req = req;
        req["cmd"] = json!("_test");
        s.write_all(format!("{req}\n").as_bytes()).unwrap();
        if req.get("close").is_some() {
            return;
        }
        let mut r = BufReader::new(s);
        loop {
            let mut line = String::new();
            r.read_line(&mut line).expect("stand-in reply");
            if line.contains("\"ok\"") {
                return;
            }
        }
    }

    fn input(&mut self, bytes: &[u8]) {
        self.act(json!({"input": b64(bytes)}));
    }

    fn wait_exit(&mut self, within: Duration) -> Option<ExitStatus> {
        let end = Instant::now() + within;
        while Instant::now() < end {
            if let Some(st) = self.child.try_wait().unwrap() {
                return Some(st);
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        None
    }

    fn window_pid(&mut self) -> i32 {
        let args: Value = serde_json::from_str(
            &std::fs::read_to_string(self.web_dir().join("args.json")).unwrap(),
        )
        .unwrap();
        args["pid"].as_i64().unwrap() as i32
    }

    fn layout(&self) -> Value {
        self.srv.ok("layout.get", json!({}))
    }
}

impl Drop for Gui {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn common_b64_decode(s: &str) -> Vec<u8> {
    fleetmux_proto::b64::decode(s).expect("base64")
}

/// `stty size` of the supervisor pane when the layout is drawn at `cols × rows`.
fn stty_size(cols: u16, rows: u16) -> String {
    let r = Layout::new().rects(cols, rows).supervisor;
    format!("{} {}", r.h, r.w)
}

#[test]
#[ignore = "M2.6: W3"]
fn gui_forwards_screen_and_overlay_frames_in_order() {
    if !have_python() {
        eprintln!("SKIP: no python3");
        return;
    }
    let mut g = Gui::start("fwd");
    let pipe = g.wait_for("a 0x85 in the window pipe", |g| {
        let p = g.pipe();
        p.iter().any(|(t, _)| *t == 0x85).then_some(p)
    });
    assert!(
        pipe.iter()
            .all(|(t, _)| *t == 0x81 || *t == 0x85 || *t == 0x86),
        "only 0x81 / 0x85 / 0x86 go into the pipe: {:?}",
        pipe.iter().map(|(t, _)| *t).collect::<Vec<_>>()
    );
    let first = pipe.iter().position(|(t, _)| *t == 0x85).unwrap();
    assert!(
        pipe[..first].iter().any(|(t, _)| *t == 0x81),
        "the server's first 0x85 follows its first screen frame"
    );
    let o = Overlay::decode(std::str::from_utf8(&pipe[first].1).unwrap()).unwrap();
    assert_eq!(o.pane.as_deref(), Some(g.hist.as_str()));
    assert_eq!(o.url.as_deref(), Some(g.url.as_str()));
    // A later change arrives behind the screen frames of the same round.
    let n = pipe.len();
    let tab = g.tab.clone();
    g.srv.ok("pane.focus", json!({"pane": tab}));
    let later = g.wait_for("a second 0x85", |g| {
        let p = g.pipe();
        (p[n..].iter().any(|(t, _)| *t == 0x85)).then_some(p)
    });
    let o = later[n..]
        .iter()
        .find(|(t, _)| *t == 0x85)
        .map(|(_, p)| Overlay::decode(std::str::from_utf8(p).unwrap()).unwrap())
        .unwrap();
    assert_eq!(o.rect, None);
}

#[test]
#[ignore = "M2.6: W3"]
fn gui_forwards_0x86_to_window_and_0x05_to_server() {
    if !have_python() {
        eprintln!("SKIP: no python3");
        return;
    }
    let mut g = Gui::start("scroll");
    g.wait_for("first screen frame in the pipe", |g| {
        g.pipe().iter().any(|(t, _)| *t == 0x81).then_some(())
    });
    let worker = g.srv.spawn_id(
        "worker",
        "worker",
        &["sh", "-c", "seq 1 100; exec sleep 1000"],
    );
    // The server's `0x86` for the worker reaches the window pipe (the handshake asked for it).
    let pane = worker.clone();
    let total = g.wait_for("a 0x86 for the worker with history", |g| {
        for (t, p) in g.pipe() {
            if t != 0x86 {
                continue;
            }
            let Ok(s) = ScrollState::decode(std::str::from_utf8(&p).unwrap()) else {
                continue;
            };
            if let Some(x) = s.panes.iter().find(|x| x.pane == pane && x.total >= 50) {
                return Some(x.total);
            }
        }
        None
    });
    assert!(total >= 50, "worker history total: {total}");
    // The window's `0x05` goes to the server as it is; the next `0x86` reports the new offset.
    g.act(json!({"scroll": {"pane": worker, "offset": 5}}));
    let pane = worker.clone();
    let off = g.wait_for("a 0x86 with the worker at offset 5", |g| {
        for (t, p) in g.pipe() {
            if t != 0x86 {
                continue;
            }
            let Ok(s) = ScrollState::decode(std::str::from_utf8(&p).unwrap()) else {
                continue;
            };
            if let Some(x) = s.panes.iter().find(|x| x.pane == pane && x.offset == 5) {
                return Some(x.offset);
            }
        }
        None
    });
    assert_eq!(off, 5);
}

#[test]
#[ignore = "M2.6: W3"]
fn gui_window_input_and_resize_reach_server() {
    if !have_python() {
        eprintln!("SKIP: no python3");
        return;
    }
    let mut g = Gui::start("in");
    g.wait_for("first screen frame in the pipe", |g| {
        g.pipe().iter().any(|(t, _)| *t == 0x81).then_some(())
    });
    // The view handshake used the window's first 0x02 (120×40).
    g.input(b"stty size\r");
    let sup = g.sup.clone();
    g.srv.wait_text(&sup, &stty_size(120, 40), T);
    g.act(json!({"resize": [100, 30]}));
    std::thread::sleep(Duration::from_millis(300));
    g.input(b"stty size\r");
    g.srv.wait_text(&sup, &stty_size(100, 30), T);
}

#[test]
#[ignore = "M2.6: W3"]
fn gui_reports_window_open_and_web_commands_hit_embedded_page() {
    if !have_python() {
        eprintln!("SKIP: no python3");
        return;
    }
    let mut g = Gui::start("web");
    g.web_dir();
    let goto = format!("{}?x=1#h", g.url);
    // The client reported {"event":"window","state":"open"}: web.goto finds a window.
    let r = g.wait_for("web.goto with a window", |g| {
        let r = g.srv.client().request("web.goto", json!({"url": goto}));
        (r["ok"] == json!(true)).then_some(r)
    });
    assert!(r["windows"].as_u64().unwrap_or(0) >= 1, "{r}");
    g.wait_for("goto on the window's web.sock", |g| {
        g.cmds()
            .iter()
            .any(|c| c["cmd"] == json!("goto") && c["url"] == json!(goto))
            .then_some(())
    });
    let r = g.srv.ok("web.url", json!({}));
    assert_eq!(r["url"], json!(goto), "{r}");
    g.srv.ok("web.reload", json!({}));
    g.wait_for("reload on the window's web.sock", |g| {
        g.cmds()
            .iter()
            .any(|c| c["cmd"] == json!("reload"))
            .then_some(())
    });
}

#[test]
#[ignore = "M2.6: W3"]
fn gui_passes_hello_theme_to_the_window() {
    if !have_python() {
        eprintln!("SKIP: no python3");
        return;
    }
    let mut g = Gui::start("theme");
    let args: Value = serde_json::from_str(
        &std::fs::read_to_string(g.web_dir().join("args.json")).unwrap(),
    )
    .unwrap();
    let argv: Vec<&str> = args["argv"]
        .as_array()
        .expect("argv")
        .iter()
        .map(|a| a.as_str().expect("argv entry"))
        .collect();
    let i = argv
        .iter()
        .position(|a| *a == "--theme")
        .unwrap_or_else(|| panic!("no --theme in the window's argv: {argv:?}"));
    let theme: Value = serde_json::from_str(argv[i + 1])
        .unwrap_or_else(|e| panic!("--theme value is not JSON ({e}): {:?}", argv[i + 1]));
    assert_eq!(theme["bg"], json!("#1e1e1e"), "{theme}");
    assert_eq!(theme["fg"], json!("#ffffff"), "{theme}");
    assert_eq!(theme["cursor"], json!("#98989d"), "{theme}");
    assert_eq!(theme["ansi"].as_array().map(Vec::len), Some(16), "{theme}");
    assert_eq!(theme["ansi"][1], json!("#cc372e"), "{theme}");
}

#[test]
#[ignore = "M2.6: W3"]
fn gui_sends_scope_before_first_load() {
    if !have_python() {
        eprintln!("SKIP: no python3");
        return;
    }
    let mut g = Gui::start("scope");
    let cmds = g.wait_for("the first page load", |g| {
        let c = g.cmds();
        c.iter()
            .any(|c| c["cmd"] == json!("open") || c["cmd"] == json!("goto"))
            .then_some(c)
    });
    let load = cmds
        .iter()
        .position(|c| c["cmd"] == json!("open") || c["cmd"] == json!("goto"))
        .unwrap();
    let scope = cmds
        .iter()
        .position(|c| c["cmd"] == json!("scope"))
        .unwrap_or_else(|| panic!("no scope before the first load: {cmds:?}"));
    assert!(scope < load, "scope after the first load: {cmds:?}");
    assert_eq!(
        cmds[scope]["run_dir"],
        json!(g.run_dir.to_str().unwrap()),
        "{cmds:?}"
    );
    assert_eq!(cmds[load]["url"], json!(g.url), "{cmds:?}");
}

#[test]
#[ignore = "M2.6: W3"]
fn gui_detach_and_window_close_exit_cleanly() {
    if !have_python() {
        eprintln!("SKIP: no python3");
        return;
    }
    // C-b d in the window: the client stops the window, then exits 0.
    let mut g = Gui::start("det");
    g.wait_for("first screen frame in the pipe", |g| {
        g.pipe().iter().any(|(t, _)| *t == 0x81).then_some(())
    });
    let pid = g.window_pid();
    g.input(b"\x02d");
    let st = g
        .wait_exit(T)
        .unwrap_or_else(|| panic!("fleetmux gui still running after C-b d:\n{}", g.output()));
    assert_eq!(st.code(), Some(0), "{}", g.output());
    assert!(
        !common::pid_alive(pid),
        "the window process outlived its client"
    );
    let sup = g.sup.clone();
    assert!(g.srv.listed(&sup), "detach must not touch the panes");

    // The person closes the window: exit 0, panes untouched.
    let mut g = Gui::start("close");
    g.wait_for("first screen frame in the pipe", |g| {
        g.pipe().iter().any(|(t, _)| *t == 0x81).then_some(())
    });
    g.act(json!({"close": true}));
    let st = g.wait_exit(T).unwrap_or_else(|| {
        panic!(
            "fleetmux gui still running after the window closed:\n{}",
            g.output()
        )
    });
    assert_eq!(st.code(), Some(0), "{}", g.output());
    let sup = g.sup.clone();
    assert!(g.srv.listed(&sup));
}

#[test]
#[ignore = "M2.6: W3"]
fn gui_prefix_o_focuses_history_pane() {
    if !have_python() {
        eprintln!("SKIP: no python3");
        return;
    }
    let mut g = Gui::start("o");
    g.wait_for("first screen frame in the pipe", |g| {
        g.pipe().iter().any(|(t, _)| *t == 0x81).then_some(())
    });
    let (tab, sup, hist) = (g.tab.clone(), g.sup.clone(), g.hist.clone());
    g.srv.ok("pane.focus", json!({"pane": tab}));
    g.srv.ok("pane.focus", json!({"pane": sup}));
    let before = g.cmds().len();
    g.input(b"\x02o");
    let l = g.wait_for("history selected and focused", |g| {
        let l = g.layout();
        (l["focus"] == json!("lower") && l["selected"]["lower"] == json!(hist)).then_some(l)
    });
    assert_eq!(l["selected"]["lower"], json!(hist), "{l}");
    g.wait_for("focus on the window's web.sock", |g| {
        g.cmds()[before..]
            .iter()
            .any(|c| c["cmd"] == json!("focus"))
            .then_some(())
    });
}

#[test]
#[ignore = "M2.6: W3"]
fn gui_history_focus_event_focuses_lower_group() {
    if !have_python() {
        eprintln!("SKIP: no python3");
        return;
    }
    let mut g = Gui::start("hf");
    g.wait_for("first screen frame in the pipe", |g| {
        g.pipe().iter().any(|(t, _)| *t == 0x81).then_some(())
    });
    let (tab, sup, hist) = (g.tab.clone(), g.sup.clone(), g.hist.clone());
    g.srv.ok("pane.focus", json!({"pane": tab}));
    g.srv.ok("pane.focus", json!({"pane": sup}));
    g.act(json!({"event": {"event": "history_focus"}}));
    g.wait_for("history selected and focused", |g| {
        let l = g.layout();
        (l["focus"] == json!("lower") && l["selected"]["lower"] == json!(hist)).then_some(())
    });
}

#[test]
#[ignore = "M2.6: W3"]
fn gui_no_display_exits_4_with_attach_hint() {
    let web = Some(PathBuf::from("/usr/local/bin/fleetmux-web"));
    let headless = OpenEnv {
        display: false,
        web_bin: web.clone(),
        version_ok: true,
    };
    let why = gui_possible(&headless).expect_err("no display → exit 4");
    assert!(
        why.contains("fleetmux attach --runtime"),
        "the exit-4 line points at attach: {why}"
    );
    for env in [
        OpenEnv {
            display: true,
            web_bin: None,
            version_ok: false,
        },
        OpenEnv {
            display: true,
            web_bin: web.clone(),
            version_ok: false,
        },
    ] {
        let why = gui_possible(&env).expect_err(&format!("{env:?}"));
        assert!(why.contains("fleetmux attach --runtime"), "{why}");
    }
    assert_eq!(
        gui_possible(&OpenEnv {
            display: true,
            web_bin: web,
            version_ok: true,
        }),
        Ok(())
    );
}
