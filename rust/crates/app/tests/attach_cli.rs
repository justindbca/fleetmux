//! `fleetmux attach` as a program (brief-m03 §3.4 `tests/attach_cli.rs`, PROTOCOL.md §9 / §10). The first two
//! are M0 (attach-render, B7); the third is M1 (views-multi, B16). Attach runs inside a real PTY.
mod common;
use common::*;

const T: Duration = Duration::from_secs(10);

#[test]
fn attach_prints_server_log_tail_when_no_server_exit_3() {
    let tmp = TempDir::new();
    let runtime = tmp.path().join("rt");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&runtime)
        .expect("create runtime dir");
    let log: String = (1..=30)
        .map(|k| format!("2026-10-28T00:00:{k:02}Z INFO LOG-LINE-{k:02}\n"))
        .collect();
    std::fs::write(runtime.join("server.log"), log).unwrap();

    let rt = runtime.to_str().expect("utf-8 runtime path");
    let mut p = PtyProc::spawn(&[BIN, "attach", "--runtime", rt], 120, 40, &[]);
    let code = p.wait_exit(T).unwrap_or_else(|| {
        panic!(
            "attach kept running with no server; output:\n{}",
            p.output()
        )
    });
    assert_eq!(code, 3, "attach with no server must exit 3; output:\n{}", p.output());
    let out = p.wait_output("LOG-LINE-30", Duration::from_secs(2));
    assert!(
        out.contains("LOG-LINE-11") && !out.contains("LOG-LINE-10"),
        "want exactly the last 20 lines of server.log:\n{out}"
    );
}

#[test]
fn attach_prefix_d_detaches_and_agent_keeps_running() {
    let srv = Server::start();
    let id = sh(&srv, "fx-worker-1", "worker", "echo AGENT-UP; exec sleep 1000");
    srv.wait_text(&id, "AGENT-UP", T);
    let pid = srv.pane(&id)["pid"].as_i64().expect("pane pid") as i32;

    let rt = srv.runtime.to_str().expect("utf-8 runtime path");
    let mut p = PtyProc::spawn(&[BIN, "attach", "--runtime", rt], 200, 50, &[]);
    p.wait_screen("AGENT-UP", 200, 50, T);
    p.write(&prefixed(b"d"));
    let code = p.wait_exit(Duration::from_secs(5)).unwrap_or_else(|| {
        panic!("attach did not exit after C-b d; screen:\n{}", p.screen(200, 50))
    });
    assert_eq!(code, 0, "C-b d must detach with exit 0");
    std::thread::sleep(Duration::from_millis(300));
    assert!(srv.listed(&id), "the pane went away with the viewer");
    assert!(pid_alive(pid), "the agent process died with the viewer");
}

#[test]
fn attach_killed_by_sigterm_restores_the_terminal() {
    let srv = Server::start();
    let id = sh(&srv, "fx-worker-1", "worker", "echo AGENT-UP; exec sleep 1000");
    srv.wait_text(&id, "AGENT-UP", T);

    let rt = srv.runtime.to_str().expect("utf-8 runtime path");
    let mut p = PtyProc::spawn(&[BIN, "attach", "--runtime", rt], 200, 50, &[]);
    p.wait_screen("AGENT-UP", 200, 50, T);
    // SAFETY: kill on the pid of a child this test owns.
    assert_eq!(unsafe { libc::kill(p.pid(), libc::SIGTERM) }, 0);
    p.wait_exit(Duration::from_secs(5))
        .unwrap_or_else(|| panic!("attach did not exit on SIGTERM"));
    // leaving the alternate screen is the last thing the terminal guard writes
    p.wait_output("\x1b[?1049l", Duration::from_secs(2));
    assert!(srv.listed(&id), "the pane went away with the viewer");
}

