//! Clean-up of everything a scenario run started, on every way out: the normal end, a failing step, a panic, the
//! `--timeout` budget running out and SIGINT / SIGTERM / SIGHUP.
//!
//! Only what this run registered is stopped:
//! - its world directory (a fresh `mkdtemp` directory, so its path is unique): the Redis servers whose socket is
//!   inside it (`SHUTDOWN NOSAVE` through that socket, then the pid from the pidfile next to it), the tmux servers
//!   whose socket is inside it (`kill-server` through that socket, then the server pid it reported), the fleetmux
//!   terminal servers whose runtime directory is inside it (`server.shutdown force` through its control socket,
//!   then `fleetmux reap --runtime`), and any process of this user whose command line names the world directory;
//! - the processes the run spawned itself (recorded with their start time, so a reused pid is never signalled).
//!
//! Nothing is found by program name: the many Redis and tmux servers other tests left on this machine are never
//! looked at. [`Guard::cleanup`] is idempotent and serialised, so the signal / timeout thread and the main thread can
//! both call it; [`Drop`] calls it as well, which covers a panic that unwinds through the owner.
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// A process this run owns: its pid and its start time as `ps -o lstart=` prints it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Owned {
    pub pid: i32,
    pub started: String,
}

#[derive(Debug, Default)]
struct State {
    roots: Vec<PathBuf>,
    redis: Vec<PathBuf>,
    tmux: Vec<PathBuf>,
    terminals: Vec<PathBuf>,
    pids: Vec<Owned>,
    groups: Vec<Owned>,
    tmux_bin: Option<PathBuf>,
    fleetmux_bin: Option<PathBuf>,
    /// Removed by a clean [`Guard::cleanup`] (nothing left running), whichever way the run ends.
    remove: Option<PathBuf>,
    cleaned: bool,
    left: Vec<String>,
}

/// The registry of what a run started, and its clean-up.
#[derive(Debug, Default)]
pub struct Guard {
    state: Mutex<State>,
    /// Held for the whole of [`Guard::cleanup`], so two callers never interleave.
    serial: Mutex<()>,
}

/// Owned by the main thread for the whole run: dropping it (normal end, or a panic unwinding through `main`)
/// cleans up, whoever else still holds the [`Guard`] (the watcher thread keeps a clone).
pub struct Owner(pub Arc<Guard>);

impl Drop for Owner {
    fn drop(&mut self) {
        self.0.cleanup();
    }
}

