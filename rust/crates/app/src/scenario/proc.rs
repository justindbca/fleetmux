//! Every command the scenario runs goes through [`run`]: it leads its own process group, the group is registered
//! with the [`Guard`] before anything waits on it, its output goes to a log file, and the wait polls, so a
//! per-command limit, the run's deadline or a signal ends it (TERM to the group, then KILL) instead of hanging.
use std::collections::BTreeMap;
use std::io::Write;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use super::guard::Guard;

/// What a finished command left.
#[derive(Debug, Clone)]
pub struct Done {
    pub code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
}

impl Done {
    pub fn ok(&self) -> bool {
        self.code == Some(0)
    }
    /// The last lines of stdout + stderr, for a failure reason.
    pub fn tail(&self, n: usize) -> String {
        let all = format!("{}{}", self.stdout, self.stderr);
        let lines: Vec<&str> = all.lines().filter(|l| !l.trim().is_empty()).collect();
        lines[lines.len().saturating_sub(n)..].join(" | ")
    }
}

/// Run `argv` with exactly `env`, `stdin` fed in, at most `limit`, stopping early when `stop` says so. Output is
/// also appended to `log` with the command line in front.
#[allow(clippy::too_many_arguments)]
pub fn run(
    guard: &Guard,
    argv: &[String],
    env: &BTreeMap<String, String>,
    cwd: &Path,
    stdin: Option<&str>,
    limit: Duration,
    log: &Path,
    stop: &dyn Fn() -> Option<String>,
) -> Result<Done, String> {
    let (prog, rest) = argv.split_first().ok_or("empty command")?;
    let out_path = log.with_extension(format!("{}.out", std::process::id()));
    let err_path = log.with_extension(format!("{}.err", std::process::id()));
    let out = std::fs::File::create(&out_path).map_err(|e| format!("{}: {e}", out_path.display()))?;
    let err = std::fs::File::create(&err_path).map_err(|e| format!("{}: {e}", err_path.display()))?;
    let mut cmd = Command::new(prog);
    cmd.args(rest)
        .env_clear()
        .envs(env)
        .current_dir(cwd)
        .stdin(if stdin.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(out)
        .stderr(err)
        .process_group(0);
    let mut child = cmd.spawn().map_err(|e| format!("cannot run {prog}: {e}"))?;
    let pid = child.id() as i32;
    guard.own_group(pid);
    if let (Some(text), Some(mut w)) = (stdin, child.stdin.take()) {
        let _ = w.write_all(text.as_bytes());
    }
    let started = Instant::now();
    let why = loop {
        match child.try_wait() {
            Ok(Some(_)) => break None,
            Ok(None) => {}
            Err(e) => break Some(format!("wait: {e}")),
        }
        if let Some(why) = stop() {
            break Some(why);
        }
        if started.elapsed() >= limit {
            break Some(format!("{prog} did not finish within {} s", limit.as_secs()));
        }
        std::thread::sleep(Duration::from_millis(100));
    };
    if why.is_some() {
        unsafe { libc::killpg(pid, libc::SIGTERM) };
        let t = Instant::now() + Duration::from_secs(5);
        while Instant::now() < t && matches!(child.try_wait(), Ok(None)) {
            std::thread::sleep(Duration::from_millis(100));
        }
        unsafe { libc::killpg(pid, libc::SIGKILL) };
    }
    let status = child.wait().ok();
    let stdout = std::fs::read_to_string(&out_path).unwrap_or_default();
    let stderr = std::fs::read_to_string(&err_path).unwrap_or_default();
    let _ = std::fs::remove_file(&out_path);
    let _ = std::fs::remove_file(&err_path);
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(log) {
        let _ = writeln!(
            f,
            "$ {}\n[exit {:?} after {:.1} s]\n{stdout}{stderr}",
            argv.join(" "),
            status.and_then(|s| s.code()),
            started.elapsed().as_secs_f64()
        );
    }
    if let Some(why) = why {
        return Err(why);
    }
    Ok(Done {
        code: status.and_then(|s| s.code()),
        stdout,
        stderr,
    })
}
