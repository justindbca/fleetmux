//! Control-protocol integration tests (plan.md §3.4, PROTOCOL.md §4): every command against the real
//! `fleetmux server`. Phase 0: red until M0 `server-core` implements the server.
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

fn is_epoch(s: &str) -> bool {
    s.len() == 4
        && s.bytes()
            .all(|b| b.is_ascii_digit() || b.is_ascii_lowercase())
}

/// A pane whose inner program never enables anything and copies every byte it receives into `out`
/// (tty in raw mode, no echo), so a test can check exactly what reached the PTY.
fn sink_argv(out: &Path, enable_2004: bool) -> Vec<String> {
    let mode = if enable_2004 {
        r"printf '\033[?2004h'; "
    } else {
        ""
    };
    let script = format!(
        "stty raw -echo; {mode}echo SINK-READY; exec cat > '{}'",
        out.display()
    );
    vec!["sh".into(), "-c".into(), script]
}

fn spawn_sink(srv: &Server, out: &Path, enable_2004: bool) -> String {
    let argv = sink_argv(out, enable_2004);
    let argv: Vec<&str> = argv.iter().map(String::as_str).collect();
    let id = srv.spawn_id("fx-sink", "worker", &argv);
    srv.wait_text(&id, "SINK-READY", Duration::from_secs(5));
    id
}

#[test]
fn hello_reports_uid_pid_epoch_versions() {
    let srv = Server::start();
    let h = srv.ok("hello", json!({}));
    assert_eq!(h["uid"].as_u64(), Some(uid() as u64), "{h}");
    assert_eq!(h["pid"].as_i64(), Some(srv.pid() as i64), "{h}");
    let epoch = h["epoch"]
        .as_str()
        .unwrap_or_else(|| panic!("hello without epoch: {h}"));
    assert!(is_epoch(epoch), "epoch must be 4 base36 chars: {epoch:?}");
    assert_eq!(
        h["version"].as_str(),
        Some(env!("CARGO_PKG_VERSION")),
        "{h}"
    );
    assert_eq!(h["protocol"].as_u64(), Some(1), "{h}");
    assert!(h["run"].is_null(), "no run marker set yet: {h}");
    // Every connection talks to the same server generation.
    assert_eq!(srv.ok("hello", json!({}))["epoch"], h["epoch"]);

    // A client of another protocol version is told so, and the connection stays usable (PROTOCOL.md §2).
    let mut c = srv.client();
    assert_eq!(c.err("hello", json!({"protocol": 2})), "protocol_version");
    assert_eq!(c.ok("hello", json!({"protocol": 1}))["epoch"], h["epoch"]);
}

