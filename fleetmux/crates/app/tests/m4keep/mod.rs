//! M4 red-test harness for `pages.rs` (design-1 m4-design.md v0.7 §8.1). It reuses `tests/common/mod.rs`
//! unchanged, with one difference: **nothing is ever deleted**. `common::Server` owns a `TempDir` whose `Drop` runs
//! `remove_dir_all`; [`Kept`] holds the server in a `ManuallyDrop`, does the same shutdown / kill as
//! `Server::drop` (the server child and the process groups this test's panes reported, nothing else) and leaves the
//! directory on disk as evidence.
//!
//! Where: `$FM4_TEST_ROOT/<pid>-<seq>/` (`rt/` = the server's runtime dir: `ctl.sock`, `server.json`,
//! `server.log`, `layout.json`; `server.out` = its stdout / stderr). Without `FM4_TEST_ROOT` the root is
//! `<temp dir>/fm4p` — still kept. `<root>/index.txt` maps each `<pid>-<seq>` to its test name, because the
//! names are too long for the 103-byte socket path limit.
//!
//! Every control request goes over the socket as JSON and every view byte over a raw view connection, so no Rust
//! symbol that M4 has not written yet is referenced: the bin compiles today and the tests fail by behaviour.
#![allow(dead_code)]

use crate::common::*;
use std::mem::ManuallyDrop;
use std::ops::Deref;
use std::sync::mpsc;

pub const T: Duration = Duration::from_secs(5);

/// The evidence root for this test process.
pub fn root() -> PathBuf {
    let p = std::env::var_os("FM4_TEST_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::temp_dir().join("fm4p"));
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&p)
        .expect("create FM4_TEST_ROOT");
    p
}

/// A `fleetmux server` this test started, kept on disk after the test.
pub struct Kept {
    srv: ManuallyDrop<Server>,
    pub dir: PathBuf,
    pub name: String,
}

impl Deref for Kept {
    type Target = Server;
    fn deref(&self) -> &Server {
        &self.srv
    }
}

impl Kept {
    /// Same fixture as `Server::start` (legacy grid chrome, `FLEETMUX_NATIVE_HEADER=0`), under the kept root.
    pub fn start(name: &str) -> Kept {
        Kept::start_with(name, &[("FLEETMUX_NATIVE_HEADER", "0")])
    }

    pub fn start_with(name: &str, env: &[(&str, &str)]) -> Kept {
        let root = root();
        let id = format!("{}-{}", std::process::id(), next_seq());
        let dir = root.join(&id);
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&dir)
            .unwrap_or_else(|e| panic!("create {}: {e}", dir.display()));
        let dir = dir.canonicalize().expect("canonical test dir");
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(root.join("index.txt"))
        {
            let _ = writeln!(f, "{id} {name}");
        }
        let _ = std::fs::write(dir.join("test.txt"), format!("{name}\n"));
        let runtime = dir.join("rt");
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&runtime)
            .expect("create runtime dir");
        let child = spawn_server(&runtime, &dir, "server", env);
        let mut srv = Server {
            child,
            tmp: TempDir(dir.clone()),
            runtime,
            pgids: Mutex::new(Vec::new()),
        };
        srv.wait_ready();
        Kept {
            srv: ManuallyDrop::new(srv),
            dir,
            name: name.to_string(),
        }
    }

    /// Where the evidence of this test is (for panic messages).
    pub fn at(&self) -> String {
        format!("[evidence {}]", self.dir.display())
    }

    /// Remember a process group a pane reported (panes spawned on a page connection are not in `srv.spawn`).
    pub fn track(&self, reply: &Value) {
        if let Some(g) = reply["pgid"].as_i64() {
            self.srv.pgids.lock().unwrap().push(g as i32);
        }
    }

    /// The server process still runs.
    pub fn alive(&self) -> bool {
        let running = unsafe { libc::kill(self.srv.child.id() as i32, 0) == 0 };
        running && try_hello(&self.srv.sock()).is_some()
    }
}

