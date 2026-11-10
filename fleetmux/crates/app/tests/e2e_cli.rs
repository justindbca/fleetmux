//! Real full-screen CLIs (vim -u NONE, less, htop) inside panes (plan.md §3.4, design §7.2).
//! Phase 0: red until M0 `server-core` / `probe-e2e`.
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

const TERM_ENV: &str = "xterm-256color";

fn on_path(prog: &str) -> bool {
    std::env::var_os("PATH")
        .is_some_and(|p| std::env::split_paths(&p).any(|d| d.join(prog).is_file()))
}

/// Polls `pane.read` until `pred` holds for the screen text.
fn wait_screen(
    srv: &Server,
    id: &str,
    what: &str,
    timeout: Duration,
    pred: impl Fn(&str) -> bool,
) -> String {
    let end = Instant::now() + timeout;
    loop {
        let text = srv.read(id, 200);
        if pred(&text) {
            return text;
        }
        if Instant::now() >= end {
            panic!("pane {id} never showed {what} within {timeout:?}; screen:\n{text}");
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[test]
fn vim_u_none_opens_types_quits_and_pane_disappears() {
    let srv = Server::start();
    let file = srv.tmp.path().join("vim.txt");
    let t = Duration::from_secs(10);
    let r = srv.spawn(
        "fx-vim",
        "worker",
        &["vim", "-u", "NONE", "-N", "-n", file.to_str().unwrap()],
        json!({"TERM": TERM_ENV}),
    );
    let id = r["pane"]
        .as_str()
        .unwrap_or_else(|| panic!("no pane: {r}"))
        .to_string();

    wait_screen(&srv, &id, "vim's empty-buffer `~` lines", t, |s| {
        s.lines().filter(|l| *l == "~").count() >= 3
    });
    srv.write(&id, b"i", "raw");
    wait_screen(&srv, &id, "insert mode", t, |s| s.contains("-- INSERT --"));
    srv.write(&id, b"hello fleetmux", "raw");
    wait_screen(&srv, &id, "the typed text", t, |s| {
        s.contains("hello fleetmux")
    });
    srv.ok("pane.key", json!({"pane": id, "key": "Escape"}));
    wait_screen(&srv, &id, "normal mode", t, |s| !s.contains("-- INSERT --"));
    srv.write(&id, b":wq\r", "raw");

    srv.wait_gone(&id, t);
    assert_eq!(
        std::fs::read_to_string(&file).expect("vim wrote the file"),
        "hello fleetmux\n"
    );
}

#[test]
fn less_pages_and_quits() {
    let srv = Server::start();
    let file = srv.tmp.path().join("pages.txt");
    let body: String = (1..=500).map(|i| format!("line {i:04}\n")).collect();
    std::fs::write(&file, body).unwrap();
    let t = Duration::from_secs(10);
    let env = json!({"TERM": TERM_ENV, "LESS": "", "LESSHISTFILE": "-"});
    let r = srv.spawn("fx-less", "worker", &["less", file.to_str().unwrap()], env);
    let id = r["pane"]
        .as_str()
        .unwrap_or_else(|| panic!("no pane: {r}"))
        .to_string();
    let rows = srv.pane(&id)["rows"].as_u64().expect("pane.get rows");

    let first = wait_screen(&srv, &id, "the first page", t, |s| s.contains("line 0001"));
    assert!(
        !first.contains("line 0400"),
        "less printed the whole file instead of paging:\n{first}"
    );
    srv.ok("pane.key", json!({"pane": id, "key": "PageDown"}));
    srv.write(&id, b" ", "raw");
    let paged = wait_screen(&srv, &id, "a later page", t, |s| {
        s.lines().any(|l| {
            l.strip_prefix("line ")
                .and_then(|n| n.parse::<u64>().ok())
                .is_some_and(|n| n > rows)
        })
    });
    let screen_now = srv.read(&id, rows);
    assert!(
        !screen_now.contains("line 0001"),
        "still on the first page:\n{paged}"
    );

    srv.write(&id, b"q", "raw");
    srv.wait_gone(&id, t);
}

#[test]
fn htop_renders_and_quits() {
    let srv = Server::start();
    if !on_path("htop") {
        eprintln!("skipped: htop is not installed (plan.md §3.4: skip when missing)");
        return;
    }
    let t = Duration::from_secs(10);
    let r = srv.spawn("fx-htop", "worker", &["htop"], json!({"TERM": TERM_ENV}));
    let id = r["pane"]
        .as_str()
        .unwrap_or_else(|| panic!("no pane: {r}"))
        .to_string();
    wait_screen(&srv, &id, "htop's process table header", t, |s| {
        s.contains("PID") && s.contains("Command")
    });
    srv.write(&id, b"q", "raw");
    srv.wait_gone(&id, t);
}
