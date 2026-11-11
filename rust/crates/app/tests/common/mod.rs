//! Shared harness for the stage-A test files (brief-m03 §3.4): render, attach_cli, mouse, scroll, views, perf.
//! The four phase-0 files (protocol, takeover, e2e_probe, e2e_cli) keep their inline copies and do not use this.
//!
//! Part 1 is the phase-0 harness (`Server` / `Client` / `TempDir` / helpers), copied from e2e_probe.rs with
//! `pub` added so test files can `mod common; use common::*;`. Part 2 adds probe helpers, a cell-level
//! [`Viewer`] (the 0x81 stream fed into `fleetmux_term::alacritty` so tests assert on cells, not escape bytes),
//! and [`PtyProc`] (runs the real `fleetmux attach` inside a PTY).
#![allow(dead_code, unused_imports)]

// ---------------------------------------------------------------------------------------------------
// Part 1: phase-0 harness (same as the inline block in protocol.rs / takeover.rs / e2e_probe.rs / e2e_cli.rs).
// ---------------------------------------------------------------------------------------------------
pub use serde_json::{Value, json};
pub use std::io::{BufRead, BufReader, Read, Write};
pub use std::os::unix::fs::DirBuilderExt;
pub use std::os::unix::net::UnixStream;
pub use std::path::{Path, PathBuf};
pub use std::process::{Child, Command, ExitStatus, Stdio};
pub use std::sync::Mutex;
pub use std::sync::atomic::{AtomicU32, Ordering};
pub use std::time::{Duration, Instant};

pub const BIN: &str = env!("CARGO_BIN_EXE_fleetmux");
pub const SESSION: &str = "e2e";
pub const START_TIMEOUT: Duration = Duration::from_secs(10);
pub const CALL_TIMEOUT: Duration = Duration::from_secs(15);

pub static SEQ: AtomicU32 = AtomicU32::new(0);

pub fn next_seq() -> u32 {
    SEQ.fetch_add(1, Ordering::SeqCst)
}

/// Short private temp directory (macOS `sockaddr_un` path limit is 103 bytes), removed on drop.
pub struct TempDir(pub PathBuf);

