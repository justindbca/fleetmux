//! `fleetmux reap --runtime D` (PROTOCOL.md §8.2, §9) and the logind `KillUserProcesses` warning (§8.7).
//!
//! Stage A (brief-m03 §3.3) wrote the signatures; package B17 `robust` implements them.
//!
//! `reap` runs after the server is gone (SIGKILL, crash), so it trusts nothing but the OS: a recorded group is
//! signalled only while its leader `pid` is alive with exactly the recorded start time (§8.6), and every later
//! escalation step re-checks that the group is still that one (a reused pgid is never signalled).

use std::collections::BTreeSet;
use std::io;
use std::path::Path;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::Value;

use super::pane::{KILL_REAP_GRACE_MS, KILL_SCHEDULE_MS};
use super::pty;

/// The §8.7 sentence; the caller adds the `<timestamp> WARN` prefix of a `server.log` line (§8.5).
const LOGIND_WARNING: &str = "logind KillUserProcesses=yes: run `loginctl enable-linger` or the server dies with your ssh session";

/// How often the escalation polls for the groups being gone.
const POLL: Duration = Duration::from_millis(20);

/// The one stdout JSON line of `fleetmux reap`: `{"reaped":[pgid…],"skipped":[{"pane","pid","reason"}]}`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Report {
    /// Process groups that were signalled and are gone.
    pub reaped: Vec<u32>,
    pub skipped: Vec<Skipped>,
}

/// A `panes.json` record that was not signalled.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Skipped {
    pub pane: String,
    pub pid: u32,
    /// `"start_time_mismatch"` (pid reused), `"unverifiable"` (start time unreadable) or `"gone"` (pid not alive).
    pub reason: &'static str,
}

/// Why `reap` could not run at all → exit 3. Added in stage A: §3.3 names the type but does not define it.
#[derive(Debug)]
pub enum ReapError {
    /// The runtime directory, or a `panes.json` that exists, could not be read.
    Unreadable(io::Error),
    /// `panes.json` exists but is not the PROTOCOL.md §8.2 shape.
    BadPanesJson(String),
}

/// Read `<runtime>/panes.json`; for every record whose `pid` is alive and whose start time read now equals the
/// recorded `start_time` exactly, kill its process group (SIGHUP → SIGTERM → SIGKILL) and confirm it is gone.
/// A missing `panes.json` is "no panes" (PROTOCOL.md §8), an empty report.
pub fn reap(runtime: &Path) -> Result<Report, ReapError> {
    reap_checked(runtime).map(|(report, _)| report)
}

/// [`reap`] plus whether a signal failed or a signalled group outlived SIGKILL and its grace (→ exit 1).
fn reap_checked(runtime: &Path) -> Result<(Report, bool), ReapError> {
    let records = read_ledger(runtime)?;
    let mut report = Report {
        reaped: Vec::new(),
        skipped: Vec::new(),
    };
    let mut targets: Vec<Target> = Vec::new();
    let mut seen = BTreeSet::new();
    for r in records {
        match verify(&r) {
            Ok(()) => {
                if seen.insert(r.pgid) {
                    targets.push(Target {
                        pid: r.pid,
                        pgid: r.pgid,
                        start_time: r.start_time.unwrap_or_default(),
                        failed: false,
                    });
                }
            }
            Err(reason) => report.skipped.push(Skipped {
                pane: r.pane,
                pid: r.pid,
                reason,
            }),
        }
    }
    let failed = escalate(&mut targets);
    report.reaped = targets.iter().map(|t| t.pgid).collect();
    Ok((report, failed))
}

/// One `panes.json` record, as far as `reap` needs it.
struct Record {
    pane: String,
    pid: u32,
    pgid: u32,
    /// `None` when absent or `null`: unverifiable.
    start_time: Option<u64>,
}

/// A verified group being escalated.
struct Target {
    pid: u32,
    pgid: u32,
    start_time: u64,
    failed: bool,
}

fn read_ledger(runtime: &Path) -> Result<Vec<Record>, ReapError> {
    let meta = std::fs::metadata(runtime).map_err(ReapError::Unreadable)?;
    if !meta.is_dir() {
        return Err(ReapError::Unreadable(io::Error::other(format!(
            "{} is not a directory",
            runtime.display()
        ))));
    }
    let text = match std::fs::read_to_string(runtime.join("panes.json")) {
        Ok(t) => t,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(ReapError::Unreadable(e)),
    };
    let doc: Value =
        serde_json::from_str(&text).map_err(|e| ReapError::BadPanesJson(e.to_string()))?;
    let panes = doc
        .get("panes")
        .and_then(Value::as_array)
        .ok_or_else(|| ReapError::BadPanesJson("no \"panes\" array".into()))?;
    panes.iter().map(parse_record).collect()
}