#[test]
#[ignore = "M1: views-multi"]
fn attach_prefix_o_receives_open_frame_with_history_url() {
    let srv = Server::start();
    sh(&srv, "fx-worker-1", "worker", "exec sleep 1000");
    srv.ok(
        "kv.set",
        json!({"key": "history_url", "value": "file:///x/history.html"}),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_quiet(Duration::from_millis(200), T);
    v.input(&prefixed(b"o"));
    assert_eq!(v.wait_open(Duration::from_secs(5)), "/x/history.html");
}

// ---- Coverage additions (fix-a-coverage-view, reviewer-1 coverage-a.md G3). ----

/// `open` / `xdg-open` stand-ins in `dir`, put first on `PATH` (no real opener or browser runs): each appends
/// its arguments as one line to `log` and exits `code`. Returns the `PATH` value to use.
fn fake_openers(dir: &Path, log: &Path, code: i32) -> String {
    std::fs::create_dir_all(dir).expect("create fake opener dir");
    for name in ["open", "xdg-open"] {
        let path = dir.join(name);
        let script = format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\nexit {code}\n",
            log.display()
        );
        std::fs::write(&path, script).expect("write fake opener");
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    format!("{}:{}", dir.display(), std::env::var("PATH").unwrap_or_default())
}

/// G3, PROTOCOL.md §6.3: on `0x83` the attach client runs `open` (macOS) / `xdg-open` (elsewhere) on the path;
/// when opening fails it shows the path.
#[test]
#[ignore = "M1: views-multi"]
fn attach_open_frame_runs_local_opener_and_shows_path_when_it_fails() {
    let srv = Server::start();
    sh(&srv, "fx-worker-1", "worker", "echo AGENT-UP; exec sleep 1000");
    // An existing file on the client's host, no extension: whatever opens it, it is not a browser.
    let target = srv.tmp.path().join("history-g3");
    std::fs::write(&target, "history\n").unwrap();
    let target = target.to_str().expect("utf-8 path").to_string();
    srv.ok(
        "kv.set",
        json!({"key": "history_url", "value": format!("file://{target}")}),
    );
    let (bin, log) = (srv.tmp.path().join("fake-bin"), srv.tmp.path().join("opener.log"));
    fake_openers(&bin, &log, 0);
    // M2.5 (test-contract, supervisor M104): this test covers the no-window steps of the `0x83` order (PROTOCOL.md
    // §6.3: window → opener → path), so no `fleetmux-web` may be found in PROTOCOL.md §9.4 order: attach runs from
    // a copy of the binary in a directory of its own, PATH holds only the fake openers, and
    // FLEETMUX_WEB_BIN names a file that does not exist.
    let solo = srv.tmp.path().join("solo-bin");
    std::fs::create_dir_all(&solo).unwrap();
    let attach_bin = solo.join("fleetmux");
    std::fs::copy(BIN, &attach_bin).expect("copy the fleetmux binary");
    let attach_bin = attach_bin.to_str().expect("utf-8 path").to_string();
    let path = bin.to_str().expect("utf-8 path").to_string();
    let no_web = solo.join("no-fleetmux-web");
    let no_web = no_web.to_str().expect("utf-8 path");

    let rt = srv.runtime.to_str().expect("utf-8 runtime path");
    // task9 (operator exception): DISPLAY=:0 keeps the opener step on headless Linux; no display: clipboard tests.
    let mut p = PtyProc::spawn(
        &[&attach_bin, "attach", "--runtime", rt],
        200,
        50,
        &[("PATH", &path), ("FLEETMUX_WEB_BIN", no_web), ("DISPLAY", ":0")],
    );
    p.wait_screen("AGENT-UP", 200, 50, T);
    p.write(&prefixed(b"o"));
    wait_file_eq(&log, format!("{target}\n").as_bytes(), T);

    // The opener now fails: the client shows the path instead.
    let missing = "/fleetmux-g3-not-on-this-host/history.html";
    srv.ok(
        "kv.set",
        json!({"key": "history_url", "value": format!("file://{missing}")}),
    );
    fake_openers(&bin, &log, 1);
    p.write(&prefixed(b"o"));
    p.wait_output(missing, T);
}

/// G3, PROTOCOL.md §6.3 / §10: with kv `history_url` unset the `0x83` payload is empty; the client shows
/// `no history url` and runs no opener.
#[test]
#[ignore = "M1: views-multi"]
fn attach_open_frame_without_history_url_shows_no_history_url() {
    let srv = Server::start();
    sh(&srv, "fx-worker-1", "worker", "echo AGENT-UP; exec sleep 1000");
    let (bin, log) = (srv.tmp.path().join("fake-bin"), srv.tmp.path().join("opener.log"));
    let path = fake_openers(&bin, &log, 0);

    let rt = srv.runtime.to_str().expect("utf-8 runtime path");
    let mut p = PtyProc::spawn(&[BIN, "attach", "--runtime", rt], 200, 50, &[("PATH", &path)]);
    p.wait_screen("AGENT-UP", 200, 50, T);
    p.write(&prefixed(b"o"));
    p.wait_output("no history url", T);
    std::thread::sleep(Duration::from_millis(300));
    assert!(!log.exists(), "an opener ran for an empty path: {}", tail(&log, 5));
}