impl Guard {
    pub fn new() -> Arc<Guard> {
        Arc::new(Guard::default())
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Own a directory: everything found inside it, and every process naming it, is this run's.
    pub fn own_root(&self, root: &Path) {
        let mut s = self.lock();
        for p in spellings(root) {
            if !s.roots.contains(&p) {
                s.roots.push(p);
            }
        }
    }

    /// Remove `dir` at the end of a clean [`Guard::cleanup`] (not when something is left running: it is evidence).
    pub fn remove_when_clean(&self, dir: &Path) {
        self.lock().remove = Some(dir.to_path_buf());
    }

    /// The tmux and fleetmux binaries used to stop servers (`kill-server`, `reap`).
    pub fn set_tools(&self, tmux: Option<&Path>, fleetmux: Option<&Path>) {
        let mut s = self.lock();
        if let Some(t) = tmux {
            s.tmux_bin = Some(t.to_path_buf());
        }
        if let Some(f) = fleetmux {
            s.fleetmux_bin = Some(f.to_path_buf());
        }
    }

    /// Own one process (recorded with its start time). Returns false when it is already gone.
    pub fn own_pid(&self, pid: i32) -> bool {
        let Some(started) = start_time(pid) else {
            return false;
        };
        let mut s = self.lock();
        let o = Owned { pid, started };
        if !s.pids.contains(&o) {
            s.pids.push(o);
        }
        true
    }

    /// Own a process group this run created (a spawned command that leads its own group).
    pub fn own_group(&self, pgid: i32) -> bool {
        let Some(started) = start_time(pgid) else {
            return false;
        };
        let mut s = self.lock();
        let o = Owned { pid: pgid, started };
        if !s.groups.contains(&o) {
            s.groups.push(o);
        }
        true
    }

    pub fn own_redis_socket(&self, sock: &Path) {
        let mut s = self.lock();
        if !s.redis.iter().any(|x| x == sock) {
            s.redis.push(sock.to_path_buf());
        }
    }

    pub fn own_tmux_socket(&self, sock: &Path) {
        let mut s = self.lock();
        if !s.tmux.iter().any(|x| x == sock) {
            s.tmux.push(sock.to_path_buf());
        }
    }

    pub fn own_terminal_runtime(&self, dir: &Path) {
        let mut s = self.lock();
        if !s.terminals.iter().any(|x| x == dir) {
            s.terminals.push(dir.to_path_buf());
        }
    }

    /// Register what exists now inside the owned directories: Redis sockets (`redis.sock`), tmux sockets (inside a
    /// `tmux-<uid>` directory or named `*.tmux`), terminal runtime directories (`ctl.sock` or `panes.json`), and
    /// the pids of the Redis (pidfile) and tmux (`#{pid}`) servers behind them.
    pub fn scan(&self) {
        let (roots, tmux_bin) = {
            let s = self.lock();
            (s.roots.clone(), s.tmux_bin.clone())
        };
        let mut redis = Vec::new();
        let mut tmux = Vec::new();
        let mut terminals = Vec::new();
        for root in roots.iter().filter(|r| r.is_dir()) {
            walk(root, SCAN_DEPTH, &mut |p| {
                if !is_socket(p) {
                    if p.file_name().is_some_and(|n| n == "panes.json")
                        && let Some(d) = p.parent()
                    {
                        terminals.push(d.to_path_buf());
                    }
                    return;
                }
                let name = p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                let parent = p
                    .parent()
                    .and_then(Path::file_name)
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default();
                if name == "redis.sock" {
                    redis.push(p.to_path_buf());
                } else if name == "ctl.sock" {
                    if let Some(d) = p.parent() {
                        terminals.push(d.to_path_buf());
                    }
                } else if parent.starts_with("tmux-") || name.contains("tmux") {
                    tmux.push(p.to_path_buf());
                }
            });
        }
        for sock in &redis {
            self.own_redis_socket(sock);
            if let Some(pid) = sock.parent().and_then(|d| read_pid(&d.join("redis.pid"))) {
                self.own_pid(pid);
            }
        }
        for sock in &tmux {
            self.own_tmux_socket(sock);
            if let Some(bin) = &tmux_bin
                && let Some(pid) = tmux_server_pid(bin, sock)
            {
                self.own_pid(pid);
            }
        }
        for dir in &terminals {
            self.own_terminal_runtime(dir);
        }
    }

    /// Stop everything registered (after a last [`Guard::scan`]). Idempotent: a second call returns the first
    /// call's result. Returns what is still running afterwards (empty = clean).
    pub fn cleanup(&self) -> Vec<String> {
        let _serial = self.serial.lock().unwrap_or_else(|e| e.into_inner());
        if self.lock().cleaned {
            return self.lock().left.clone();
        }
        self.scan();
        let s = {
            let s = self.lock();
            State {
                roots: s.roots.clone(),
                redis: s.redis.clone(),
                tmux: s.tmux.clone(),
                terminals: s.terminals.clone(),
                pids: s.pids.clone(),
                groups: s.groups.clone(),
                tmux_bin: s.tmux_bin.clone(),
                fleetmux_bin: s.fleetmux_bin.clone(),
                remove: s.remove.clone(),
                cleaned: false,
                left: Vec::new(),
            }
        };
        // 1. the servers, each through its own control channel
        for dir in &s.terminals {
            stop_terminal(dir, s.fleetmux_bin.as_deref());
        }
        for sock in &s.tmux {
            if let Some(bin) = &s.tmux_bin {
                let _ = Command::new(bin)
                    .arg("-S")
                    .arg(sock)
                    .arg("kill-server")
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
            }
        }
        for sock in &s.redis {
            redis_shutdown(sock);
        }
        // 2. every process of the run: TERM, a grace period, then KILL
        let my_group = unsafe { libc::getpgrp() };
        let groups: Vec<i32> = s
            .groups
            .iter()
            .filter(|g| g.pid != my_group && group_safe(g))
            .map(|g| g.pid)
            .collect();
        for g in &groups {
            unsafe { libc::killpg(*g, libc::SIGTERM) };
        }
        for (pid, _) in owned_processes(&s.roots, &s.pids) {
            unsafe { libc::kill(pid, libc::SIGTERM) };
        }
        let deadline = Instant::now() + STOP_GRACE;
        let mut left = owned_processes(&s.roots, &s.pids);
        while !left.is_empty() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(200));
            left = owned_processes(&s.roots, &s.pids);
        }
        for g in &groups {
            unsafe { libc::killpg(*g, libc::SIGKILL) };
        }
        for (pid, _) in &left {
            unsafe { libc::kill(*pid, libc::SIGKILL) };
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut left = owned_processes(&s.roots, &s.pids);
        while !left.is_empty() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(200));
            left = owned_processes(&s.roots, &s.pids);
        }
        let left: Vec<String> = left
            .into_iter()
            .map(|(pid, cmd)| format!("{pid} {}", cmd.chars().take(160).collect::<String>()))
            .collect();
        if left.is_empty()
            && let Some(dir) = &s.remove
        {
            let _ = std::fs::remove_dir_all(dir);
        }
        let mut st = self.lock();
        st.cleaned = true;
        st.left = left.clone();
        left
    }

    pub fn cleaned(&self) -> bool {
        self.lock().cleaned
    }
}

