//! `fleetmux scenario upgrade` through the real binary: usage and environment errors (exit 2, never a skip), and
//! the clean-up on every way out of a run (a failing step, a panic, the timeout, SIGTERM).
//!
//! The clean-up cases use a stand-in `--from` release whose `bin/fleetmux start` leaves real servers behind in the
//! scenario's world, the way a real `start` does: a daemonized Redis with its socket under the Redis runtime root, a
//! private tmux server under TMUX_TMPDIR, and a detached process whose command line names the world (a daemon).
//! Each case then checks that all three are gone after the scenario exits. redis-server, tmux and git are required:
//! a missing program fails these tests.
use std::io::{BufRead, BufReader};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_fleetmux");

fn tool(name: &str) -> PathBuf {
    for dir in ["/opt/homebrew/opt/redis/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"] {
        let p = Path::new(dir).join(name);
        if p.is_file() {
            return p;
        }
    }
    panic!("{name} is required by this test and was not found (a missing program is a failure, not a skip)");
}

fn tempdir(tag: &str) -> PathBuf {
    let mut buf = format!("/tmp/fmxs{tag}-XXXXXX\0").into_bytes();
    let p = unsafe { libc::mkdtemp(buf.as_mut_ptr() as *mut libc::c_char) };
    assert!(!p.is_null());
    buf.pop();
    PathBuf::from(String::from_utf8(buf).unwrap())
}

fn alive(pid: i32) -> bool {
    unsafe { libc::kill(pid, 0) == 0 }
}

fn wait_dead(pid: i32) -> bool {
    let t = Instant::now() + Duration::from_secs(10);
    while Instant::now() < t {
        if !alive(pid) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}

/// The tools' directories, then the system PATH: what the scenario finds its programs on.
fn tool_path() -> String {
    let mut dirs: Vec<String> = ["redis-server", "tmux", "git"]
        .iter()
        .map(|t| tool(t).parent().unwrap().display().to_string())
        .collect();
    dirs.dedup();
    dirs.push("/usr/bin:/bin:/usr/sbin:/sbin".into());
    dirs.join(":")
}

fn last_json(stdout: &str) -> serde_json::Value {
    let last = stdout.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("");
    serde_json::from_str(last).unwrap_or_else(|_| panic!("the last line is not JSON: {last:?}\n{stdout}"))
}

/// A stand-in release directory. `bin/fleetmux start` starts the three servers and records their pids in
/// `$SCENARIO_TEST_PIDS` (redis, tmux, daemon); `status` prints `{}`.
fn fake_release(dir: &Path) {
    for d in [".venv/bin", "fleetmux/config", "bin"] {
        std::fs::create_dir_all(dir.join(d)).unwrap();
    }
    std::os::unix::fs::symlink("/bin/sh", dir.join(".venv/bin/python")).unwrap();
    std::fs::write(dir.join("fleetmux/fleet.py"), "DEFAULTS = {'worker_model': 'sonnet'}\n").unwrap();
    std::fs::write(dir.join("fleetmux/config/fleet.yaml"), "discovery:\n  supervisor: [x]\n  worker: [y]\n").unwrap();
    std::fs::write(dir.join("fleetmux/demo_actor.py"), "# stand-in\n").unwrap();
    std::fs::write(dir.join("fleetmux/scenario_actor.py"), "# stand-in\n").unwrap();
    let script = r#"#!/bin/sh
# stand-in fleetmux of a test release: `start` leaves real servers behind, as a real start does
cmd="$1"; shift
case "$cmd" in
start)
  rr=""; redis=""; fh=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --redis-runtime-dir) rr="$2"; shift ;;
      --redis-server) redis="$2"; shift ;;
      --fleet-home) fh="$2"; shift ;;
    esac
    shift
  done
  mkdir -p "$rr/run1"
  "$redis" --port 0 --unixsocket "$rr/run1/redis.sock" --daemonize yes --save "" --appendonly no \
    --pidfile "$rr/run1/redis.pid" --dir "$rr/run1" --logfile "$rr/run1/redis.log" || exit 3
  i=0; while [ ! -s "$rr/run1/redis.pid" ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done
  tmux new-session -d -s fake "sleep 600" || exit 4
  tpid=$(tmux display-message -p '#{pid}')
  echo > "$fh/daemon.marker"
  nohup tail -f "$fh/daemon.marker" >/dev/null 2>&1 &
  echo "$(cat "$rr/run1/redis.pid") $tpid $!" > "$SCENARIO_TEST_PIDS"
  echo "{\"config\": \"$fh/run/config.json\"}" > "$fh/latest.json"
  echo started
  ;;
status) echo '{}' ;;
*) echo "stand-in: $cmd" >&2; exit 9 ;;
esac
"#;
    let p = dir.join("bin/fleetmux");
    std::fs::write(&p, script).unwrap();
    std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
}