impl TempDir {
    pub fn new() -> TempDir {
        let p = std::env::temp_dir().join(format!("ftm{}-{}", std::process::id(), next_seq()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&p)
            .expect("create temp dir");
        TempDir(p.canonicalize().expect("canonical temp dir"))
    }

    pub fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

pub fn uid() -> u32 {
    unsafe { libc::getuid() }
}

pub fn pid_alive(pid: i32) -> bool {
    unsafe { libc::kill(pid, 0) == 0 }
}

pub fn group_alive(pgid: i32) -> bool {
    unsafe { libc::kill(-pgid, 0) == 0 }
}

pub fn wait_until<T>(what: &str, timeout: Duration, mut f: impl FnMut() -> Option<T>) -> T {
    let end = Instant::now() + timeout;
    loop {
        if let Some(v) = f() {
            return v;
        }
        if Instant::now() >= end {
            panic!("timed out after {timeout:?} waiting for {what}");
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

pub fn wait_exit(child: &mut Child, timeout: Duration) -> Option<ExitStatus> {
    let end = Instant::now() + timeout;
    loop {
        if let Ok(Some(status)) = child.try_wait() {
            return Some(status);
        }
        if Instant::now() >= end {
            return None;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

pub fn b64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [
            chunk[0],
            chunk.get(1).copied().unwrap_or(0),
            chunk.get(2).copied().unwrap_or(0),
        ];
        let n = (b[0] as u32) << 16 | (b[1] as u32) << 8 | b[2] as u32;
        for i in 0..4 {
            out.push(if i <= chunk.len() {
                T[(n >> (18 - 6 * i)) as usize & 63] as char
            } else {
                '='
            });
        }
    }
    out
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn tail(path: &Path, lines: usize) -> String {
    let text =
        std::fs::read_to_string(path).unwrap_or_else(|e| format!("<{}: {e}>", path.display()));
    let all: Vec<&str> = text.lines().collect();
    all[all.len().saturating_sub(lines)..].join("\n")
}

/// Polls `path` until its bytes equal `want`; on timeout shows what was there.
pub fn wait_file_eq(path: &Path, want: &[u8], timeout: Duration) {
    let end = Instant::now() + timeout;
    loop {
        let got = std::fs::read(path).unwrap_or_default();
        if got == want {
            return;
        }
        if Instant::now() >= end {
            panic!(
                "{}: want {:?}, got {:?}",
                path.display(),
                String::from_utf8_lossy(want),
                String::from_utf8_lossy(&got)
            );
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// One control connection: one JSON request per line, replies in request order (PROTOCOL.md §1).
pub struct Client {
    pub reader: BufReader<UnixStream>,
    pub writer: UnixStream,
    pub next_id: u64,
}

impl Client {
    pub fn connect(sock: &Path) -> std::io::Result<Client> {
        let s = UnixStream::connect(sock)?;
        s.set_read_timeout(Some(CALL_TIMEOUT))?;
        s.set_write_timeout(Some(CALL_TIMEOUT))?;
        Ok(Client {
            reader: BufReader::new(s.try_clone()?),
            writer: s,
            next_id: 1,
        })
    }

    pub fn send_line(&mut self, line: &str) {
        self.writer
            .write_all(format!("{line}\n").as_bytes())
            .expect("write request line");
    }

    pub fn try_recv(&mut self) -> std::io::Result<Value> {
        let mut line = String::new();
        if self.reader.read_line(&mut line)? == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "server closed the connection",
            ));
        }
        assert!(
            line.ends_with('\n'),
            "reply is not newline-terminated: {line:?}"
        );
        serde_json::from_str(&line).map_err(|e| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, format!("{e}: {line:?}"))
        })
    }

    pub fn recv(&mut self) -> Value {
        self.try_recv()
            .unwrap_or_else(|e| panic!("reading a reply: {e}"))
    }

    pub fn try_request(&mut self, cmd: &str, args: Value) -> std::io::Result<Value> {
        let id = self.next_id;
        self.next_id += 1;
        let mut req = if args.is_object() { args } else { json!({}) };
        req["id"] = json!(id);
        req["cmd"] = json!(cmd);
        self.writer.write_all(format!("{req}\n").as_bytes())?;
        let reply = self.try_recv()?;
        assert_eq!(
            reply["id"],
            json!(id),
            "reply id does not match request {req}: {reply}"
        );
        Ok(reply)
    }

    /// Full reply, `ok` either way.
    pub fn request(&mut self, cmd: &str, args: Value) -> Value {
        self.try_request(cmd, args.clone())
            .unwrap_or_else(|e| panic!("{cmd} {args}: {e}"))
    }

    /// Asserts `ok: true`, returns the reply.
    pub fn ok(&mut self, cmd: &str, args: Value) -> Value {
        let r = self.request(cmd, args.clone());
        assert_eq!(r["ok"], json!(true), "{cmd} {args} failed: {r}");
        r
    }

    /// Asserts `ok: false` with a non-empty `error`, returns the `code`.
    pub fn err(&mut self, cmd: &str, args: Value) -> String {
        let r = self.request(cmd, args.clone());
        assert_eq!(
            r["ok"],
            json!(false),
            "{cmd} {args} should have failed: {r}"
        );
        assert!(
            r["error"].as_str().is_some_and(|e| !e.is_empty()),
            "error reply without message: {r}"
        );
        r["code"]
            .as_str()
            .unwrap_or_else(|| panic!("error reply without code: {r}"))
            .to_string()
    }
}

pub fn try_hello(sock: &Path) -> Option<Value> {
    let mut c = Client::connect(sock).ok()?;
    let r = c.try_request("hello", json!({})).ok()?;
    (r["ok"] == json!(true)).then_some(r)
}

/// `fleetmux server --runtime <dir> --session e2e` in the foreground, output to `<log_dir>/<name>.out`.
pub fn spawn_server(runtime: &Path, log_dir: &Path, name: &str, env: &[(&str, &str)]) -> Child {
    let out =
        std::fs::File::create(log_dir.join(format!("{name}.out"))).expect("server output file");
    Command::new(BIN)
        .arg("server")
        .arg("--runtime")
        .arg(runtime)
        .args(["--session", SESSION])
        .envs(env.iter().copied())
        .stdin(Stdio::null())
        .stdout(Stdio::from(out.try_clone().expect("dup output file")))
        .stderr(Stdio::from(out))
        .spawn()
        .expect("spawn fleetmux server")
}

/// A server this test started; on drop it is shut down / killed and every pane group it reported is killed.
pub struct Server {
    pub child: Child,
    pub tmp: TempDir,
    pub runtime: PathBuf,
    pub pgids: Mutex<Vec<i32>>,
}

impl Server {
    /// Legacy grid-chrome fixtures run identically on both hosts. Native-header tests use
    /// start_with_env explicitly, so they exercise the real platform default instead.
    pub fn start() -> Server {
        let mut srv = Server::launch(&[("FLEETMUX_NATIVE_HEADER", "0")]);
        srv.wait_ready();
        srv
    }

    /// Starts the process without waiting for `hello` (for servers that are expected to refuse peers).
    pub fn launch(env: &[(&str, &str)]) -> Server {
        let tmp = TempDir::new();
        let runtime = tmp.path().join("rt");
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&runtime)
            .expect("create runtime dir");
        let child = spawn_server(&runtime, tmp.path(), "server", env);
        Server {
            child,
            tmp,
            runtime,
            pgids: Mutex::new(Vec::new()),
        }
    }

    pub fn sock(&self) -> PathBuf {
        self.runtime.join("ctl.sock")
    }

    pub fn pid(&self) -> i32 {
        self.child.id() as i32
    }

    pub fn diagnostics(&self) -> String {
        format!(
            "server output:\n{}\nserver.log tail:\n{}",
            tail(&self.tmp.path().join("server.out"), 20),
            tail(&self.runtime.join("server.log"), 20)
        )
    }

    pub fn wait_ready(&mut self) -> Value {
        let end = Instant::now() + START_TIMEOUT;
        loop {
            if let Some(h) = try_hello(&self.sock()) {
                return h;
            }
            if let Ok(Some(status)) = self.child.try_wait() {
                panic!(
                    "fleetmux server exited before answering hello ({status})\n{}",
                    self.diagnostics()
                );
            }
            if Instant::now() >= end {
                panic!(
                    "fleetmux server did not answer hello within {START_TIMEOUT:?}\n{}",
                    self.diagnostics()
                );
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    pub fn client(&self) -> Client {
        Client::connect(&self.sock()).unwrap_or_else(|e| {
            panic!(
                "connect {}: {e}\n{}",
                self.sock().display(),
                self.diagnostics()
            )
        })
    }

    pub fn ok(&self, cmd: &str, args: Value) -> Value {
        self.client().ok(cmd, args)
    }

    pub fn err(&self, cmd: &str, args: Value) -> String {
        self.client().err(cmd, args)
    }

    /// `pane.spawn` with cwd = this test's temp dir; remembers the pgid for cleanup.
    pub fn spawn(&self, title: &str, ty: &str, argv: &[&str], env: Value) -> Value {
        let cwd = self.tmp.path().to_str().expect("utf-8 temp path");
        let r = self.ok(
            "pane.spawn",
            json!({"argv": argv, "cwd": cwd, "env": env, "title": title, "type": ty}),
        );
        if let Some(g) = r["pgid"].as_i64() {
            self.pgids.lock().unwrap().push(g as i32);
        }
        r
    }

    /// Spawns and returns the pane id.
    pub fn spawn_id(&self, title: &str, ty: &str, argv: &[&str]) -> String {
        let r = self.spawn(title, ty, argv, json!({}));
        r["pane"]
            .as_str()
            .unwrap_or_else(|| panic!("pane.spawn reply without pane: {r}"))
            .to_string()
    }

    pub fn panes(&self) -> Vec<Value> {
        let r = self.ok("pane.list", json!({}));
        r["panes"]
            .as_array()
            .unwrap_or_else(|| panic!("pane.list reply without panes array: {r}"))
            .clone()
    }

    /// M4: `pane.list {all: true}` — every page's panes (each record carries its `page`).
    pub fn panes_all(&self) -> Vec<Value> {
        let r = self.ok("pane.list", json!({"all": true}));
        r["panes"]
            .as_array()
            .unwrap_or_else(|| panic!("pane.list reply without panes array: {r}"))
            .clone()
    }

    /// M4: the current page's id from `page.list` (`""` = the root page).
    pub fn current_page(&self) -> String {
        let r = self.ok("page.list", json!({}));
        r["pages"]
            .as_array()
            .and_then(|ps| ps.iter().find(|p| p["current"] == json!(true)))
            .and_then(|p| p["page"].as_str())
            .unwrap_or_else(|| panic!("page.list without a current page: {r}"))
            .to_string()
    }

    /// M4: the page ids `page.list` names (`""` = the root page).
    pub fn page_ids(&self) -> Vec<String> {
        let r = self.ok("page.list", json!({}));
        r["pages"]
            .as_array()
            .map(|ps| ps.iter().filter_map(|p| p["page"].as_str().map(String::from)).collect())
            .unwrap_or_default()
    }

    pub fn listed(&self, id: &str) -> bool {
        self.panes().iter().any(|p| p["pane"] == json!(id))
    }

    pub fn pane(&self, id: &str) -> Value {
        self.ok("pane.get", json!({"pane": id}))
    }

    pub fn read(&self, id: &str, lines: u64) -> String {
        let r = self.ok("pane.read", json!({"pane": id, "lines": lines}));
        r["text"]
            .as_str()
            .unwrap_or_else(|| panic!("pane.read reply without text: {r}"))
            .to_string()
    }

    pub fn write(&self, id: &str, data: &[u8], mode: &str) -> Value {
        self.ok(
            "pane.write",
            json!({"pane": id, "data": b64(data), "mode": mode}),
        )
    }

    pub fn wait_text(&self, id: &str, needle: &str, timeout: Duration) -> String {
        let end = Instant::now() + timeout;
        loop {
            let text = self.read(id, 1000);
            if text.contains(needle) {
                return text;
            }
            if Instant::now() >= end {
                panic!("pane {id} never showed {needle:?} within {timeout:?}; screen:\n{text}");
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    pub fn wait_gone(&self, id: &str, timeout: Duration) {
        wait_until(&format!("pane {id} to leave pane.list"), timeout, || {
            (!self.listed(id)).then_some(())
        });
    }

    /// Ids of `group`, in tab order.
    pub fn group_order(&self, group: &str) -> Vec<String> {
        let mut panes: Vec<Value> = self
            .panes()
            .into_iter()
            .filter(|p| p["group"] == json!(group))
            .collect();
        panes.sort_by_key(|p| {
            p["index"]
                .as_u64()
                .unwrap_or_else(|| panic!("pane without index: {p}"))
        });
        panes
            .iter()
            .map(|p| p["pane"].as_str().unwrap().to_string())
            .collect()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        if let Ok(mut c) = Client::connect(&self.sock()) {
            let _ = c.writer.set_read_timeout(Some(Duration::from_secs(2)));
            let _ = c.try_request("server.shutdown", json!({"force": true}));
        }
        if wait_exit(&mut self.child, Duration::from_secs(2)).is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
        for &g in self.pgids.lock().unwrap().iter() {
            // Only groups this test's server reported, and only while their leader still leads them.
            if g > 1 && unsafe { libc::getpgid(g) } == g {
                unsafe { libc::kill(-g, libc::SIGKILL) };
            }
        }
    }
}
// ---------------------------------------------------------------------------------------------------
// Part 2: additions for the stage-A files.
// ---------------------------------------------------------------------------------------------------
pub use fleetmux_term::alacritty::Alacritty;
pub use fleetmux_term::{Attrs, Cell, Color, TermParser};
pub use std::sync::Arc;

/// Outer size the layout contract numbers are given for (brief-m03 §3.4 `hit_test_tab_pane_separator_status`).
pub const W: u16 = 240;
pub const H: u16 = 60;
/// 0-based cells at 240x60 with the default 50 / 60 ratios: status row, vertical separator column, the left
/// column's horizontal separator row, and one cell inside each group's content area, as (col, row).
pub const STATUS_ROW: u16 = 59;
pub const VSEP_COL: u16 = 119;
pub const HSEP_ROW: u16 = 35;
pub const IN_SUPERVISOR: (u16, u16) = (60, 10);
pub const IN_LOWER: (u16, u16) = (60, 45);
pub const IN_WORKERS: (u16, u16) = (180, 30);

/// The default prefix key, `C-b` (PROTOCOL.md §10).
pub const PREFIX: u8 = 0x02;

/// Prefix + `key`, as one input frame's payload.
pub fn prefixed(key: &[u8]) -> Vec<u8> {
    let mut v = vec![PREFIX];
    v.extend_from_slice(key);
    v
}

/// SGR mouse button codes (low bits) and modifier / motion bits.
pub const BTN_LEFT: u8 = 0;
pub const WHEEL_UP: u8 = 64;
pub const WHEEL_DOWN: u8 = 65;
pub const MOD_SHIFT: u8 = 4;
pub const MOTION: u8 = 32;

/// SGR mouse report (`CSI < b ; x ; y M|m`) for a 0-based cell; the wire coordinates are 1-based.
pub fn sgr(b: u8, col: u16, row: u16, press: bool) -> Vec<u8> {
    format!(
        "\x1b[<{b};{};{}{}",
        col + 1,
        row + 1,
        if press { 'M' } else { 'm' }
    )
    .into_bytes()
}

/// Left-button press at `from`, one drag motion to `to`, release at `to` (0-based cells).
pub fn sgr_drag(from: (u16, u16), to: (u16, u16), mods: u8) -> Vec<u8> {
    let mut v = sgr(BTN_LEFT | mods, from.0, from.1, true);
    v.extend(sgr(BTN_LEFT | MOTION | mods, to.0, to.1, true));
    v.extend(sgr(BTN_LEFT | mods, to.0, to.1, false));
    v
}

/// Left click (press + release) at a 0-based cell.
pub fn sgr_click(col: u16, row: u16, mods: u8) -> Vec<u8> {
    let mut v = sgr(BTN_LEFT | mods, col, row, true);
    v.extend(sgr(BTN_LEFT | mods, col, row, false));
    v
}

/// Standard base64 decoder for test assertions (OSC 52 payloads); `None` on malformed input.
pub fn b64_decode(s: &str) -> Option<Vec<u8>> {
    let val = |c: u8| -> Option<u32> {
        Some(match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        } as u32)
    };
    let s = s.trim_end_matches('=').as_bytes();
    let mut out = Vec::with_capacity(s.len() * 3 / 4);
    for chunk in s.chunks(4) {
        if chunk.len() == 1 {
            return None;
        }
        let mut n = 0u32;
        for (i, &c) in chunk.iter().enumerate() {
            n |= val(c)? << (18 - 6 * i);
        }
        out.push((n >> 16) as u8);
        if chunk.len() > 2 {
            out.push((n >> 8) as u8);
        }
        if chunk.len() > 3 {
            out.push(n as u8);
        }
    }
    Some(out)
}

impl Server {
    /// [`Server::launch`] with extra environment, then waits for `hello`.
    pub fn start_with_env(env: &[(&str, &str)]) -> Server {
        let mut srv = Server::launch(env);
        srv.wait_ready();
        srv
    }

    /// `server.shutdown` (force) and wait for the process to exit, then start a new server on the same
    /// runtime directory; returns the new server's `hello`.
    pub fn restart(&mut self) -> Value {
        if let Ok(mut c) = Client::connect(&self.sock()) {
            let _ = c.try_request("server.shutdown", json!({"force": true}));
        }
        if wait_exit(&mut self.child, Duration::from_secs(10)).is_none() {
            panic!(
                "server did not exit after server.shutdown\n{}",
                self.diagnostics()
            );
        }
        self.child = spawn_server(&self.runtime, self.tmp.path(), "server", &[]);
        self.wait_ready()
    }

    /// `server.stats` result (PROTOCOL.md §4.17).
    pub fn stats(&self) -> Value {
        self.ok("server.stats", json!({}))
    }

    /// `layout.get` result.
    pub fn layout(&self) -> Value {
        self.ok("layout.get", json!({}))
    }

    /// Waits until `layout.get` satisfies `pred`; returns that layout.
    pub fn wait_layout(&self, what: &str, timeout: Duration, pred: impl Fn(&Value) -> bool) -> Value {
        let end = Instant::now() + timeout;
        loop {
            let last = self.layout();
            if pred(&last) {
                return last;
            }
            if Instant::now() >= end {
                panic!("layout never reached {what} within {timeout:?}; last layout.get: {last}");
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }
}

/// A shell one-liner pane: `sh -c <script>`.
pub fn sh(srv: &Server, title: &str, ty: &str, script: &str) -> String {
    srv.spawn_id(title, ty, &["sh", "-c", script])
}

// ----------------------------------------------- ft-probe ------------------------------------------

/// ft-probe lives in another package, so Cargo gives no `CARGO_BIN_EXE_` for it: `$FT_PROBE_BIN`, else
/// next to the fleetmux binary (`cargo build -p ft-probe` / `cargo build --workspace` puts it there).
pub fn probe_bin() -> PathBuf {
    let path = std::env::var_os("FT_PROBE_BIN")
        .map(PathBuf::from)
        .unwrap_or_else(|| Path::new(BIN).with_file_name("ft-probe"));
    assert!(
        path.exists(),
        "ft-probe not found at {} (run `cargo build -p ft-probe` or set FT_PROBE_BIN)",
        path.display()
    );
    path
}

/// The probe's JSON-lines log (crates/probe/README.md "Event log"); a torn last line is skipped.
pub fn probe_events(log: &Path) -> Vec<Value> {
    std::fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter_map(|l| serde_json::from_str(l).ok())
        .collect()
}

pub fn ev_is(e: &Value, kind: &str) -> bool {
    e["ev"] == json!(kind)
}

pub fn wait_event(log: &Path, what: &str, timeout: Duration, pred: impl Fn(&Value) -> bool) -> Value {
    let end = Instant::now() + timeout;
    loop {
        if let Some(ev) = probe_events(log).into_iter().find(|e| pred(e)) {
            return ev;
        }
        if Instant::now() >= end {
            panic!(
                "probe never logged {what} within {timeout:?}; log:\n{}",
                tail(log, 40)
            );
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

pub fn wait_events(
    log: &Path,
    what: &str,
    n: usize,
    timeout: Duration,
    pred: impl Fn(&Value) -> bool,
) -> Vec<Value> {
    let end = Instant::now() + timeout;
    loop {
        let found: Vec<Value> = probe_events(log).into_iter().filter(|e| pred(e)).collect();
        if found.len() >= n {
            return found;
        }
        if Instant::now() >= end {
            panic!(
                "probe logged {} of {n} {what} within {timeout:?}; log:\n{}",
                found.len(),
                tail(log, 40)
            );
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// Spawns ft-probe as a pane of type `ty` and waits for its `ready` event. Returns (pane id, log path).
pub fn start_probe_as(srv: &Server, title: &str, ty: &str, flags: &[&str]) -> (String, PathBuf) {
    let bin = probe_bin();
    let log = srv.tmp.path().join(format!("probe-{}.jsonl", next_seq()));
    let mut argv = vec![bin.to_str().expect("utf-8 probe path")];
    argv.extend_from_slice(flags);
    let r = srv.spawn(
        title,
        ty,
        &argv,
        json!({"FT_PROBE_LOG": log.to_str().unwrap()}),
    );
    let id = r["pane"]
        .as_str()
        .unwrap_or_else(|| panic!("no pane: {r}"))
        .to_string();
    wait_event(&log, "ready", Duration::from_secs(10), |e| {
        ev_is(e, "ready")
    });
    (id, log)
}

/// ft-probe as a worker pane titled `fx-probe`.
pub fn start_probe(srv: &Server, flags: &[&str]) -> (String, PathBuf) {
    start_probe_as(srv, "fx-probe", "worker", flags)
}

pub fn pane_size(srv: &Server, id: &str) -> (u64, u64) {
    let p = srv.pane(id);
    (
        p["cols"]
            .as_u64()
            .unwrap_or_else(|| panic!("pane.get without cols: {p}")),
        p["rows"]
            .as_u64()
            .unwrap_or_else(|| panic!("pane.get without rows: {p}")),
    )
}

// ----------------------------------------------- Viewer --------------------------------------------

/// What the reader thread collected from the server's frames.
#[derive(Default)]
struct Inbox {
    /// Every 0x81 payload byte, in order.
    screen: Vec<u8>,
    /// Payload length of each 0x81 frame.
    frames: Vec<usize>,
    /// 0x83 OPEN payloads (UTF-8, lossy).
    opens: Vec<String>,
    /// 0x82 payload, once the server said it is exiting.
    exit: Option<String>,
    /// The connection is closed.
    closed: bool,
}

/// A view connection (PROTOCOL.md §6) whose 0x81 bytes are fed into an `alacritty` parser of the viewer's
/// size, so tests read the picture cell by cell. A background thread drains the socket into an inbox (the
/// server never blocks on us); the parser lives on the test thread and catches up on every read.
pub struct Viewer {
    pub screen: Alacritty,
    pub cols: u16,
    pub rows: u16,
    stream: UnixStream,
    inbox: Arc<Mutex<Inbox>>,
    fed: usize,
}

impl Viewer {
    /// Truecolor viewer (`term` xterm-256color, `colorterm` truecolor).
    pub fn attach(srv: &Server, cols: u16, rows: u16) -> Viewer {
        Viewer::attach_with(srv, cols, rows, "truecolor")
    }

    /// Viewer with the given `colorterm` handshake value (`""` = unset).
    pub fn attach_with(srv: &Server, cols: u16, rows: u16, colorterm: &str) -> Viewer {
        Self::attach_chrome(srv, cols, rows, colorterm, false)
    }

    /// The Linux GUI's capability handshake, without pretending a terminal attach has GTK chrome.
    pub fn attach_native(srv: &Server, cols: u16, rows: u16) -> Viewer {
        Self::attach_chrome(srv, cols, rows, "truecolor", true)
    }

    fn attach_chrome(srv: &Server, cols: u16, rows: u16, colorterm: &str, native: bool) -> Viewer {
        let mut stream = srv.client().writer.try_clone().expect("clone stream");
        let hello = json!({"view": 1, "cols": cols, "rows": rows, "term": "xterm-256color", "colorterm": colorterm,
            "native_header": u8::from(native)});
        stream
            .write_all(format!("{hello}\n").as_bytes())
            .expect("send view handshake");
        // Read the reply line byte by byte so no frame bytes are swallowed by a buffer.
        let mut line = Vec::new();
        let mut byte = [0u8; 1];
        while byte[0] != b'\n' {
            stream.read_exact(&mut byte).expect("view handshake reply");
            line.push(byte[0]);
        }
        let reply: Value = serde_json::from_slice(&line).expect("view handshake reply is JSON");
        assert_eq!(reply["ok"], json!(true), "view handshake refused: {reply}");
        assert_eq!(reply["protocol"].as_u64(), Some(1), "{reply}");

        let inbox = Arc::new(Mutex::new(Inbox::default()));
        let (mut rx, sink) = (stream.try_clone().unwrap(), inbox.clone());
        rx.set_read_timeout(None).unwrap();
        std::thread::spawn(move || {
            let mut head = [0u8; 5];
            while rx.read_exact(&mut head).is_ok() {
                let len = u32::from_be_bytes([head[1], head[2], head[3], head[4]]) as usize;
                let mut payload = vec![0u8; len];
                if rx.read_exact(&mut payload).is_err() {
                    break;
                }
                let mut ib = sink.lock().unwrap();
                match head[0] {
                    0x81 => {
                        ib.frames.push(len);
                        ib.screen.extend_from_slice(&payload);
                    }
                    0x82 => ib.exit = Some(String::from_utf8_lossy(&payload).into_owned()),
                    0x83 => ib.opens.push(String::from_utf8_lossy(&payload).into_owned()),
                    _ => {}
                }
            }
            sink.lock().unwrap().closed = true;
        });
        Viewer {
            screen: fleetmux_term::alacritty::new(cols, rows, 0),
            cols,
            rows,
            stream,
            inbox,
            fed: 0,
        }
    }

    pub fn send(&mut self, ty: u8, payload: &[u8]) {
        let mut frame = vec![ty];
        frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        frame.extend_from_slice(payload);
        self.stream.write_all(&frame).expect("send view frame");
    }

    /// One 0x01 input frame.
    pub fn input(&mut self, bytes: &[u8]) {
        self.send(0x01, bytes);
    }

    /// 0x02 resize; the local parser follows.
    pub fn resize(&mut self, cols: u16, rows: u16) {
        let mut p = cols.to_be_bytes().to_vec();
        p.extend_from_slice(&rows.to_be_bytes());
        self.send(0x02, &p);
        self.pump();
        self.screen.resize(cols, rows);
        (self.cols, self.rows) = (cols, rows);
    }

    /// 0x03 detach.
    pub fn detach(&mut self) {
        self.send(0x03, &[]);
    }

    /// Feeds the 0x81 bytes received since the last call into the parser.
    fn pump(&mut self) {
        let inbox = self.inbox.clone();
        let ib = inbox.lock().unwrap();
        if ib.screen.len() > self.fed {
            self.screen.feed(&ib.screen[self.fed..]);
            self.fed = ib.screen.len();
        }
        drop(ib);
        let _ = self.screen.take_replies();
    }

    /// All 0x81 bytes so far.
    pub fn raw(&self) -> Vec<u8> {
        self.inbox.lock().unwrap().screen.clone()
    }

    /// Number of 0x81 bytes so far.
    pub fn raw_len(&self) -> usize {
        self.inbox.lock().unwrap().screen.len()
    }

    /// Payload sizes of the 0x81 frames so far.
    pub fn frames(&self) -> Vec<usize> {
        self.inbox.lock().unwrap().frames.clone()
    }

    /// 0x83 OPEN payloads so far.
    pub fn opens(&self) -> Vec<String> {
        self.inbox.lock().unwrap().opens.clone()
    }

    pub fn closed(&self) -> bool {
        self.inbox.lock().unwrap().closed
    }

    /// The cell at (`row`, `col`) of the viewer's screen, 0-based.
    pub fn cell(&mut self, row: u16, col: u16) -> Cell {
        self.pump();
        self.screen.cell(row, col)
    }

    /// Row `row` as text: one char per cell (wide-char placeholders skipped), trailing blanks stripped.
    pub fn row_text(&mut self, row: u16) -> String {
        self.pump();
        self.parsed_row(row)
    }

    /// Row `row` of the parser as it is now, without feeding newer bytes.
    fn parsed_row(&self, row: u16) -> String {
        let s: String = (0..self.cols)
            .map(|c| self.screen.cell(row, c))
            .filter(|c| c.width != 0)
            .map(|c| if c.ch == '\0' { ' ' } else { c.ch })
            .collect();
        s.trim_end().to_string()
    }

    /// The whole screen, rows joined with `\n`. Fed once, then read: every row comes from the same frame (feeding
    /// per row let a frame that arrived mid-read put its lower rows under the previous frame's upper rows).
    pub fn text(&mut self) -> String {
        self.pump();
        (0..self.rows)
            .map(|r| self.parsed_row(r))
            .collect::<Vec<_>>()
            .join("\n")
    }

    /// The bottom row (fleetmux's status line).
    pub fn status_line(&mut self) -> String {
        self.row_text(self.rows - 1)
    }

    /// First (row, col) where `needle` starts on screen, scanning rows top to bottom.
    pub fn find(&mut self, needle: &str) -> Option<(u16, u16)> {
        self.pump();
        let want: Vec<char> = needle.chars().collect();
        for row in 0..self.rows {
            let cells: Vec<(u16, char)> = (0..self.cols)
                .map(|c| (c, self.screen.cell(row, c)))
                .filter(|(_, cell)| cell.width != 0)
                .map(|(c, cell)| (c, cell.ch))
                .collect();
            if let Some(i) = cells
                .windows(want.len().max(1))
                .position(|w| w.iter().map(|(_, ch)| *ch).eq(want.iter().copied()))
            {
                return Some((row, cells[i].0));
            }
        }
        None
    }

    /// Waits until the screen text contains `needle`; returns the screen.
    pub fn wait_text(&mut self, needle: &str, timeout: Duration) -> String {
        let end = Instant::now() + timeout;
        loop {
            let text = self.text();
            if text.contains(needle) {
                return text;
            }
            if Instant::now() >= end {
                panic!("viewer never showed {needle:?} within {timeout:?}; screen:\n{text}");
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    /// Waits until the screen text no longer contains `needle`; returns the screen.
    pub fn wait_no_text(&mut self, needle: &str, timeout: Duration) -> String {
        let end = Instant::now() + timeout;
        loop {
            let text = self.text();
            if !text.contains(needle) {
                return text;
            }
            if Instant::now() >= end {
                panic!("viewer still shows {needle:?} after {timeout:?}; screen:\n{text}");
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    /// Waits until `needle` is on screen; returns where it starts.
    pub fn wait_find(&mut self, needle: &str, timeout: Duration) -> (u16, u16) {
        self.wait_text(needle, timeout);
        self.find(needle).expect("text on screen but not found by cells")
    }

    /// Waits until the status line satisfies `pred`; returns it.
    pub fn wait_status(&mut self, what: &str, timeout: Duration, pred: impl Fn(&str) -> bool) -> String {
        let end = Instant::now() + timeout;
        loop {
            let line = self.status_line();
            if pred(&line) {
                return line;
            }
            if Instant::now() >= end {
                panic!("status line never showed {what} within {timeout:?}; last: {line:?}");
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    /// Waits until the cell at (`row`, `col`) satisfies `pred`; returns it.
    pub fn wait_cell(
        &mut self,
        row: u16,
        col: u16,
        timeout: Duration,
        pred: impl Fn(&Cell) -> bool,
    ) -> Cell {
        let end = Instant::now() + timeout;
        loop {
            let cell = self.cell(row, col);
            if pred(&cell) {
                return cell;
            }
            if Instant::now() >= end {
                panic!("cell ({row},{col}) never matched within {timeout:?}; last: {cell:?}");
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    /// Waits until the raw 0x81 bytes contain `needle`.
    pub fn wait_raw(&self, needle: &[u8], timeout: Duration) -> Vec<u8> {
        wait_until(
            &format!("{:?} in the viewer's screen bytes", String::from_utf8_lossy(needle)),
            timeout,
            || {
                let raw = self.raw();
                raw.windows(needle.len()).any(|w| w == needle).then_some(raw)
            },
        )
    }

    /// Waits until no 0x81 byte arrived for `quiet`; returns the byte count at that point.
    pub fn wait_quiet(&self, quiet: Duration, timeout: Duration) -> usize {
        let end = Instant::now() + timeout;
        let (mut last, mut since) = (self.raw_len(), Instant::now());
        loop {
            std::thread::sleep(Duration::from_millis(20));
            let now = self.raw_len();
            if now != last {
                (last, since) = (now, Instant::now());
            } else if since.elapsed() >= quiet {
                return now;
            }
            if Instant::now() >= end {
                panic!("viewer stream never went quiet for {quiet:?} within {timeout:?}");
            }
        }
    }

    /// Waits for the next 0x83 OPEN frame; returns its payload.
    pub fn wait_open(&self, timeout: Duration) -> String {
        wait_until("a 0x83 OPEN frame", timeout, || self.opens().into_iter().next())
    }
}

impl Drop for Viewer {
    fn drop(&mut self) {
        let _ = self.stream.write_all(&[0x03, 0, 0, 0, 0]);
        let _ = self.stream.shutdown(std::net::Shutdown::Both);
    }
}

// ----------------------------------------------- PtyProc -------------------------------------------

/// A program (normally `fleetmux attach`) running on the slave side of a fresh PTY; everything it writes is
/// drained into a buffer by a background thread so it never blocks. Killed on drop if still running.
pub struct PtyProc {
    child: Box<dyn portable_pty::Child + Send + Sync>,
    writer: Box<dyn Write + Send>,
    out: Arc<Mutex<Vec<u8>>>,
    _master: Box<dyn portable_pty::MasterPty + Send>,
}

impl PtyProc {
    pub fn spawn(argv: &[&str], cols: u16, rows: u16, env: &[(&str, &str)]) -> PtyProc {
        use portable_pty::{CommandBuilder, PtySize, native_pty_system};
        let pair = native_pty_system()
            .openpty(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            })
            .expect("openpty");
        let mut cmd = CommandBuilder::new(argv[0]);
        for a in &argv[1..] {
            cmd.arg(a);
        }
        cmd.env("TERM", "xterm-256color");
        for (k, v) in env {
            cmd.env(k, v);
        }
        let child = pair.slave.spawn_command(cmd).expect("spawn in pty");
        drop(pair.slave);
        let mut reader = pair.master.try_clone_reader().expect("pty reader");
        let writer = pair.master.take_writer().expect("pty writer");
        let out = Arc::new(Mutex::new(Vec::new()));
        let sink = out.clone();
        std::thread::spawn(move || {
            let mut buf = [0u8; 8192];
            while let Ok(n) = reader.read(&mut buf) {
                if n == 0 {
                    break;
                }
                sink.lock().unwrap().extend_from_slice(&buf[..n]);
            }
        });
        PtyProc {
            child,
            writer,
            out,
            _master: pair.master,
        }
    }

    pub fn write(&mut self, bytes: &[u8]) {
        self.writer.write_all(bytes).expect("write to pty");
        let _ = self.writer.flush();
    }

    /// Everything the program wrote so far (lossy UTF-8).
    pub fn output(&self) -> String {
        String::from_utf8_lossy(&self.out.lock().unwrap()).into_owned()
    }

    pub fn wait_output(&self, needle: &str, timeout: Duration) -> String {
        wait_until(&format!("{needle:?} in the pty output"), timeout, || {
            let out = self.output();
            out.contains(needle).then_some(out)
        })
    }

    /// Waits until at least `n` bytes were written.
    pub fn wait_bytes(&self, n: usize, timeout: Duration) {
        wait_until(&format!("{n} bytes of pty output"), timeout, || {
            (self.out.lock().unwrap().len() >= n).then_some(())
        })
    }

    /// Exit code once the program exited, `None` on timeout.
    pub fn wait_exit(&mut self, timeout: Duration) -> Option<u32> {
        let end = Instant::now() + timeout;
        loop {
            if let Ok(Some(status)) = self.child.try_wait() {
                return Some(status.exit_code());
            }
            if Instant::now() >= end {
                return None;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }
}

impl PtyProc {
    /// The program's output so far as a `cols` x `rows` screen (fed through a fresh `alacritty` parser).
    pub fn screen(&self, cols: u16, rows: u16) -> String {
        screen_of(&self.out.lock().unwrap(), cols, rows)
    }

    /// Waits until the rendered screen contains `needle`; returns it.
    pub fn wait_screen(&self, needle: &str, cols: u16, rows: u16, timeout: Duration) -> String {
        wait_until(&format!("{needle:?} on the pty screen"), timeout, || {
            let s = self.screen(cols, rows);
            s.contains(needle).then_some(s)
        })
    }
}

/// `bytes` written to a fresh `cols` x `rows` terminal, read back as text (one char per cell, trailing blanks
/// stripped, rows joined with `\n`).
pub fn screen_of(bytes: &[u8], cols: u16, rows: u16) -> String {
    let mut t = fleetmux_term::alacritty::new(cols, rows, 0);
    t.feed(bytes);
    (0..rows)
        .map(|r| {
            let s: String = (0..cols)
                .map(|c| t.cell(r, c))
                .filter(|c| c.width != 0)
                .map(|c| if c.ch == '\0' { ' ' } else { c.ch })
                .collect();
            s.trim_end().to_string()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

impl Drop for PtyProc {
    fn drop(&mut self) {
        if let Ok(None) = self.child.try_wait() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}