impl Drop for Guard {
    fn drop(&mut self) {
        self.cleanup();
    }
}

/// `root` as given and with links resolved (`/tmp` is `/private/tmp` on macOS; processes may name either).
fn spellings(root: &Path) -> Vec<PathBuf> {
    let mut out = vec![root.to_path_buf()];
    if let Ok(c) = std::fs::canonicalize(root)
        && c != root
    {
        out.push(c);
    }
    if let Ok(rest) = root.strip_prefix("/private/tmp") {
        out.push(Path::new("/tmp").join(rest));
    }
    out
}

/// How deep [`Guard::scan`] looks under an owned root (`<world>/runs/<run>/sub/M1/...` is about 8 levels).
const SCAN_DEPTH: usize = 12;
/// How long processes get between TERM and KILL.
const STOP_GRACE: Duration = Duration::from_secs(10);
/// Directories that never hold a server socket and can be large (installed releases, sources, repositories).
const SCAN_SKIP: &[&str] = &[".venv", ".git", "site-packages", "node_modules", "target", "__pycache__"];

fn walk(dir: &Path, depth: usize, f: &mut dyn FnMut(&Path)) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        let Ok(ft) = e.file_type() else { continue };
        if ft.is_symlink() {
            continue;
        }
        f(&p);
        if ft.is_dir() && SCAN_SKIP.iter().any(|s| e.file_name() == *s) {
            continue;
        }
        if ft.is_dir() && depth > 0 {
            walk(&p, depth - 1, f);
        }
    }
}

fn is_socket(p: &Path) -> bool {
    use std::os::unix::fs::FileTypeExt;
    std::fs::symlink_metadata(p).is_ok_and(|m| m.file_type().is_socket())
}

fn read_pid(p: &Path) -> Option<i32> {
    std::fs::read_to_string(p).ok()?.trim().parse().ok().filter(|v| *v > 1)
}

/// `ps -o lstart=` of `pid`, or None when it is not running.
pub fn start_time(pid: i32) -> Option<String> {
    if pid <= 1 {
        return None;
    }
    let out = Command::new("ps")
        .args(["-o", "lstart=", "-p", &pid.to_string()])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    let t = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (!t.is_empty()).then_some(t)
}

fn same_process(o: &Owned) -> bool {
    start_time(o.pid).as_deref() == Some(o.started.as_str())
}

/// A recorded process group may be signalled: its leader is still the recorded process, or the leader is gone
/// (a pid that still names a live group is never handed to a new process, so the group is still the run's).
fn group_safe(o: &Owned) -> bool {
    match start_time(o.pid) {
        None => true,
        Some(t) => t == o.started,
    }
}