/// What a case started, stopped on drop whatever happened (so a failing case leaves nothing behind).
struct Pids(Vec<i32>, PathBuf);

impl Pids {
    fn read(file: &Path, dir: PathBuf) -> Pids {
        let text = std::fs::read_to_string(file).unwrap_or_default();
        Pids(text.split_whitespace().filter_map(|s| s.parse().ok()).collect(), dir)
    }
}

impl Drop for Pids {
    fn drop(&mut self) {
        for p in &self.0 {
            if alive(*p) {
                unsafe { libc::kill(*p, libc::SIGKILL) };
            }
        }
        let _ = std::fs::remove_dir_all(&self.1);
    }
}

struct Case {
    dir: PathBuf,
    /// The `--from` release.
    rel: PathBuf,
    /// The `--to` release: another directory (the same one has nothing to switch).
    to: PathBuf,
    pids: PathBuf,
}

fn case(tag: &str) -> Case {
    let dir = tempdir(tag);
    let rel = dir.join("rel");
    fake_release(&rel);
    let to = dir.join("rel-to");
    fake_release(&to);
    let pids = dir.join("pids");
    Case { dir, rel, to, pids }
}

fn scenario(c: &Case, fault: &str, extra: &[&str]) -> Command {
    scenario_to(c, &c.to, fault, extra)
}

fn scenario_to(c: &Case, to: &Path, fault: &str, extra: &[&str]) -> Command {
    let mut cmd = Command::new(BIN);
    let rel = c.rel.display().to_string();
    let to = to.display().to_string();
    cmd.args(["scenario", "upgrade", "--from", &rel, "--to", &to])
        .args(extra)
        .env_clear()
        .env("PATH", tool_path())
        .env("HOME", &c.dir)
        .env("FLEETMUX_SCENARIO_FAULT", fault)
        .env("SCENARIO_TEST_PIDS", &c.pids)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    cmd
}

fn assert_all_stopped(pids: &Pids, out: &str) {
    assert_eq!(pids.0.len(), 3, "the stand-in start did not record redis, tmux and daemon pids\n{out}");
    for (what, pid) in ["redis-server", "tmux server", "daemon stand-in"].iter().zip(&pids.0) {
        assert!(wait_dead(*pid), "{what} (pid {pid}) still runs after the scenario exited\n{out}");
    }
    assert_all_stopped_world(out);
}

fn assert_all_stopped_world(out: &str) {
    // the world directory goes too (no --keep), on this exit path as on every other
    let v = last_json(out);
    let report = v["report"].as_str().unwrap_or("");
    assert!(!report.is_empty(), "no report path in the last line\n{out}");
    let r: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(report).unwrap()).unwrap();
    let _ = std::fs::remove_file(report);
    let world = r["world"].as_str().expect("the report names the world");
    assert!(world.contains("/fmxu-"), "{world}");
    assert!(!Path::new(world).exists(), "the world {world} was left behind\n{out}");
}

#[test]
fn a_usage_error_exits_2_with_a_json_last_line() {
    let out = Command::new(BIN).args(["scenario", "upgrade", "--to", "HEAD"]).output().unwrap();
    assert_eq!(out.status.code(), Some(2));
    let v = last_json(&String::from_utf8_lossy(&out.stdout));
    assert_eq!(v["result"], "fail");
    assert_eq!(v["step"], "usage");
    assert!(v["reason"].as_str().unwrap().contains("--from"));
}

#[test]
fn a_missing_program_fails_with_exit_2_and_is_never_a_skip() {
    let d = tempdir("np");
    let out = Command::new(BIN)
        .args(["scenario", "upgrade", "--from", "/nonexistent/a", "--to", "/nonexistent/b"])
        .env_clear()
        .env("PATH", d.join("empty"))
        .env("HOME", &d)
        .env("FLEETMUX_SCENARIO_REDIS_BIN_DIR", d.join("none"))
        .output()
        .unwrap();
    let _ = std::fs::remove_dir_all(&d);
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert_eq!(out.status.code(), Some(2), "{stdout}");
    let v = last_json(&stdout);
    assert_eq!(v["result"], "fail");
    assert_eq!(v["step"], "prepare");
    let why = v["reason"].as_str().unwrap();
    assert!(why.contains("redis-server") && why.contains("tmux"), "{why}");
    assert!(stdout.contains("scenario: prepare: FAIL"), "{stdout}");
}