fn parse_record(v: &Value) -> Result<Record, ReapError> {
    let bad = |what: &str| ReapError::BadPanesJson(format!("{what} in record {v}"));
    let id = |key: &str| -> Result<u32, ReapError> {
        v.get(key)
            .and_then(Value::as_u64)
            .filter(|n| (2..=i32::MAX as u64).contains(n))
            .map(|n| n as u32)
            .ok_or_else(|| bad(&format!("missing or invalid \"{key}\"")))
    };
    let pane = v
        .get("pane")
        .and_then(Value::as_str)
        .ok_or_else(|| bad("missing \"pane\""))?
        .to_string();
    let start_time = match v.get("start_time") {
        None | Some(Value::Null) => None,
        Some(t) => Some(
            t.as_u64()
                .ok_or_else(|| bad("non-integer \"start_time\""))?,
        ),
    };
    Ok(Record {
        pane,
        pid: id("pid")?,
        pgid: id("pgid")?,
        start_time,
    })
}

/// §8.2 / §8.6 identity check of one record: `Ok` = signal its group, `Err` = the skip reason.
fn verify(r: &Record) -> Result<(), &'static str> {
    match pid_state(r.pid) {
        PidState::Gone => return Err("gone"),
        // Alive but owned by another uid: never one of our panes any more.
        PidState::Foreign => return Err("start_time_mismatch"),
        PidState::Ours => {}
    }
    let Some(recorded) = r.start_time else {
        return Err("unverifiable");
    };
    match pty::start_time(r.pid) {
        None => Err("unverifiable"),
        Some(now) if now != recorded => Err("start_time_mismatch"),
        // The leader must still lead the recorded group, or the group's identity is not the recorded one.
        Some(_) if unsafe { libc::getpgid(r.pid as i32) } != r.pgid as i32 => Err("unverifiable"),
        Some(_) => Ok(()),
    }
}

enum PidState {
    Gone,
    Ours,
    Foreign,
}

fn pid_state(pid: u32) -> PidState {
    if unsafe { libc::kill(pid as i32, 0) } == 0 {
        return PidState::Ours;
    }
    match io::Error::last_os_error().raw_os_error() {
        Some(libc::EPERM) => PidState::Foreign,
        _ => PidState::Gone,
    }
}

/// Still the group we verified: alive, and either its leader is the verified process or the leader is gone and
/// no new process holds its pid (a pgid is not handed out again while the group exists).
fn still_ours(t: &Target) -> bool {
    if !pty::group_alive(t.pgid) {
        return false;
    }
    match pid_state(t.pid) {
        PidState::Gone => true,
        PidState::Foreign => false,
        // An unreadable start time while `kill(pid, 0)` succeeds is a zombie leader on macOS (`proc_pidinfo` fails
        // for zombies; Linux still reads it): its pid — and so the pgid — cannot be reused until it is reaped, so
        // the group is still the verified one and its other members must still be escalated (fix-m0-6).
        PidState::Ours => pty::start_time(t.pid).is_none_or(|now| now == t.start_time),
    }
}