/// `cmd` names `root` as a whole path (followed by `/`, white space, a quote or the end), so `/tmp/fmxu-ab` never
/// matches `/tmp/fmxu-abc`.
fn names(cmd: &str, root: &str) -> bool {
    let mut from = 0;
    while let Some(i) = cmd[from..].find(root) {
        let end = from + i + root.len();
        match cmd[end..].chars().next() {
            None => return true,
            Some(c) if c == '/' || c.is_whitespace() || c == '"' || c == '\'' => return true,
            _ => from = end,
        }
    }
    false
}

fn tmux_server_pid(bin: &Path, sock: &Path) -> Option<i32> {
    let out = Command::new(bin)
        .arg("-S")
        .arg(sock)
        .args(["display-message", "-p", "#{pid}"])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    String::from_utf8_lossy(&out.stdout).trim().parse().ok()
}

/// `SHUTDOWN NOSAVE` through the socket; waits until Redis closes the connection (it exits).
pub fn redis_shutdown(sock: &Path) {
    let Ok(mut s) = UnixStream::connect(sock) else { return };
    let _ = s.set_read_timeout(Some(Duration::from_secs(5)));
    let _ = s.write_all(b"*2\r\n$8\r\nSHUTDOWN\r\n$6\r\nNOSAVE\r\n");
    let mut buf = [0u8; 512];
    while let Ok(n) = s.read(&mut buf) {
        if n == 0 {
            break;
        }
    }
}

/// One fleetmux terminal server: `server.shutdown` with `force` through its control socket (it kills every
/// pane's process group), then `fleetmux reap --runtime` for whatever its pane record still names.
fn stop_terminal(dir: &Path, fleetmux: Option<&Path>) {
    let sock = dir.join("ctl.sock");
    if sock.exists()
        && let Ok(s) = UnixStream::connect(&sock)
    {
        let _ = s.set_read_timeout(Some(Duration::from_secs(30)));
        let mut w = match s.try_clone() {
            Ok(w) => w,
            Err(_) => return,
        };
        let mut r = BufReader::new(s);
        for req in [
            r#"{"id":1,"cmd":"hello"}"#,
            r#"{"id":2,"cmd":"server.shutdown","force":true}"#,
        ] {
            if w.write_all(format!("{req}\n").as_bytes()).is_err() {
                break;
            }
            let mut line = String::new();
            if r.read_line(&mut line).is_err() {
                break;
            }
        }
        let deadline = Instant::now() + Duration::from_secs(10);
        while sock.exists() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(200));
        }
    }
    if dir.join("panes.json").exists()
        && let Some(bin) = fleetmux
    {
        let _ = Command::new(bin)
            .arg("reap")
            .arg("--runtime")
            .arg(dir)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

/// This user's processes that are this run's: the command line names an owned directory, or the pid is recorded
/// (and still the same process). This process and its ancestors are never included.
pub fn owned_processes(roots: &[PathBuf], pids: &[Owned]) -> Vec<(i32, String)> {
    let me = std::process::id() as i32;
    let ancestors = ancestors(me);
    let uid = unsafe { libc::getuid() };
    let out = Command::new("ps")
        .args(["-ww", "-U", &uid.to_string(), "-o", "pid=,command="])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output();
    let Ok(out) = out else { return Vec::new() };
    let text = String::from_utf8_lossy(&out.stdout);
    let needles: Vec<String> = roots.iter().map(|r| r.display().to_string()).collect();
    let mut found = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        let Some((pid, cmd)) = line.split_once(char::is_whitespace) else { continue };
        let Ok(pid) = pid.parse::<i32>() else { continue };
        if pid == me || ancestors.contains(&pid) {
            continue;
        }
        let cmd = cmd.trim();
        let named = needles.iter().any(|n| names(cmd, n));
        let recorded = pids.iter().any(|o| o.pid == pid && same_process(o));
        if named || recorded {
            found.push((pid, cmd.to_string()));
        }
    }
    found
}