#[test]
fn a_failing_step_is_named_and_everything_it_started_is_stopped() {
    let c = case("f");
    let out = scenario(&c, "start_from:fail", &["--timeout", "120"]).output().unwrap();
    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    let pids = Pids::read(&c.pids, c.dir.clone());
    assert_eq!(out.status.code(), Some(1), "{stdout}");
    let v = last_json(&stdout);
    assert_eq!(v["step"], "start_from", "{stdout}");
    assert!(stdout.contains("scenario: world: ok"), "{stdout}");
    assert!(stdout.contains("scenario: cleanup: ok"), "{stdout}");
    assert!(!stdout.contains("scenario: subfleet_up"), "a step after the failure ran\n{stdout}");
    assert_all_stopped(&pids, &stdout);
}

#[test]
fn a_panicking_step_still_stops_everything() {
    let c = case("p");
    let out = scenario(&c, "start_from:panic", &["--timeout", "120"]).output().unwrap();
    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    let pids = Pids::read(&c.pids, c.dir.clone());
    assert_eq!(out.status.code(), Some(1), "{stdout}");
    let v = last_json(&stdout);
    assert_eq!(v["step"], "start_from");
    assert!(v["reason"].as_str().unwrap().contains("panic"), "{stdout}");
    assert_all_stopped(&pids, &stdout);
}

#[test]
fn the_timeout_fails_the_running_step_and_stops_everything() {
    let c = case("t");
    let t0 = Instant::now();
    let out = scenario(&c, "start_from:hang", &["--timeout", "15"]).output().unwrap();
    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    let pids = Pids::read(&c.pids, c.dir.clone());
    assert_eq!(out.status.code(), Some(1), "{stdout}");
    assert!(t0.elapsed() < Duration::from_secs(60), "the run did not end at its timeout");
    let v = last_json(&stdout);
    assert_eq!(v["step"], "start_from");
    assert!(v["reason"].as_str().unwrap().contains("timed out"), "{stdout}");
    assert_all_stopped(&pids, &stdout);
}

#[test]
fn sigterm_exits_130_and_stops_everything() {
    let c = case("s");
    let mut child = scenario(&c, "start_from:hang", &["--timeout", "300"]).spawn().unwrap();
    let pid = child.id() as i32;
    let mut stdout = String::new();
    let mut r = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    while r.read_line(&mut line).unwrap_or(0) > 0 {
        stdout.push_str(&line);
        if line.starts_with("scenario: world: ok") {
            // start_from runs (and then hangs): wait for the stand-in to record its servers, then interrupt
            let t = Instant::now() + Duration::from_secs(60);
            while !c.pids.exists() && Instant::now() < t {
                std::thread::sleep(Duration::from_millis(100));
            }
            std::thread::sleep(Duration::from_millis(500));
            unsafe { libc::kill(pid, libc::SIGTERM) };
        }
        line.clear();
    }
    let status = child.wait().unwrap();
    let pids = Pids::read(&c.pids, c.dir.clone());
    assert_eq!(status.code(), Some(130), "{stdout}");
    let v = last_json(&stdout);
    assert_eq!(v["result"], "fail");
    assert!(v["reason"].as_str().unwrap().contains("signal"), "{stdout}");
    assert_all_stopped(&pids, &stdout);
}

#[test]
fn the_same_release_as_from_and_to_passes_after_prepare_without_starting_a_tree() {
    let c = case("same");
    // --to names the --from release through a link, as rollout's versions/<v> does
    let link = c.dir.join("same-link");
    std::os::unix::fs::symlink(&c.rel, &link).unwrap();
    let out = scenario_to(&c, &link, "", &[]).output().unwrap();
    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let pids = Pids::read(&c.pids, c.dir.clone());
    assert_eq!(out.status.code(), Some(0), "{stdout}");
    let v = last_json(&stdout);
    assert_eq!(v["result"], "pass", "{stdout}");
    assert_eq!(v["step"], "", "{stdout}");
    assert_eq!(v["reason"], "from and to are the same release: nothing to switch", "{stdout}");
    assert!(stdout.contains("scenario: prepare: ok"), "{stdout}");
    assert!(!stdout.contains("scenario: world"), "a step after prepare ran\n{stdout}");
    assert!(stdout.contains("scenario: cleanup: ok"), "{stdout}");
    assert!(pids.0.is_empty(), "the stand-in start ran: {:?}\n{stdout}", pids.0);
    assert_all_stopped_world(&stdout);
}
