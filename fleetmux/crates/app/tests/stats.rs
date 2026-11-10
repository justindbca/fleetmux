//! `server.stats` (PROTOCOL.md §4.17; brief-m03 §3.4, M3, package B21 stats). Red in stage A (`todo!()`);
//! ignored until the M3 gate runs it with `--include-ignored`. Self-contained harness (no `tests/common`).

use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_fleetmux");
const KEYS: [&str; 7] = [
    "panes",
    "viewers",
    "frames",
    "parse_bytes",
    "wakeups",
    "rss_bytes",
    "scrollback_rows",
];

fn request(sock: &Path, cmd: &str, args: Value) -> std::io::Result<Value> {
    let mut s = UnixStream::connect(sock)?;
    s.set_read_timeout(Some(Duration::from_secs(15)))?;
    let mut req = args;
    req["id"] = json!(1);
    req["cmd"] = json!(cmd);
    let mut line = serde_json::to_vec(&req).unwrap();
    line.push(b'\n');
    s.write_all(&line)?;
    let mut reply = String::new();
    BufReader::new(s).read_line(&mut reply)?;
    serde_json::from_str(&reply).map_err(|e| std::io::Error::other(format!("{e}: {reply:?}")))
}

fn ok(sock: &Path, cmd: &str, args: Value) -> Value {
    let r = request(sock, cmd, args).unwrap_or_else(|e| panic!("{cmd}: {e}"));
    assert_eq!(r["ok"], json!(true), "{cmd}: {r}");
    r
}

fn stats(sock: &Path) -> Value {
    let r = ok(sock, "server.stats", json!({}));
    for k in KEYS {
        assert!(
            r[k].is_u64(),
            "server.stats {k} not a non-negative int: {r}"
        );
    }
    r
}

struct Server {
    child: Child,
    dir: PathBuf,
    pgids: Vec<i32>,
}

impl Server {
    fn start() -> Server {
        let dir = std::env::temp_dir().join(format!("fts{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(dir.join("rt"))
            .unwrap();
        let dir = dir.canonicalize().unwrap();
        let out = std::fs::File::create(dir.join("server.out")).unwrap();
        let child = Command::new(BIN)
            .arg("server")
            .arg("--runtime")
            .arg(dir.join("rt"))
            .args(["--session", "stats"])
            .stdin(Stdio::null())
            .stdout(Stdio::from(out.try_clone().unwrap()))
            .stderr(Stdio::from(out))
            .spawn()
            .expect("spawn fleetmux server");
        let mut srv = Server {
            child,
            dir,
            pgids: Vec::new(),
        };
        let end = Instant::now() + Duration::from_secs(10);
        while request(&srv.sock(), "hello", json!({}))
            .map(|r| r["ok"] == json!(true))
            .ok()
            != Some(true)
        {
            if let Ok(Some(st)) = srv.child.try_wait() {
                panic!("server exited early ({st})");
            }
            assert!(Instant::now() < end, "no hello within 10 s");
            std::thread::sleep(Duration::from_millis(50));
        }
        srv
    }

    fn sock(&self) -> PathBuf {
        self.dir.join("rt").join("ctl.sock")
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = request(&self.sock(), "server.shutdown", json!({"force": true}));
        std::thread::sleep(Duration::from_millis(200));
        let _ = self.child.kill();
        let _ = self.child.wait();
        for &g in &self.pgids {
            if g > 1 && unsafe { libc::getpgid(g) } == g {
                unsafe { libc::kill(-g, libc::SIGKILL) };
            }
        }
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// A view connection (PROTOCOL.md §6.1) that drains frames in the background and stays open until dropped.
fn connect_viewer(sock: &Path) -> UnixStream {
    let mut s = UnixStream::connect(sock).expect("connect viewer");
    let hello = json!({"view": 1, "cols": 240, "rows": 60, "term": "xterm-256color", "colorterm": "truecolor"});
    let mut line = serde_json::to_vec(&hello).unwrap();
    line.push(b'\n');
    s.write_all(&line).unwrap();
    let mut r = BufReader::new(s.try_clone().unwrap());
    let mut reply = String::new();
    r.read_line(&mut reply).unwrap();
    let v: Value = serde_json::from_str(&reply).unwrap_or_else(|e| panic!("{e}: {reply:?}"));
    assert_eq!(v["ok"], json!(true), "{v}");
    std::thread::spawn(move || {
        let mut buf = [0u8; 65536];
        while matches!(r.read(&mut buf), Ok(n) if n > 0) {}
    });
    s
}

#[test]
fn server_stats_counts_panes_viewers_frames_and_idle_wakeups_zero() {
    let mut srv = Server::start();
    let s0 = stats(&srv.sock());
    assert_eq!(
        (s0["panes"].as_u64(), s0["viewers"].as_u64()),
        (Some(0), Some(0)),
        "{s0}"
    );
    assert!(s0["rss_bytes"].as_u64().unwrap() > 0, "{s0}");
    let cwd = srv.dir.to_str().unwrap().to_string();
    for title in ["w1", "w2"] {
        let r = ok(
            &srv.sock(),
            "pane.spawn",
            json!({"argv": ["sleep", "60"], "cwd": cwd, "title": title, "type": "worker"}),
        );
        if let Some(g) = r["pgid"].as_i64() {
            srv.pgids.push(g as i32);
        }
    }
    let _viewer = connect_viewer(&srv.sock());
    // Wait for the first frame to be drawn for the viewer.
    let end = Instant::now() + Duration::from_secs(5);
    let s1 = loop {
        let s = stats(&srv.sock());
        if s["frames"].as_u64().unwrap() > 0 && s["viewers"] == json!(1) {
            break s;
        }
        assert!(
            Instant::now() < end,
            "no frame drawn for the viewer within 5 s: {s}"
        );
        std::thread::sleep(Duration::from_millis(50));
    };
    assert_eq!(
        (s1["panes"].as_u64(), s1["viewers"].as_u64()),
        (Some(2), Some(1)),
        "{s1}"
    );
    assert_eq!(ok(&srv.sock(), "hello", json!({}))["viewers"], json!(1));
    // Idle: nothing writes, nobody types. Let the first frame settle, then 1 s apart wakeups must not move.
    std::thread::sleep(Duration::from_millis(1000));
    let a = stats(&srv.sock());
    std::thread::sleep(Duration::from_millis(1000));
    let b = stats(&srv.sock());
    assert_eq!(a["wakeups"], b["wakeups"], "idle server woke up: {a} → {b}");
}