/// SIGHUP → SIGTERM → SIGKILL at [`KILL_SCHEDULE_MS`], all groups in parallel, until every group is gone or
/// [`KILL_REAP_GRACE_MS`] after SIGKILL. Returns whether anything failed.
fn escalate(targets: &mut [Target]) -> bool {
    const SIGNALS: [i32; 3] = [libc::SIGHUP, libc::SIGTERM, libc::SIGKILL];
    let start = Instant::now();
    let last = KILL_SCHEDULE_MS[KILL_SCHEDULE_MS.len() - 1];
    let deadline = start + Duration::from_millis(u64::from(last + KILL_REAP_GRACE_MS));
    let mut next = 0;
    let mut live: Vec<usize> = (0..targets.len()).collect();
    loop {
        let elapsed = start.elapsed();
        while next < SIGNALS.len()
            && elapsed >= Duration::from_millis(u64::from(KILL_SCHEDULE_MS[next]))
        {
            for &i in &live {
                let t = &mut targets[i];
                if !still_ours(t) {
                    continue;
                }
                if let Err(e) = pty::signal_group(t.pgid, SIGNALS[next]) {
                    // ESRCH: gone meanwhile. EPERM: macOS for a group of zombies only. The gone-check decides.
                    if !matches!(e.raw_os_error(), Some(libc::ESRCH) | Some(libc::EPERM)) {
                        eprintln!(
                            "fleetmux reap: signal {} to group {}: {e}",
                            SIGNALS[next], t.pgid
                        );
                        t.failed = true;
                    }
                }
            }
            next += 1;
        }
        live.retain(|&i| still_ours(&targets[i]));
        if live.is_empty() {
            break;
        }
        let now = Instant::now();
        if now >= deadline {
            for &i in &live {
                eprintln!(
                    "fleetmux reap: process group {} still alive after SIGKILL",
                    targets[i].pgid
                );
                targets[i].failed = true;
            }
            break;
        }
        std::thread::sleep(POLL.min(deadline - now));
    }
    targets.iter().any(|t| t.failed)
}

/// `fleetmux reap --runtime D`: prints the [`Report`] as one JSON line. Exit 0 = no `unverifiable`; 1 = at least
/// one `unverifiable` or a failed signal; 2 = usage; 3 = [`ReapError`] (PROTOCOL.md §9).
pub fn run(args: &[String]) -> ExitCode {
    const USAGE: &str = "usage: fleetmux reap --runtime D";
    let runtime = match args {
        [flag, dir] if flag == "--runtime" && !dir.is_empty() => Path::new(dir),
        [one] if one.starts_with("--runtime=") && one.len() > "--runtime=".len() => {
            Path::new(&one["--runtime=".len()..])
        }
        _ => {
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        }
    };
    let (report, failed) = match reap_checked(runtime) {
        Ok(r) => r,
        Err(e) => {
            match e {
                ReapError::Unreadable(e) => eprintln!("fleetmux reap: {}: {e}", runtime.display()),
                ReapError::BadPanesJson(m) => eprintln!(
                    "fleetmux reap: {}: bad panes.json: {m}",
                    runtime.join("panes.json").display()
                ),
            }
            return ExitCode::from(3);
        }
    };
    match serde_json::to_string(&report) {
        Ok(line) => println!("{line}"),
        Err(e) => {
            eprintln!("fleetmux reap: {e}");
            return ExitCode::from(1);
        }
    }
    let unverifiable = report.skipped.iter().any(|s| s.reason == "unverifiable");
    if unverifiable || failed {
        ExitCode::from(1)
    } else {
        ExitCode::SUCCESS
    }
}