fn ancestors(pid: i32) -> Vec<i32> {
    let mut out = Vec::new();
    let mut cur = pid;
    for _ in 0..64 {
        let o = Command::new("ps")
            .args(["-o", "ppid=", "-p", &cur.to_string()])
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output();
        let Some(ppid) = o
            .ok()
            .and_then(|o| String::from_utf8_lossy(&o.stdout).trim().parse::<i32>().ok())
        else {
            break;
        };
        if ppid <= 1 {
            break;
        }
        out.push(ppid);
        cur = ppid;
    }
    out
}

// ------------------------------------------------------------------------------------------- signals and timeout

static SIGNALLED: AtomicI32 = AtomicI32::new(0);

extern "C" fn on_signal(sig: libc::c_int) {
    SIGNALLED.store(sig, Ordering::SeqCst);
}

/// The signal that arrived (SIGINT / SIGTERM / SIGHUP), if any.
pub fn signalled() -> Option<i32> {
    match SIGNALLED.load(Ordering::SeqCst) {
        0 => None,
        s => Some(s),
    }
}

/// Catch SIGINT, SIGTERM and SIGHUP: the handler only records the signal; the watcher thread started by
/// [`watch`] does the clean-up.
pub fn catch_signals() {
    for sig in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
        unsafe {
            let mut sa: libc::sigaction = std::mem::zeroed();
            sa.sa_sigaction = on_signal as *const () as usize;
            libc::sigemptyset(&mut sa.sa_mask);
            sa.sa_flags = 0;
            libc::sigaction(sig, &sa, std::ptr::null_mut());
        }
    }
}

/// Why the run has to stop now (a signal, or the deadline passed), if it has to.
pub fn stop_reason(deadline: Instant, budget_s: u64) -> Option<String> {
    if let Some(sig) = signalled() {
        return Some(format!("interrupted by signal {sig}"));
    }
    (Instant::now() >= deadline).then(|| format!("timed out: the whole run is limited to {budget_s} s"))
}

