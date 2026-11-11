//! `fleetmux server` command line (PROTOCOL.md §9.1; brief-m03 §3.4, M0, package B6 server-ctl). Red in stage A
//! (`todo!()`), green from B6. Self-contained harness (no `tests/common`), like the phase 0 files.

use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

use fleetmux::server::{Opts, parse_opts};

const BIN: &str = env!("CARGO_BIN_EXE_fleetmux");
const START_TIMEOUT: Duration = Duration::from_secs(10);

static SEQ: AtomicU32 = AtomicU32::new(0);

/// Short private temp directory (macOS `sockaddr_un` limit), removed on drop.
struct TempDir(PathBuf);

impl TempDir {
    fn new() -> TempDir {
        let p = std::env::temp_dir().join(format!(
            "ftc{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&p)
            .expect("create temp dir");
        TempDir(p.canonicalize().expect("canonical temp dir"))
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn wait_exit(child: &mut Child, timeout: Duration) -> Option<ExitStatus> {
    let end = Instant::now() + timeout;
    loop {
        if let Ok(Some(s)) = child.try_wait() {
            return Some(s);
        }
        if Instant::now() >= end {
            return None;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn read_or_empty(p: &Path) -> String {
    std::fs::read_to_string(p).unwrap_or_default()
}

/// One request on a fresh connection.
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

/// A foreground `fleetmux server --runtime <tmp>/rt --session cli <extra…>`; shut down and killed on drop.
struct Server {
    child: Child,
    tmp: TempDir,
    runtime: PathBuf,
    pgids: Vec<i32>,
}

impl Server {
    fn start(extra: &[&str]) -> Server {
        let tmp = TempDir::new();
        let runtime = tmp.0.join("rt");
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&runtime)
            .expect("create runtime dir");
        let out = std::fs::File::create(tmp.0.join("server.out")).unwrap();
        let child = Command::new(BIN)
            .arg("server")
            .arg("--runtime")
            .arg(&runtime)
            .args(["--session", "cli"])
            .args(extra)
            .stdin(Stdio::null())
            .stdout(Stdio::from(out.try_clone().unwrap()))
            .stderr(Stdio::from(out))
            .spawn()
            .expect("spawn fleetmux server");
        let mut srv = Server {
            child,
            tmp,
            runtime,
            pgids: Vec::new(),
        };
        let end = Instant::now() + START_TIMEOUT;
        loop {
            if let Ok(r) = request(&srv.sock(), "hello", json!({})) {
                if r["ok"] == json!(true) {
                    break;
                }
            }
            if let Ok(Some(st)) = srv.child.try_wait() {
                panic!("server exited early ({st})\n{}", srv.diagnostics());
            }
            assert!(
                Instant::now() < end,
                "no hello within {START_TIMEOUT:?}\n{}",
                srv.diagnostics()
            );
            std::thread::sleep(Duration::from_millis(50));
        }
        srv
    }

    fn sock(&self) -> PathBuf {
        self.runtime.join("ctl.sock")
    }

    fn diagnostics(&self) -> String {
        format!(
            "server output:\n{}\nserver.log:\n{}",
            read_or_empty(&self.tmp.0.join("server.out")),
            read_or_empty(&self.runtime.join("server.log"))
        )
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = request(&self.sock(), "server.shutdown", json!({"force": true}));
        if wait_exit(&mut self.child, Duration::from_secs(2)).is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
        for &g in &self.pgids {
            if g > 1 && unsafe { libc::getpgid(g) } == g {
                unsafe { libc::kill(-g, libc::SIGKILL) };
            }
        }
    }
}

#[test]
fn server_refuses_too_long_socket_exit_4_and_logs() {
    let tmp = TempDir::new();
    // An owner-only runtime directory whose `ctl.sock` is longer than any platform's sun_path.
    let runtime = tmp.0.join("d".repeat(120));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&runtime)
        .expect("create long runtime dir");
    let sock = runtime.join("ctl.sock");
    assert!(
        sock.as_os_str().len() > 107,
        "precondition: {}",
        sock.display()
    );
    let out = std::fs::File::create(tmp.0.join("server.out")).unwrap();
    let mut child = Command::new(BIN)
        .arg("server")
        .arg("--runtime")
        .arg(&runtime)
        .args(["--session", "cli"])
        .stdin(Stdio::null())
        .stdout(Stdio::from(out.try_clone().unwrap()))
        .stderr(Stdio::from(out))
        .spawn()
        .expect("spawn fleetmux server");
    let status = wait_exit(&mut child, START_TIMEOUT).unwrap_or_else(|| {
        let _ = child.kill();
        let _ = child.wait();
        panic!("server with a too-long socket path did not exit within {START_TIMEOUT:?}")
    });
    let log = read_or_empty(&runtime.join("server.log"));
    assert_eq!(
        status.code(),
        Some(4),
        "exit code; output:\n{}\nserver.log:\n{log}",
        read_or_empty(&tmp.0.join("server.out"))
    );
    // Exactly one ERROR line saying why, in server.log (PROTOCOL.md §9.1, §8.5).
    let errors: Vec<&str> = log.lines().filter(|l| l.contains(" ERROR ")).collect();
    assert_eq!(errors.len(), 1, "server.log:\n{log}");
    // No socket was bound and no server.json written.
    assert!(!sock.exists());
    assert!(!runtime.join("server.json").exists());
}

#[test]
fn server_default_size_and_prefix_flags_parse() {
    // Pure option parsing: given flags and the documented defaults.
    let args = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    let o = parse_opts(&args(&[
        "--runtime",
        "/r",
        "--session",
        "s",
        "--default-size",
        "100x30",
        "--prefix",
        "C-a",
    ]))
    .unwrap();
    assert_eq!(
        o,
        Opts {
            runtime: PathBuf::from("/r"),
            session: "s".into(),
            daemonize: false,
            default_size: (100, 30),
            record: None,
            scrollback: 10000,
            prefix: "C-a".into(),
            palette: fleetmux_term::Palette::CMUX,
        }
    );
    let d = parse_opts(&args(&["--runtime", "/r", "--session", "s"])).unwrap();
    assert_eq!(
        (d.default_size, d.prefix.as_str(), d.scrollback),
        ((240, 60), "C-b", 10000)
    );
    for bad in [
        &["--runtime", "/r", "--session", "s", "--default-size", "100"][..],
        &[
            "--runtime",
            "/r",
            "--session",
            "s",
            "--default-size",
            "0x30",
        ],
        &["--runtime", "/r", "--session", "s", "--default-size", "axb"],
        &["--runtime", "/r", "--session", "s", "--scrollback", "many"],
        &["--runtime", "/r", "--session", "s", "--bogus"],
        &["--session", "s"],
        &["--runtime", "/r"],
    ] {
        assert!(parse_opts(&args(bad)).is_err(), "{bad:?}");
    }

    // The real binary: hello reports the prefix, and with no viewer a pane is sized from --default-size.
    let mut srv = Server::start(&["--default-size", "100x30", "--prefix", "C-a"]);
    let h = ok(&srv.sock(), "hello", json!({}));
    assert_eq!(h["prefix"], json!("C-a"), "{h}");
    assert_eq!(h["viewers"], json!(0), "{h}");
    let cwd = srv.tmp.0.to_str().unwrap().to_string();
    let r = ok(
        &srv.sock(),
        "pane.spawn",
        json!({"argv": ["sleep", "30"], "cwd": cwd, "title": "w", "type": "worker"}),
    );
    if let Some(g) = r["pgid"].as_i64() {
        srv.pgids.push(g as i32);
    }
    let p = ok(&srv.sock(), "pane.get", json!({"pane": r["pane"]}));
    let want = fleetmux_layout::Layout::for_platform().rects(100, 30).workers;
    let default = fleetmux_layout::Layout::for_platform().rects(240, 60).workers;
    assert_ne!((want.w, want.h), (default.w, default.h));
    assert_eq!(
        (p["cols"].as_u64(), p["rows"].as_u64()),
        (Some(want.w as u64), Some(want.h as u64)),
        "{p}"
    );
}

/// `--theme FILE` (PROTOCOL.md §9.1): the default palette, a partial file overriding `Palette::CMUX`, and a bad
/// file / value / unknown key / unreadable path all making `parse_opts` fail (usage, exit 2).
#[test]
fn server_theme_flag_parses_and_hello_reports_it() {
    let args = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    let tmp = TempDir::new();
    let write = |name: &str, body: &str| {
        let p = tmp.0.join(name);
        std::fs::write(&p, body).unwrap();
        p.to_str().unwrap().to_string()
    };

    // ① No `--theme`: `Opts.palette` is the cmux default and a live server reports all 19 values in `hello.theme`.
    let o = parse_opts(&args(&["--runtime", "/r", "--session", "s"])).unwrap();
    assert_eq!(o.palette, fleetmux_term::Palette::CMUX);
    let srv = Server::start(&[]);
    let h = ok(&srv.sock(), "hello", json!({}));
    let t = &h["theme"];
    assert_eq!(t["bg"], json!("#1e1e1e"), "{h}");
    assert_eq!(t["fg"], json!("#ffffff"), "{h}");
    assert_eq!(t["cursor"], json!("#98989d"), "{h}");
    let ansi = t["ansi"].as_array().unwrap_or_else(|| panic!("{h}"));
    assert_eq!(ansi.len(), 16, "{h}");
    assert_eq!(ansi[0], json!("#1a1a1a"), "{h}");
    assert_eq!(ansi[1], json!("#cc372e"), "{h}");
    assert_eq!(ansi[15], json!("#ffffff"), "{h}");

    // ② A partial file overrides only the keys it names; the rest stays cmux. Uppercase input is accepted and
    // reported lowercase.
    let partial = write("partial.json", r##"{"bg":"#123456"}"##);
    let o = parse_opts(&args(&["--runtime", "/r", "--session", "s", "--theme", &partial])).unwrap();
    assert_eq!(o.palette.bg, (0x12, 0x34, 0x56));
    assert_eq!(o.palette.fg, fleetmux_term::Palette::CMUX.fg);
    let upper = write("upper.json", r##"{"BG":"#ABCDEF"}"##);
    assert!(parse_opts(&args(&["--runtime", "/r", "--session", "s", "--theme", &upper])).is_err());
    let upper = write("upper2.json", r##"{"bg":"#ABCDEF"}"##);
    let o = parse_opts(&args(&["--runtime", "/r", "--session", "s", "--theme", &upper])).unwrap();
    assert_eq!(o.palette.bg, (0xab, 0xcd, 0xef));
    let all = write(
        "all.json",
        r##"{"bg":"#123456","fg":"#abcdef","cursor":"#000001","ansi":["#000000","#000001","#000002","#000003",
            "#000004","#000005","#000006","#000007","#000008","#000009","#00000a","#00000b","#00000c","#00000d",
            "#00000e","#00000f"]}"##,
    );
    let o = parse_opts(&args(&["--runtime", "/r", "--session", "s", "--theme", &all])).unwrap();
    assert_eq!(o.palette.bg, (0x12, 0x34, 0x56));
    assert_eq!(o.palette.fg, (0xab, 0xcd, 0xef));
    assert_eq!(o.palette.cursor, (0, 0, 1));
    assert_eq!(o.palette.ansi[15], (0, 0, 0x0f));

    let srv2 = Server::start(&["--theme", &partial]);
    let h = ok(&srv2.sock(), "hello", json!({}));
    assert_eq!(h["theme"]["bg"], json!("#123456"), "{h}");
    assert_eq!(h["theme"]["fg"], json!("#ffffff"), "{h}");
    assert_eq!(h["theme"]["ansi"][1], json!("#cc372e"), "{h}");

    // ③ Bad files and values are usage errors, like `--scrollback many`.
    let bad = [
        write("notobj.json", r##"["#000000"]"##),
        write("null.json", "null"),
        write("notjson.json", "nope"),
        write("blue.json", r##"{"bg":"blue"}"##),
        write("nullbg.json", r##"{"bg":null}"##),
        write("num.json", r##"{"bg":1}"##),
        write("unknown.json", r##"{"foo":1}"##),
        write("short.json", r##"{"ansi":["#000000"]}"##),
        write("shorthex.json", r##"{"ansi":["#000"]}"##),
        write("ansiobj.json", r##"{"ansi":{"0":"#000000"}}"##),
        tmp.0.join("missing.json").to_str().unwrap().to_string(),
    ];
    for path in &bad {
        let e = parse_opts(&args(&["--runtime", "/r", "--session", "s", "--theme", path]))
            .unwrap_err();
        assert!(e.contains("usage: fleetmux server"), "{path}: {e}");
        assert!(e.starts_with("--theme"), "{path}: {e}");
    }
    // The real binary refuses it too, exit 2, and never binds the socket.
    let out = std::fs::File::create(tmp.0.join("bad.out")).unwrap();
    let mut child = Command::new(BIN)
        .args(["server", "--runtime", tmp.0.join("r2").to_str().unwrap()])
        .args(["--session", "cli", "--theme", &bad[3]])
        .stdin(Stdio::null())
        .stdout(Stdio::from(out.try_clone().unwrap()))
        .stderr(Stdio::from(out))
        .spawn()
        .expect("spawn fleetmux server");
    let status = wait_exit(&mut child, START_TIMEOUT).unwrap_or_else(|| {
        let _ = child.kill();
        let _ = child.wait();
        panic!("server with a bad --theme did not exit within {START_TIMEOUT:?}")
    });
    assert_eq!(status.code(), Some(2), "{}", read_or_empty(&tmp.0.join("bad.out")));
}

/// Shuts down a daemonized server this test started: `server.shutdown` on its socket, else SIGTERM to the pid in
/// its own `server.json` (never anything else).
struct DaemonGuard {
    runtime: PathBuf,
}

impl Drop for DaemonGuard {
    fn drop(&mut self) {
        let sock = self.runtime.join("ctl.sock");
        if request(&sock, "server.shutdown", json!({"force": true})).is_ok() {
            return;
        }
        let pid = std::fs::read_to_string(self.runtime.join("server.json"))
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .and_then(|v| v["pid"].as_i64());
        if let Some(pid) = pid.filter(|p| *p > 1) {
            unsafe { libc::kill(pid as i32, libc::SIGTERM) };
        }
    }
}

/// fix-m0-2: the starter owns `start.lock` (design.md §2.3, PROTOCOL.md §8.4) and holds it while it runs
/// `fleetmux server --daemonize` and waits for `hello`. The server must not wait for that lock itself: the
/// daemonize parent exits 0 and `hello` answers while the starter still holds it.
#[test]
fn daemonize_starts_while_starter_holds_start_lock() {
    use std::os::unix::io::AsRawFd;

    let tmp = TempDir::new();
    let runtime = tmp.0.join("rt");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&runtime)
        .expect("create runtime dir");
    let lock = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(runtime.join("start.lock"))
        .expect("open start.lock");
    assert_eq!(
        unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) },
        0,
        "flock start.lock"
    );
    let _guard = DaemonGuard {
        runtime: runtime.clone(),
    };
    let out = std::fs::File::create(tmp.0.join("server.out")).unwrap();
    let mut parent = Command::new(BIN)
        .arg("server")
        .arg("--runtime")
        .arg(&runtime)
        .args(["--session", "cli", "--daemonize"])
        .stdin(Stdio::null())
        .stdout(Stdio::from(out.try_clone().unwrap()))
        .stderr(Stdio::from(out))
        .spawn()
        .expect("spawn fleetmux server --daemonize");
    let status = wait_exit(&mut parent, Duration::from_secs(5));
    if status.is_none() {
        let _ = parent.kill();
        let _ = parent.wait();
    }
    let status = status.unwrap_or_else(|| {
        panic!(
            "daemonize parent still running after 5 s while the starter holds start.lock\nserver.out:\n{}\nserver.log:\n{}",
            read_or_empty(&tmp.0.join("server.out")),
            read_or_empty(&runtime.join("server.log"))
        )
    });
    assert!(
        status.success(),
        "daemonize parent: {status}\n{}",
        read_or_empty(&runtime.join("server.log"))
    );
    // Still holding the lock: the daemon answers.
    let end = Instant::now() + START_TIMEOUT;
    let hello = loop {
        if let Ok(r) = request(&runtime.join("ctl.sock"), "hello", json!({})) {
            break r;
        }
        assert!(
            Instant::now() < end,
            "no hello while the starter holds start.lock\n{}",
            read_or_empty(&runtime.join("server.log"))
        );
        std::thread::sleep(Duration::from_millis(50));
    };
    assert_eq!(hello["ok"], json!(true), "{hello}");
    let server_json: Value =
        serde_json::from_str(&read_or_empty(&runtime.join("server.json"))).expect("server.json");
    assert_eq!(server_json["pid"], hello["pid"], "{server_json} vs {hello}");
    drop(lock);
}

/// fix-m0-6: `--daemonize` keeps no descriptor its starter happened to pass in. The starter hands the server an
/// unrelated listening socket and an accepted connection (inheritable copies at fixed numbers) and then closes
/// its own copies: the unrelated listener must stop answering and the accepted connection's peer must read EOF,
/// while the daemon's own control listener answers `hello` and `server.log` still gets the startup line.
#[test]
fn daemonize_closes_unrelated_inherited_sockets_keeps_ctl_listener() {
    use std::io::Read;
    use std::os::unix::io::AsRawFd;
    use std::os::unix::net::UnixListener;
    use std::os::unix::process::CommandExt;

    const LISTENER_AT: i32 = 50;
    const ACCEPTED_AT: i32 = 51;
    let tmp = TempDir::new();
    let runtime = tmp.0.join("rt");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&runtime)
        .expect("create runtime dir");
    let unrelated_path = tmp.0.join("unrelated.sock");
    let unrelated = UnixListener::bind(&unrelated_path).expect("bind unrelated listener");
    let x = UnixListener::bind(tmp.0.join("x.sock")).expect("bind x");
    let mut peer = UnixStream::connect(tmp.0.join("x.sock")).expect("connect x");
    // Set now: macOS refuses SO_RCVTIMEO (EINVAL) on a socket whose peer has already gone.
    peer.set_read_timeout(Some(Duration::from_secs(2)))
        .expect("peer read timeout");
    let (accepted, _) = x.accept().expect("accept");
    drop(x);
    let _guard = DaemonGuard {
        runtime: runtime.clone(),
    };
    let (listener_fd, accepted_fd) = (unrelated.as_raw_fd(), accepted.as_raw_fd());
    let out = std::fs::File::create(tmp.0.join("server.out")).unwrap();
    let mut cmd = Command::new(BIN);
    cmd.arg("server")
        .arg("--runtime")
        .arg(&runtime)
        .args(["--session", "cli", "--daemonize"])
        .stdin(Stdio::null())
        .stdout(Stdio::from(out.try_clone().unwrap()))
        .stderr(Stdio::from(out));
    // SAFETY: only dup2 (async-signal-safe) between fork and exec; dup2 leaves the copies without FD_CLOEXEC,
    // i.e. inherited by the server like any descriptor a careless starter forgot to close.
    unsafe {
        cmd.pre_exec(move || {
            if libc::dup2(listener_fd, LISTENER_AT) == -1
                || libc::dup2(accepted_fd, ACCEPTED_AT) == -1
            {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut parent = cmd.spawn().expect("spawn fleetmux server --daemonize");
    let status = wait_exit(&mut parent, Duration::from_secs(5));
    if status.is_none() {
        let _ = parent.kill();
        let _ = parent.wait();
    }
    let status = status.expect("daemonize parent still running after 5 s");
    assert!(
        status.success(),
        "daemonize parent: {status}\n{}",
        read_or_empty(&runtime.join("server.log"))
    );
    drop(unrelated);
    drop(accepted); // the test's own copies: from here only the daemon could still hold either

    let end = Instant::now() + START_TIMEOUT;
    let hello = loop {
        if let Ok(r) = request(&runtime.join("ctl.sock"), "hello", json!({})) {
            break r;
        }
        assert!(
            Instant::now() < end,
            "no hello from the daemon\n{}",
            read_or_empty(&runtime.join("server.log"))
        );
        std::thread::sleep(Duration::from_millis(50));
    };
    assert_eq!(hello["ok"], json!(true), "{hello}");
    let server_json: Value =
        serde_json::from_str(&read_or_empty(&runtime.join("server.json"))).expect("server.json");
    assert_eq!(server_json["pid"], hello["pid"], "{server_json} vs {hello}");
    assert!(
        read_or_empty(&runtime.join("server.log")).contains("listening on"),
        "server.log lost the startup line:\n{}",
        read_or_empty(&runtime.join("server.log"))
    );

    // The unrelated listener: nobody may hold it any more, so a connect is refused.
    let end = Instant::now() + Duration::from_secs(2);
    loop {
        match UnixStream::connect(&unrelated_path) {
            Err(e) => {
                assert_eq!(e.kind(), std::io::ErrorKind::ConnectionRefused, "{e}");
                break;
            }
            Ok(_) => assert!(
                Instant::now() < end,
                "the unrelated listener still accepts connections 2 s after its starter closed it: the daemon \
                 (pid {}) kept the inherited descriptor",
                hello["pid"]
            ),
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    // The accepted connection: its peer must see EOF (within the 2 s read timeout set above).
    let mut byte = [0u8; 1];
    match peer.read(&mut byte) {
        Ok(0) => {}
        other => panic!(
            "the peer of the inherited accepted connection got {other:?} instead of EOF: the daemon (pid {}) kept \
             the descriptor",
            hello["pid"]
        ),
    }
}