/// The `WARN` line to log (and print to stderr) when `conf` has an uncommented `KillUserProcesses=yes`
/// (PROTOCOL.md §8.7); `None` otherwise, including when `conf` cannot be read. `boot::run` calls it on Linux
/// with `$FLEETMUX_LOGIND_CONF` or `/etc/systemd/logind.conf`.
///
/// Returns the sentence without the `<timestamp> WARN` prefix: the caller logs it with level `WARN`.
pub fn logind_warning(conf: &Path) -> Option<String> {
    let text = std::fs::read_to_string(conf).ok()?;
    text.lines()
        .map(str::trim)
        .filter(|l| !l.starts_with('#') && !l.starts_with(';'))
        .filter_map(|l| l.split_once('='))
        .any(|(k, v)| k.trim() == "KillUserProcesses" && v.trim() == "yes")
        .then(|| LOGIND_WARNING.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::process::CommandExt;
    use std::path::PathBuf;
    use std::process::{Child, Command, Stdio};
    use std::sync::atomic::{AtomicU32, Ordering};
    use std::sync::mpsc;

    struct Tmp(PathBuf);
    impl Tmp {
        fn new() -> Tmp {
            static N: AtomicU32 = AtomicU32::new(0);
            let p = std::env::temp_dir().join(format!(
                "fleetmux-reap-unit-{}-{}",
                std::process::id(),
                N.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir_all(&p).unwrap();
            Tmp(p)
        }
    }
    impl Drop for Tmp {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    /// SIGHUP gets its default action in the child (fix-m0-6): an ignored SIGHUP survives exec, so a test process
    /// started under `nohup` (or any launcher that ignores SIGHUP) would hand its children an ignored SIGHUP.
    fn default_sighup(cmd: &mut Command) -> &mut Command {
        // SAFETY: only sigaction(2), which is async-signal-safe, runs between fork and exec.
        unsafe {
            cmd.pre_exec(|| {
                let mut sa: libc::sigaction = std::mem::zeroed();
                sa.sa_sigaction = libc::SIG_DFL;
                if libc::sigaction(libc::SIGHUP, &sa, std::ptr::null_mut()) == 0 {
                    Ok(())
                } else {
                    Err(std::io::Error::last_os_error())
                }
            })
        }
    }

    /// `sleep 30` leading its own process group, SIGHUP at its default action; a thread waits on it so it never
    /// lingers as a zombie.
    fn own_group_child() -> (u32, mpsc::Receiver<std::process::ExitStatus>) {
        let mut cmd = Command::new("sleep");
        cmd.arg("30").process_group(0).stdin(Stdio::null());
        default_sighup(&mut cmd);
        let mut child: Child = cmd.spawn().unwrap();
        let pid = child.id();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let _ = tx.send(child.wait().unwrap());
        });
        (pid, rx)
    }

    fn ledger(dir: &Path, recs: Value) {
        std::fs::write(
            dir.join("panes.json"),
            serde_json::json!({"epoch": "u1", "panes": recs}).to_string(),
        )
        .unwrap();
    }

    fn rec(pane: &str, pid: u32, start_time: Value) -> Value {
        serde_json::json!({"pane": pane, "pid": pid, "pgid": pid, "start_time": start_time,
            "tty": "/dev/ttys000", "group": "workers", "index": 0, "title": pane, "type": "worker"})
    }

    #[test]
    fn logind_warning_only_for_uncommented_yes() {
        let t = Tmp::new();
        let f = t.0.join("logind.conf");
        let cases = [
            (
                "[Login]\n#KillUserProcesses=no\nKillUserProcesses=yes\n",
                true,
            ),
            ("[Login]\n  KillUserProcesses = yes  \n", true),
            ("[Login]\n#KillUserProcesses=yes\n", false),
            ("[Login]\n;KillUserProcesses=yes\n", false),
            ("[Login]\nKillUserProcesses=no\n", false),
            ("[Login]\nKillUserProcesses=yes-please\n", false),
            ("[Login]\nKillExcludeUsers=yes\n", false),
            ("", false),
        ];
        for (text, warn) in cases {
            std::fs::write(&f, text).unwrap();
            let got = logind_warning(&f);
            assert_eq!(got.is_some(), warn, "{text:?} → {got:?}");
            if let Some(w) = got {
                assert_eq!(w, LOGIND_WARNING);
                assert!(
                    w.starts_with("logind KillUserProcesses=yes: run `loginctl enable-linger`")
                );
            }
        }
        assert_eq!(logind_warning(&t.0.join("missing.conf")), None);
        assert_eq!(
            logind_warning(&t.0),
            None,
            "a directory is unreadable as a file"
        );
    }

    #[test]
    fn missing_ledger_is_empty_and_unreadable_runtime_is_an_error() {
        let t = Tmp::new();
        assert_eq!(
            reap(&t.0).unwrap(),
            Report {
                reaped: vec![],
                skipped: vec![]
            }
        );
        assert!(matches!(
            reap(&t.0.join("nope")),
            Err(ReapError::Unreadable(_))
        ));
        std::fs::write(t.0.join("panes.json"), "{not json").unwrap();
        assert!(matches!(reap(&t.0), Err(ReapError::BadPanesJson(_))));
        std::fs::write(
            t.0.join("panes.json"),
            r#"{"epoch":"u1","panes":[{"pane":"u1-1","pid":1,"pgid":1}]}"#,
        )
        .unwrap();
        assert!(
            matches!(reap(&t.0), Err(ReapError::BadPanesJson(_))),
            "pgid 1 is never a pane group"
        );
    }

    #[test]
    fn mismatched_unverifiable_and_gone_records_are_not_signalled() {
        let t = Tmp::new();
        let (alive, alive_rx) = own_group_child();
        let st = pty::start_time(alive).expect("start time of our own child");
        let (dead, dead_rx) = own_group_child();
        unsafe { libc::kill(dead as i32, libc::SIGKILL) };
        dead_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        ledger(
            &t.0,
            serde_json::json!([
                rec("u1-1", alive, serde_json::json!(st + 1)),
                rec("u1-2", alive, Value::Null),
                rec("u1-3", dead, serde_json::json!(st)),
            ]),
        );
        let (report, failed) = reap_checked(&t.0).unwrap();
        assert!(!failed);
        assert_eq!(report.reaped, Vec::<u32>::new());
        let reasons: Vec<_> = report
            .skipped
            .iter()
            .map(|s| (s.pane.as_str(), s.reason))
            .collect();
        assert_eq!(
            reasons,
            [
                ("u1-1", "start_time_mismatch"),
                ("u1-2", "unverifiable"),
                ("u1-3", "gone")
            ]
        );
        assert!(
            alive_rx.recv_timeout(Duration::from_millis(300)).is_err(),
            "a skipped group must not be signalled"
        );
        unsafe { libc::kill(alive as i32, libc::SIGKILL) };
        alive_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    }

    /// fix-m0-6: a leader that died (SIGHUP) but is not reaped yet is a zombie; a member that ignores SIGHUP lives
    /// on. On macOS the zombie's start time is unreadable (`proc_pidinfo` → ESRCH) although `kill(pid, 0)` still
    /// succeeds; its pid, and so the pgid, cannot be reused until it is reaped, so the group is still the verified
    /// one and must stay in the escalation (else the member outlives `reap`).
    #[test]
    fn zombie_leader_with_a_live_member_is_still_ours() {
        /// Kills the whole group and reaps the leader however the test ends (assertion panics included).
        struct Group(Child);
        impl Drop for Group {
            fn drop(&mut self) {
                unsafe { libc::kill(-(self.0.id() as i32), libc::SIGKILL) };
                let _ = self.0.wait();
            }
        }
        let mut cmd = Command::new("bash");
        cmd.args(["-c", "(trap '' HUP TERM; exec sleep 30) & exec sleep 30"])
            .process_group(0)
            .stdin(Stdio::null());
        default_sighup(&mut cmd);
        let group = Group(cmd.spawn().unwrap());
        let pid = group.0.id();
        let members = |pgid: u32| -> Vec<String> {
            let out = Command::new("ps")
                .args(["-axo", "pid=,pgid=,stat="])
                .output()
                .unwrap();
            String::from_utf8_lossy(&out.stdout)
                .lines()
                .filter(|l| l.split_whitespace().nth(1) == Some(pgid.to_string().as_str()))
                .map(str::to_string)
                .collect()
        };
        // Preconditions, each bounded: both processes exist, then the leader is a zombie and the member is not.
        let wait_for = |what: &str, ok: &dyn Fn() -> bool| {
            let end = Instant::now() + Duration::from_secs(5);
            while !ok() {
                assert!(Instant::now() < end, "{what}: {:?}", members(pid));
                std::thread::yield_now();
            }
        };
        wait_for("leader and member", &|| members(pid).len() == 2);
        let st = pty::start_time(pid).expect("start time of our own live leader");
        unsafe { libc::kill(pid as i32, libc::SIGHUP) };
        wait_for("zombie leader + live member", &|| {
            let m = members(pid);
            m.len() == 2
                && m.iter()
                    .filter(|l| {
                        l.split_whitespace()
                            .nth(2)
                            .is_some_and(|s| s.starts_with('Z'))
                    })
                    .count()
                    == 1
        });
        let t = Target {
            pid,
            pgid: pid,
            start_time: st,
            failed: false,
        };
        let ours = still_ours(&t);
        drop(group);
        assert!(
            ours,
            "a zombie leader with a live member is still the verified group"
        );
    }

    #[test]
    fn verified_group_is_signalled_and_gone_before_reap_returns() {
        use std::os::unix::process::ExitStatusExt;
        let t = Tmp::new();
        let (pid, rx) = own_group_child();
        let st = pty::start_time(pid).expect("start time of our own child");
        ledger(
            &t.0,
            serde_json::json!([rec("u1-1", pid, serde_json::json!(st))]),
        );
        let started = Instant::now();
        let (report, failed) = reap_checked(&t.0).unwrap();
        assert!(!failed);
        assert_eq!(
            report,
            Report {
                reaped: vec![pid],
                skipped: vec![]
            }
        );
        assert!(
            !pty::group_alive(pid),
            "reap returned while group {pid} is alive"
        );
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "SIGHUP alone ends sleep"
        );
        let status = rx.recv_timeout(Duration::from_secs(1)).unwrap();
        assert_eq!(status.signal(), Some(libc::SIGHUP));
    }
}