/// The backstop thread: when a signal arrives or the deadline plus `grace` passes and the main thread has not
/// finished its own clean-up by then, clean up here and end the process through `finish(code, reason)`.
pub fn watch(
    guard: Arc<Guard>,
    deadline: Instant,
    grace: Duration,
    finish: Arc<dyn Fn(i32, String) + Send + Sync>,
) {
    std::thread::Builder::new()
        .name("scenario-guard".into())
        .spawn(move || {
            let mut since: Option<Instant> = None;
            loop {
                std::thread::sleep(Duration::from_millis(100));
                if guard.cleaned() {
                    return;
                }
                let late = Instant::now() >= deadline;
                let sig = signalled();
                if sig.is_none() && !late {
                    continue;
                }
                let first = *since.get_or_insert_with(Instant::now);
                if Instant::now() < first + grace {
                    continue; // the main thread sees the same condition between operations and cleans up itself
                }
                let left = guard.cleanup();
                let (code, why) = match sig {
                    Some(s) => (130, format!("interrupted by signal {s}")),
                    None => (1, "timed out and the step did not return".to_string()),
                };
                let why = if left.is_empty() {
                    why
                } else {
                    format!("{why}; left running: {}", left.join("; "))
                };
                finish(code, why);
                return;
            }
        })
        .expect("spawn the scenario guard thread");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tool(name: &str) -> PathBuf {
        for dir in ["/opt/homebrew/opt/redis/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"] {
            let p = Path::new(dir).join(name);
            if p.is_file() {
                return p;
            }
        }
        if let Some(path) = std::env::var_os("PATH") {
            for d in std::env::split_paths(&path) {
                let p = d.join(name);
                if p.is_file() {
                    return p;
                }
            }
        }
        panic!("{name} is required by this test and was not found (a missing program is a failure, not a skip)");
    }

    fn tempdir(tag: &str) -> PathBuf {
        let tmpl = format!("/tmp/fmxg{tag}-XXXXXX\0");
        let mut buf = tmpl.into_bytes();
        let p = unsafe { libc::mkdtemp(buf.as_mut_ptr() as *mut libc::c_char) };
        assert!(!p.is_null());
        buf.pop();
        PathBuf::from(String::from_utf8(buf).unwrap())
    }

    fn alive(pid: i32) -> bool {
        start_time(pid).is_some()
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

    /// A Redis the way `start` runs one: daemonized, Unix socket only, pidfile next to the socket.
    fn redis_in(dir: &Path) -> i32 {
        std::fs::create_dir_all(dir).unwrap();
        let st = Command::new(tool("redis-server"))
            .args(["--port", "0", "--unixsocket"])
            .arg(dir.join("redis.sock"))
            .args(["--daemonize", "yes", "--save", "", "--appendonly", "no", "--pidfile"])
            .arg(dir.join("redis.pid"))
            .arg("--dir")
            .arg(dir)
            .arg("--logfile")
            .arg(dir.join("redis.log"))
            .status()
            .unwrap();
        assert!(st.success());
        let t = Instant::now() + Duration::from_secs(10);
        loop {
            if let Some(pid) = read_pid(&dir.join("redis.pid"))
                && dir.join("redis.sock").exists()
            {
                return pid;
            }
            assert!(Instant::now() < t, "redis did not come up in {}", dir.display());
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    /// A private tmux server (`-f /dev/null`) with its socket in `<root>/tmux-<uid>/default`.
    fn tmux_in(root: &Path) -> (PathBuf, i32) {
        let uid = unsafe { libc::getuid() };
        let d = root.join(format!("tmux-{uid}"));
        std::fs::create_dir_all(&d).unwrap();
        let sock = d.join("default");
        let st = Command::new(tool("tmux"))
            .args(["-f", "/dev/null", "-S"])
            .arg(&sock)
            .args(["new-session", "-d", "-s", "g", "sleep", "600"])
            .env_remove("TMUX")
            .status()
            .unwrap();
        assert!(st.success());
        let pid = tmux_server_pid(&tool("tmux"), &sock).expect("tmux server pid");
        (sock, pid)
    }

    /// The test's own record of what it started, stopped on drop whatever the code under test did (so a failing
    /// assertion, or the red phase, never leaves a server behind).
    #[derive(Default)]
    struct Started {
        redis: Vec<(PathBuf, i32)>,
        tmux: Vec<(PathBuf, i32)>,
        dirs: Vec<PathBuf>,
    }

    impl Started {
        fn redis(&mut self, dir: &Path) -> i32 {
            let pid = redis_in(dir);
            self.redis.push((dir.join("redis.sock"), pid));
            pid
        }
        fn tmux(&mut self, root: &Path) -> (PathBuf, i32) {
            let (s, pid) = tmux_in(root);
            self.tmux.push((s.clone(), pid));
            (s, pid)
        }
        fn dir(&mut self, tag: &str) -> PathBuf {
            let d = tempdir(tag);
            self.dirs.push(d.clone());
            d
        }
    }

    impl Drop for Started {
        fn drop(&mut self) {
            for (sock, pid) in &self.redis {
                redis_shutdown(sock);
                if alive(*pid) && !wait_dead(*pid) {
                    unsafe { libc::kill(*pid, libc::SIGKILL) };
                }
            }
            for (sock, pid) in &self.tmux {
                let _ = Command::new(tool("tmux")).arg("-S").arg(sock).arg("kill-server")
                    .stdout(Stdio::null()).stderr(Stdio::null()).status();
                if alive(*pid) && !wait_dead(*pid) {
                    unsafe { libc::kill(*pid, libc::SIGKILL) };
                }
            }
            for d in &self.dirs {
                let _ = std::fs::remove_dir_all(d);
            }
        }
    }

    fn guard_for(root: &Path) -> Arc<Guard> {
        let g = Guard::new();
        g.own_root(root);
        g.set_tools(Some(&tool("tmux")), None);
        g
    }

    #[test]
    fn cleanup_stops_the_owned_redis_and_tmux_servers() {
        let mut st = Started::default();
        let root = st.dir("a");
        let redis = st.redis(&root.join("r/run1"));
        let (_sock, tmux) = st.tmux(&root);
        let g = guard_for(&root);
        let left = g.cleanup();
        assert!(left.is_empty(), "left: {left:?}");
        assert!(wait_dead(redis), "redis {redis} still runs");
        assert!(wait_dead(tmux), "tmux {tmux} still runs");
    }

    #[test]
    fn cleanup_leaves_servers_outside_the_owned_root_alone() {
        let mut st = Started::default();
        let mine = st.dir("b");
        let other = st.dir("c");
        let my_redis = st.redis(&mine.join("r/x"));
        let their_redis = st.redis(&other.join("r/x"));
        let (_their_sock, their_tmux) = st.tmux(&other);
        let g = guard_for(&mine);
        g.cleanup();
        assert!(wait_dead(my_redis), "the owned Redis still runs");
        assert!(alive(their_redis), "a Redis outside the run's root was stopped");
        assert!(alive(their_tmux), "a tmux server outside the run's root was stopped");
    }

    #[test]
    fn the_owner_cleans_up_on_a_panic_while_the_watcher_holds_a_clone() {
        let mut st = Started::default();
        let root = st.dir("d");
        let redis = st.redis(&root.join("r/run1"));
        let (_s, tmux) = st.tmux(&root);
        let r2 = root.clone();
        let res = std::panic::catch_unwind(move || {
            let g = guard_for(&r2);
            let _watcher_clone = Arc::clone(&g); // what watch() keeps: the Guard itself is not dropped
            std::mem::forget(_watcher_clone);
            let _owner = Owner(g);
            panic!("a step panicked");
        });
        assert!(res.is_err());
        assert!(wait_dead(redis), "redis survived the panic");
        assert!(wait_dead(tmux), "tmux survived the panic");
    }

    /// Start `script` detached (its parent shell exits at once), returning the pid it echoes.
    fn detached(script: &str, arg: &Path) -> i32 {
        let out = Command::new("/bin/sh").arg("-c").arg(script).arg(arg).output().unwrap();
        String::from_utf8_lossy(&out.stdout).trim().parse().unwrap()
    }

    #[test]
    fn processes_naming_the_root_and_recorded_pids_are_stopped() {
        let mut st = Started::default();
        let root = st.dir("e");
        let marker = root.join("daemon-marker");
        std::fs::write(&marker, "").unwrap();
        // stands in for a daemon: its command line names the run's root
        let named = detached("tail -f \"$0\" >/dev/null 2>&1 & echo $!", &marker);
        // a recorded pid whose command line does not name the root
        let recorded = detached("sleep 601 >/dev/null 2>&1 & echo $!", &marker);
        struct Kill(Vec<i32>);
        impl Drop for Kill {
            fn drop(&mut self) {
                for p in &self.0 {
                    if alive(*p) {
                        unsafe { libc::kill(*p, libc::SIGKILL) };
                    }
                }
            }
        }
        let _kill = Kill(vec![named, recorded]);
        let g = guard_for(&root);
        assert!(g.own_pid(recorded));
        let left = g.cleanup();
        assert!(left.is_empty(), "left: {left:?}");
        assert!(wait_dead(named), "the process naming the root still runs");
        assert!(wait_dead(recorded), "the recorded pid still runs");
    }

    #[test]
    fn cleanup_is_idempotent() {
        let mut st = Started::default();
        let root = st.dir("f");
        let redis = st.redis(&root.join("r/run1"));
        let g = guard_for(&root);
        assert!(g.cleanup().is_empty());
        assert!(g.cleaned());
        assert!(g.cleanup().is_empty());
        assert!(wait_dead(redis));
    }

    #[test]
    fn a_root_is_named_only_as_a_whole_path() {
        assert!(names("tail -f /tmp/fmxu-ab/x", "/tmp/fmxu-ab"));
        assert!(names("daemon /tmp/fmxu-ab", "/tmp/fmxu-ab"));
        assert!(names("sh -c 'x /tmp/fmxu-ab'", "/tmp/fmxu-ab"));
        assert!(!names("tail -f /tmp/fmxu-abc/x", "/tmp/fmxu-ab"));
        assert!(names("a /tmp/fmxu-abc/x b /tmp/fmxu-ab/y", "/tmp/fmxu-ab"));
    }

    #[test]
    fn a_reused_pid_is_not_signalled() {
        let o = Owned {
            pid: std::process::id() as i32,
            started: "Thu Jan  1 00:00:00 1970".into(),
        };
        assert!(!same_process(&o));
    }
}
