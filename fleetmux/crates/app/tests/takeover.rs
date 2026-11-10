//! Takeover / single-server / orphan-reaping tests (plan.md §3.4; the Rust version of the WezTerm review
//! trio, design §5). Phase 0: red until M0 `server-core` (first 3) and M2 `robust` (reap, peer uid).
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

/// Extra server processes a test starts by hand; killed on drop.
struct Procs(Vec<Child>);

impl Drop for Procs {
    fn drop(&mut self) {
        for c in &mut self.0 {
            let _ = c.kill();
            let _ = c.wait();
        }
    }
}

fn server_json(runtime: &Path) -> Value {
    let text = std::fs::read_to_string(runtime.join("server.json")).expect("read server.json");
    serde_json::from_str(&text).expect("server.json is JSON")
}

/// Panes whose whole process group ignores SIGHUP, so they outlive the server's PTY masters and only a
/// reap (or our cleanup) ends them.
fn spawn_orphan_candidate(srv: &Server, title: &str) -> (String, i32) {
    let script = r#"trap '' HUP; sleep 1000 & sleep 1000 & echo ORPHAN-READY; wait"#;
    let r = srv.spawn(title, "worker", &["sh", "-c", script], json!({}));
    let id = r["pane"]
        .as_str()
        .unwrap_or_else(|| panic!("no pane: {r}"))
        .to_string();
    let pgid = r["pgid"].as_i64().unwrap_or_else(|| panic!("no pgid: {r}")) as i32;
    srv.wait_text(&id, "ORPHAN-READY", Duration::from_secs(5));
    (id, pgid)
}

fn sigkill_server(srv: &mut Server) {
    unsafe { libc::kill(srv.pid(), libc::SIGKILL) };
    wait_exit(&mut srv.child, Duration::from_secs(5)).expect("server survived SIGKILL");
}

fn reap(runtime: &Path) -> std::process::Output {
    Command::new(BIN)
        .arg("reap")
        .arg("--runtime")
        .arg(runtime)
        .stdin(Stdio::null())
        .output()
        .expect("run fleetmux reap")
}

#[test]
fn second_server_refuses_when_socket_answers() {
    let srv = Server::start();
    let first = srv.ok("hello", json!({}));

    let mut procs = Procs(vec![spawn_server(
        &srv.runtime,
        srv.tmp.path(),
        "second",
        &[],
    )]);
    let status = wait_exit(&mut procs.0[0], Duration::from_secs(10))
        .expect("second server kept running on a live socket");
    assert!(
        !status.success(),
        "second server must refuse with a failure status: {status}"
    );

    let again = srv.ok("hello", json!({}));
    assert_eq!(
        (again["pid"].clone(), again["epoch"].clone()),
        (first["pid"].clone(), first["epoch"].clone())
    );
    assert_eq!(
        server_json(&srv.runtime)["pid"],
        first["pid"],
        "server.json was overwritten by the refused server"
    );
}

#[test]
fn two_starters_race_only_one_server_wins_start_lock() {
    let tmp = TempDir::new();
    let runtime = tmp.path().join("rt");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&runtime)
        .unwrap();
    let sock = runtime.join("ctl.sock");
    let mut procs = Procs(vec![
        spawn_server(&runtime, tmp.path(), "a", &[]),
        spawn_server(&runtime, tmp.path(), "b", &[]),
    ]);

    let hello = wait_until(
        "one of two racing servers to answer hello",
        START_TIMEOUT,
        || {
            if let Some(h) = try_hello(&sock) {
                return Some(h);
            }
            let exited: Vec<bool> = procs
                .0
                .iter_mut()
                .map(|c| matches!(c.try_wait(), Ok(Some(_))))
                .collect();
            if exited.iter().all(|e| *e) {
                panic!(
                    "both racing servers exited\na:\n{}\nb:\n{}",
                    tail(&tmp.path().join("a.out"), 20),
                    tail(&tmp.path().join("b.out"), 20)
                );
            }
            None
        },
    );

    let (winner, loser_status) =
        wait_until("the losing server to exit", Duration::from_secs(10), || {
            let a = procs.0[0].try_wait().unwrap();
            let b = procs.0[1].try_wait().unwrap();
            match (a, b) {
                (None, Some(s)) => Some((0, s)),
                (Some(s), None) => Some((1, s)),
                (Some(_), Some(_)) => panic!("both servers exited after one answered hello"),
                (None, None) => None,
            }
        });
    assert!(
        !loser_status.success(),
        "losing server must exit with a failure status: {loser_status}"
    );
    assert_eq!(
        hello["pid"].as_i64(),
        Some(procs.0[winner].id() as i64),
        "hello answered by the process that later exited"
    );
    assert_eq!(server_json(&runtime)["pid"], hello["pid"]);
    // The winner keeps serving.
    assert_eq!(
        try_hello(&sock).expect("winner stopped answering")["epoch"],
        hello["epoch"]
    );
}