impl Drop for Kept {
    fn drop(&mut self) {
        // What `Server::drop` does, minus the `TempDir` removal (the evidence stays).
        let s: &mut Server = &mut self.srv;
        if let Ok(mut c) = Client::connect(&s.sock()) {
            let _ = c.writer.set_read_timeout(Some(Duration::from_secs(2)));
            let _ = c.try_request("server.shutdown", json!({"force": true}));
        }
        if wait_exit(&mut s.child, Duration::from_secs(2)).is_none() {
            let _ = s.child.kill();
            let _ = s.child.wait();
        }
        for &g in s.pgids.lock().unwrap().iter() {
            // Only groups this test's server reported, and only while their leader still leads them.
            if g > 1 && unsafe { libc::getpgid(g) } == g {
                unsafe { libc::kill(-g, libc::SIGKILL) };
            }
        }
        let _ = std::fs::write(
            self.dir.join("done.txt"),
            format!("{} panicking={}\n", self.name, std::thread::panicking()),
        );
        // `self.srv` is never dropped: its `TempDir` would `remove_dir_all` the evidence.
    }
}

// ------------------------------------------------------------------ control connections bound to a page

/// `hello{page}` on a fresh connection, asserting the M4 echo (design §4.2 `hello` row: `pages == 1`, `page`).
pub fn bind(srv: &Kept, page: &str) -> Client {
    let mut c = srv.client();
    let r = c.request("hello", json!({"page": page}));
    assert_eq!(
        r["ok"],
        json!(true),
        "hello{{page:{page:?}}} failed: {r} {}",
        srv.at()
    );
    assert_eq!(
        (r["pages"].as_u64(), r["page"].as_str()),
        (Some(1), Some(page)),
        "hello{{page:{page:?}}} must echo pages:1 and page:{page:?} (design §4.2); got {r} {}",
        srv.at()
    );
    c
}

/// The root connection's `hello`, asserting the server declares pages.
pub fn root_hello(srv: &Kept) -> Value {
    let r = srv.ok("hello", json!({}));
    assert_eq!(
        (r["pages"].as_u64(), r["page"].as_str()),
        (Some(1), Some("")),
        "root hello must say pages:1, page:\"\" (design §4.2); got {r} {}",
        srv.at()
    );
    r
}

/// The current page from `hello.current`.
pub fn current(srv: &Kept) -> String {
    let r = srv.ok("hello", json!({}));
    r["current"]
        .as_str()
        .unwrap_or_else(|| panic!("hello has no `current` (design §4.2): {r} {}", srv.at()))
        .to_string()
}

