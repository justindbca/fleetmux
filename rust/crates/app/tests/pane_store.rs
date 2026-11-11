//! `PaneStore` seam tests (brief-m03 §3.4, M0, package B5 `server-pty`): the pane ledger in-process, without the
//! server binary. Stage A: red until B5 (every `PaneStore` / `pty` body is `todo!()`).
#![allow(dead_code)]

use std::collections::BTreeMap;
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::time::{Duration, Instant};

use fleetmux::server::pane::{KillError, PaneStore, WRITE_QUEUE_MAX, WriteError};
use fleetmux::server::pty;
use fleetmux_proto::{PaneInfo, Spawn, WriteMode};
use serde_json::Value;

static SEQ: AtomicU32 = AtomicU32::new(0);

/// Short private temp directory, removed on drop.
struct TempDir(PathBuf);

impl TempDir {
    fn new() -> TempDir {
        let p = std::env::temp_dir().join(format!(
            "fps{}-{}",
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

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// SIGKILLs every process group it was told about, so a failing test leaves no children behind.
#[derive(Default)]
struct Groups(Mutex<Vec<i32>>);

impl Groups {
    fn add(&self, info: &PaneInfo) {
        self.0.lock().unwrap().push(info.pgid as i32);
    }
}

impl Drop for Groups {
    fn drop(&mut self) {
        for pgid in self.0.lock().unwrap().iter() {
            unsafe {
                libc::kill(-pgid, libc::SIGKILL);
            }
        }
    }
}

fn pid_alive(pid: u32) -> bool {
    unsafe { libc::kill(pid as i32, 0) == 0 }
}

fn group_alive(pgid: u32) -> bool {
    unsafe { libc::kill(-(pgid as i32), 0) == 0 }
}

fn spawn_args(title: &str, argv: &[&str]) -> Spawn {
    Spawn {
        argv: argv.iter().map(|s| s.to_string()).collect(),
        cwd: None,
        env: BTreeMap::new(),
        title: title.to_string(),
        ty: "worker".to_string(),
        group: None,
        anchor: None,
        before: false,
    }
}

fn spawn(store: &mut PaneStore, groups: &Groups, title: &str, argv: &[&str]) -> PaneInfo {
    spawn_in(store, groups, title, argv, None)
}

fn spawn_in(
    store: &mut PaneStore,
    groups: &Groups,
    title: &str,
    argv: &[&str],
    cwd: Option<&Path>,
) -> PaneInfo {
    let mut args = spawn_args(title, argv);
    args.cwd = cwd.map(|p| p.to_string_lossy().into_owned());
    let info = store
        .spawn(&args, 80, 24)
        .unwrap_or_else(|e| panic!("spawn {argv:?}: {e:?}"));
    groups.add(&info);
    info
}

fn listed(store: &PaneStore, pane: &str) -> bool {
    store.list().iter().any(|p| p.pane == pane)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn spawn_sets_session_group_and_controlling_tty() {
    let groups = Groups::default();
    let mut store = PaneStore::new("t1".into(), 1000, None);
    let info = spawn(&mut store, &groups, "sleeper", &["sleep", "30"]);

    assert_eq!(info.pid, info.pgid, "the child leads its own process group");
    assert_eq!(
        unsafe { libc::getsid(info.pid as i32) },
        info.pid as i32,
        "the child leads its own session"
    );
    assert!(
        info.tty.starts_with("/dev/"),
        "tty is a device path: {:?}",
        info.tty
    );
    assert!(listed(&store, &info.pane), "{} not listed", info.pane);
    assert!(
        info.pane.starts_with("t1-"),
        "pane id carries the epoch: {}",
        info.pane
    );

    // `ps -o tty=` prints the controlling terminal without `/dev/` (`ttys012`, `pts/3`); `??` / `?` = none.
    let out = Command::new("ps")
        .args(["-o", "tty=", "-p", &info.pid.to_string()])
        .output()
        .expect("run ps");
    let ps_tty = String::from_utf8_lossy(&out.stdout).trim().to_string();
    assert_eq!(
        format!("/dev/{ps_tty}"),
        info.tty,
        "the PTY slave is the child's controlling terminal"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn write_queue_is_per_pane_and_bounded_16mib() {
    let groups = Groups::default();
    let mut store = PaneStore::new("t2".into(), 1000, None);
    // Neither program reads its input: whatever the PTY does not absorb stays in the pane's queue.
    let stuck = spawn(&mut store, &groups, "stuck", &["sleep", "30"]);
    let other = spawn(&mut store, &groups, "other", &["sleep", "30"]);

    let chunk = vec![b'x'; 1 << 20];
    let started = Instant::now();
    let mut accepted = 0usize;
    let mut full = false;
    for _ in 0..17 {
        match store.write(&stuck.pane, &chunk, WriteMode::Raw) {
            Ok(n) => {
                assert_eq!(n, chunk.len(), "a write is accepted whole or not at all");
                accepted += n;
            }
            Err(WriteError::QueueFull) => {
                full = true;
                break;
            }
            Err(e) => panic!("write to {}: {e:?}", stuck.pane),
        }
    }
    assert!(
        full,
        "17 MiB into a pane that never reads must hit QueueFull"
    );
    assert!(
        accepted >= WRITE_QUEUE_MAX - (1 << 20),
        "the bound is 16 MiB, not less: only {accepted} bytes accepted"
    );
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "writes are queued, never blocking on the PTY ({:?})",
        started.elapsed()
    );

    assert_eq!(
        store
            .write(&other.pane, b"hello\n", WriteMode::Raw)
            .unwrap_or_else(|e| panic!("another pane must still take writes: {e:?}")),
        6
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn kill_escalation_follows_schedule_and_grace() {
    let groups = Groups::default();
    let mut store = PaneStore::new("t3".into(), 1000, None);
    // SIG_IGN survives exec: `sleep` ignores SIGHUP and SIGTERM, only SIGKILL (t = 5000 ms) ends it.
    let info = spawn(
        &mut store,
        &groups,
        "stubborn",
        &["bash", "-c", "trap '' HUP TERM; exec sleep 600"],
    );
    std::thread::sleep(Duration::from_millis(500));

    // timeout 2000: SIGHUP at 0, SIGTERM at 2000, no SIGKILL, grace to 2500 → kill_timeout, pane stays.
    let t = Instant::now();
    let r = store.kill(&info.pane, 2000).await;
    let took = t.elapsed();
    assert!(
        matches!(r, Err(KillError::KillTimeout)),
        "want KillTimeout, got {r:?}"
    );
    assert!(
        took >= Duration::from_millis(2000) && took <= Duration::from_millis(3000),
        "kill_timeout after the 2000 ms step + 500 ms grace, took {took:?}"
    );
    assert!(
        pid_alive(info.pid),
        "SIGKILL must not be sent when timeout_ms < 5000"
    );
    assert!(
        store.info(&info.pane).is_some(),
        "the pane stays after kill_timeout"
    );

    // timeout 5000: restarts at SIGHUP; SIGKILL at 5000; reaped within the 500 ms grace.
    let t = Instant::now();
    store
        .kill(&info.pane, 5000)
        .await
        .unwrap_or_else(|e| panic!("kill with SIGKILL allowed: {e:?}"));
    let took = t.elapsed();
    assert!(
        took >= Duration::from_millis(5000) && took <= Duration::from_millis(5600),
        "SIGKILL at 5000 ms then reap, took {took:?}"
    );
    assert!(
        !group_alive(info.pgid),
        "process group {} still alive",
        info.pgid
    );
    assert!(
        store.info(&info.pane).is_none(),
        "the killed pane is removed"
    );
    assert!(!listed(&store, &info.pane));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn exited_child_is_reaped_and_pane_removed() {
    let groups = Groups::default();
    let mut store = PaneStore::new("t4".into(), 1000, None);
    let info = spawn(&mut store, &groups, "quitter", &["bash", "-c", "exit 0"]);

    let end = Instant::now() + Duration::from_secs(1);
    let mut removed = Vec::new();
    // A42-LINUX-RESTORE-FAILURE-04: the store finalises a reaped pane as soon as its PTY has reached EOF (the
    // reader thread's `eof`), so this loop only has to cover the race between the exit and the EOF.
    while store.contains(&info.pane) {
        assert!(
            Instant::now() < end,
            "{} still in the store 1 s after its child exited",
            info.pane
        );
        std::thread::sleep(Duration::from_millis(20));
        removed.extend(store.reap_exited());
    }
    assert!(
        !listed(&store, &info.pane),
        "a reaped pane is still listed: {:?}",
        store.list()
    );
    let reaped = removed
        .iter()
        .find(|e| e.pane == info.pane)
        .unwrap_or_else(|| panic!("reap_exited reports the removed pane: {removed:?}"));
    assert_eq!(
        reaped.status.as_ref().map(|s| s.exit_code()),
        Some(0),
        "reap_exited keeps the child's exit status: {reaped:?}"
    );
    assert!(store.info(&info.pane).is_none());
    // A zombie still answers kill(pid, 0); a reaped child does not.
    assert!(
        !pid_alive(info.pid),
        "child {} exited but was not reaped",
        info.pid
    );
}

/// Checks `panes.json` against the store: parses, same panes, §8.2 keys, mode 0600, no `.tmp` left behind.
fn check_panes_json(dir: &Path, store: &PaneStore, epoch: &str) {
    let path = dir.join("panes.json");
    let text = std::fs::read_to_string(&path).expect("read panes.json");
    let v: Value =
        serde_json::from_str(&text).unwrap_or_else(|e| panic!("panes.json: {e}: {text}"));
    assert_eq!(v["epoch"], epoch, "{text}");
    let recs = v["panes"]
        .as_array()
        .unwrap_or_else(|| panic!("no panes array: {text}"));
    let mut want: Vec<PaneInfo> = store.list();
    want.sort_by(|a, b| a.pane.cmp(&b.pane));
    let mut got: Vec<&Value> = recs.iter().collect();
    got.sort_by(|a, b| a["pane"].as_str().cmp(&b["pane"].as_str()));
    assert_eq!(got.len(), want.len(), "{text}");
    for (rec, p) in got.iter().zip(&want) {
        assert_eq!(rec["pane"], p.pane.as_str(), "{text}");
        assert_eq!(rec["pid"], p.pid, "{text}");
        assert_eq!(rec["pgid"], p.pgid, "{text}");
        assert_eq!(rec["tty"], p.tty.as_str(), "{text}");
        assert_eq!(rec["title"], p.title.as_str(), "{text}");
        assert_eq!(
            rec["start_time"].as_u64(),
            pty::start_time(p.pid),
            "start_time is the OS start time (PROTOCOL.md §8.6): {text}"
        );
    }
    let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode, 0o600, "panes.json mode {mode:o}");
    let leftovers: Vec<String> = std::fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n.ends_with(".tmp"))
        .collect();
    assert!(leftovers.is_empty(), "temp files left: {leftovers:?}");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn panes_json_rewritten_atomically_on_change() {
    let tmp = TempDir::new();
    let path = tmp.path().join("panes.json");
    let groups = Groups::default();
    let mut store = PaneStore::new("t5".into(), 1000, None);

    let a = spawn(&mut store, &groups, "first", &["sleep", "30"]);
    store.write_panes_json(&path).expect("write panes.json");
    check_panes_json(tmp.path(), &store, "t5");

    let b = spawn(&mut store, &groups, "second", &["sleep", "30"]);
    store.write_panes_json(&path).expect("write panes.json");
    check_panes_json(tmp.path(), &store, "t5");

    assert!(store.rename(&a.pane, "first-renamed"));
    store.write_panes_json(&path).expect("write panes.json");
    check_panes_json(tmp.path(), &store, "t5");

    store
        .kill(&b.pane, 5000)
        .await
        .unwrap_or_else(|e| panic!("kill {}: {e:?}", b.pane));
    store.write_panes_json(&path).expect("write panes.json");
    check_panes_json(tmp.path(), &store, "t5");

    // A reader never sees a torn file while the store keeps rewriting it.
    let stop = std::sync::Arc::new(AtomicBool::new(false));
    let reader = {
        let stop = stop.clone();
        let path = path.clone();
        std::thread::spawn(move || {
            let mut reads = 0u32;
            while !stop.load(Ordering::SeqCst) {
                if let Ok(text) = std::fs::read_to_string(&path) {
                    serde_json::from_str::<Value>(&text)
                        .unwrap_or_else(|e| panic!("torn panes.json: {e}: {text:?}"));
                    reads += 1;
                }
            }
            reads
        })
    };
    for _ in 0..200 {
        store.write_panes_json(&path).expect("rewrite panes.json");
    }
    stop.store(true, Ordering::SeqCst);
    let reads = reader.join().expect("reader thread panicked");
    assert!(reads > 0, "the reader never got to read panes.json");
    check_panes_json(tmp.path(), &store, "t5");
}

// ---- B5 seam tests (appended in B5; brief-m03 §3.3 additions ruled in [M13]: events, kill ticket, layout
// mirror, id preview). M0, no ignore. ----

use fleetmux::server::pane::{KillPoll, PaneEvent, SpawnError};

/// Drives one kill ticket to its end the way the handlers do: a step, then a sleep with nothing borrowed.
fn drive_kill(store: &mut PaneStore, t: &mut fleetmux::server::pane::KillTicket) -> KillPoll {
    loop {
        match store.kill_step(t).expect("kill_step") {
            KillPoll::Wait(d) => {
                assert!(
                    d <= Duration::from_millis(50),
                    "kill waits are short polls: {d:?}"
                );
                std::thread::sleep(d);
            }
            end => return end,
        }
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn events_deliver_output_then_closed_and_feed_reaches_the_parser() {
    let groups = Groups::default();
    let mut store = PaneStore::new("t6".into(), 1000, None);
    let mut rx = store.take_events().expect("first take_events");
    assert!(
        store.take_events().is_none(),
        "the receiver is handed out once"
    );

    let info = spawn(
        &mut store,
        &groups,
        "talker",
        &["bash", "-c", "printf 'ready-out\\n'; exit 0"],
    );
    let mut out = Vec::new();
    let end = Instant::now() + Duration::from_secs(5);
    loop {
        let left = end.saturating_duration_since(Instant::now());
        match tokio::time::timeout(left, rx.recv()).await {
            Ok(Some(PaneEvent::Output { pane, bytes })) => {
                assert_eq!(pane, info.pane);
                store.feed_output(&pane, &bytes);
                out.extend_from_slice(&bytes);
            }
            Ok(Some(PaneEvent::Closed { pane })) => {
                assert_eq!(pane, info.pane);
                break;
            }
            Ok(None) => panic!("event channel closed"),
            Err(_) => panic!(
                "no Closed within 5 s; output so far {:?}",
                String::from_utf8_lossy(&out)
            ),
        }
    }
    assert!(
        String::from_utf8_lossy(&out).contains("ready-out"),
        "output events carry the PTY bytes: {:?}",
        String::from_utf8_lossy(&out)
    );
    let text = store
        .read_text(&info.pane, 24)
        .expect("pane still held until reaped");
    assert!(
        text.contains("ready-out"),
        "fed bytes reach the parser: {text:?}"
    );
    assert!(
        store.info(&info.pane).unwrap().unseen,
        "output to a pane the layout does not display marks it unseen"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn next_pane_id_previews_and_failed_spawn_does_not_use_it() {
    let groups = Groups::default();
    let mut store = PaneStore::new("t7".into(), 1000, None);
    assert_eq!(store.next_pane_id(), "t7-1");

    let mut bad = spawn_args("bad", &["sleep", "1"]);
    bad.cwd = Some("/nonexistent/fleetmux-b5".into());
    assert!(matches!(
        store.spawn(&bad, 80, 24),
        Err(SpawnError::BadCwd(_))
    ));
    assert!(matches!(
        store.spawn(&spawn_args("empty", &[]), 80, 24),
        Err(SpawnError::EmptyArgv)
    ));
    assert_eq!(
        store.next_pane_id(),
        "t7-1",
        "a failed spawn does not use up the id"
    );

    let info = spawn(&mut store, &groups, "ok", &["sleep", "30"]);
    assert_eq!(info.pane, "t7-1");
    assert_eq!(store.next_pane_id(), "t7-2");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn kill_tickets_step_without_borrow_and_stale_tickets_end_done() {
    let groups = Groups::default();
    let mut store = PaneStore::new("t8".into(), 1000, None);
    let info = spawn(&mut store, &groups, "sleeper", &["sleep", "30"]);
    std::thread::sleep(Duration::from_millis(200));

    // Two concurrent kills of one pane: the first to see the reap removes it, the other ends Done, no signal.
    let mut first = store.kill_begin(&info.pane, 5000).expect("kill_begin");
    let mut second = store
        .kill_begin(&info.pane, 5000)
        .expect("second kill_begin");
    let t = Instant::now();
    assert_eq!(drive_kill(&mut store, &mut first), KillPoll::Done);
    assert!(
        t.elapsed() < Duration::from_secs(1),
        "SIGHUP ends sleep: {:?}",
        t.elapsed()
    );
    assert!(store.info(&info.pane).is_none(), "Done removes the pane");
    assert!(!group_alive(info.pgid));
    assert_eq!(
        store.kill_step(&mut second).expect("stale ticket"),
        KillPoll::Done
    );
    assert!(matches!(
        store.kill_begin(&info.pane, 5000),
        Err(KillError::NoSuchPane)
    ));
}

/// A42-LINUX-RESTORE-FAILURE-04 P-04-REAP: the PTY reaches EOF *before* the child exits (it closes 0/1/2, then
/// exits 7 a moment later). The `Closed` event is already behind us when the child is reaped, so the reap turn
/// itself must report the exit and remove the pane — it may not wait for a further PTY / SIGCHLD / other-pane
/// event (the 37303cb8 real-server probe left the dead pane in the layout and logged nothing).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_pty_that_closed_before_the_exit_is_finalised_by_the_reap_turn() {
    use fleetmux::server::pane::PaneEvent;
    let groups = Groups::default();
    let mut store = PaneStore::new("t8".into(), 1000, None);
    let mut events = store.take_events().expect("the event receiver");
    let info = spawn(
        &mut store,
        &groups,
        "eof-first",
        &[
            "sh",
            "-c",
            "echo boom >&2; exec 0<&- 1>&- 2>&-; sleep 0.4; exit 7",
        ],
    );
    // `Closed` is sent only after the last read: once it arrives, the PTY has nothing more to give.
    let closed = tokio::time::timeout(Duration::from_secs(5), async {
        while let Some(ev) = events.recv().await {
            if matches!(ev, PaneEvent::Closed { .. }) {
                return true;
            }
        }
        false
    })
    .await
    .expect("a Closed event within 5 s");
    assert!(closed, "the reader thread never reported EOF");
    assert!(
        listed(&store, &info.pane),
        "the pane left before its child exited"
    );
    std::thread::sleep(Duration::from_millis(600)); // the child exits 7 at ~0.4 s
    let gone = store.reap_exited();
    let exited = gone
        .iter()
        .find(|e| e.pane == info.pane)
        .unwrap_or_else(|| panic!("the reap turn did not finalise the pane: {gone:?}"));
    assert_eq!(
        exited.status.as_ref().map(|s| s.exit_code()),
        Some(7),
        "the exit status was lost: {exited:?}"
    );
    assert!(!store.contains(&info.pane), "the dead pane is still in the store");
    assert!(!listed(&store, &info.pane), "the dead pane is still listed");
}

/// A42-LINUX-RESTORE-FAILURE-04 P-04-REAP: the launcher exits nonzero while a background child still holds the PTY
/// slave, so the PTY stays open past the observation window. The reap turn that observes the exit must finalise the
/// pane at once (exit status kept, `pty_open` says the tail is partial) instead of waiting for a PTY EOF that may
/// never come.
///
/// The launcher leaves only once the holder has reported ready: the holder writes `ready` after it has installed
/// `trap '' HUP`, so it really survives the session leader's exit and keeps the slave open. Exiting straight after
/// the fork races the kernel's SIGHUP (the holder dies before its `trap` runs, the slave closes and the case
/// degenerates into the EOF-first one); the wait is bounded and fails loudly instead of sleeping a fixed time.
///
/// Cleanup follows the fixture rule, never a command name: the holder is inside the pane's own process group and
/// goes with this test's normal teardown (`Groups::drop` kills that captured pgid); the ready file sits in this
/// test's `TempDir`. No `pkill`/`killall`/name pattern is used, so no process outside this test's runtime can be hit.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_nonzero_exit_while_a_child_still_holds_the_pty_is_finalised_at_once() {
    let dir = TempDir::new();
    let groups = Groups::default();
    let mut store = PaneStore::new("t9".into(), 1000, None);
    let info = spawn_in(
        &mut store,
        &groups,
        "holder",
        &[
            "sh",
            "-c",
            "echo boom >&2; r=ready; (trap '' HUP; : > \"$r\"; sleep 12) & \
             i=0; while [ ! -e \"$r\" ]; do i=$((i+1)); \
             if [ \"$i\" -gt 300 ]; then echo 'the holder never reported ready' >&2; exit 9; fi; \
             sleep 0.01; done; exit 7",
        ],
        Some(dir.path()),
    );
    std::thread::sleep(Duration::from_millis(600)); // the launcher is gone; the background child holds the slave
    let gone = store.reap_exited();
    let exited = gone
        .iter()
        .find(|e| e.pane == info.pane)
        .unwrap_or_else(|| panic!("the reap turn waited for the PTY: {gone:?}"));
    assert_eq!(
        exited.status.as_ref().map(|s| s.exit_code()),
        Some(7),
        "the exit status was lost: {exited:?}"
    );
    // macOS hangs the terminal up when the pane's session leader exits, so the slave cannot stay open there; on
    // Linux the background child (SIGHUP ignored) keeps it open past the observation window, and the reap turn
    // must still finalise the pane (that is what `pty_open` reports).
    #[cfg(target_os = "linux")]
    assert!(
        exited.pty_open,
        "this case needs a still-open PTY on Linux: {exited:?}"
    );
    assert!(!store.contains(&info.pane), "the dead pane is still in the store");
    assert!(!listed(&store, &info.pane), "the dead pane is still listed");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn reap_exited_leaves_a_pane_being_killed_to_its_kill() {
    let groups = Groups::default();
    let mut store = PaneStore::new("t9".into(), 1000, None);
    let info = spawn(&mut store, &groups, "sleeper", &["sleep", "30"]);
    std::thread::sleep(Duration::from_millis(200));

    let mut t = store.kill_begin(&info.pane, 5000).expect("kill_begin");
    std::thread::sleep(Duration::from_millis(300));
    assert!(
        store.reap_exited().is_empty(),
        "the kill in progress, not reap_exited, removes its pane"
    );
    assert!(store.info(&info.pane).is_some());
    assert_eq!(drive_kill(&mut store, &mut t), KillPoll::Done);
    assert!(store.info(&info.pane).is_none());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn kill_timeout_zero_sends_sighup_only() {
    let groups = Groups::default();
    let mut store = PaneStore::new("ta".into(), 1000, None);
    let info = spawn(
        &mut store,
        &groups,
        "stubborn",
        &["bash", "-c", "trap '' HUP; exec sleep 600"],
    );
    std::thread::sleep(Duration::from_millis(300));
    let t = Instant::now();
    let r = store.kill(&info.pane, 0).await;
    assert!(matches!(r, Err(KillError::KillTimeout)), "{r:?}");
    let took = t.elapsed();
    assert!(
        took >= Duration::from_millis(500) && took < Duration::from_millis(1000),
        "timeout 0: SIGHUP, then only the 500 ms grace: {took:?}"
    );
    assert!(
        pid_alive(info.pid),
        "no SIGTERM / SIGKILL when timeout_ms = 0"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn sync_layout_mirrors_group_index_focus_and_clears_unseen() {
    use fleetmux_layout::{Group as LGroup, Layout, PaneType};

    let tmp = TempDir::new();
    let groups = Groups::default();
    let mut store = PaneStore::new("tb".into(), 1000, None);
    let a = spawn(&mut store, &groups, "a", &["sleep", "30"]);
    let b = spawn(&mut store, &groups, "b", &["sleep", "30"]);

    let mut layout = Layout::new();
    layout.insert(a.pane.clone(), PaneType::Worker);
    layout.insert(b.pane.clone(), PaneType::Worker);
    // Placement only; selecting a new group's first tab is the caller's (PROTOCOL.md §4.4), done explicitly.
    assert!(layout.select(a.pane.clone()));
    layout.focus_group(LGroup::Workers);
    store.sync_layout(&layout);

    let (ia, ib) = (store.info(&a.pane).unwrap(), store.info(&b.pane).unwrap());
    assert_eq!(
        (ia.group, ia.index, ia.focused),
        (fleetmux_proto::Group::Workers, 0, true)
    );
    assert_eq!(
        (ib.group, ib.index, ib.focused),
        (fleetmux_proto::Group::Workers, 1, false)
    );

    store.feed_output(&a.pane, b"shown\r\n");
    store.feed_output(&b.pane, b"hidden\r\n");
    assert!(
        !store.info(&a.pane).unwrap().unseen,
        "the displayed tab is not unseen"
    );
    assert!(
        store.info(&b.pane).unwrap().unseen,
        "a background tab with output is unseen"
    );

    assert!(layout.select(b.pane.clone()));
    store.sync_layout(&layout);
    assert!(
        !store.info(&b.pane).unwrap().unseen,
        "displaying the tab clears unseen"
    );
    assert!(store.info(&b.pane).unwrap().focused);
    store.feed_output(&a.pane, b"now hidden\r\n");
    assert!(store.info(&a.pane).unwrap().unseen);

    let path = tmp.path().join("panes.json");
    store.write_panes_json(&path).expect("write panes.json");
    let v: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    let rec = v["panes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["pane"] == b.pane.as_str())
        .expect("b in panes.json")
        .clone();
    assert_eq!(
        (rec["group"].as_str(), rec["index"].as_u64()),
        (Some("workers"), Some(1))
    );
}

// ---- pane.kill also ends the PTY's foreground job (PROTOCOL.md §4.13, 2026-11-08). An interactive shell puts
// its foreground job into a process group of its own; signalling only the shell's group left a job that ignores
// SIGHUP running as an orphan. ----

/// A shell with job control (`set -m`) running a foreground job that ignores SIGHUP (SIG_IGN survives `exec`):
/// the job sits in its own process group, inside the pane's session.
const FG_JOB_IGNORING_HUP: &str = "set -m; sh -c 'trap \"\" HUP; exec sleep 600'; :";

/// The pane's foreground group once the job has taken the terminal; also queued for `Groups` cleanup.
fn wait_foreground_job(store: &PaneStore, groups: &Groups, info: &PaneInfo) -> u32 {
    let end = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(fg) = store
            .foreground_pgid(&info.pane)
            .filter(|&g| g != info.pgid)
        {
            groups.0.lock().unwrap().push(fg as i32);
            assert_eq!(
                unsafe { libc::getsid(fg as i32) },
                info.pid as i32,
                "the foreground job is in the pane's session"
            );
            return fg;
        }
        assert!(
            Instant::now() < end,
            "the foreground job never took the terminal"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn kill_also_ends_a_foreground_job_that_ignores_sighup() {
    let groups = Groups::default();
    let mut store = PaneStore::new("tf".into(), 1000, None);
    let info = spawn(
        &mut store,
        &groups,
        "bash",
        &["bash", "-c", FG_JOB_IGNORING_HUP],
    );
    let fg = wait_foreground_job(&store, &groups, &info);

    let t0 = Instant::now();
    let mut t = store.kill_begin(&info.pane, 5000).expect("kill_begin");
    assert_eq!(drive_kill(&mut store, &mut t), KillPoll::Done);
    let took = t0.elapsed();
    assert!(
        !group_alive(fg),
        "the foreground job's group {fg} outlived pane.kill (orphaned, pane pgid {})",
        info.pgid
    );
    assert!(!group_alive(info.pgid));
    assert!(
        store.info(&info.pane).is_none(),
        "the killed pane is removed"
    );
    assert!(
        took < Duration::from_millis(3000),
        "SIGTERM at 2000 ms ends the job: {took:?}"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn kill_does_not_wait_past_its_deadline_for_a_foreground_job() {
    let groups = Groups::default();
    let mut store = PaneStore::new("tg".into(), 1000, None);
    let info = spawn(
        &mut store,
        &groups,
        "bash",
        &["bash", "-c", FG_JOB_IGNORING_HUP],
    );
    let fg = wait_foreground_job(&store, &groups, &info);

    // timeout 0: SIGHUP only. The shell dies, the job ignores it; the kill still ends at the grace, not later,
    // and is not a kill_timeout (only the pane's own group decides that).
    let t0 = Instant::now();
    let r = store.kill(&info.pane, 0).await;
    let took = t0.elapsed();
    assert!(r.is_ok(), "the pane's own group is gone: {r:?}");
    assert!(took < Duration::from_millis(1500), "{took:?}");
    assert!(store.info(&info.pane).is_none());
    unsafe { libc::kill(-(fg as i32), libc::SIGKILL) };
}