#[test]
fn fresh_client_finds_server_by_path_alone() {
    let srv = Server::start();
    let id = srv.spawn_id("fx-control", "daemon", &["sleep", "1000"]);

    // A brand-new client that knows nothing but the runtime directory.
    let runtime = srv.runtime.clone();
    let mut fresh = Client::connect(&runtime.join("ctl.sock")).expect("connect by path");
    let h = fresh.ok("hello", json!({}));
    let sj = server_json(&runtime);
    assert_eq!(
        (sj["pid"].clone(), sj["epoch"].clone()),
        (h["pid"].clone(), h["epoch"].clone()),
        "server.json disagrees with hello"
    );
    let panes = fresh.ok("pane.list", json!({}));
    assert!(
        panes["panes"]
            .as_array()
            .is_some_and(|ps| ps.iter().any(|p| p["pane"] == json!(id))),
        "{panes}"
    );
    // server.json and panes.json (PROTOCOL.md §8.1, §8.2) describe the same server and pane.
    assert!(
        sj["start_time"].is_u64(),
        "server.json start_time must be integer Unix seconds: {sj}"
    );
    assert_eq!(
        sj["version"].as_str(),
        Some(env!("CARGO_PKG_VERSION")),
        "{sj}"
    );
    assert_eq!(sj["protocol"].as_u64(), Some(1), "{sj}");
    let ledger: Value = serde_json::from_str(
        &std::fs::read_to_string(runtime.join("panes.json")).expect("panes.json"),
    )
    .expect("panes.json is JSON");
    assert_eq!(ledger["epoch"], h["epoch"], "{ledger}");
    let rec = ledger["panes"]
        .as_array()
        .and_then(|ps| ps.iter().find(|p| p["pane"] == json!(id)))
        .unwrap_or_else(|| panic!("panes.json does not record {id}: {ledger}"));
    assert!(rec["start_time"].is_u64(), "{rec}");
    assert_eq!(rec["group"], json!("lower"), "{rec}");
    assert_eq!(rec["type"], json!("daemon"), "{rec}");
}

#[test]
fn sigkilled_server_then_reap_kills_every_recorded_process_group() {
    let mut srv = Server::start();
    let (_, g1) = spawn_orphan_candidate(&srv, "fx-worker-1");
    let (_, g2) = spawn_orphan_candidate(&srv, "fx-worker-2");

    sigkill_server(&mut srv);
    std::thread::sleep(Duration::from_millis(500));
    assert!(
        group_alive(g1) && group_alive(g2),
        "fixture broken: panes died with the server, reap has nothing to do"
    );

    let out = reap(&srv.runtime);
    assert!(
        out.status.success(),
        "fleetmux reap failed: {}\n{}",
        out.status,
        String::from_utf8_lossy(&out.stderr)
    );
    for g in [g1, g2] {
        wait_until(
            &format!("process group {g} to be gone after reap"),
            Duration::from_secs(2),
            || (!group_alive(g)).then_some(()),
        );
    }
}

