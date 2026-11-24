//! The server raises its open-file soft limit to the hard limit at start (RLIMIT_NOFILE).
//!
//! Promise: a server started under a low soft limit (macOS launchd default: 256) still opens every pane it is
//! asked for. Each pane holds a few descriptors in the server (PTY master and its copies), so at 256 the
//! server used to fail near 80 panes with `pane.spawn ... dup of fd N failed`.
//!
//! Real path: the real `fleetmux` binary, started with a soft limit of 256, opens 100 real panes; a pane child
//! also reports the soft limit it inherited, which must be above 256.
mod common;
use common::*;
use serde_json::json;
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

const LOW: libc::rlim_t = 256;
const PANES: usize = 100;

/// Like `Server::launch`, with the child's soft RLIMIT_NOFILE set to `LOW` before `exec`.
fn launch_low_limit() -> Server {
    let tmp = TempDir::new();
    let runtime = tmp.path().join("rt");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&runtime)
        .expect("create runtime dir");
    let out = std::fs::File::create(tmp.path().join("server.out")).expect("server output file");
    let mut cmd = Command::new(BIN);
    cmd.arg("server")
        .arg("--runtime")
        .arg(&runtime)
        .args(["--session", SESSION])
        .env("FLEETMUX_UI_LANGUAGE", "zh-Hans")
        .env("FLEETMUX_NATIVE_HEADER", "0")
        .stdin(Stdio::null())
        .stdout(Stdio::from(out.try_clone().expect("dup output file")))
        .stderr(Stdio::from(out));
    unsafe {
        cmd.pre_exec(|| {
            let mut lim = libc::rlimit {
                rlim_cur: 0,
                rlim_max: 0,
            };
            if libc::getrlimit(libc::RLIMIT_NOFILE, &mut lim) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            lim.rlim_cur = LOW.min(lim.rlim_max);
            if libc::setrlimit(libc::RLIMIT_NOFILE, &lim) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let child = cmd.spawn().expect("spawn fleetmux server");
    let mut srv = Server {
        child,
        tmp,
        runtime,
        pgids: Mutex::new(Vec::new()),
    };
    srv.wait_ready();
    srv
}

fn hard_limit() -> libc::rlim_t {
    let mut lim = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    assert_eq!(unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut lim) }, 0);
    lim.rlim_max
}

#[test]
fn server_under_a_256_soft_limit_opens_100_panes() {
    assert!(
        hard_limit() > LOW,
        "hard RLIMIT_NOFILE {} is not above {LOW}",
        hard_limit()
    );
    let srv = launch_low_limit();
    let mut failed = Vec::new();
    for i in 0..PANES {
        let r = srv.client().request(
            "pane.spawn",
            json!({"argv": ["sleep", "60"], "cwd": srv.tmp.path().to_str().unwrap(), "env": {},
                   "title": format!("p{i}"), "type": "worker"}),
        );
        if let Some(g) = r["pgid"].as_i64() {
            srv.pgids.lock().unwrap().push(g as i32);
        }
        if r["ok"] != json!(true) {
            failed.push(format!("pane {i}: {r}"));
            break;
        }
    }
    assert!(
        failed.is_empty(),
        "pane.spawn failed under a soft limit of {LOW}: {failed:?}\n{}",
        srv.diagnostics()
    );
    let probe = srv.spawn_id(
        "probe",
        "worker",
        &["sh", "-c", "echo NOFILE=$(ulimit -n); sleep 60"],
    );
    let text = srv.wait_text(&probe, "NOFILE=", Duration::from_secs(10));
    let value = text
        .split("NOFILE=")
        .nth(1)
        .and_then(|s| s.split_whitespace().next())
        .unwrap_or("")
        .to_string();
    let raised = value == "unlimited" || value.parse::<u64>().map(|n| n > LOW).unwrap_or(false);
    assert!(
        raised,
        "pane child inherited soft limit {value:?}, want above {LOW}\n{}",
        srv.diagnostics()
    );
}
