//! Test fixture of the M4 end-to-end test (brief-m4 §5 item 1; tests/m4e/fake_cli.py), not a product binary.
//!
//! The fleet decides whether a pane still runs its CLI from `ps -o comm=` on the pane's tty: the executable
//! must be named `claude` / `codex` (a `#!` script shows `/bin/sh`, a Python one `Python`). The test copies
//! this example, not a symlink, to `<test root>/bin/claude` and `<test root>/bin/codex`; it runs
//! `<python> -I <fake_cli.py> --as <own file name> --log-dir <dir> <argv...>` from the three lines of
//! `<own dir>/fake_cli.conf` (python, script, log dir) on the same tty and in the same process group,
//! forwards SIGTERM / SIGINT / SIGHUP to it and exits with its code (128 + signal when it was killed).
//! std and the crate's existing `libc` only; built with `cargo build -p ft-probe --example fake_cli_shim`.

use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::process::{Command, ExitCode};
use std::sync::atomic::{AtomicI32, Ordering};

static CHILD: AtomicI32 = AtomicI32::new(0);

extern "C" fn forward(sig: libc::c_int) {
    let pid = CHILD.load(Ordering::SeqCst);
    if pid > 0 {
        // SAFETY: kill(2) is async-signal-safe; the pid is our own child.
        unsafe {
            libc::kill(pid, sig);
        }
    }
}

fn fail(why: &str) -> ExitCode {
    eprintln!("fake_cli_shim: {why}");
    ExitCode::from(70)
}

fn main() -> ExitCode {
    let exe = match std::env::current_exe() {
        Ok(path) => path,
        Err(err) => return fail(&format!("current_exe: {err}")),
    };
    let name = exe
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let dir = exe.parent().unwrap_or_else(|| Path::new("."));
    let conf = dir.join("fake_cli.conf");
    let text = match std::fs::read_to_string(&conf) {
        Ok(text) => text,
        Err(err) => return fail(&format!("{}: {err}", conf.display())),
    };
    let lines: Vec<&str> = text.lines().collect();
    if lines.len() < 3 || lines.iter().take(3).any(|l| l.is_empty()) {
        return fail(&format!("{}: want 3 lines (python, script, log dir)", conf.display()));
    }
    let mut child = match Command::new(lines[0])
        .arg("-I")
        .arg(lines[1])
        .arg("--as")
        .arg(&name)
        .arg("--log-dir")
        .arg(lines[2])
        .args(std::env::args_os().skip(1))
        .spawn()
    {
        Ok(child) => child,
        Err(err) => return fail(&format!("spawn {}: {err}", lines[0])),
    };
    CHILD.store(child.id() as i32, Ordering::SeqCst);
    for sig in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
        // SAFETY: installs a handler that only calls kill(2).
        unsafe {
            libc::signal(sig, forward as extern "C" fn(libc::c_int) as libc::sighandler_t);
        }
    }
    match child.wait() {
        Ok(status) => match (status.code(), status.signal()) {
            (Some(code), _) => ExitCode::from((code & 0xff) as u8),
            (None, Some(sig)) => ExitCode::from((128 + (sig & 0x7f)) as u8),
            (None, None) => ExitCode::from(1),
        },
        Err(err) => fail(&format!("wait: {err}")),
    }
}