pub fn wait_current(srv: &Kept, want: &str) {
    let end = Instant::now() + T;
    loop {
        let r = srv.ok("hello", json!({}));
        if r["current"].as_str() == Some(want) {
            return;
        }
        assert!(
            Instant::now() < end,
            "current page never became {want:?}; last hello {r} {}",
            srv.at()
        );
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// `page.open`, asserting ok.
pub fn open(srv: &Kept, page: &str, title: &str) -> Value {
    let r = srv
        .client()
        .request("page.open", json!({"page": page, "title": title}));
    assert_eq!(
        r["ok"],
        json!(true),
        "page.open {page:?} failed: {r} {}",
        srv.at()
    );
    r
}

/// `page.open{reclaimed:true, history_url}`, asserting ok.
pub fn open_reclaimed(srv: &Kept, page: &str, title: &str, url: &str) -> Value {
    let r = srv.client().request(
        "page.open",
        json!({"page": page, "title": title, "reclaimed": true, "history_url": url}),
    );
    assert_eq!(
        r["ok"],
        json!(true),
        "page.open reclaimed {page:?} failed: {r} {}",
        srv.at()
    );
    r
}

/// `page.list` entries (design §4.2: `[{page, title, state, panes, current}]`, root first). The reply key is
/// taken to be `pages`.
pub fn page_list(srv: &Kept) -> Vec<Value> {
    let r = srv.client().request("page.list", json!({}));
    assert_eq!(r["ok"], json!(true), "page.list failed: {r} {}", srv.at());
    r["pages"]
        .as_array()
        .unwrap_or_else(|| panic!("page.list reply has no `pages` array: {r} {}", srv.at()))
        .clone()
}

pub fn page_entry(srv: &Kept, page: &str) -> Value {
    page_list(srv)
        .into_iter()
        .find(|p| p["page"] == json!(page))
        .unwrap_or_else(|| panic!("page.list has no {page:?} {}", srv.at()))
}

/// A request that must fail with exactly `code` (`Client::err` only checks `ok:false`).
pub fn expect_code(c: &mut Client, srv: &Kept, cmd: &str, args: Value, code: &str) -> Value {
    let r = c.request(cmd, args.clone());
    srv.track(&r);
    assert_eq!(
        (r["ok"].clone(), r["code"].as_str()),
        (json!(false), Some(code)),
        "{cmd} {args} must fail with {code} (design §4.2); got {r} {}",
        srv.at()
    );
    r
}

/// `pane.spawn` on connection `c` (cwd = the test dir); extra keys are merged in. Tracks the pgid.
pub fn spawn_on(
    srv: &Kept,
    c: &mut Client,
    title: &str,
    ty: &str,
    argv: &[&str],
    extra: Value,
) -> Value {
    let mut args =
        json!({"argv": argv, "cwd": srv.dir.to_str().unwrap(), "title": title, "type": ty});
    if let Some(m) = extra.as_object() {
        for (k, v) in m {
            args[k] = v.clone();
        }
    }
    let r = c.request("pane.spawn", args.clone());
    srv.track(&r);
    assert_eq!(
        r["ok"],
        json!(true),
        "pane.spawn {args} failed: {r} {}",
        srv.at()
    );
    r
}

pub fn spawn_id_on(srv: &Kept, c: &mut Client, title: &str, ty: &str) -> String {
    spawn_on(srv, c, title, ty, &["sleep", "1000"], json!({}))["pane"]
        .as_str()
        .unwrap()
        .to_string()
}

/// A `sh -c` pane on connection `c` that prints `marker` and idles.
pub fn sh_on(srv: &Kept, c: &mut Client, title: &str, ty: &str, marker: &str) -> String {
    let script = format!("echo {marker}; exec sleep 1000");
    spawn_on(srv, c, title, ty, &["sh", "-c", &script], json!({}))["pane"]
        .as_str()
        .unwrap()
        .to_string()
}

pub fn list_on(c: &mut Client, args: Value) -> Vec<Value> {
    let r = c.ok("pane.list", args);
    r["panes"].as_array().cloned().unwrap_or_default()
}

pub fn ids(panes: &[Value]) -> Vec<String> {
    panes
        .iter()
        .map(|p| p["pane"].as_str().unwrap_or("").to_string())
        .collect()
}

pub fn group_of(layout: &Value, group: &str) -> Vec<String> {
    layout["groups"][group]
        .as_array()
        .map(|a| {
            a.iter()
                .map(|v| v.as_str().unwrap_or("").to_string())
                .collect()
        })
        .unwrap_or_default()
}

pub fn all_layout_ids(layout: &Value) -> Vec<String> {
    ["supervisor", "lower", "workers"]
        .iter()
        .flat_map(|g| group_of(layout, g))
        .collect()
}

// ------------------------------------------------------------------ raw view connections

/// A raw view connection: handshake with the given extra keys, then a reader thread forwarding every frame
/// (`None` = closed). Unlike `common::Viewer` it keeps unknown frame types, so a test can assert on them.
pub struct Raw {
    pub stream: UnixStream,
    pub reply: Value,
    frames: mpsc::Receiver<Option<(u8, Vec<u8>)>>,
    pub seen: Vec<(u8, Vec<u8>)>,
    pub closed: bool,
}

impl Raw {
    pub fn attach(srv: &Kept, extra: Value) -> Raw {
        let mut stream = UnixStream::connect(srv.sock()).expect("connect view");
        let mut hello = json!({"view": 1, "cols": W, "rows": H, "term": "xterm-256color", "colorterm": "truecolor"});
        if let Some(m) = extra.as_object() {
            for (k, v) in m {
                hello[k] = v.clone();
            }
        }
        stream.write_all(format!("{hello}\n").as_bytes()).unwrap();
        let mut line = Vec::new();
        let mut byte = [0u8; 1];
        while byte[0] != b'\n' {
            stream.read_exact(&mut byte).expect("view handshake reply");
            line.push(byte[0]);
        }
        let reply: Value = serde_json::from_slice(&line).unwrap();
        assert_eq!(
            reply["ok"],
            json!(true),
            "view handshake refused: {reply} {}",
            srv.at()
        );
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
        Raw {
            stream,
            reply,
            frames,
            seen: Vec::new(),
            closed: false,
        }
    }

    /// Send one client → server frame.
    pub fn send(&mut self, ty: u8, payload: &[u8]) {
        let mut frame = vec![ty];
        frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        frame.extend_from_slice(payload);
        self.stream.write_all(&frame).unwrap();
    }

    /// One `0x04` line (M2.5 web events / replies).
    pub fn send_json(&mut self, v: Value) {
        self.send(0x04, v.to_string().as_bytes());
    }

    /// Report a web window as open (web_relay.rs `window_open`), with a moment for the server to read it.
    pub fn window_open(&mut self) {
        self.send_json(json!({"event": "window", "state": "open"}));
        std::thread::sleep(Duration::from_millis(300));
    }

    /// Take every frame that arrives within `window`.
    pub fn drain(&mut self, window: Duration) {
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

    /// The next frame of type `ty` (JSON-decoded) for which `pred` holds, within `timeout`.
    pub fn next_json(
        &mut self,
        ty: u8,
        timeout: Duration,
        pred: impl Fn(&Value) -> bool,
    ) -> Option<Value> {
        let end = Instant::now() + timeout;
        while let Some(left) = end.checked_duration_since(Instant::now()) {
            match self.frames.recv_timeout(left) {
                Ok(Some((t, payload))) => {
                    self.seen.push((t, payload.clone()));
                    if t == ty {
                        if let Ok(v) = serde_json::from_slice::<Value>(&payload) {
                            if pred(&v) {
                                return Some(v);
                            }
                        }
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

    pub fn count(&self, ty: u8) -> usize {
        self.seen.iter().filter(|(t, _)| *t == ty).count()
    }

    pub fn types(&self) -> std::collections::BTreeSet<u8> {
        self.seen.iter().map(|(t, _)| *t).collect()
    }
}

/// Run one control request on its own thread (the server holds `web.*` replies until a window answers).
pub fn request_async(srv: &Kept, cmd: &'static str, a: Value) -> std::thread::JoinHandle<Value> {
    request_async_on(srv.client(), cmd, a)
}

/// Same, on an already bound connection.
pub fn request_async_on(
    mut c: Client,
    cmd: &'static str,
    a: Value,
) -> std::thread::JoinHandle<Value> {
    std::thread::spawn(move || c.request(cmd, a))
}

/// A history page on disk under this test's dir (`<dir>/run/<sub>/history.html`), as a `file://` URL.
pub fn history_page(srv: &Kept, sub: &str) -> String {
    let d = if sub.is_empty() {
        srv.dir.join("run")
    } else {
        srv.dir.join("run").join(sub)
    };
    std::fs::create_dir_all(&d).unwrap();
    let p = d.join("history.html");
    std::fs::write(&p, format!("<html><body>history {sub}</body></html>")).unwrap();
    format!("file://{}", p.display())
}

/// A kv `sidebar` value (sidebar plan §4.5) with the given cards; each card is
/// `(id, title, state, page)`, `page = None` = no `page` key (the M3 shape).
pub fn sidebar_json(current: &str, cards: &[(&str, &str, &str, Option<&str>)]) -> String {
    let fleets: Vec<Value> = cards
        .iter()
        .map(|(id, title, state, page)| {
            let mut c = json!({"id": id, "title": title, "state": state,
                "line1": "运行中 · 监工+1 worker（0忙）", "line2": "~/.cmux-fleet-m4"});
            if let Some(p) = page {
                c["page"] = json!(p);
            }
            c
        })
        .collect();
    json!({"v": 1, "current": current, "fleets": fleets}).to_string()
}
