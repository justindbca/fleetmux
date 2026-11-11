//! End-to-end tests with the ft-probe fixture inside a pane (plan.md §3.4, crates/probe/README.md).
//! Phase 0: red until M0 `server-core` / `attach-render` / `probe-e2e`.
#![allow(dead_code, unused_imports)]

// ---------------------------------------------------------------------------------------------------
// Harness: starts the real `fleetmux` binary and talks the control protocol (PROTOCOL.md §1, §4) as raw
// JSON lines, so the tests depend on the wire format only. The same block is repeated in protocol.rs,
// takeover.rs, e2e_probe.rs and e2e_cli.rs (phase-0 file ownership has no shared tests/common module;
// the integrator may hoist it later). Keep the copies identical.
// ---------------------------------------------------------------------------------------------------
use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_fleetmux");
const SESSION: &str = "e2e";
const START_TIMEOUT: Duration = Duration::from_secs(10);
const CALL_TIMEOUT: Duration = Duration::from_secs(15);

static SEQ: AtomicU32 = AtomicU32::new(0);

fn next_seq() -> u32 {
    SEQ.fetch_add(1, Ordering::SeqCst)
}

/// Short private temp directory (macOS `sockaddr_un` path limit is 103 bytes), removed on drop.
struct TempDir(PathBuf);