#[test]
fn unknown_cmd_and_bad_json_reply_error_and_keep_connection() {
    let srv = Server::start();
    let bad = |r: &Value, what: &str| {
        assert_eq!(
            (r["ok"].clone(), r["code"].clone(), r["id"].clone()),
            (json!(false), json!("bad_request"), Value::Null),
            "{what}: {r}"
        );
    };

    // Bad JSON as the very first line: a control-line error, connection kept (PROTOCOL.md §1.1).
    let mut c = srv.client();
    c.send_line("this is not json");
    bad(&c.recv(), "bad JSON first line");
    assert_eq!(c.ok("hello", json!({}))["protocol"].as_u64(), Some(1));

    assert_eq!(c.err("no.such.cmd", json!({})), "unknown_cmd");

    c.send_line("[1, 2, 3]");
    bad(&c.recv(), "not an object");

    c.send_line(r#"{"cmd":"hello"}"#);
    bad(&c.recv(), "missing id");

    // pane.get requires `pane` (R10).
    c.send_line(r#"{"id":41,"cmd":"pane.get"}"#);
    let r = c.recv();
    assert_eq!(r["id"], json!(41), "{r}");
    assert_eq!(
        (r["ok"].clone(), r["code"].clone()),
        (json!(false), json!("bad_request")),
        "missing pane: {r}"
    );
    // pane.list takes no arguments (R10).
    assert!(c.ok("pane.list", json!({}))["panes"].is_array());

    // Same connection still serves requests after every kind of error.
    let h = c.ok("hello", json!({}));
    assert_eq!(h["protocol"].as_u64(), Some(1));
}

#[test]
fn kv_set_get_roundtrip() {
    let srv = Server::start();
    let mut c = srv.client();
    c.ok(
        "kv.set",
        json!({"key": "run", "value": "run-20261028-0001"}),
    );
    assert_eq!(
        c.ok("kv.get", json!({"key": "run"}))["value"],
        json!("run-20261028-0001")
    );
    // Stored in the server, not the connection.
    assert_eq!(
        srv.ok("kv.get", json!({"key": "run"}))["value"],
        json!("run-20261028-0001")
    );
    c.ok("kv.set", json!({"key": "run", "value": "run-2"}));
    assert_eq!(
        srv.ok("kv.get", json!({"key": "run"}))["value"],
        json!("run-2")
    );
    assert!(srv.ok("kv.get", json!({"key": "never-set"}))["value"].is_null());
    // `hello` reports the run marker (kv key `run`, PROTOCOL.md §4.1).
    assert_eq!(srv.ok("hello", json!({}))["run"], json!("run-2"));
}

#[test]
fn pane_spawn_returns_id_pid_pgid_tty_and_list_shows_it() {
    let srv = Server::start();
    let epoch = srv.ok("hello", json!({}))["epoch"]
        .as_str()
        .unwrap()
        .to_string();
    let script = r#"echo "mark=$FT_E2E_MARK cwd=$(pwd -P) term=$TERM colorterm=$COLORTERM"; exec sleep 1000"#;
    let r = srv.spawn(
        "fx-worker-1",
        "worker",
        &["sh", "-c", script],
        json!({"FT_E2E_MARK": "m1"}),
    );

    let id = r["pane"]
        .as_str()
        .unwrap_or_else(|| panic!("no pane: {r}"))
        .to_string();
    let (id_epoch, seq) = id
        .split_once('-')
        .unwrap_or_else(|| panic!("id not <epoch>-<seq>: {id}"));
    assert_eq!(id_epoch, epoch);
    assert!(
        !seq.is_empty() && seq.bytes().all(|b| b.is_ascii_digit()),
        "{id}"
    );
    let pid = r["pid"].as_i64().unwrap_or_else(|| panic!("no pid: {r}")) as i32;
    let pgid = r["pgid"].as_i64().unwrap_or_else(|| panic!("no pgid: {r}")) as i32;
    let tty = r["tty"]
        .as_str()
        .unwrap_or_else(|| panic!("no tty: {r}"))
        .to_string();
    assert!(pid > 1 && pid_alive(pid));
    assert_eq!(pgid, pid, "the child leads its own session / process group");
    assert_eq!(unsafe { libc::getpgid(pid) }, pgid);
    assert!(
        tty.starts_with("/dev/") && Path::new(&tty).exists(),
        "{tty}"
    );

    srv.wait_text(
        &id,
        &format!(
            "mark=m1 cwd={} term=xterm-256color colorterm=truecolor",
            srv.tmp.path().display()
        ),
        Duration::from_secs(5),
    );

    let listed = srv
        .panes()
        .into_iter()
        .find(|p| p["pane"] == json!(id))
        .unwrap_or_else(|| panic!("{id} not listed"));
    assert_eq!(listed["title"], json!("fx-worker-1"));
    assert_eq!(listed["type"], json!("worker"));
    assert_eq!(listed["group"], json!("workers"));
    assert_eq!(listed["pid"].as_i64(), Some(pid as i64));
    assert_eq!(listed["pgid"].as_i64(), Some(pgid as i64));
    assert_eq!(listed["tty"], json!(tty));

    let second = srv.spawn_id("fx-worker-2", "worker", &["sleep", "1000"]);
    assert_ne!(second, id);
    assert!(second.starts_with(&format!("{epoch}-")));

    // An unknown pane type is not an error: it lands last in `workers` (R5).
    let odd = srv.spawn_id("fx-odd", "mystery", &["sleep", "1000"]);
    assert_eq!(srv.pane(&odd)["type"], json!("mystery"));
    assert_eq!(
        srv.group_order("workers"),
        vec![id.clone(), second.clone(), odd.clone()]
    );
    // `anchor` + boolean `before` (R3): right before / right after the anchor.
    let ahead = srv.ok(
        "pane.spawn",
        json!({"argv": ["sleep", "1000"], "title": "fx-ahead", "type": "worker", "anchor": second, "before": true}),
    );
    let ahead = ahead["pane"].as_str().unwrap().to_string();
    srv.pgids
        .lock()
        .unwrap()
        .push(srv.pane(&ahead)["pgid"].as_i64().unwrap() as i32);
    let behind = srv.ok(
        "pane.spawn",
        json!({"argv": ["sleep", "1000"], "title": "fx-behind", "type": "worker", "anchor": second}),
    );
    let behind = behind["pane"].as_str().unwrap().to_string();
    srv.pgids
        .lock()
        .unwrap()
        .push(srv.pane(&behind)["pgid"].as_i64().unwrap() as i32);
    assert_eq!(
        srv.group_order("workers"),
        vec![id.clone(), ahead, second, behind, odd]
    );
    // A bad group is `unknown_type` (R5); a bad anchor is `no_such_pane`.
    assert_eq!(
        srv.err("pane.spawn", json!({"argv": ["true"], "group": "nowhere"})),
        "unknown_type"
    );
    assert_eq!(
        srv.err(
            "pane.spawn",
            json!({"argv": ["true"], "anchor": "zzzz-999"})
        ),
        "no_such_pane"
    );
    // exec failure and a missing cwd are `spawn_failed`; nothing is left in the list.
    let before = srv.panes().len();
    assert_eq!(
        srv.err(
            "pane.spawn",
            json!({"argv": ["/nonexistent/ft-no-such-program"], "type": "worker"})
        ),
        "spawn_failed"
    );
    assert_eq!(
        srv.err(
            "pane.spawn",
            json!({"argv": ["sleep", "1"], "cwd": "/nonexistent/ft-no-such-dir"})
        ),
        "spawn_failed"
    );
    assert_eq!(
        srv.panes().len(),
        before,
        "a failed spawn left a pane behind"
    );
}

#[test]
fn pane_write_raw_bracketed_paste_modes() {
    let srv = Server::start();
    let out = srv.tmp.path().join("plain.bin");
    let id = spawn_sink(&srv, &out, false);
    let t = Duration::from_secs(5);

    assert_eq!(srv.write(&id, b"raw-1", "raw")["written"].as_u64(), Some(5));
    wait_file_eq(&out, b"raw-1", t);
    // bracketed: always wrapped, even though the inner program never enabled 2004.
    srv.write(&id, b"br", "bracketed");
    wait_file_eq(&out, b"raw-1\x1b[200~br\x1b[201~", t);
    // paste: follows the inner 2004 mode, which is off here.
    srv.write(&id, b"pa", "paste");
    wait_file_eq(&out, b"raw-1\x1b[200~br\x1b[201~pa", t);
    // Data that looks like an option is still data.
    srv.write(&id, b"-n x", "raw");
    wait_file_eq(&out, b"raw-1\x1b[200~br\x1b[201~pa-n x", t);

    let out2 = srv.tmp.path().join("paste2004.bin");
    let id2 = spawn_sink(&srv, &out2, true);
    srv.write(&id2, b"pb", "paste");
    wait_file_eq(&out2, b"\x1b[200~pb\x1b[201~", t);
    // raw never wraps, even with 2004 on; `written` never counts the markers.
    assert_eq!(srv.write(&id2, b"r", "raw")["written"].as_u64(), Some(1));
    wait_file_eq(&out2, b"\x1b[200~pb\x1b[201~r", t);
    assert_eq!(
        srv.write(&id2, b"xy", "bracketed")["written"].as_u64(),
        Some(2)
    );
    wait_file_eq(&out2, b"\x1b[200~pb\x1b[201~r\x1b[200~xy\x1b[201~", t);

    // An unknown mode is `unknown_type` (R5) and writes nothing.
    let r = srv.client().request(
        "pane.write",
        json!({"pane": id2, "data": b64(b"no"), "mode": "shout"}),
    );
    assert_eq!(
        (r["ok"].clone(), r["code"].clone()),
        (json!(false), json!("unknown_type")),
        "{r}"
    );
    std::thread::sleep(Duration::from_millis(300));
    wait_file_eq(&out2, b"\x1b[200~pb\x1b[201~r\x1b[200~xy\x1b[201~", t);
}

#[test]
fn pane_key_unknown_name_writes_nothing() {
    let srv = Server::start();
    let out = srv.tmp.path().join("keys.bin");
    let id = spawn_sink(&srv, &out, false);
    assert_eq!(
        srv.err("pane.key", json!({"pane": id, "key": "NoSuchKey"})),
        "unknown_key"
    );
    srv.ok("pane.key", json!({"pane": id, "key": "Enter"}));
    srv.ok("pane.key", json!({"pane": id, "key": "C-c"}));
    // Exactly the two known keys arrived; the unknown one wrote nothing.
    wait_file_eq(&out, b"\r\x03", Duration::from_secs(5));
}

#[test]
fn pane_read_scrollback_plus_screen_no_trailing_blanks() {
    let srv = Server::start();
    let script = r#"i=1; while [ $i -le 300 ]; do echo "line $i   "; i=$((i+1)); done; head -c 250 /dev/zero | tr '\000' x; echo; echo DONE; exec sleep 1000"#;
    let id = srv.spawn_id("fx-reader", "worker", &["sh", "-c", script]);
    srv.wait_text(&id, "DONE", Duration::from_secs(10));
    let cols = srv.pane(&id)["cols"].as_u64().expect("pane.get cols") as usize;
    assert!(
        cols < 250,
        "default worker pane is narrower than the 250-char line: {cols}"
    );

    let text = srv.read(&id, 1000);
    let lines: Vec<&str> = text.lines().collect();
    for line in &lines {
        assert_eq!(line.trim_end(), *line, "trailing blanks kept: {line:?}");
        assert!(
            line.chars().count() <= cols,
            "not a physical row (wider than {cols}): {line:?}"
        );
    }
    // All 300 lines, in order: scrollback + current screen.
    let mut prev = None;
    for i in 1..=300 {
        let want = format!("line {i}");
        let at = lines
            .iter()
            .position(|l| *l == want)
            .unwrap_or_else(|| panic!("{want:?} missing"));
        assert!(prev.is_none_or(|p| p < at), "{want:?} out of order");
        prev = Some(at);
    }
    // The 250-char line comes back as wrapped physical rows.
    let xs: Vec<&&str> = lines
        .iter()
        .filter(|l| !l.is_empty() && l.bytes().all(|b| b == b'x'))
        .collect();
    assert!(
        xs.len() >= 2,
        "long line not split into physical rows: {xs:?}"
    );
    assert_eq!(xs.iter().map(|l| l.len()).sum::<usize>(), 250);

    // Exactly the last 10 physical rows, joined by `\n`: blank rows are kept, only trailing blanks of a
    // row are stripped (ruling "收敛 1"). The output scrolled, so the last row is the cursor's empty row.
    let last = srv.read(&id, 10);
    let rows: Vec<&str> = last.split('\n').collect();
    assert_eq!(rows.len(), 10, "asked for 10 rows: {last:?}");
    assert_eq!(
        rows[9], "",
        "the empty cursor row at the bottom was dropped: {last:?}"
    );
    assert!(rows.contains(&"DONE"), "{last:?}");
    assert!(!last.contains("line 250"), "{last:?}");

    // A screen that is mostly empty comes back with all its blank rows.
    let short = srv.spawn_id(
        "fx-short",
        "worker",
        &["sh", "-c", "echo SHORT; exec sleep 1000"],
    );
    srv.wait_text(&short, "SHORT", Duration::from_secs(5));
    let height = srv.pane(&short)["rows"].as_u64().expect("pane.get rows");
    let screen = srv.read(&short, height);
    let rows: Vec<&str> = screen.split('\n').collect();
    assert_eq!(rows.len() as u64, height, "{screen:?}");
    assert_eq!(rows[0], "SHORT");
    assert!(rows[1..].iter().all(|r| r.is_empty()), "{screen:?}");
    // Asking for more rows than exist returns all of them.
    assert_eq!(srv.read(&short, 100_000), screen);
}

#[test]
fn pane_rename_focus_move() {
    let srv = Server::start();
    let a = srv.spawn_id("fx-worker-1", "worker", &["sleep", "1000"]);
    let b = srv.spawn_id("fx-worker-2", "worker", &["sleep", "1000"]);
    let c = srv.spawn_id("fx-worker-3", "worker", &["sleep", "1000"]);
    assert_eq!(
        srv.group_order("workers"),
        vec![a.clone(), b.clone(), c.clone()]
    );

    srv.ok("pane.rename", json!({"pane": a, "title": "fx-worker-1.1"}));
    assert_eq!(srv.pane(&a)["title"], json!("fx-worker-1.1"));

    srv.ok("pane.focus", json!({"pane": b}));
    assert_eq!(srv.pane(&b)["focused"], json!(true));
    assert_eq!(srv.pane(&a)["focused"], json!(false));

    srv.ok(
        "pane.move",
        json!({"pane": c, "group": "workers", "before": a}),
    );
    assert_eq!(
        srv.group_order("workers"),
        vec![c.clone(), a.clone(), b.clone()]
    );
    srv.ok("pane.move", json!({"pane": a, "group": "lower"}));
    assert_eq!(srv.pane(&a)["group"], json!("lower"));
    assert_eq!(srv.group_order("workers"), vec![c.clone(), b.clone()]);
    srv.ok(
        "pane.move",
        json!({"pane": a, "group": "workers", "after": c}),
    );
    assert_eq!(
        srv.group_order("workers"),
        vec![c.clone(), a.clone(), b.clone()]
    );

    // Bad group → `unknown_type` (R5); contradictory / foreign references → `bad_request`.
    assert_eq!(
        srv.err("pane.move", json!({"pane": a, "group": "nowhere"})),
        "unknown_type"
    );
    assert_eq!(
        srv.err(
            "pane.move",
            json!({"pane": a, "group": "workers", "before": c, "after": b})
        ),
        "bad_request"
    );
    assert_eq!(
        srv.err(
            "pane.move",
            json!({"pane": a, "group": "lower", "before": c})
        ),
        "bad_request"
    );
    assert_eq!(
        srv.group_order("workers"),
        vec![c.clone(), a.clone(), b.clone()],
        "a refused move changed the order"
    );

    let ghost = json!({"pane": "zzzz-999"});
    assert_eq!(srv.err("pane.focus", ghost.clone()), "no_such_pane");
    assert_eq!(
        srv.err(
            "pane.move",
            json!({"pane": a, "group": "workers", "after": "zzzz-999"})
        ),
        "no_such_pane"
    );
    assert_eq!(
        srv.err("pane.rename", json!({"pane": "zzzz-999", "title": "x"})),
        "no_such_pane"
    );
    assert_eq!(
        srv.err("pane.move", json!({"pane": "zzzz-999", "group": "workers"})),
        "no_such_pane"
    );
}

#[test]
fn pane_kill_returns_after_process_group_is_reaped() {
    let srv = Server::start();
    let r = srv.spawn(
        "fx-worker-1",
        "worker",
        &["sh", "-c", "sleep 1000 & sleep 1000 & echo KIDS-UP; wait"],
        json!({}),
    );
    let id = r["pane"].as_str().unwrap().to_string();
    let (pid, pgid) = (
        r["pid"].as_i64().unwrap() as i32,
        r["pgid"].as_i64().unwrap() as i32,
    );
    srv.wait_text(&id, "KIDS-UP", Duration::from_secs(5));
    assert!(group_alive(pgid));

    srv.ok("pane.kill", json!({"pane": id}));
    // The reply means "reaped": no member of the group is left, not even a zombie.
    assert!(
        !pid_alive(pid),
        "pane leader {pid} still exists after pane.kill returned"
    );
    assert!(
        !group_alive(pgid),
        "process group {pgid} still has members after pane.kill returned"
    );
    assert!(!srv.listed(&id));
    assert_eq!(srv.err("pane.kill", json!({"pane": id})), "no_such_pane");
}

#[test]
fn pane_kill_timeout_is_error_and_pane_stays() {
    let srv = Server::start();
    let script = r#"trap '' HUP TERM; echo STUBBORN; while :; do sleep 1; done"#;
    let r = srv.spawn("fx-worker-1", "worker", &["sh", "-c", script], json!({}));
    let id = r["pane"].as_str().unwrap().to_string();
    let pid = r["pid"].as_i64().unwrap() as i32;
    srv.wait_text(&id, "STUBBORN", Duration::from_secs(5));

    // Schedule (R4): HUP at 0, TERM at 2000, KILL at 5000 ms, each only if its time is <= timeout_ms;
    // then at most 500 ms to confirm the reap. 300 ms → HUP only (ignored) → kill_timeout at ~800 ms.
    let t0 = Instant::now();
    assert_eq!(
        srv.err("pane.kill", json!({"pane": id, "timeout_ms": 300})),
        "kill_timeout"
    );
    let took = t0.elapsed();
    assert!(
        took >= Duration::from_millis(300) && took < Duration::from_millis(1500),
        "timeout_ms 300 + 500 ms grace not honoured: {took:?}"
    );
    assert!(
        srv.listed(&id),
        "pane removed although the kill was not confirmed"
    );
    assert!(pid_alive(pid));

    // 2000 ms → HUP and TERM (both ignored), never KILL: still a timeout, the process survives.
    let t0 = Instant::now();
    assert_eq!(
        srv.err("pane.kill", json!({"pane": id, "timeout_ms": 2000})),
        "kill_timeout"
    );
    let took = t0.elapsed();
    assert!(
        took >= Duration::from_millis(2000) && took < Duration::from_millis(3500),
        "{took:?}"
    );
    std::thread::sleep(Duration::from_millis(200));
    assert!(
        pid_alive(pid),
        "SIGKILL was sent although timeout_ms < 5000"
    );
    assert!(srv.listed(&id));

    // The default (5000) reaches KILL at 5 s and confirms within the grace.
    let t0 = Instant::now();
    srv.ok("pane.kill", json!({"pane": id}));
    let took = t0.elapsed();
    assert!(
        took >= Duration::from_millis(5000) && took < Duration::from_millis(6500),
        "{took:?}"
    );
    assert!(!pid_alive(pid));
    assert!(!srv.listed(&id));
}

#[test]
fn layout_get_set_writes_layout_json() {
    let srv = Server::start();
    let l = srv.ok("layout.get", json!({}));
    assert_eq!(
        (l["h_ratio"].as_u64(), l["v_ratio"].as_u64()),
        (Some(50), Some(60)),
        "defaults: {l}"
    );
    assert!(l["zoom"].is_null(), "{l}");

    assert_eq!(l["focus"], json!("supervisor"), "{l}");
    assert!(l["groups"].is_object() && l["selected"].is_object(), "{l}");

    // layout.set replies with the whole layout after the change.
    let set = srv.ok("layout.set", json!({"h_ratio": 70, "v_ratio": 30}));
    assert_eq!(
        (set["h_ratio"].as_u64(), set["v_ratio"].as_u64()),
        (Some(70), Some(30)),
        "{set}"
    );
    let l = srv.ok("layout.get", json!({}));
    assert_eq!(
        (l["h_ratio"].as_u64(), l["v_ratio"].as_u64()),
        (Some(70), Some(30)),
        "{l}"
    );

    let file = srv.runtime.join("layout.json");
    let saved: Value = wait_until(
        "layout.json with the new ratios",
        Duration::from_secs(5),
        || {
            let v: Value = serde_json::from_str(&std::fs::read_to_string(&file).ok()?).ok()?;
            (v["h_ratio"] == json!(70) && v["v_ratio"] == json!(30)).then_some(v)
        },
    );
    assert!(saved["zoom"].is_null(), "{saved}");
    assert_eq!(saved["version"], json!(1), "{saved}");

    // Ratios are clamped to 10–90; `{}` is a no-op that returns the layout.
    let clamped = srv.ok("layout.set", json!({"h_ratio": 5, "v_ratio": 95}));
    assert_eq!(
        (clamped["h_ratio"].as_u64(), clamped["v_ratio"].as_u64()),
        (Some(10), Some(90)),
        "{clamped}"
    );
    assert_eq!(
        srv.ok("layout.set", json!({}))["h_ratio"].as_u64(),
        Some(10)
    );
    assert_eq!(
        srv.err("layout.set", json!({"zoom": "zzzz-999"})),
        "no_such_pane"
    );
}

#[test]
fn shutdown_refused_while_panes_exist_unless_force() {
    let mut srv = Server::start();
    let r = srv.spawn("fx-worker-1", "worker", &["sleep", "1000"], json!({}));
    let id = r["pane"].as_str().unwrap().to_string();
    let pgid = r["pgid"].as_i64().unwrap() as i32;

    assert_eq!(srv.err("server.shutdown", json!({})), "not_empty");
    srv.ok("hello", json!({}));
    assert!(srv.listed(&id));

    let had_lock = srv.runtime.join("start.lock").exists();
    srv.ok("server.shutdown", json!({"force": true}));
    let status = wait_exit(&mut srv.child, Duration::from_secs(10))
        .expect("server still running after forced shutdown");
    assert!(status.success(), "{status}");
    assert!(
        !group_alive(pgid),
        "forced shutdown left the pane's process group running"
    );
    // Live-state files go; the log, the layout preference and the lock target stay (R7).
    for gone in ["ctl.sock", "server.json", "panes.json"] {
        assert!(!srv.runtime.join(gone).exists(), "{gone} left behind");
    }
    for kept in ["server.log", "layout.json"] {
        assert!(srv.runtime.join(kept).exists(), "{kept} was removed");
    }
    if had_lock {
        assert!(
            srv.runtime.join("start.lock").exists(),
            "start.lock was deleted"
        );
    }

    // With no panes a plain shutdown is accepted.
    let mut empty = Server::start();
    empty.ok("server.shutdown", json!({}));
    let status =
        wait_exit(&mut empty.child, Duration::from_secs(10)).expect("idle server did not exit");
    assert!(status.success(), "{status}");
    assert!(!empty.sock().exists());
}

#[test]
fn pane_queue_is_per_pane_a_stuck_pane_does_not_block_others() {
    let srv = Server::start();
    // Raw mode + a program that never reads: the PTY input queue fills up and stays full.
    let stuck = srv.spawn_id(
        "fx-stuck",
        "worker",
        &[
            "sh",
            "-c",
            "stty raw -echo; echo STUCK-READY; exec sleep 1000",
        ],
    );
    srv.wait_text(&stuck, "STUCK-READY", Duration::from_secs(5));
    let out = srv.tmp.path().join("flowing.bin");
    let sink = spawn_sink(&srv, &out, false);

    let big = vec![b'x'; 1 << 20];
    let mut c = srv.client();
    c.writer
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let t0 = Instant::now();
    let r = c.try_request(
        "pane.write",
        json!({"pane": stuck, "data": b64(&big), "mode": "raw"}),
    );
    let r = r.unwrap_or_else(|e| panic!("pane.write to a stuck pane blocked the connection: {e}"));
    assert_eq!(r["ok"], json!(true), "{r}");
    assert!(t0.elapsed() < Duration::from_secs(3));

    // The per-pane queue is bounded (16 MiB pending, PROTOCOL.md §4.7): past it a write is refused with
    // `write_failed`, promptly, and the connection stays usable. 2 MiB chunks keep each line under 4 MiB.
    let chunk = vec![b'y'; 2 << 20];
    let mut refused = None;
    for n in 0..12 {
        let t = Instant::now();
        let r = c
            .try_request(
                "pane.write",
                json!({"pane": stuck, "data": b64(&chunk), "mode": "raw"}),
            )
            .unwrap_or_else(|e| {
                panic!("pane.write #{n} to a full queue blocked the connection: {e}")
            });
        assert!(t.elapsed() < Duration::from_secs(3));
        if r["ok"] == json!(false) {
            refused = Some(r);
            break;
        }
    }
    let refused = refused.expect("17+ MiB queued on one pane without write_failed");
    assert_eq!(refused["code"], json!("write_failed"), "{refused}");
    assert_eq!(c.ok("hello", json!({}))["protocol"].as_u64(), Some(1));

    // Other panes and other connections keep working while the stuck pane's queue is full.
    srv.write(&sink, b"still-flowing", "raw");
    wait_file_eq(&out, b"still-flowing", Duration::from_secs(3));
    let t1 = Instant::now();
    srv.ok("hello", json!({}));
    srv.read(&stuck, 10);
    assert!(
        t1.elapsed() < Duration::from_secs(1),
        "server slowed down by a stuck pane: {:?}",
        t1.elapsed()
    );
}

// ------------------------------------------------- foreground job (2026-11-08) ----------------------
// Named `shell_*` so they sort after `pane_spawn_*`: each `Server::start` takes the next temp-dir number, and a
// two-digit one makes `pane_spawn_returns_id_pid_pgid_tty_and_list_shows_it`'s echoed cwd wrap at 119 columns.

/// A shell with job control running a foreground job (its own process group) that ignores SIGHUP.
const FG_JOB_IGNORING_HUP: &str = "set -m; echo FG-UP; sh -c 'trap \"\" HUP; exec sleep 1000'; :";

/// Waits until the record's `fg_pgid` differs from its `pgid` and returns it.
fn wait_fg_pgid(srv: &Server, id: &str, pgid: i64) -> i64 {
    wait_until(
        "the foreground job in fg_pgid",
        Duration::from_secs(5),
        || srv.pane(id)["fg_pgid"].as_i64().filter(|&g| g != pgid),
    )
}

#[test]
fn shell_pane_record_reports_the_foreground_group() {
    let srv = Server::start();
    let idle = srv.spawn("bash-1", "shell", &["sleep", "1000"], json!({}));
    let idle_id = idle["pane"].as_str().unwrap().to_string();
    let idle_pgid = idle["pgid"].as_i64().unwrap();
    let rec = srv.pane(&idle_id);
    assert_eq!(
        rec["fg_pgid"].as_i64(),
        Some(idle_pgid),
        "an idle pane's foreground group is its own: {rec}"
    );
    let listed = srv
        .panes()
        .into_iter()
        .find(|p| p["pane"] == json!(idle_id))
        .unwrap();
    assert_eq!(listed["fg_pgid"].as_i64(), Some(idle_pgid), "{listed}");

    let busy = srv.spawn(
        "bash-2",
        "shell",
        &["bash", "-c", "set -m; sleep 1000; :"],
        json!({}),
    );
    let busy_id = busy["pane"].as_str().unwrap().to_string();
    let busy_pid = busy["pid"].as_i64().unwrap();
    let fg = wait_fg_pgid(&srv, &busy_id, busy["pgid"].as_i64().unwrap());
    srv.pgids.lock().unwrap().push(fg as i32);
    assert_eq!(unsafe { libc::getsid(fg as i32) } as i64, busy_pid);
}

#[test]
fn shell_foreground_job_ignoring_sighup_ends_with_pane_kill_and_forced_shutdown() {
    let mut srv = Server::start();
    let a = srv.spawn(
        "bash-1",
        "shell",
        &["bash", "-c", FG_JOB_IGNORING_HUP],
        json!({}),
    );
    let a_id = a["pane"].as_str().unwrap().to_string();
    srv.wait_text(&a_id, "FG-UP", Duration::from_secs(5));
    let a_fg = wait_fg_pgid(&srv, &a_id, a["pgid"].as_i64().unwrap()) as i32;
    srv.pgids.lock().unwrap().push(a_fg);
    srv.ok("pane.kill", json!({"pane": a_id}));
    assert!(!srv.listed(&a_id));
    assert!(
        !group_alive(a_fg),
        "pane.kill left the foreground job's group {a_fg} running"
    );

    let b = srv.spawn(
        "bash-2",
        "shell",
        &["bash", "-c", FG_JOB_IGNORING_HUP],
        json!({}),
    );
    let b_id = b["pane"].as_str().unwrap().to_string();
    srv.wait_text(&b_id, "FG-UP", Duration::from_secs(5));
    let b_fg = wait_fg_pgid(&srv, &b_id, b["pgid"].as_i64().unwrap()) as i32;
    srv.pgids.lock().unwrap().push(b_fg);
    srv.ok("server.shutdown", json!({"force": true}));
    let status = wait_exit(&mut srv.child, Duration::from_secs(10))
        .expect("server still running after forced shutdown");
    assert!(status.success(), "{status}");
    assert!(
        !group_alive(b_fg),
        "forced shutdown left the foreground job's group {b_fg} running"
    );
}