#[test]
fn reap_skips_pid_whose_start_time_differs() {
    let mut srv = Server::start();
    let (_, pgid) = spawn_orphan_candidate(&srv, "fx-worker-1");
    let (_, unverifiable) = spawn_orphan_candidate(&srv, "fx-worker-2");
    sigkill_server(&mut srv);

    // Make the ledger's recorded start time disagree with the live process by one second, as if the
    // pid had been reused: start times are compared exactly, with no ±1 s tolerance (R6, PROTOCOL.md §8.6).
    let path = srv.runtime.join("panes.json");
    let mut ledger: Value =
        serde_json::from_str(&std::fs::read_to_string(&path).expect("panes.json"))
            .expect("panes.json is JSON");
    let rec = ledger["panes"]
        .as_array_mut()
        .and_then(|ps| {
            ps.iter_mut()
                .find(|p| p["pgid"].as_i64() == Some(pgid as i64))
        })
        .unwrap_or_else(|| panic!("panes.json has no record for pgid {pgid}"));
    let recorded = rec["start_time"]
        .as_u64()
        .unwrap_or_else(|| panic!("start_time must be integer Unix seconds: {rec}"));
    rec["start_time"] = json!(recorded + 1);
    // A record whose start time cannot be verified is never signalled either (PROTOCOL.md §8.6).
    let rec = ledger["panes"]
        .as_array_mut()
        .and_then(|ps| {
            ps.iter_mut()
                .find(|p| p["pgid"].as_i64() == Some(unverifiable as i64))
        })
        .unwrap_or_else(|| panic!("panes.json has no record for pgid {unverifiable}"));
    rec["start_time"] = Value::Null;
    std::fs::write(&path, ledger.to_string()).unwrap();

    let out = reap(&srv.runtime);
    std::thread::sleep(Duration::from_millis(500));
    assert!(
        group_alive(pgid),
        "reap killed a group whose start time did not match ({}): {}",
        out.status,
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        group_alive(unverifiable),
        "reap killed a group whose start time could not be verified ({}): {}",
        out.status,
        String::from_utf8_lossy(&out.stderr)
    );
}

#[test]
fn peer_uid_check_rejects_other_uid() {
    // plan.md §3.4 exception: CI has no second user, so this one test calls the pure peer check of
    // PROTOCOL.md §3 with injected uids instead of starting the binary.
    use fleetmux_proto::peer_allowed;
    let me = uid();
    assert!(peer_allowed(me, me), "same uid must be served");
    assert!(!peer_allowed(me, me + 1), "a different uid must be refused");
    assert!(!peer_allowed(me + 1, me), "a different uid must be refused");
    assert!(peer_allowed(0, 0));
    assert!(!peer_allowed(501, 0), "root is a different uid too");
    assert!(!peer_allowed(0, 501));
    assert!(!peer_allowed(501, 502));
}

#[test]
fn kill_user_processes_warning_is_printed_when_set() {
    // PROTOCOL.md §8.7: Linux only; the server reads $FLEETMUX_LOGIND_CONF (default /etc/systemd/logind.conf).
    if !cfg!(target_os = "linux") {
        eprintln!("skipped: logind is Linux-only");
        return;
    }
    let conf_dir = TempDir::new();
    let conf = conf_dir.path().join("logind.conf");
    std::fs::write(
        &conf,
        "[Login]\n#KillUserProcesses=no\nKillUserProcesses=yes\n",
    )
    .unwrap();
    let mut srv = Server::launch(&[("FLEETMUX_LOGIND_CONF", conf.to_str().unwrap())]);
    srv.wait_ready();
    let log = srv.runtime.join("server.log");
    wait_until(
        "a WARN logind line in server.log",
        Duration::from_secs(5),
        || {
            std::fs::read_to_string(&log)
                .ok()
                .filter(|t| t.lines().any(|l| l.contains("WARN") && l.contains("logind")))
        },
    );
}