impl TempDir {
    fn new() -> TempDir {
        let p = std::env::temp_dir().join(format!("ftm{}-{}", std::process::id(), next_seq()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&p)
            .expect("create temp dir");
        TempDir(p.canonicalize().expect("canonical temp dir"))
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn uid() -> u32 {
    unsafe { libc::getuid() }
}

fn pid_alive(pid: i32) -> bool {
    unsafe { libc::kill(pid, 0) == 0 }
}

fn group_alive(pgid: i32) -> bool {
    unsafe { libc::kill(-pgid, 0) == 0 }
}

fn wait_until<T>(what: &str, timeout: Duration, mut f: impl FnMut() -> Option<T>) -> T {
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

fn wait_exit(child: &mut Child, timeout: Duration) -> Option<ExitStatus> {
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

fn b64(data: &[u8]) -> String {
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

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn tail(path: &Path, lines: usize) -> String {
    let text =
        std::fs::read_to_string(path).unwrap_or_else(|e| format!("<{}: {e}>", path.display()));
    let all: Vec<&str> = text.lines().collect();
    all[all.len().saturating_sub(lines)..].join("\n")
}

/// Polls `path` until its bytes equal `want`; on timeout shows what was there.
fn wait_file_eq(path: &Path, want: &[u8], timeout: Duration) {
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
struct Client {
    reader: BufReader<UnixStream>,
    writer: UnixStream,
    next_id: u64,
}

impl Client {
    fn connect(sock: &Path) -> std::io::Result<Client> {
        let s = UnixStream::connect(sock)?;
        s.set_read_timeout(Some(CALL_TIMEOUT))?;
        s.set_write_timeout(Some(CALL_TIMEOUT))?;
        Ok(Client {
            reader: BufReader::new(s.try_clone()?),
            writer: s,
            next_id: 1,
        })
    }

    fn send_line(&mut self, line: &str) {
        self.writer
            .write_all(format!("{line}\n").as_bytes())
            .expect("write request line");
    }

    fn try_recv(&mut self) -> std::io::Result<Value> {
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

    fn recv(&mut self) -> Value {
        self.try_recv()
            .unwrap_or_else(|e| panic!("reading a reply: {e}"))
    }

    fn try_request(&mut self, cmd: &str, args: Value) -> std::io::Result<Value> {
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
    fn request(&mut self, cmd: &str, args: Value) -> Value {
        self.try_request(cmd, args.clone())
            .unwrap_or_else(|e| panic!("{cmd} {args}: {e}"))
    }

    /// Asserts `ok: true`, returns the reply.
    fn ok(&mut self, cmd: &str, args: Value) -> Value {
        let r = self.request(cmd, args.clone());
        assert_eq!(r["ok"], json!(true), "{cmd} {args} failed: {r}");
        r
    }

    /// Asserts `ok: false` with a non-empty `error`, returns the `code`.
    fn err(&mut self, cmd: &str, args: Value) -> String {
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

fn try_hello(sock: &Path) -> Option<Value> {
    let mut c = Client::connect(sock).ok()?;
    let r = c.try_request("hello", json!({})).ok()?;
    (r["ok"] == json!(true)).then_some(r)
}

/// `fleetmux server --runtime <dir> --session e2e` in the foreground, output to `<log_dir>/<name>.out`.
fn spawn_server(runtime: &Path, log_dir: &Path, name: &str, env: &[(&str, &str)]) -> Child {
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
struct Server {
    child: Child,
    tmp: TempDir,
    runtime: PathBuf,
    pgids: Mutex<Vec<i32>>,
}

impl Server {
    fn start() -> Server {
        let mut srv = Server::launch(&[]);
        srv.wait_ready();
        srv
    }

    /// Starts the process without waiting for `hello` (for servers that are expected to refuse peers).
    fn launch(env: &[(&str, &str)]) -> Server {
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

    fn sock(&self) -> PathBuf {
        self.runtime.join("ctl.sock")
    }

    fn pid(&self) -> i32 {
        self.child.id() as i32
    }

    fn diagnostics(&self) -> String {
        format!(
            "server output:\n{}\nserver.log tail:\n{}",
            tail(&self.tmp.path().join("server.out"), 20),
            tail(&self.runtime.join("server.log"), 20)
        )
    }

    fn wait_ready(&mut self) -> Value {
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

    fn client(&self) -> Client {
        Client::connect(&self.sock()).unwrap_or_else(|e| {
            panic!(
                "connect {}: {e}\n{}",
                self.sock().display(),
                self.diagnostics()
            )
        })
    }

    fn ok(&self, cmd: &str, args: Value) -> Value {
        self.client().ok(cmd, args)
    }

    fn err(&self, cmd: &str, args: Value) -> String {
        self.client().err(cmd, args)
    }

    /// `pane.spawn` with cwd = this test's temp dir; remembers the pgid for cleanup.
    fn spawn(&self, title: &str, ty: &str, argv: &[&str], env: Value) -> Value {
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
    fn spawn_id(&self, title: &str, ty: &str, argv: &[&str]) -> String {
        let r = self.spawn(title, ty, argv, json!({}));
        r["pane"]
            .as_str()
            .unwrap_or_else(|| panic!("pane.spawn reply without pane: {r}"))
            .to_string()
    }

    fn panes(&self) -> Vec<Value> {
        let r = self.ok("pane.list", json!({}));
        r["panes"]
            .as_array()
            .unwrap_or_else(|| panic!("pane.list reply without panes array: {r}"))
            .clone()
    }

    fn listed(&self, id: &str) -> bool {
        self.panes().iter().any(|p| p["pane"] == json!(id))
    }

    fn pane(&self, id: &str) -> Value {
        self.ok("pane.get", json!({"pane": id}))
    }

    fn read(&self, id: &str, lines: u64) -> String {
        let r = self.ok("pane.read", json!({"pane": id, "lines": lines}));
        r["text"]
            .as_str()
            .unwrap_or_else(|| panic!("pane.read reply without text: {r}"))
            .to_string()
    }

    fn write(&self, id: &str, data: &[u8], mode: &str) -> Value {
        self.ok(
            "pane.write",
            json!({"pane": id, "data": b64(data), "mode": mode}),
        )
    }

    fn wait_text(&self, id: &str, needle: &str, timeout: Duration) -> String {
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

    fn wait_gone(&self, id: &str, timeout: Duration) {
        wait_until(&format!("pane {id} to leave pane.list"), timeout, || {
            (!self.listed(id)).then_some(())
        });
    }

    /// Ids of `group`, in tab order.
    fn group_order(&self, group: &str) -> Vec<String> {
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
// ------------------------------------------------- end harness -------------------------------------

use std::sync::Arc;

/// ft-probe lives in another package, so Cargo gives no `CARGO_BIN_EXE_` for it: `$FT_PROBE_BIN`, else
/// next to the fleetmux binary (`cargo build -p ft-probe` / `cargo build --workspace` puts it there).
fn probe_bin() -> PathBuf {
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
fn probe_events(log: &Path) -> Vec<Value> {
    std::fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter_map(|l| serde_json::from_str(l).ok())
        .collect()
}

fn wait_event(log: &Path, what: &str, timeout: Duration, pred: impl Fn(&Value) -> bool) -> Value {
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

fn wait_events(
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

fn ev_is(e: &Value, kind: &str) -> bool {
    e["ev"] == json!(kind)
}

/// Spawns ft-probe as a worker pane and waits for its `ready` event. Returns (pane id, log path).
fn start_probe(srv: &Server, flags: &[&str]) -> (String, PathBuf) {
    let bin = probe_bin();
    let log = srv.tmp.path().join(format!("probe-{}.jsonl", next_seq()));
    let mut argv = vec![bin.to_str().expect("utf-8 probe path")];
    argv.extend_from_slice(flags);
    let r = srv.spawn(
        "fx-probe",
        "worker",
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

fn pane_size(srv: &Server, id: &str) -> (u64, u64) {
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

/// A view connection (PROTOCOL.md §6): JSON handshake line, then `type u8 + len u32 BE + payload` frames.
/// A background thread drains server frames so the server never blocks on us; screen bytes (0x81) are kept.
struct Viewer {
    stream: UnixStream,
    screen: Arc<Mutex<Vec<u8>>>,
}

impl Viewer {
    fn attach(srv: &Server, cols: u16, rows: u16) -> Viewer {
        let mut stream = srv.client().writer.try_clone().expect("clone stream");
        let hello = json!({"view": 1, "cols": cols, "rows": rows, "term": "xterm-256color", "colorterm": "truecolor"});
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

        let screen = Arc::new(Mutex::new(Vec::new()));
        let (mut rx, sink) = (stream.try_clone().unwrap(), screen.clone());
        rx.set_read_timeout(None).unwrap();
        std::thread::spawn(move || {
            let mut head = [0u8; 5];
            while rx.read_exact(&mut head).is_ok() {
                let len = u32::from_be_bytes([head[1], head[2], head[3], head[4]]) as usize;
                let mut payload = vec![0u8; len];
                if rx.read_exact(&mut payload).is_err() {
                    break;
                }
                if head[0] == 0x81 {
                    sink.lock().unwrap().extend_from_slice(&payload);
                }
            }
        });
        Viewer { stream, screen }
    }

    fn send(&mut self, ty: u8, payload: &[u8]) {
        let mut frame = vec![ty];
        frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        frame.extend_from_slice(payload);
        self.stream.write_all(&frame).expect("send view frame");
    }

    fn input(&mut self, bytes: &[u8]) {
        self.send(0x01, bytes);
    }

    fn resize(&mut self, cols: u16, rows: u16) {
        let mut p = cols.to_be_bytes().to_vec();
        p.extend_from_slice(&rows.to_be_bytes());
        self.send(0x02, &p);
    }

    fn wait_screen(&self, needle: &str, timeout: Duration) {
        wait_until(
            &format!("{needle:?} in the viewer's screen bytes"),
            timeout,
            || {
                String::from_utf8_lossy(&self.screen.lock().unwrap())
                    .contains(needle)
                    .then_some(())
            },
        );
    }
}

impl Drop for Viewer {
    fn drop(&mut self) {
        let _ = self.stream.write_all(&[0x03, 0, 0, 0, 0]);
        let _ = self.stream.shutdown(std::net::Shutdown::Both);
    }
}

#[test]
fn probe_gets_dsr_and_osc11_answers() {
    let srv = Server::start();
    let (_, log) = start_probe(&srv, &[]);
    let events = probe_events(&log);
    let dsr = events
        .iter()
        .find(|e| ev_is(e, "dsr"))
        .unwrap_or_else(|| panic!("no dsr answer; log:\n{}", tail(&log, 40)));
    assert!(
        dsr["row"].as_u64().is_some_and(|r| r >= 1) && dsr["col"].as_u64().is_some_and(|c| c >= 1),
        "{dsr}"
    );
    let osc = events
        .iter()
        .find(|e| ev_is(e, "osc11"))
        .unwrap_or_else(|| panic!("no OSC 11 answer; log:\n{}", tail(&log, 40)));
    assert!(
        osc["color"].as_str().is_some_and(|c| c.starts_with("rgb:")),
        "{osc}"
    );
    let timeouts: Vec<&Value> = events.iter().filter(|e| ev_is(e, "timeout")).collect();
    assert!(timeouts.is_empty(), "queries timed out: {timeouts:?}");
}

#[test]
fn probe_receives_sgr_mouse_with_translated_coordinates() {
    let srv = Server::start();
    let (id, log) = start_probe(&srv, &[]);
    let mut v = Viewer::attach(&srv, 200, 50);
    let (cols, rows) = wait_until(
        "worker pane to take the viewer's geometry",
        Duration::from_secs(5),
        || {
            let size = pane_size(&srv, &id);
            (size.0 < 110).then_some(size)
        },
    );
    // Outer (150, 25) is inside the right-hand worker group at 200x50 with the default 50/50 split.
    v.input(b"\x1b[<0;150;25M\x1b[<0;150;25m");
    v.input(b"\x1b[<0;153;27M\x1b[<0;153;27m");
    let presses = wait_events(&log, "2 mouse presses", 2, Duration::from_secs(5), |e| {
        ev_is(e, "mouse") && e["press"] == json!(true)
    });
    let (a, b) = (&presses[0], &presses[1]);
    for e in [a, b] {
        assert_eq!(e["encoding"], json!("sgr"), "{e}");
        assert_eq!(e["button"].as_u64(), Some(0), "{e}");
        let (c, r) = (e["col"].as_u64().unwrap(), e["row"].as_u64().unwrap());
        assert!(
            (1..=cols).contains(&c) && (1..=rows).contains(&r),
            "outside the {cols}x{rows} pane: {e}"
        );
    }
    let (ac, ar) = (a["col"].as_u64().unwrap(), a["row"].as_u64().unwrap());
    assert!(
        ac < 150 && ar < 25,
        "coordinates not translated to the pane origin: {a}"
    );
    assert_eq!(
        (
            b["col"].as_u64().unwrap() - ac,
            b["row"].as_u64().unwrap() - ar
        ),
        (3, 2),
        "offsets not preserved: {a} {b}"
    );
    let releases = probe_events(&log)
        .into_iter()
        .filter(|e| ev_is(e, "mouse") && e["press"] == json!(false))
        .count();
    assert!(releases >= 1, "release (SGR 'm') not forwarded");
}

#[test]
fn probe_receives_bracketed_paste_when_2004_on() {
    let srv = Server::start();
    let (id, log) = start_probe(&srv, &[]);
    srv.write(&id, b"hello\nworld", "paste");
    let ev = wait_event(&log, "a paste event", Duration::from_secs(5), |e| {
        ev_is(e, "paste")
    });
    assert_eq!(ev["text_hex"], json!(hex(b"hello\nworld")), "{ev}");
    assert_eq!(
        ev["hex"],
        json!(hex(b"\x1b[200~hello\nworld\x1b[201~")),
        "{ev}"
    );
}

#[test]
fn probe_receives_raw_paste_when_2004_off_in_paste_mode() {
    let srv = Server::start();
    let (id, log) = start_probe(&srv, &["--no-paste"]);
    srv.write(&id, b"abc", "paste");
    wait_until(
        "the pasted bytes as key input",
        Duration::from_secs(5),
        || {
            let keys: String = probe_events(&log)
                .iter()
                .filter(|e| ev_is(e, "key"))
                .filter_map(|e| e["hex"].as_str().map(String::from))
                .collect();
            (keys == hex(b"abc")).then_some(())
        },
    );
    let events = probe_events(&log);
    assert!(
        !events.iter().any(|e| ev_is(e, "paste")),
        "wrapped although 2004 is off"
    );
    assert!(
        !events.iter().any(|e| e["hex"]
            .as_str()
            .is_some_and(|h| h.contains(&hex(b"\x1b[200~")))),
        "paste marker leaked"
    );
}

#[test]
fn probe_alt_screen_truecolor_text_is_readable() {
    let srv = Server::start();
    let (id, log) = start_probe(&srv, &[]);
    let start = probe_events(&log)
        .into_iter()
        .find(|e| ev_is(e, "start"))
        .expect("start event");
    assert_eq!(start["mouse"], json!(true));

    let text = srv.wait_text(&id, "TRUECOLOR", Duration::from_secs(5));
    for k in 0..10 {
        assert!(
            text.lines().any(|l| l == format!("ROW {k}")),
            "ROW {k} missing:\n{text}"
        );
    }
    // A truecolor viewer gets the 24-bit colours as drawn, not a 256-colour approximation.
    let v = Viewer::attach(&srv, 200, 50);
    v.wait_screen("38;2;255;128;0", Duration::from_secs(5));
    v.wait_screen("48;2;0;64;128", Duration::from_secs(5));
    v.wait_screen("ROW 9", Duration::from_secs(5));

    // `q` quits the probe (exit 0), and the pane goes with its process.
    srv.write(&id, b"q", "raw");
    wait_event(&log, "exit", Duration::from_secs(5), |e| {
        ev_is(e, "exit") && e["reason"] == json!("q")
    });
    srv.wait_gone(&id, Duration::from_secs(5));
}

#[test]
fn probe_kitty_query_gets_no_support() {
    let srv = Server::start();
    let (_, log) = start_probe(&srv, &["--kitty-query"]);
    let ev = wait_event(
        &log,
        "the kitty query result",
        Duration::from_secs(5),
        |e| ev_is(e, "kitty"),
    );
    let unsupported = ev["answered"] == json!(false) || ev["flags"].as_u64() == Some(0);
    assert!(
        unsupported,
        "server advertised kitty keyboard support: {ev}"
    );
}

#[test]
fn pty_size_follows_latest_viewer_resize() {
    let srv = Server::start();
    let (id, log) = start_probe(&srv, &[]);
    let t = Duration::from_secs(5);
    let width_now = |pred: &dyn Fn(u64) -> bool| {
        wait_until("pane width to change", t, || {
            let size = pane_size(&srv, &id);
            pred(size.0).then_some(size)
        })
    };

    // PROTOCOL.md §6.3: sizes follow the viewer that most recently sent a 0x01 input frame; before any
    // input, the one that connected last. A 0x02 resize updates that viewer's size but not who is "latest".
    let mut wide = Viewer::attach(&srv, 200, 50);
    let s_wide = width_now(&|c| c < 110);
    let mut narrow = Viewer::attach(&srv, 120, 40);
    let s_narrow = width_now(&|c| c < s_wide.0);
    wide.resize(160, 45);
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(
        pane_size(&srv, &id),
        s_narrow,
        "a resize from a viewer that is not the latest changed the PTY"
    );
    wide.input(b"w");
    let s_mid = width_now(&|c| c > s_narrow.0 && c < s_wide.0);
    narrow.input(b"n");
    assert_eq!(width_now(&|c| c < s_mid.0), s_narrow);
    wide.input(b"w");
    width_now(&|c| c == s_mid.0);

    // The program inside saw the same sizes, ending with the latest one.
    let resizes = wait_events(&log, "a resize to the latest size", 1, t, |e| {
        ev_is(e, "resize")
            && (e["cols"].as_u64(), e["rows"].as_u64()) == (Some(s_mid.0), Some(s_mid.1))
    });
    assert!(!resizes.is_empty());
    let last = probe_events(&log)
        .into_iter()
        .filter(|e| ev_is(e, "resize"))
        .last()
        .unwrap();
    assert_eq!(
        (last["cols"].as_u64(), last["rows"].as_u64()),
        (Some(s_mid.0), Some(s_mid.1)),
        "{last}"
    );
}

#[test]
fn arrow_keys_app_cursor_mode_via_pane_key() {
    let srv = Server::start();
    let (app, app_log) = start_probe(&srv, &["--app-cursor"]);
    srv.ok("pane.key", json!({"pane": app, "key": "Up"}));
    let ev = wait_event(&app_log, "the Up key", Duration::from_secs(5), |e| {
        ev_is(e, "key")
    });
    assert_eq!(
        ev["hex"],
        json!(hex(b"\x1bOA")),
        "application cursor mode must send ESC O A: {ev}"
    );

    let (normal, normal_log) = start_probe(&srv, &[]);
    srv.ok("pane.key", json!({"pane": normal, "key": "Up"}));
    let ev = wait_event(&normal_log, "the Up key", Duration::from_secs(5), |e| {
        ev_is(e, "key")
    });
    assert_eq!(
        ev["hex"],
        json!(hex(b"\x1b[A")),
        "normal cursor mode must send ESC [ A: {ev}"
    );
}
