//! M2.5 attach side of the web window (design.md §3.5.2–§3.5.6, PROTOCOL.md §6.3 / §15): start, reuse and stop
//! this client's `fleetmux-web`, the same-host check, the cross-host page copy and the prefix-`o` order.
//! Signatures from W0 (ctl-1, brief-m25 §3.1); W3 (attach-1) implements them.
//!
//! Hook points (wired in `attach::run`):
//! - every `ClientAction::Web(line)` (a `0x84`) goes to [`WebClient::on_server_line`]; the `0x04` frames it returns
//!   are sent to the server;
//! - `ClientAction::Open(path)` (`0x83`) goes to [`WebClient::on_open_key`], which follows [`open_order`]:
//!   window → `open` / `xdg-open` → show the path. The steps after the window come back from
//!   [`WebClient::take_fallbacks`] and run on the client as before (`clipboard::open_notice`);
//! - a window the person closes comes back as a generation number on the event channel of [`WebClient::new`] →
//!   [`WebClient::on_window_event`]; page exchanges that the server leaves unanswered end at
//!   [`WebClient::deadline`] → [`WebClient::on_deadline`];
//! - on exit / `C-b d` / `0x82` the client calls [`WebClient::shutdown`] (`quit`, close the lifeline, wait, kill
//!   only if it still runs), so a window is never left without its client. `Drop` does the same as a backstop.
//!
//! Why a window could not be used is said only on this client's status line ([`WebClient::take_notes`]), never
//! to the server (design.md §3.5.6). A headless host (no display) falls back silently, as before M2.5.
//!
//! Page exchange (PROTOCOL.md §6.3): before loading a `file://` page for `0x84 open` / `reload` or a `0x83`, the
//! client sends `0x04 {"seq"?,"op":"page","path":P,"have":…}`. `have` is the client's own copy of `P` (same host),
//! else the server's `{size, mtime}` recorded when it pushed `P` into `page/history.html` — never the local copy's
//! own stat, which would never match the server's — else `null`. A reply with `b64` is written to
//! `page/history.html` (0600) and loaded from there with the original query string and fragment; `web.goto` to
//! `P` is rewritten to that copy and `web.url` reports `P` again, so the server side only ever sees its own paths.

use std::ffi::{OsStr, OsString};
use std::fs::{DirBuilder, OpenOptions};
use std::io::{self, BufRead as _, BufReader, Read as _, Write as _};
use std::os::unix::ffi::{OsStrExt as _, OsStringExt as _};
use std::os::unix::fs::{
    DirBuilderExt as _, MetadataExt as _, OpenOptionsExt as _, PermissionsExt as _,
};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant, UNIX_EPOCH};

use fleetmux_proto::view::Frame;
use serde_json::{Value, json};
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender, unbounded_channel};

/// One step of the prefix-`o` / `0x83` order (PROTOCOL.md §6.3).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OpenStep {
    /// Show the page in this client's `fleetmux-web` window (start it if needed).
    Window,
    /// `open` (macOS) / `xdg-open` (elsewhere).
    Opener,
    /// Show the path on the status line.
    ShowPath,
}

/// What the client knows about its host when it decides how to open a page.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenEnv {
    /// A graphical session: macOS always; Linux when `DISPLAY` or `WAYLAND_DISPLAY` is set.
    pub display: bool,
    /// `fleetmux-web` as found in PROTOCOL.md §9.4 order, if found.
    pub web_bin: Option<PathBuf>,
    /// Its `--version` equals this client's.
    pub version_ok: bool,
}

/// The steps to try, in order: `[Window, Opener, ShowPath]` when a window can be started, else
/// `[Opener, ShowPath]` (design.md §3.5.6).
pub fn open_order(env: &OpenEnv) -> Vec<OpenStep> {
    if env.display && env.web_bin.is_some() && env.version_ok {
        vec![OpenStep::Window, OpenStep::Opener, OpenStep::ShowPath]
    } else {
        vec![OpenStep::Opener, OpenStep::ShowPath]
    }
}

/// Is there a graphical session on this host? `os` is `std::env::consts::OS`; `var` reads an environment variable.
pub fn has_display(os: &str, var: impl Fn(&str) -> Option<String>) -> bool {
    os == "macos"
        || ["DISPLAY", "WAYLAND_DISPLAY"]
            .iter()
            .any(|k| var(k).is_some_and(|v| !v.is_empty()))
}

/// This client's web runtime directory (PROTOCOL.md §15.2): `<root>/fleetmux-web/<first 24 chars of token>-<pid>/`.
pub fn web_dir(root: &Path, token: &str, pid: u32) -> PathBuf {
    let short: String = token.chars().take(24).collect();
    root.join("fleetmux-web").join(format!("{short}-{pid}"))
}

/// The `<root>` of [`web_dir`] (PROTOCOL.md §15.2): `$XDG_RUNTIME_DIR`, else `~/.local/state`; `None` without either.
pub fn web_root(var: impl Fn(&str) -> Option<String>) -> Option<PathBuf> {
    let set = |k: &str| var(k).filter(|v| !v.is_empty());
    set("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .or_else(|| set("HOME").map(|h| Path::new(&h).join(".local").join("state")))
}

/// Size and mtime (integer nanoseconds since the Unix epoch) of a regular file, as PROTOCOL.md §4.21 counts them.
fn stat_file(path: &Path) -> Option<(u64, i64)> {
    let meta = std::fs::metadata(path).ok()?;
    if !meta.is_file() {
        return None;
    }
    let mtime = meta.modified().ok()?.duration_since(UNIX_EPOCH).ok()?;
    Some((meta.len(), i64::try_from(mtime.as_nanos()).ok()?))
}

/// Does this host have the same copy of the page at `path` as the server (same `size`, same `mtime` in
/// nanoseconds since the Unix epoch)? A missing file is `false` (design.md §3.5.3).
pub fn same_host(path: &Path, size: u64, mtime: i64) -> bool {
    stat_file(path) == Some((size, mtime))
}

/// Write a page pushed from another host to `<web_dir>/page/history.html` (mode 0600, directory created 0700),
/// replacing any older copy; returns its path (design.md §3.5.3).
pub fn write_page(web_dir: &Path, bytes: &[u8]) -> io::Result<PathBuf> {
    let page_dir = web_dir.join("page");
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&page_dir)?;
    let path = page_dir.join("history.html");
    // Write beside it, then rename: a window reloading meanwhile sees the old page or the new one, never half.
    let tmp = page_dir.join(".history.html.tmp");
    let _ = std::fs::remove_file(&tmp);
    let mut f = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&tmp)?;
    f.write_all(bytes)?;
    f.set_permissions(std::fs::Permissions::from_mode(0o600))?;
    drop(f);
    std::fs::rename(&tmp, &path)?;
    Ok(path)
}

/// The window binary's name.
pub const WEB_BIN: &str = "fleetmux-web";
/// The last place PROTOCOL.md §9.4 looks for it.
pub const WEB_BIN_ENV: &str = "CMUX_FLEET_FLEETMUX_WEB";

/// `fleetmux-web` in PROTOCOL.md §9.4 order: the directory of the running `fleetmux` (`exe_dir`), then each
/// `PATH` entry, then `$CMUX_FLEET_FLEETMUX_WEB` (`env_bin`). Only an executable regular file counts.
pub fn find_web_bin(
    exe_dir: Option<&Path>,
    path_var: Option<&OsStr>,
    env_bin: Option<&OsStr>,
) -> Option<PathBuf> {
    let runnable = |p: &Path| {
        std::fs::metadata(p).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
    };
    exe_dir
        .map(|d| d.join(WEB_BIN))
        .into_iter()
        .chain(
            path_var
                .map(|p| std::env::split_paths(p).collect::<Vec<_>>())
                .unwrap_or_default()
                .into_iter()
                .filter(|d| !d.as_os_str().is_empty())
                .map(|d| d.join(WEB_BIN)),
        )
        .chain(env_bin.filter(|v| !v.is_empty()).map(PathBuf::from))
        .find(|p| runnable(p))
}

/// The `--version` line a matching `fleetmux-web` prints (PROTOCOL.md §15.1): this `fleetmux`'s version.
pub fn expected_version() -> String {
    format!(
        "fleetmux-web {} (protocol {})",
        env!("CARGO_PKG_VERSION"),
        fleetmux_proto::PROTOCOL
    )
}

/// How long `fleetmux-web --version` may take.
const VERSION_WAIT: Duration = Duration::from_secs(2);
/// Readiness: `web.sock` accepts within this (PROTOCOL.md §15.1).
const READY_WAIT: Duration = Duration::from_secs(3);
/// One `web.sock` request's reply.
const REPLY_WAIT: Duration = Duration::from_secs(2);
/// A page exchange left unanswered by the server ends here; below the server's 3 s for the whole command.
const PAGE_WAIT: Duration = Duration::from_millis(2500);
/// After `quit` and EOF on the lifeline the window has 1 s to exit (PROTOCOL.md §15.1); then it is killed.
const EXIT_WAIT: Duration = Duration::from_millis(1500);
/// `web.sock` path limit (PROTOCOL.md §15.2).
const MAX_SOCK_PATH: usize = if cfg!(target_os = "macos") { 103 } else { 107 };

/// Run `<bin> --version` (stdin closed, at most [`VERSION_WAIT`]) and compare with [`expected_version`].
fn check_version(bin: &Path) -> Result<(), String> {
    let mut child = Command::new(bin)
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("web: cannot run {}: {e}", bin.display()))?;
    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) if started.elapsed() < VERSION_WAIT => {
                std::thread::sleep(Duration::from_millis(20))
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("web: {} --version did not answer", bin.display()));
            }
        }
    };
    let mut out = String::new();
    if let Some(mut so) = child.stdout.take() {
        let _ = so.read_to_string(&mut out);
    }
    let got = out.lines().next().unwrap_or("").trim();
    let want = expected_version();
    if status.success() && got == want {
        Ok(())
    } else if got.is_empty() {
        Err(format!(
            "web: {} --version failed ({status}); want {want}",
            bin.display()
        ))
    } else {
        Err(format!(
            "web: fleetmux-web version differs: {got}; want {want}"
        ))
    }
}

/// `url` split before its query string / fragment: `("file:///r/history.html", "?x=1#top")`.
fn split_url(url: &str) -> (&str, &str) {
    let cut = url.find(['?', '#']).unwrap_or(url.len());
    url.split_at(cut)
}

/// Bytes kept as they are in a `file://` path; everything else is `%XX`.
fn plain(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b"-._~/!$&'()*+,;=:@".contains(&b)
}

/// The local path of a `file://` URL (`file:///p` or `file://localhost/p`), percent-decoded, without its query
/// string and fragment; `None` for any other URL.
fn file_path(url: &str) -> Option<PathBuf> {
    let (base, _) = split_url(url);
    let rest = base.strip_prefix("file://")?;
    let rest = rest.strip_prefix("localhost").unwrap_or(rest);
    if !rest.starts_with('/') {
        return None;
    }
    let raw = rest.as_bytes();
    let mut out = Vec::with_capacity(raw.len());
    let mut i = 0;
    while i < raw.len() {
        let hex = |c: u8| (c as char).to_digit(16);
        match (
            raw[i],
            raw.get(i + 1).copied().and_then(hex),
            raw.get(i + 2).copied().and_then(hex),
        ) {
            (b'%', Some(h), Some(l)) => {
                out.push((h * 16 + l) as u8);
                i += 3;
            }
            (b, _, _) => {
                out.push(b);
                i += 1;
            }
        }
    }
    Some(PathBuf::from(OsString::from_vec(out)))
}

/// `file://` + `path` (percent-encoded) + `suffix` (query string / fragment, kept as given).
fn file_url(path: &Path, suffix: &str) -> String {
    let mut url = String::from("file://");
    for &b in path.as_os_str().as_bytes() {
        if plain(b) {
            url.push(b as char);
        } else {
            url.push_str(&format!("%{b:02X}"));
        }
    }
    url.push_str(suffix);
    url
}

/// A `0x04` line.
fn reply_frame(v: Value) -> Frame {
    Frame::WebReply(v.to_string())
}

/// The reply to one `0x84` (`seq` echoed as the server sent it).
fn answer(seq: &Option<Value>, ok: bool, extra: Value) -> Frame {
    let mut v = json!({ "ok": ok });
    if let Some(s) = seq {
        v["seq"] = s.clone();
    }
    if let (Value::Object(m), Value::Object(e)) = (&mut v, extra) {
        m.extend(e);
    }
    reply_frame(v)
}

fn fail(seq: &Option<Value>, error: &str) -> Frame {
    answer(seq, false, json!({ "error": error }))
}

fn window_event(state: &str) -> Frame {
    reply_frame(json!({"event": "window", "state": state}))
}

/// Wait for our own child: up to [`EXIT_WAIT`] after its lifeline closed, then kill it. Never leaves a zombie.
fn reap(mut child: Child) {
    drop(child.stdin.take()); // EOF on the lifeline
    let started = Instant::now();
    while started.elapsed() < EXIT_WAIT {
        if let Ok(Some(_)) = child.try_wait() {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let _ = child.kill();
    let _ = child.wait();
}

/// The open `web.sock` connection: requests are written here; replies come from the reader thread.
#[derive(Debug)]
struct Window {
    conn: UnixStream,
    replies: mpsc::Receiver<String>,
    /// Replies still due for requests that timed out (fix-m25-1 D2). The window answers every request exactly
    /// once, in order (PROTOCOL.md §15.3), so the next this-many reply lines belong to those requests and are
    /// dropped instead of being taken as the reply of a later request.
    owed: usize,
}

/// What a pending page exchange is for.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Purpose {
    /// `0x84 open` (needs a reply).
    Open,
    /// `0x84 reload` (needs a reply).
    Reload,
    /// `0x83` / prefix `o` (no `seq`, no reply; falls back to the opener).
    Key,
    /// M4 `0x84 goto` (needs a reply) to a server page this client does not have: after `page.select` the
    /// server sends the new page's `history_url`, which a cross-host client has never received.
    Goto,
}

/// Where the `have` of a page request came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Have {
    /// The client's own file at the same path.
    Local,
    /// The page pushed earlier into `page/history.html`.
    Pushed,
    /// No copy.
    Nothing,
}

#[derive(Debug)]
struct Pending {
    seq: Option<Value>,
    purpose: Purpose,
    /// The page's path on the server's host.
    source: PathBuf,
    /// Query string / fragment of the URL to load.
    suffix: String,
    have: Have,
    deadline: Instant,
}

/// This client's window, if it has one: the `fleetmux-web` child (stdin = the lifeline pipe) and its directory.
#[derive(Debug, Default)]
pub struct WebClient {
    child: Option<Child>,
    dir: Option<PathBuf>,
    /// The server's runtime token (last component of `--runtime`), for [`web_dir`].
    token: String,
    /// A window-capable environment once found; recomputed while it is not.
    env: Option<OpenEnv>,
    win: Option<Window>,
    /// Bumped for every window started or stopped; stale close events carry an old number.
    generation: u64,
    events: Option<UnboundedSender<u64>>,
    pending: Vec<Pending>,
    /// The server path and the server's `(size, mtime)` of the page now in `page/history.html`.
    pushed: Option<(PathBuf, u64, i64)>,
    notes: Vec<String>,
    fallbacks: Vec<(String, Option<String>)>,
    /// `<root>` of [`web_dir`] when set (tests); else [`web_root`] from the environment.
    root: Option<PathBuf>,
    /// M2.6: frames for the server the caller takes with [`WebClient::take_frames`] (the GUI window's `open`).
    queued: Vec<Frame>,
    /// M4 (design m4 §4.2 查看协议): the server's view `ok` line said `"pages":1` ([`WebClient::set_pages`]).
    pages: bool,
    /// M4: `?fleet-jump=` URLs the M2.5 window reported (`{"event":"jump"}` on `web.sock`), waiting for
    /// [`WebClient::on_window_event`] with [`JUMP_WAKE`].
    jumps: std::sync::Arc<std::sync::Mutex<Vec<String>>>,
}

/// M4: the "generation" the `web.sock` reader sends on the event channel after it queued a jump. Generations count
/// up from 0 and never get here, so [`WebClient::on_window_event`] cannot take it for a window that went away.
pub const JUMP_WAKE: u64 = u64::MAX;

impl WebClient {
    /// A client for the server whose runtime directory is `runtime`, and the channel on which a window that went
    /// away reports its generation (feed it to [`WebClient::on_window_event`]).
    pub fn new(runtime: &Path) -> (WebClient, UnboundedReceiver<u64>) {
        let (tx, rx) = unbounded_channel();
        let token = runtime
            .file_name()
            .map(|t| t.to_string_lossy().into_owned())
            .unwrap_or_default();
        let mut client = WebClient::default();
        client.token = token;
        client.events = Some(tx);
        (client, rx)
    }

    /// One `0x84` line from the server (PROTOCOL.md §6.3): start / reuse the window for `open`, forward `goto` /
    /// `url` / `reload` / `quit` over `web.sock`, write a pushed page. Returns the `0x04` frames to send: the reply
    /// for its `seq`, plus `window` / `page` events.
    pub fn on_server_line(&mut self, line: &str) -> Vec<Frame> {
        let Ok(v) = serde_json::from_str::<Value>(line) else {
            return Vec::new(); // not ours to answer: no seq can be read
        };
        let seq = v.get("seq").filter(|s| !s.is_null()).cloned();
        let url = v.get("url").and_then(Value::as_str).map(str::to_string);
        match v.get("op").and_then(Value::as_str).unwrap_or("") {
            "open" => self.op_open(seq, url),
            "page" => self.op_page(seq, &v),
            "goto" => self.op_goto(seq, url),
            "url" => self.op_url(seq),
            "reload" => self.op_reload(seq),
            "quit" => {
                let had = self.win.is_some();
                self.shutdown();
                let mut out = vec![answer(&seq, true, json!({}))];
                if had {
                    out.push(window_event("closed"));
                }
                out
            }
            "eval" => vec![fail(&seq, "eval is not implemented")],
            other => vec![fail(&seq, &format!("unknown web op {other:?}"))],
        }
    }

    /// Prefix `o` / `0x83` (PROTOCOL.md §6.3): the window when [`open_order`] allows it, after the page exchange;
    /// otherwise (or when it fails) the opener and then the path, via [`WebClient::take_fallbacks`].
    pub fn on_open_key(&mut self, path: &str) -> Vec<Frame> {
        if path.is_empty() {
            self.fallbacks.push((String::new(), None));
            return Vec::new();
        }
        if let Err(why) = self.window_possible() {
            self.fallbacks.push((path.to_string(), why));
            return Vec::new();
        }
        let (raw, suffix) = split_url(path);
        let source = PathBuf::from(raw);
        vec![self.ask_page(None, Purpose::Key, source, suffix.to_string())]
    }

    /// The person closed the window (or it died): forget it and tell the server. Stale generations are ignored.
    /// M4: [`JUMP_WAKE`] instead hands the queued jumps to the server (`0x04 {"event":"jump","url"}`).
    pub fn on_window_event(&mut self, generation: u64) -> Vec<Frame> {
        if generation == JUMP_WAKE {
            let urls = std::mem::take(&mut *self.jumps.lock().unwrap_or_else(|p| p.into_inner()));
            return urls.iter().filter_map(|u| self.jump_frame(u)).collect();
        }
        if generation != self.generation || self.win.is_none() {
            return Vec::new();
        }
        self.drop_window();
        vec![window_event("closed")]
    }

    /// The earliest page-exchange deadline, if any exchange is waiting.
    pub fn deadline(&self) -> Option<Instant> {
        self.pending.iter().map(|p| p.deadline).min()
    }

    /// End every page exchange whose deadline passed without the server's page: load the local copy if there is
    /// one, else fail the command / fall back to the opener.
    pub fn on_deadline(&mut self, now: Instant) -> Vec<Frame> {
        let (due, keep): (Vec<_>, Vec<_>) = std::mem::take(&mut self.pending)
            .into_iter()
            .partition(|p| p.deadline <= now);
        self.pending = keep;
        due.into_iter().flat_map(|p| self.finish(p, None)).collect()
    }

    /// Why a window could not be used, for this client's status line only (design.md §3.5.6).
    pub fn take_notes(&mut self) -> Vec<String> {
        std::mem::take(&mut self.notes)
    }

    /// Paths for the next steps of the `0x83` order (opener, then show the path), each with the reason the window
    /// step was skipped, if it was skipped for a reason worth showing.
    pub fn take_fallbacks(&mut self) -> Vec<(String, Option<String>)> {
        std::mem::take(&mut self.fallbacks)
    }

    /// Does this client have a window now?
    pub fn has_window(&self) -> bool {
        self.win.is_some()
    }

    /// Stop the window: `quit` over `web.sock`, then close the lifeline and wait for the child.
    pub fn shutdown(&mut self) {
        self.pending.clear();
        if let Some(mut w) = self.win.take() {
            let _ = writeln!(w.conn, "{}", json!({"cmd": "quit"}));
            let _ = w.replies.recv_timeout(Duration::from_secs(1));
            let _ = w.conn.shutdown(std::net::Shutdown::Both);
        }
        self.generation += 1;
        if let Some(child) = self.child.take() {
            reap(child);
        }
    }

    // ---- internals ----

    /// `Ok` when a window can be used (one is running, or one can be started); else why not — `None` when the
    /// reason is simply "no display" (silent, as before M2.5).
    fn window_possible(&mut self) -> Result<(), Option<String>> {
        if self.win.is_some() {
            return Ok(());
        }
        if self.env.is_none() {
            let display = has_display(std::env::consts::OS, |k| std::env::var(k).ok());
            if !display {
                return Err(None);
            }
            let exe_dir = std::env::current_exe()
                .ok()
                .and_then(|e| e.parent().map(Path::to_path_buf));
            let bin = find_web_bin(
                exe_dir.as_deref(),
                std::env::var_os("PATH").as_deref(),
                std::env::var_os(WEB_BIN_ENV).as_deref(),
            )
            .ok_or_else(|| {
                Some(format!(
                    "web: fleetmux-web not found (next to fleetmux, on PATH, ${WEB_BIN_ENV})"
                ))
            })?;
            check_version(&bin).map_err(Some)?;
            self.env = Some(OpenEnv {
                display,
                web_bin: Some(bin),
                version_ok: true,
            });
        }
        let env = self.env.as_ref().expect("set above");
        if !open_order(env).contains(&OpenStep::Window) {
            return Err(None);
        }
        self.dir_path().map(|_| ()).map_err(Some)
    }

    /// The web runtime directory and its socket path, checked against the platform limit.
    fn dir_path(&self) -> Result<PathBuf, String> {
        let root = self
            .root
            .clone()
            .or_else(|| web_root(|k| std::env::var(k).ok()))
            .ok_or_else(|| String::from("web: neither XDG_RUNTIME_DIR nor HOME is set"))?;
        let dir = web_dir(&root, &self.token, std::process::id());
        let sock = dir.join("web.sock");
        let len = sock.as_os_str().len();
        if len > MAX_SOCK_PATH {
            return Err(format!(
                "web: socket path too long ({len} > {MAX_SOCK_PATH} bytes): {}",
                sock.display()
            ));
        }
        Ok(dir)
    }

    /// Create the web runtime directory (0700, ours only); returns it.
    fn ensure_dir(&mut self) -> Result<PathBuf, String> {
        if let Some(d) = &self.dir {
            return Ok(d.clone());
        }
        let dir = self.dir_path()?;
        let made = DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&dir)
            .and_then(|_| std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)))
            .and_then(|_| std::fs::metadata(&dir));
        match made {
            Ok(m) if m.uid() == unsafe { libc::geteuid() } => {
                self.dir = Some(dir.clone());
                Ok(dir)
            }
            Ok(_) => Err(format!("web: {} belongs to another user", dir.display())),
            Err(e) => Err(format!("web: cannot create {}: {e}", dir.display())),
        }
    }

    /// The window, started if needed (PROTOCOL.md §15.1): `--dir` / `--run-dir`, stdin = lifeline pipe,
    /// output to `web.log`, ready when `web.sock` accepts within 3 s. `Ok(true)` = just started.
    fn ensure_window(&mut self, run_dir: Option<&Path>) -> Result<bool, String> {
        if self.win.is_some() {
            return Ok(false);
        }
        self.window_possible()
            .map_err(|why| why.unwrap_or_else(|| "web: no display".into()))?;
        let bin = self
            .env
            .as_ref()
            .and_then(|e| e.web_bin.clone())
            .ok_or_else(|| String::from("web: fleetmux-web not found"))?;
        let dir = self.ensure_dir()?;
        let sock = dir.join("web.sock");
        let _ = std::fs::remove_file(&sock); // a stale socket in our own directory
        let log = OpenOptions::new()
            .append(true)
            .create(true)
            .mode(0o600)
            .open(dir.join("web.log"))
            .map_err(|e| format!("web: cannot open {}: {e}", dir.join("web.log").display()))?;
        let err_log = log.try_clone().map_err(|e| format!("web: web.log: {e}"))?;
        let run_dir = run_dir
            .map(Path::to_path_buf)
            .unwrap_or_else(|| dir.clone());
        let mut child = Command::new(&bin)
            .arg("--dir")
            .arg(&dir)
            .arg("--run-dir")
            .arg(&run_dir)
            .stdin(Stdio::piped())
            .stdout(log)
            .stderr(err_log)
            .spawn()
            .map_err(|e| format!("web: cannot start {}: {e}", bin.display()))?;
        let started = Instant::now();
        let conn = loop {
            if let Ok(Some(status)) = child.try_wait() {
                return Err(format!(
                    "web: fleetmux-web exited ({status}); see {}",
                    dir.join("web.log").display()
                ));
            }
            if let Ok(c) = UnixStream::connect(&sock) {
                break c;
            }
            if started.elapsed() >= READY_WAIT {
                let _ = child.kill();
                let _ = child.wait();
                self.env = None; // look again next time
                return Err(String::from("web: the window did not answer within 3 s"));
            }
            std::thread::sleep(Duration::from_millis(50));
        };
        self.generation += 1;
        let win = self
            .attach_conn(conn)
            .map_err(|e| format!("web: web.sock: {e}"))?;
        self.win = Some(win);
        self.child = Some(child);
        Ok(true)
    }

    /// Start the reader thread for a connected `web.sock`: replies go to the returned [`Window`], a `closed` event
    /// or EOF goes to the event channel with the current generation. M4: a `jump` event is queued and announced
    /// with [`JUMP_WAKE`] instead.
    fn attach_conn(&self, conn: UnixStream) -> io::Result<Window> {
        let reader = conn.try_clone()?;
        let (tx, replies) = mpsc::channel();
        let events = self.events.clone();
        let generation = self.generation;
        let jumps = self.jumps.clone();
        std::thread::Builder::new()
            .name("fleetmux-web-sock".into())
            .spawn(move || {
                for line in BufReader::new(reader).lines() {
                    let Ok(line) = line else { break };
                    let event = serde_json::from_str::<Value>(&line)
                        .ok()
                        .and_then(|v| v.get("event").cloned().map(|e| (e, v)));
                    match event {
                        Some((e, v)) if e == json!("jump") => {
                            if let Some(u) = v.get("url").and_then(Value::as_str) {
                                jumps.lock().unwrap_or_else(|p| p.into_inner()).push(u.to_string());
                                if let Some(ev) = &events {
                                    let _ = ev.send(JUMP_WAKE);
                                }
                            }
                        }
                        Some(_) => break, // `{"event":"window","state":"closed"}`: the window is going away
                        None => {
                            if tx.send(line).is_err() {
                                return;
                            }
                        }
                    }
                }
                if let Some(ev) = events {
                    let _ = ev.send(generation);
                }
            })?;
        Ok(Window {
            conn,
            replies,
            owed: 0,
        })
    }

    /// One `web.sock` request and its reply line (PROTOCOL.md §15.3). Replies are in request order, so the late
    /// replies of earlier timed-out requests ([`Window::owed`]) come first and are skipped; all of it within the
    /// one [`REPLY_WAIT`] of this request. A request that times out adds itself to `owed`.
    fn request(&mut self, req: Value) -> Result<Value, String> {
        let w = self.win.as_mut().ok_or_else(|| String::from("no window"))?;
        writeln!(w.conn, "{req}").map_err(|e| format!("web.sock: {e}"))?;
        let deadline = Instant::now() + REPLY_WAIT;
        let line = loop {
            let left = deadline.saturating_duration_since(Instant::now());
            match w.replies.recv_timeout(left) {
                Ok(_late) if w.owed > 0 => w.owed -= 1, // an earlier request's reply, not ours
                Ok(line) => break line,
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    w.owed += 1; // ours will still come; the next request skips it
                    return Err(String::from("the window did not answer"));
                }
                // The reader ended (window closed or gone): the close event is on its way to the client.
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return Err(String::from("the window did not answer"));
                }
            }
        };
        let v: Value =
            serde_json::from_str(&line).map_err(|e| format!("bad web.sock reply ({e}): {line}"))?;
        if v["ok"] == json!(true) {
            Ok(v)
        } else {
            Err(v["error"].as_str().unwrap_or("window refused").to_string())
        }
    }

    /// The window and its child are gone (closed by the person, or died): reap the child.
    fn drop_window(&mut self) {
        self.win = None;
        self.generation += 1;
        if let Some(child) = self.child.take() {
            reap(child);
        }
    }

    /// The local page file a pushed page lives in.
    fn page_file(&self) -> Option<PathBuf> {
        self.dir
            .as_ref()
            .map(|d| d.join("page").join("history.html"))
    }

    /// `0x04 {"seq"?,"op":"page","path","have"}` and the pending exchange it starts.
    fn ask_page(
        &mut self,
        seq: Option<Value>,
        purpose: Purpose,
        source: PathBuf,
        suffix: String,
    ) -> Frame {
        let (have, stat) = if let Some(s) = stat_file(&source) {
            (Have::Local, Some(s))
        } else {
            match &self.pushed {
                Some((p, size, mtime)) if *p == source => (Have::Pushed, Some((*size, *mtime))),
                _ => (Have::Nothing, None),
            }
        };
        let mut req = json!({
            "op": "page",
            "path": source.to_string_lossy(),
            "have": stat.map(|(size, mtime)| json!({"size": size, "mtime": mtime})),
        });
        if let Some(s) = &seq {
            req["seq"] = s.clone();
        }
        self.pending.push(Pending {
            seq,
            purpose,
            source,
            suffix,
            have,
            deadline: Instant::now() + PAGE_WAIT,
        });
        reply_frame(req)
    }

    fn op_open(&mut self, seq: Option<Value>, url: Option<String>) -> Vec<Frame> {
        let Some(url) = url else {
            return vec![fail(&seq, "open without a url")];
        };
        if let Err(why) = self.window_possible() {
            let why = why.unwrap_or_else(|| String::from("web: no display"));
            self.notes.push(why.clone());
            return vec![fail(&seq, &why)];
        }
        if let Some(source) = file_path(&url) {
            let suffix = split_url(&url).1.to_string();
            return vec![self.ask_page(seq, Purpose::Open, source, suffix)];
        }
        // Not a file (about:blank, http…): no page to exchange; the window's allow-list decides.
        self.show(seq, &url, None, true)
    }

    /// Bring up the window if needed and load `url` in it (`open` = also to the front); frames: `window open`
    /// when it was just started, then the reply (when `reply`).
    fn show(
        &mut self,
        seq: Option<Value>,
        url: &str,
        run_dir: Option<&Path>,
        reply: bool,
    ) -> Vec<Frame> {
        let mut out = Vec::new();
        match self.ensure_window(run_dir) {
            Ok(started) => {
                if started {
                    out.push(window_event("open"));
                }
                match self.request(self.open_req(url)) {
                    Ok(_) if reply => out.push(answer(&seq, true, json!({}))),
                    Ok(_) => {}
                    Err(e) if reply => out.push(fail(&seq, &e)),
                    Err(e) => self.notes.push(format!("web: {e}")),
                }
            }
            Err(why) => {
                self.notes.push(why.clone());
                if reply {
                    out.push(fail(&seq, &why));
                }
            }
        }
        out
    }

    fn op_page(&mut self, seq: Option<Value>, v: &Value) -> Vec<Frame> {
        let Some(i) = self.pending.iter().position(|p| p.seq == seq) else {
            return Vec::new(); // late (after its deadline) or not asked for
        };
        let p = self.pending.remove(i);
        let page = v.get("page").filter(|pg| pg.is_object());
        self.finish(p, page)
    }

    /// End one page exchange: decide which local file to load (the client's own `P`, or the pushed copy, freshly
    /// written when the server sent `b64`), then do what the exchange was for.
    fn finish(&mut self, p: Pending, page: Option<&Value>) -> Vec<Frame> {
        let target: Result<(PathBuf, bool), String> = match page {
            Some(pg) if pg.get("b64").and_then(Value::as_str).is_some() => {
                let b64 = pg["b64"].as_str().unwrap_or_default();
                let size = pg["size"].as_u64().unwrap_or(0);
                let mtime = pg["mtime"].as_i64().unwrap_or(0);
                fleetmux_proto::b64::decode(b64)
                    .map_err(|e| format!("bad page from the server: {e:?}"))
                    .and_then(|bytes| {
                        let dir = self.ensure_dir()?;
                        write_page(&dir, &bytes)
                            .map_err(|e| format!("web: cannot write the page: {e}"))
                    })
                    .map(|path| {
                        self.pushed = Some((p.source.clone(), size, mtime));
                        (path, true)
                    })
            }
            Some(_) => match p.have {
                Have::Local => Ok((p.source.clone(), false)),
                Have::Pushed => self
                    .page_file()
                    .map(|f| (f, true))
                    .ok_or_else(|| String::from("no pushed page")),
                Have::Nothing => Err(format!(
                    "the server sent no page for {}",
                    p.source.display()
                )),
            },
            None if stat_file(&p.source).is_some() => Ok((p.source.clone(), false)),
            None => Err(format!("page not available here: {}", p.source.display())),
        };
        let (file, pushed) = match target {
            Ok(t) => t,
            Err(e) => {
                return match p.purpose {
                    Purpose::Key => {
                        self.fallbacks
                            .push((p.source.to_string_lossy().into_owned(), None));
                        Vec::new()
                    }
                    _ => vec![fail(&p.seq, &e)],
                };
            }
        };
        let url = file_url(&file, &p.suffix);
        match p.purpose {
            Purpose::Reload => match self.request(json!({"cmd": "reload"})) {
                Ok(_) => vec![answer(&p.seq, true, json!({}))],
                Err(e) => vec![fail(&p.seq, &e)],
            },
            Purpose::Goto => match self.request(json!({"cmd": "goto", "url": url})) {
                Ok(_) => vec![answer(&p.seq, true, json!({}))],
                Err(e) => vec![fail(&p.seq, &e)],
            },
            Purpose::Open => {
                let run_dir = if pushed {
                    None
                } else {
                    file.parent().map(Path::to_path_buf)
                };
                self.show(p.seq, &url, run_dir.as_deref(), true)
            }
            Purpose::Key => {
                // The window step; when it fails, the next steps run on the local copy (the pushed one when it
                // came over), with the reason on the status line.
                let run_dir = if pushed {
                    None
                } else {
                    file.parent().map(Path::to_path_buf)
                };
                let local = file.to_string_lossy().into_owned();
                match self.ensure_window(run_dir.as_deref()) {
                    Ok(started) => match self.request(self.open_req(&url)) {
                        Ok(_) if started => vec![window_event("open")],
                        Ok(_) => Vec::new(),
                        Err(e) => {
                            self.fallbacks.push((local, Some(format!("web: {e}"))));
                            if started {
                                vec![window_event("open")]
                            } else {
                                Vec::new()
                            }
                        }
                    },
                    Err(why) => {
                        self.fallbacks.push((local, Some(why)));
                        Vec::new()
                    }
                }
            }
        }
    }

    /// `web.goto`: a URL for the server's page that this client shows as a pushed copy loads that copy, with the
    /// same query string and fragment.
    fn op_goto(&mut self, seq: Option<Value>, url: Option<String>) -> Vec<Frame> {
        let Some(url) = url else {
            return vec![fail(&seq, "goto without a url")];
        };
        if self.win.is_none() {
            return vec![fail(&seq, "no window")];
        }
        let local = match (file_path(&url), &self.pushed, self.page_file()) {
            (Some(p), Some((src, _, _)), Some(copy)) if p == *src && stat_file(&p).is_none() => {
                file_url(&copy, split_url(&url).1)
            }
            // M4 (server with pages): a page that is neither on this host nor the pushed one — the history page of
            // the fleet page just selected — is exchanged first and its pushed copy loaded (never the server
            // path, which does not exist here). Without pages the goto goes out as before.
            (Some(p), _, _) if self.pages && stat_file(&p).is_none() => {
                let suffix = split_url(&url).1.to_string();
                return vec![self.ask_page(seq, Purpose::Goto, p, suffix)];
            }
            _ => url,
        };
        match self.request(json!({"cmd": "goto", "url": local})) {
            Ok(_) => vec![answer(&seq, true, json!({}))],
            Err(e) => vec![fail(&seq, &e)],
        }
    }

    /// `web.url`: the window's URL; a pushed copy is reported as the server's path (query / fragment kept).
    fn op_url(&mut self, seq: Option<Value>) -> Vec<Frame> {
        if self.win.is_none() {
            return vec![fail(&seq, "no window")];
        }
        match self.request(json!({"cmd": "url"})) {
            Ok(v) => {
                let url = v["url"].as_str().unwrap_or_default().to_string();
                let url = match (file_path(&url), &self.pushed, self.page_file()) {
                    (Some(p), Some((src, _, _)), Some(copy)) if p == copy => {
                        file_url(src, split_url(&url).1)
                    }
                    _ => url,
                };
                vec![answer(&seq, true, json!({ "url": url }))]
            }
            Err(e) => vec![fail(&seq, &e)],
        }
    }

    /// `web.reload`: when the window shows a `file://` page (the client's own or the pushed copy), exchange the
    /// page first so a changed page is pushed and an unchanged one is not; then reload.
    fn op_reload(&mut self, seq: Option<Value>) -> Vec<Frame> {
        if self.win.is_none() {
            return vec![fail(&seq, "no window")];
        }
        let current = match self.request(json!({"cmd": "url"})) {
            Ok(v) => v["url"].as_str().unwrap_or_default().to_string(),
            Err(e) => return vec![fail(&seq, &e)],
        };
        let source = match (file_path(&current), &self.pushed, self.page_file()) {
            (Some(p), Some((src, _, _)), Some(copy)) if p == copy => Some(src.clone()),
            (Some(p), _, _) => Some(p),
            (None, _, _) => None,
        };
        match source {
            Some(src) => {
                vec![self.ask_page(seq, Purpose::Reload, src, split_url(&current).1.to_string())]
            }
            None => match self.request(json!({"cmd": "reload"})) {
                Ok(_) => vec![answer(&seq, true, json!({}))],
                Err(e) => vec![fail(&seq, &e)],
            },
        }
    }

    /// M4 (design m4 §4.2 查看协议): the server's view `ok` line carried `"pages":1`. From now on every `open` on
    /// `web.sock` says `"pages":1` too, so the window reports `?fleet-jump=` links instead of loading them, and the
    /// reports go to the server ([`WebClient::jump_frame`]).
    pub fn set_pages(&mut self, on: bool) {
        self.pages = on;
    }

    /// The `web.sock` `open` request for `url` (`"pages":1` once [`WebClient::set_pages`] said so).
    fn open_req(&self, url: &str) -> Value {
        if self.pages {
            json!({"cmd": "open", "url": url, "pages": 1})
        } else {
            json!({"cmd": "open", "url": url})
        }
    }

    /// M4: the `0x04 {"event":"jump","url"}` for a `?fleet-jump=` URL the window reported — the pushed copy in
    /// `page/` is named by the server's own path again (query kept), as in `web.url` — or `None` when the server has
    /// no pages (it would not know the event).
    pub fn jump_frame(&self, url: &str) -> Option<Frame> {
        if !self.pages {
            return None;
        }
        let url = match (file_path(url), &self.pushed, self.page_file()) {
            (Some(p), Some((src, _, _)), Some(copy)) if p == copy => file_url(src, split_url(url).1),
            _ => url.to_string(),
        };
        Some(reply_frame(json!({"event": "jump", "url": url})))
    }

    /// Test hook: use `conn` as the window's `web.sock` (no child process).
    #[cfg(test)]
    fn use_window(&mut self, conn: UnixStream, dir: PathBuf) {
        self.generation += 1;
        self.dir = Some(dir);
        self.env = Some(OpenEnv {
            display: true,
            web_bin: Some(PathBuf::from("/test/fleetmux-web")),
            version_ok: true,
        });
        let w = self.attach_conn(conn).expect("attach test window");
        self.win = Some(w);
    }
}

/// M2.6: events only a GUI window (`fleetmux-web --gui`) sends on `web.sock` (PROTOCOL.md §15.4).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GuiEvent {
    /// `{"event":"history_focus"}`: the person clicked into the embedded page → `pane.focus <history pane>`.
    HistoryFocus,
    /// M4 `{"event":"jump","url"}`: the embedded page followed a `?fleet-jump=` link and the window stopped it →
    /// [`WebClient::jump_frame`] to the server.
    Jump(String),
}

/// M2.6 additions (brief-m26 §3.1 / §3.4). In GUI mode the window is `fleetmux-web --gui`, started and reaped by
/// `gui.rs` (which holds its pipes); this client only owns its `web.sock`. Everything else — page exchange,
/// cross-host copy in `page/`, `web.goto` / `web.url` path rewriting, seq / timeout / late-reply handling — is the
/// M2.5 code as it is. A GUI client never starts an M2.5 window: its environment says "no window binary", so once
/// the GUI window is gone every window step fails instead of spawning one.
impl WebClient {
    /// The GUI window (already started by `gui.rs`, `web.sock` connected) becomes this client's window; reports
    /// `{"event":"window","state":"open"}` on the next `take_frames`; returns the channel of GUI-only events.
    pub fn adopt_gui(&mut self, conn: UnixStream, dir: PathBuf) -> UnboundedReceiver<GuiEvent> {
        let (tx, rx) = unbounded_channel();
        self.generation += 1;
        self.dir = Some(dir);
        self.env = Some(OpenEnv {
            display: true,
            web_bin: None,
            version_ok: false,
        });
        match self.attach_gui_conn(conn, tx) {
            Ok(w) => {
                self.win = Some(w);
                self.queued.push(window_event("open"));
            }
            Err(e) => {
                // No reader: the window counts as gone at once (the caller sees it on the event channel).
                self.notes.push(format!("web: web.sock: {e}"));
                if let Some(ev) = &self.events {
                    let _ = ev.send(self.generation);
                }
            }
        }
        rx
    }

    /// `web.sock` `scope {run_dir}`: the run directory whose pages the GUI window may show.
    pub fn set_scope(&mut self, run_dir: &Path) -> Result<(), String> {
        self.request(json!({"cmd": "scope", "run_dir": run_dir.to_string_lossy()}))
            .map(|_| ())
    }

    /// `web.sock` `focus`: give the keyboard to the embedded page.
    pub fn focus_page(&mut self) -> Result<(), String> {
        self.request(json!({"cmd": "focus"})).map(|_| ())
    }

    /// Frames queued for the server outside a reply (the GUI window's `{"event":"window","state":"open"}`).
    pub fn take_frames(&mut self) -> Vec<Frame> {
        std::mem::take(&mut self.queued)
    }

    /// GUI: load `history_url` into the embedded page through the same page exchange as prefix `o` (same host →
    /// the client's own file; cross-host → the pushed copy in `page/`). A failure comes back from
    /// [`WebClient::take_fallbacks`]; the GUI shows it as a notice and never runs the system opener for it.
    pub fn load_history(&mut self, url: &str) -> Vec<Frame> {
        match file_path(url) {
            Some(source) if self.win.is_some() => {
                let suffix = split_url(url).1.to_string();
                vec![self.ask_page(None, Purpose::Key, source, suffix)]
            }
            Some(_) => Vec::new(),
            None => {
                self.notes
                    .push(format!("web: history_url is not a file:// URL: {url}"));
                Vec::new()
            }
        }
    }

    /// GUI: create this client's web runtime directory (PROTOCOL.md §15.2: 0700, ours, socket path within the
    /// platform limit) before the window is started in it.
    pub(crate) fn gui_dir(&mut self) -> Result<PathBuf, String> {
        self.ensure_dir()
    }

    /// The reader thread of a GUI `web.sock`: replies go to the returned [`Window`]; `history_focus` goes to `gui`;
    /// the `window` event (`closed`) or EOF ends it and reports the current generation on the event channel.
    /// Other unsolicited lines are ignored.
    fn attach_gui_conn(
        &self,
        conn: UnixStream,
        gui: UnboundedSender<GuiEvent>,
    ) -> io::Result<Window> {
        let reader = conn.try_clone()?;
        let (tx, replies) = mpsc::channel();
        let events = self.events.clone();
        let generation = self.generation;
        std::thread::Builder::new()
            .name("fleetmux-gui-sock".into())
            .spawn(move || {
                for line in BufReader::new(reader).lines() {
                    let Ok(line) = line else { break };
                    let event = serde_json::from_str::<Value>(&line)
                        .ok()
                        .and_then(|v| v.get("event").and_then(Value::as_str).map(str::to_string));
                    match event.as_deref() {
                        Some("history_focus") => {
                            let _ = gui.send(GuiEvent::HistoryFocus);
                        }
                        // M4: a `?fleet-jump=` link the GUI window stopped (see `GuiEvent::Jump`).
                        Some("jump") => {
                            let url = serde_json::from_str::<Value>(&line).ok().and_then(|v| {
                                v.get("url").and_then(Value::as_str).map(str::to_string)
                            });
                            if let Some(u) = url {
                                let _ = gui.send(GuiEvent::Jump(u));
                            }
                        }
                        Some("window") => break,
                        Some(_) => {}
                        None => {
                            if tx.send(line).is_err() {
                                return;
                            }
                        }
                    }
                }
                if let Some(ev) = events {
                    let _ = ev.send(generation);
                }
            })?;
        Ok(Window {
            conn,
            replies,
            owed: 0,
        })
    }
}

/// M2.6 `fleetmux gui`: this host's window environment (PROTOCOL.md §9.4 / §9.5) — display, `fleetmux-web` in §9.4
/// order, its `--version` — and, when the version check failed, its reason.
pub(crate) fn gui_env() -> (OpenEnv, Option<String>) {
    let display = has_display(std::env::consts::OS, |k| std::env::var(k).ok());
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|e| e.parent().map(Path::to_path_buf));
    let web_bin = find_web_bin(
        exe_dir.as_deref(),
        std::env::var_os("PATH").as_deref(),
        std::env::var_os(WEB_BIN_ENV).as_deref(),
    );
    let check = match (&web_bin, display) {
        (Some(bin), true) => check_version(bin),
        _ => Err(String::new()),
    };
    let version_ok = check.is_ok();
    let why = check.err().filter(|e| !e.is_empty());
    (
        OpenEnv {
            display,
            web_bin,
            version_ok,
        },
        why,
    )
}

/// M2.6: the local path of a `file://` URL (percent-decoded, no query / fragment), as the page exchange reads it.
pub(crate) fn file_url_path(url: &str) -> Option<PathBuf> {
    file_path(url)
}

impl Drop for WebClient {
    fn drop(&mut self) {
        if self.win.is_some() || self.child.is_some() {
            self.shutdown();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        if let Some(root) = m4_test_root() {
            // the m4 gate (M56): a new directory under $FM4_TEST_ROOT, kept; an existing path is a failure
            static N: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
            let n = N.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            let d = m4_fresh(&root, format!("w3-{name}-{}-{n}", std::process::id()), 0);
            std::fs::create_dir(&d)
                .unwrap_or_else(|e| panic!("m4 evidence dir {} is not new: {e}", d.display()));
            return d;
        }
        let d = std::env::temp_dir().join(format!("fm-w3-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    /// `$FM4_TEST_ROOT` when the m4 gate runs these tests (`FLEETMUX_GATE=m4`, which always sets it), else `None`.
    fn m4_test_root() -> Option<PathBuf> {
        if std::env::var("FLEETMUX_GATE").as_deref() != Ok("m4") {
            return None;
        }
        let root = std::env::var_os("FM4_TEST_ROOT").filter(|v| !v.is_empty());
        Some(PathBuf::from(root.expect(
            "FLEETMUX_GATE=m4 needs FM4_TEST_ROOT (scripts/fleetmux_gate.sh sets it)",
        )))
    }

    /// The m4 gate's socket budget (M58, `scripts/fleetmux_gate.sh`): `M4E_ROOT` is at most 103 - `M4_SUFFIX_MAX` (64)
    /// = 39 bytes and `FM4_TEST_ROOT` is `$M4E_ROOT/r.XXXXXX` (9 more), so whatever root a run has, `/<name>` plus
    /// the longest socket below it must fit 103 - 39 - 9 = 55 bytes.
    const M4_BELOW_TEST_ROOT: usize = 103 - (103 - 64) - "/r.XXXXXX".len();

    /// Digits of a pid on this OS (macOS pids stay below 100000; Linux `pid_max` can reach 4194304).
    const PID_DIGITS: usize = if cfg!(target_os = "macos") { 5 } else { 7 };

    /// `<root>/<name>`, not yet existing, whose sockets (`sock_suffix` bytes below it) fit `sun_path` (103 bytes).
    fn m4_fresh(root: &Path, name: String, sock_suffix: usize) -> PathBuf {
        // M58: the name holds this process's pid; count it at the most digits this OS allows
        let pid_len = std::process::id().to_string().len();
        let worst = 1 + name.len() + PID_DIGITS.saturating_sub(pid_len) + sock_suffix;
        assert!(
            worst <= M4_BELOW_TEST_ROOT,
            "m4 evidence name {name} gives up to {worst} bytes below FM4_TEST_ROOT (> {M4_BELOW_TEST_ROOT})"
        );
        let p = root.join(name);
        let longest = p.as_os_str().len() + sock_suffix;
        assert!(
            longest <= 103,
            "m4 evidence path {} gives {longest}-byte socket paths (> 103)",
            p.display()
        );
        assert!(
            std::fs::symlink_metadata(&p).is_err(),
            "m4 evidence path {} already exists (never reused)",
            p.display()
        );
        println!("m4 evidence path: {}", p.display());
        p
    }

    /// The end of a test: outside the m4 gate its directory is removed; in the m4 gate it is kept (M56).
    fn done(d: &Path) {
        if m4_test_root().is_none() {
            std::fs::remove_dir_all(d).unwrap();
        }
    }

    fn json_of(f: &Frame) -> Value {
        match f {
            Frame::WebReply(s) => serde_json::from_str(s).unwrap(),
            other => panic!("not a 0x04: {other:?}"),
        }
    }

    /// The URL a fake window reports for `{"cmd":"url"}`; one per test (tests run in parallel).
    type UrlCell = std::sync::Arc<std::sync::Mutex<String>>;

    /// A fake `web.sock` peer: answers every request `{"ok":true}` (`url` with the test's own [`UrlCell`]),
    /// records the requests.
    fn fake_window() -> (UnixStream, mpsc::Receiver<Value>, UrlCell) {
        let (ours, theirs) = UnixStream::pair().unwrap();
        let (tx, rx) = mpsc::channel();
        let url_now = UrlCell::default();
        let current = url_now.clone();
        std::thread::spawn(move || {
            let mut w = theirs.try_clone().unwrap();
            for line in BufReader::new(theirs).lines() {
                let Ok(line) = line else { return };
                let req: Value = serde_json::from_str(&line).unwrap();
                let reply = match req["cmd"].as_str() {
                    Some("url") => json!({"ok": true, "url": current.lock().unwrap().clone()}),
                    _ => json!({"ok": true}),
                };
                let _ = writeln!(w, "{reply}");
                if tx.send(req).is_err() {
                    return;
                }
            }
        });
        (ours, rx, url_now)
    }

    #[test]
    fn find_web_bin_follows_exe_dir_then_path_then_env() {
        let d = tmp("find");
        let (a, b, c) = (d.join("a"), d.join("b"), d.join("c"));
        for x in [&a, &b, &c] {
            std::fs::create_dir_all(x).unwrap();
        }
        let put = |dir: &Path, mode: u32| {
            let p = dir.join(WEB_BIN);
            std::fs::write(&p, "#!/bin/sh\n").unwrap();
            std::fs::set_permissions(&p, std::fs::Permissions::from_mode(mode)).unwrap();
            p
        };
        let env_bin = put(&c, 0o755);
        let path = std::env::join_paths([&b]).unwrap();
        assert_eq!(
            find_web_bin(Some(&a), Some(&path), Some(env_bin.as_os_str())),
            Some(env_bin.clone())
        );
        let in_path = put(&b, 0o755);
        assert_eq!(
            find_web_bin(Some(&a), Some(&path), Some(env_bin.as_os_str())),
            Some(in_path.clone())
        );
        let next_to = put(&a, 0o755);
        assert_eq!(
            find_web_bin(Some(&a), Some(&path), Some(env_bin.as_os_str())),
            Some(next_to)
        );
        put(&a, 0o644); // not executable: skipped
        assert_eq!(find_web_bin(Some(&a), Some(&path), None), Some(in_path));
        assert_eq!(find_web_bin(None, None, None), None);
        done(&d);
    }

    #[test]
    fn version_must_match_exactly() {
        let d = tmp("ver");
        let script = |name: &str, body: &str| {
            let p = d.join(name);
            std::fs::write(&p, format!("#!/bin/sh\n{body}\n")).unwrap();
            std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
            p
        };
        let good = script("good", &format!("echo '{}'", expected_version()));
        assert_eq!(check_version(&good), Ok(()));
        let old = script("old", "echo 'fleetmux-web 0.0.1 (protocol 1)'");
        assert!(check_version(&old).unwrap_err().contains("version differs"));
        let broken = script("broken", "exit 101");
        assert!(check_version(&broken).is_err());
        done(&d);
    }

    #[test]
    fn web_root_prefers_xdg_runtime_dir_then_local_state() {
        let vars = |set: &'static [(&'static str, &'static str)]| {
            move |k: &str| {
                set.iter()
                    .find(|(n, _)| *n == k)
                    .map(|(_, v)| v.to_string())
            }
        };
        assert_eq!(
            web_root(vars(&[("XDG_RUNTIME_DIR", "/run/user/1"), ("HOME", "/h")])),
            Some(PathBuf::from("/run/user/1"))
        );
        assert_eq!(
            web_root(vars(&[("XDG_RUNTIME_DIR", ""), ("HOME", "/h")])),
            Some(PathBuf::from("/h/.local/state"))
        );
        assert_eq!(web_root(vars(&[])), None);
        // A token shorter than 24 characters is used whole.
        assert_eq!(
            web_dir(Path::new("/r"), "abc", 7),
            PathBuf::from("/r/fleetmux-web/abc-7")
        );
    }

    #[test]
    fn file_urls_keep_query_and_fragment_and_round_trip_paths() {
        assert_eq!(
            split_url("file:///r/h.html?fleet-jump=1&rel=child#top"),
            ("file:///r/h.html", "?fleet-jump=1&rel=child#top")
        );
        assert_eq!(
            file_path("file:///r/a%20b/h.html?x=1"),
            Some(PathBuf::from("/r/a b/h.html"))
        );
        assert_eq!(
            file_path("file://localhost/r/h.html"),
            Some(PathBuf::from("/r/h.html"))
        );
        assert_eq!(file_path("https://example.com/"), None);
        assert_eq!(file_path("about:blank"), None);
        let p = Path::new("/r/a b/%/h#.html");
        assert_eq!(file_path(&file_url(p, "?q=1")), Some(p.to_path_buf()));
        assert_eq!(file_url(Path::new("/r/h.html"), "#f"), "file:///r/h.html#f");
    }

    #[test]
    fn same_host_open_loads_the_local_file_and_reload_sends_have_without_retransfer() {
        let d = tmp("same");
        let page = d.join("history.html");
        std::fs::write(&page, "<html>same</html>").unwrap();
        let (size, mtime) = stat_file(&page).unwrap();
        let (conn, seen, url_now) = fake_window();
        let (mut c, _ev) = WebClient::new(Path::new("/rt/tok"));
        c.use_window(conn, d.join("web"));

        let url = format!("{}?x=1", file_url(&page, ""));
        let out = c.on_server_line(&json!({"seq": 7, "op": "open", "url": url}).to_string());
        let ask = json_of(&out[0]);
        assert_eq!(ask["op"], json!("page"));
        assert_eq!(ask["seq"], json!(7));
        assert_eq!(ask["have"], json!({"size": size, "mtime": mtime}));
        // The server's copy is the same file: no b64 → load the local path with the query string.
        let page_msg =
            json!({"seq": 7, "op": "page", "page": {"path": page, "size": size, "mtime": mtime}});
        let out = c.on_server_line(&page_msg.to_string());
        assert_eq!(json_of(&out[0]), json!({"seq": 7, "ok": true}));
        let req = seen.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(req, json!({"cmd": "open", "url": url}));

        // reload: the client reports the same `have`; no b64 comes back, the window reloads.
        *url_now.lock().unwrap() = url.clone();
        let out = c.on_server_line(&json!({"seq": 8, "op": "reload"}).to_string());
        let ask = json_of(&out[0]);
        assert_eq!(
            (ask["op"].clone(), ask["have"].clone()),
            (json!("page"), json!({"size": size, "mtime": mtime}))
        );
        let out = c.on_server_line(
            &json!({"seq": 8, "op": "page", "page": {"path": page, "size": size, "mtime": mtime}})
                .to_string(),
        );
        assert_eq!(json_of(&out[0]), json!({"seq": 8, "ok": true}));
        let reqs: Vec<Value> = seen.try_iter().collect();
        assert_eq!(reqs, vec![json!({"cmd": "url"}), json!({"cmd": "reload"})]);
        assert!(!d.join("web").join("page").exists(), "nothing was pushed");
        drop(c);
        done(&d);
    }

    #[test]
    fn cross_host_page_is_written_mapped_and_not_resent_when_unchanged() {
        let d = tmp("cross");
        let web = d.join("web");
        let (conn, seen, url_now) = fake_window();
        let (mut c, _ev) = WebClient::new(Path::new("/rt/tok"));
        c.use_window(conn, web.clone());
        let server_page = "/srv/run/history.html"; // not on this host
        let out = c.on_server_line(
            &json!({"seq": 1, "op": "open", "url": format!("file://{server_page}?fleet-jump=a#h")})
                .to_string(),
        );
        assert_eq!(json_of(&out[0])["have"], Value::Null);
        let b64 = fleetmux_proto::b64::encode(b"<html>pushed</html>");
        let out = c.on_server_line(
            &json!({"seq": 1, "op": "page", "page": {"path": server_page, "size": 19, "mtime": 1234, "b64": b64}})
                .to_string(),
        );
        assert_eq!(json_of(&out[0]), json!({"seq": 1, "ok": true}));
        let copy = web.join("page").join("history.html");
        assert_eq!(std::fs::read(&copy).unwrap(), b"<html>pushed</html>");
        assert_eq!(
            std::fs::metadata(&copy).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let local_url = format!("{}?fleet-jump=a#h", file_url(&copy, ""));
        assert_eq!(
            seen.recv_timeout(Duration::from_secs(2)).unwrap(),
            json!({"cmd": "open", "url": local_url})
        );

        // web.url reports the server's path, query and fragment kept.
        *url_now.lock().unwrap() = local_url.clone();
        let out = c.on_server_line(&json!({"seq": 2, "op": "url"}).to_string());
        assert_eq!(
            json_of(&out[0])["url"],
            json!(format!("file://{server_page}?fleet-jump=a#h"))
        );
        // web.goto to the server's page loads the copy.
        let out = c.on_server_line(
            &json!({"seq": 3, "op": "goto", "url": format!("file://{server_page}?x=2")})
                .to_string(),
        );
        assert_eq!(json_of(&out[0]), json!({"seq": 3, "ok": true}));
        let reqs: Vec<Value> = seen.try_iter().collect();
        assert_eq!(
            reqs.last().unwrap(),
            &json!({"cmd": "goto", "url": format!("{}?x=2", file_url(&copy, ""))})
        );

        // reload: `have` is the server's stat of the pushed copy; no b64 → the copy is not rewritten.
        let before = std::fs::metadata(&copy).unwrap().modified().unwrap();
        let out = c.on_server_line(&json!({"seq": 4, "op": "reload"}).to_string());
        let ask = json_of(&out[0]);
        assert_eq!(
            (ask["path"].clone(), ask["have"].clone()),
            (json!(server_page), json!({"size": 19, "mtime": 1234}))
        );
        let out = c.on_server_line(&json!({"seq": 4, "op": "page", "page": {"path": server_page, "size": 19, "mtime": 1234}}).to_string());
        assert_eq!(json_of(&out[0]), json!({"seq": 4, "ok": true}));
        assert_eq!(
            std::fs::metadata(&copy).unwrap().modified().unwrap(),
            before
        );
        drop(c);
        done(&d);
    }

    #[test]
    fn unanswered_page_exchange_ends_at_its_deadline() {
        let d = tmp("deadline");
        let (conn, _seen, _url) = fake_window();
        let (mut c, _ev) = WebClient::new(Path::new("/rt/tok"));
        c.use_window(conn, d.join("web"));
        let out = c.on_server_line(
            &json!({"seq": 5, "op": "open", "url": "file:///nowhere/history.html"}).to_string(),
        );
        assert_eq!(out.len(), 1);
        let due = c.deadline().expect("an exchange is waiting");
        assert!(c.on_deadline(due - Duration::from_millis(1)).is_empty());
        let out = c.on_deadline(due);
        let r = json_of(&out[0]);
        assert_eq!(
            (r["seq"].clone(), r["ok"].clone()),
            (json!(5), json!(false))
        );
        assert_eq!(c.deadline(), None);
        // prefix o: no seq; an unanswered exchange for a missing page falls back to the opener with the path.
        let out = c.on_open_key("/nowhere/history.html");
        assert!(json_of(&out[0]).get("seq").is_none());
        let due = c.deadline().unwrap();
        assert!(c.on_deadline(due).is_empty());
        assert_eq!(
            c.take_fallbacks(),
            vec![("/nowhere/history.html".to_string(), None)]
        );
        drop(c);
        done(&d);
    }

    #[test]
    fn window_ops_without_a_window_fail_and_quit_and_close_report_closed() {
        let (mut c, mut ev) = WebClient::new(Path::new("/rt/tok"));
        for op in ["goto", "url", "reload"] {
            let out =
                c.on_server_line(&json!({"seq": 1, "op": op, "url": "file:///x"}).to_string());
            assert_eq!(
                json_of(&out[0]),
                json!({"seq": 1, "ok": false, "error": "no window"})
            );
        }
        let out = c.on_server_line(&json!({"seq": 2, "op": "eval", "js": "1"}).to_string());
        assert_eq!(json_of(&out[0])["ok"], json!(false));

        let d = tmp("close");
        let (conn, _seen, _url) = fake_window();
        c.use_window(conn, d.clone());
        let out = c.on_server_line(&json!({"seq": 3, "op": "quit"}).to_string());
        assert_eq!(
            out.iter().map(json_of).collect::<Vec<_>>(),
            vec![
                json!({"seq": 3, "ok": true}),
                json!({"event": "window", "state": "closed"})
            ]
        );
        assert!(!c.has_window());

        // The person closes the window: the window says so on web.sock → one `closed` event to the server.
        let (ours, theirs) = UnixStream::pair().unwrap();
        c.use_window(ours, d.clone());
        let mut w = theirs.try_clone().unwrap();
        writeln!(w, "{}", json!({"event": "window", "state": "closed"})).unwrap();
        // The first (quit) window's reader also reported its EOF, with its older generation: ignored.
        let mut reported = Vec::new();
        let mut last = 0;
        while reported.is_empty() {
            last = ev.blocking_recv().expect("close event");
            reported = c.on_window_event(last);
        }
        assert_eq!(
            reported.iter().map(json_of).collect::<Vec<_>>(),
            vec![json!({"event": "window", "state": "closed"})]
        );
        assert!(c.on_window_event(last).is_empty(), "reported once");
        assert!(!c.has_window());
        drop(theirs);
        done(&d);
    }

    #[test]
    fn shutdown_quits_then_closes_the_lifeline_and_reaps_the_child() {
        // A stand-in window that exits on stdin EOF (the lifeline), like fleetmux-web.
        let mut c = WebClient::default();
        let child = Command::new("sh")
            .args(["-c", "cat >/dev/null"])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        let pid = child.id() as libc::pid_t;
        c.child = Some(child);
        let started = Instant::now();
        c.shutdown();
        assert!(started.elapsed() < EXIT_WAIT, "exited on EOF, not killed");
        assert!(c.child.is_none());
        // Reaped: the pid is no longer our child.
        assert_eq!(
            unsafe { libc::waitpid(pid, std::ptr::null_mut(), libc::WNOHANG) },
            -1
        );
    }
    /// The real start path with a stand-in `fleetmux-web` (Python, no GUI): `--dir` / `--run-dir`, directory 0700,
    /// readiness on `web.sock`, `open` → `window open` + reply, the person closing the window → `closed`, and the
    /// child reaped (no orphan). Skipped without `python3`.
    #[test]
    fn started_window_answers_on_web_sock_and_is_reaped() {
        if Command::new("python3")
            .arg("-c")
            .arg("1")
            .status()
            .map(|s| !s.success())
            .unwrap_or(true)
        {
            eprintln!("SKIP: no python3");
            return;
        }
        let d = tmp("start");
        let fake = d.join("fake-web");
        std::fs::write(
            &fake,
            "#!/usr/bin/env python3\n\
             import json, os, socket, sys, threading\n\
             a = sys.argv[1:]\n\
             d = a[a.index('--dir') + 1]; r = a[a.index('--run-dir') + 1]\n\
             open(os.path.join(d, 'args.json'), 'w').write(json.dumps({'run_dir': r, 'mode': oct(os.stat(d).st_mode & 0o777)}))\n\
             threading.Thread(target=lambda: (sys.stdin.read(), os._exit(0)), daemon=True).start()\n\
             s = socket.socket(socket.AF_UNIX); s.bind(os.path.join(d, 'web.sock')); s.listen(1)\n\
             c, _ = s.accept(); f = c.makefile('rw')\n\
             for line in f:\n\
             \x20   req = json.loads(line)\n\
             \x20   if req['cmd'] == 'open' and req['url'].endswith('#close'):\n\
             \x20       f.write(json.dumps({'ok': True}) + '\\n'); f.write(json.dumps({'event': 'window', 'state': 'closed'}) + '\\n'); f.flush(); os._exit(0)\n\
             \x20   f.write(json.dumps({'ok': True}) + '\\n'); f.flush()\n\
             \x20   if req['cmd'] == 'quit': os._exit(0)\n",
        )
        .unwrap();
        std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755)).unwrap();
        let page = d.join("run").join("history.html");
        std::fs::create_dir_all(page.parent().unwrap()).unwrap();
        std::fs::write(&page, "<html>p</html>").unwrap();

        let (mut c, mut ev) = WebClient::new(Path::new("/rt/0123456789abcdef0123456789abcdef"));
        // A short root: the macOS temp dir would push web.sock past the 103-byte limit. In the m4 gate (M56) a
        // fresh path under $FM4_TEST_ROOT (not created here, as before); below it the socket is
        // `fleetmux-web/<24 hex>-<pid>/web.sock`: 48 bytes + the pid (macOS: 5 digits, else up to 7).
        let pid_digits = if cfg!(target_os = "macos") { 5 } else { 7 };
        let root = match m4_test_root() {
            // M58: $FM4_TEST_ROOT itself, with no directory of our own (a `w3r<pid>` one gave 99 bytes and leaves no
            // room for 7-digit pids); the client's own leaf `fleetmux-web/<24>-<pid>` is per process and must be new
            Some(r) => {
                let below = "/fleetmux-web/".len() + 24 + 1 + pid_digits + "/web.sock".len();
                assert!(
                    below <= M4_BELOW_TEST_ROOT,
                    "{below} bytes below FM4_TEST_ROOT"
                );
                assert!(
                    r.as_os_str().len() + below <= 103,
                    "m4 root {} is too long",
                    r.display()
                );
                let leaf = r
                    .join("fleetmux-web")
                    .join(format!("0123456789abcdef01234567-{}", std::process::id()));
                assert!(
                    std::fs::symlink_metadata(&leaf).is_err(),
                    "m4 evidence path {} already exists (never reused)",
                    leaf.display()
                );
                println!("m4 evidence path: {}", leaf.display());
                r
            }
            None => {
                let root = PathBuf::from(format!("/tmp/fmw3-{}", std::process::id()));
                let _ = std::fs::remove_dir_all(&root);
                root
            }
        };
        c.root = Some(root.clone());
        c.env = Some(OpenEnv {
            display: true,
            web_bin: Some(fake),
            version_ok: true,
        });
        let url = file_url(&page, "#close");
        let ask = c.on_server_line(&json!({"seq": 1, "op": "open", "url": url}).to_string());
        assert_eq!(
            json_of(&ask[0])["op"],
            json!("page"),
            "{:?}",
            json_of(&ask[0])
        );
        let out = c.on_server_line(
            &json!({"seq": 1, "op": "page", "page": {"path": page, "size": 14, "mtime": 0}})
                .to_string(),
        );
        assert_eq!(
            out.iter().map(json_of).collect::<Vec<_>>(),
            vec![
                json!({"event": "window", "state": "open"}),
                json!({"seq": 1, "ok": true})
            ]
        );
        let dir = root
            .join("fleetmux-web")
            .join(format!("0123456789abcdef01234567-{}", std::process::id()));
        let args: Value =
            serde_json::from_str(&std::fs::read_to_string(dir.join("args.json")).unwrap()).unwrap();
        assert_eq!(
            args,
            json!({"run_dir": page.parent().unwrap(), "mode": "0o700"})
        );
        let pid = c.child.as_ref().unwrap().id() as libc::pid_t;
        // The page asked to be closed: the window says `closed` and exits; the client reports it once and reaps.
        let mut reported = Vec::new();
        while reported.is_empty() {
            reported = c.on_window_event(ev.blocking_recv().unwrap());
        }
        assert_eq!(
            json_of(&reported[0]),
            json!({"event": "window", "state": "closed"})
        );
        assert!(c.child.is_none());
        assert_eq!(
            unsafe { libc::waitpid(pid, std::ptr::null_mut(), libc::WNOHANG) },
            -1
        );
        done(&root);
        done(&d);
    }
    // ---- fix-m25-1 D2: a reply that arrives after its request timed out is never taken by a later request ----

    /// What a scripted fake window does with its n-th request (0-based): wait, then write these lines (a reply
    /// and/or events); `None` lines = close the connection after waiting.
    type Script = fn(usize, &Value) -> (Duration, Option<Vec<Value>>);

    /// A fake `web.sock` peer that follows `script` request by request (FIFO, PROTOCOL.md §15.3).
    fn scripted_window(script: Script) -> UnixStream {
        let (ours, theirs) = UnixStream::pair().unwrap();
        std::thread::spawn(move || {
            let mut w = theirs.try_clone().unwrap();
            for (n, line) in BufReader::new(theirs).lines().enumerate() {
                let Ok(line) = line else { return };
                let req: Value = serde_json::from_str(&line).unwrap();
                let (wait, out) = script(n, &req);
                std::thread::sleep(wait);
                match out {
                    Some(lines) => {
                        for l in lines {
                            if writeln!(w, "{l}").is_err() {
                                return;
                            }
                        }
                    }
                    None => {
                        let _ = w.shutdown(std::net::Shutdown::Both);
                        return;
                    }
                }
            }
        });
        ours
    }

    const OWN_URL: &str = "file:///own/history.html?x=1#h";

    #[test]
    fn late_reply_after_a_timeout_is_not_taken_by_the_next_request() {
        // Request 0 (goto) is answered 2.5 s later, past the client's 2 s wait; every later request at once.
        fn script(n: usize, req: &Value) -> (Duration, Option<Vec<Value>>) {
            let reply = match req["cmd"].as_str() {
                Some("url") => json!({"ok": true, "url": OWN_URL}),
                _ => json!({"ok": true}),
            };
            let wait = if n == 0 {
                Duration::from_millis(2500)
            } else {
                Duration::ZERO
            };
            (wait, Some(vec![reply]))
        }
        let d = tmp("late");
        let (mut c, _ev) = WebClient::new(Path::new("/rt/tok"));
        c.use_window(scripted_window(script), d.clone());

        let out = c.on_server_line(
            &json!({"seq": 1, "op": "goto", "url": "file:///r/a.html?x=1#h"}).to_string(),
        );
        assert_eq!(
            json_of(&out[0]),
            json!({"seq": 1, "ok": false, "error": "the window did not answer"})
        );
        // The goto's late `{"ok":true}` arrives while this url waits: it must not be taken as the url's reply.
        let started = Instant::now();
        let out = c.on_server_line(&json!({"seq": 2, "op": "url"}).to_string());
        assert_eq!(
            json_of(&out[0]),
            json!({"seq": 2, "ok": true, "url": OWN_URL})
        );
        assert!(
            started.elapsed() < REPLY_WAIT,
            "still within the 2 s wait: {:?}",
            started.elapsed()
        );
        // And nothing is shifted afterwards: goto gets its own ok, url its own URL.
        let out = c.on_server_line(
            &json!({"seq": 3, "op": "goto", "url": "file:///r/a.html"}).to_string(),
        );
        assert_eq!(json_of(&out[0]), json!({"seq": 3, "ok": true}));
        let out = c.on_server_line(&json!({"seq": 4, "op": "url"}).to_string());
        assert_eq!(
            json_of(&out[0]),
            json!({"seq": 4, "ok": true, "url": OWN_URL})
        );
        drop(c);
        done(&d);
    }

    #[test]
    fn late_reply_then_window_closed_reports_closed_once_and_nothing_stale() {
        // Request 0 is answered late, then the person closes the window: `closed`, then EOF.
        fn script(_: usize, _: &Value) -> (Duration, Option<Vec<Value>>) {
            (
                Duration::from_millis(2500),
                Some(vec![
                    json!({"ok": true}),
                    json!({"event": "window", "state": "closed"}),
                ]),
            )
        }
        let d = tmp("late-closed");
        let (mut c, mut ev) = WebClient::new(Path::new("/rt/tok"));
        c.use_window(scripted_window(script), d.clone());
        let out = c.on_server_line(
            &json!({"seq": 1, "op": "goto", "url": "file:///r/a.html"}).to_string(),
        );
        assert_eq!(json_of(&out[0])["ok"], json!(false));
        let mut reported = Vec::new();
        while reported.is_empty() {
            reported = c.on_window_event(ev.blocking_recv().expect("close event"));
        }
        assert_eq!(
            reported.iter().map(json_of).collect::<Vec<_>>(),
            vec![json!({"event": "window", "state": "closed"})]
        );
        let out = c.on_server_line(&json!({"seq": 2, "op": "url"}).to_string());
        assert_eq!(
            json_of(&out[0]),
            json!({"seq": 2, "ok": false, "error": "no window"})
        );
        done(&d);
    }

    #[test]
    fn window_closing_while_a_reply_is_still_owed_fails_the_next_request_fast() {
        // Request 0 never gets a reply: 2.5 s later the window says `closed` and goes away.
        fn script(_: usize, _: &Value) -> (Duration, Option<Vec<Value>>) {
            (
                Duration::from_millis(2500),
                Some(vec![json!({"event": "window", "state": "closed"})]),
            )
        }
        let d = tmp("owed-closed");
        let (mut c, mut ev) = WebClient::new(Path::new("/rt/tok"));
        c.use_window(scripted_window(script), d.clone());
        let out = c.on_server_line(
            &json!({"seq": 1, "op": "goto", "url": "file:///r/a.html"}).to_string(),
        );
        assert_eq!(json_of(&out[0])["ok"], json!(false));
        // This url waits for its own reply behind the owed one; the window closes meanwhile: an error, not a
        // stale or invented reply, and well before another full 2 s.
        let started = Instant::now();
        let out = c.on_server_line(&json!({"seq": 2, "op": "url"}).to_string());
        let r = json_of(&out[0]);
        assert_eq!(
            (r["seq"].clone(), r["ok"].clone()),
            (json!(2), json!(false)),
            "{r}"
        );
        assert!(
            started.elapsed() < Duration::from_millis(1500),
            "{:?}",
            started.elapsed()
        );
        let mut reported = Vec::new();
        while reported.is_empty() {
            reported = c.on_window_event(ev.blocking_recv().expect("close event"));
        }
        assert_eq!(
            json_of(&reported[0]),
            json!({"event": "window", "state": "closed"})
        );
        done(&d);
    }

    /// M4 (design m4 §4.2 查看协议): after `set_pages(true)` the window's `open` says `"pages":1`, and a `jump`
    /// event from the window reaches the server as `0x04 {"event":"jump","url"}` (the window stays: the reader
    /// does not take the event for a close). Without pages there is no jump frame.
    #[test]
    fn m4_open_says_pages_and_a_window_jump_goes_to_the_server() {
        let d = tmp("m4jump");
        let page = d.join("history.html");
        std::fs::write(&page, "<html>root</html>").unwrap();
        let (size, mtime) = stat_file(&page).unwrap();
        let (ours, theirs) = UnixStream::pair().unwrap();
        let mut events_out = theirs.try_clone().unwrap();
        let (tx, seen) = mpsc::channel();
        std::thread::spawn(move || {
            let mut w = theirs.try_clone().unwrap();
            for line in BufReader::new(theirs).lines() {
                let Ok(line) = line else { return };
                let _ = writeln!(w, "{}", json!({"ok": true}));
                if tx.send(serde_json::from_str::<Value>(&line).unwrap()).is_err() {
                    return;
                }
            }
        });
        let (mut c, mut ev) = WebClient::new(Path::new("/rt/tok"));
        assert!(c.jump_frame("file:///r/h.html?fleet-jump=a").is_none(), "no pages, no jump frame");
        c.set_pages(true);
        c.use_window(ours, d.join("web"));
        let url = file_url(&page, "");
        c.on_server_line(&json!({"seq": 1, "op": "open", "url": url}).to_string());
        let msg = json!({"seq": 1, "op": "page", "page": {"path": page, "size": size, "mtime": mtime}});
        c.on_server_line(&msg.to_string());
        let req = seen.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(req, json!({"cmd": "open", "url": url, "pages": 1}));

        let jump = format!("{url}?fleet-jump=k7-1&rel=child&to=file%3A%2F%2F%2Fr%2Fsub%2FM0%2Fhistory.html");
        writeln!(events_out, "{}", json!({"event": "jump", "url": jump})).unwrap();
        assert_eq!(ev.blocking_recv(), Some(JUMP_WAKE));
        let out = c.on_window_event(JUMP_WAKE);
        assert_eq!(out.iter().map(json_of).collect::<Vec<_>>(), vec![json!({"event": "jump", "url": jump})]);
        assert!(c.has_window(), "a jump event is not a closed window");
    }

    /// M4 cross-host repro (supervisor M26): a fake window over a socket pair records every `web.sock` request and
    /// answers `url` with what it last loaded; `events` writes the window's own event lines.
    fn xhost_window() -> (UnixStream, mpsc::Receiver<Value>, UnixStream) {
        let (ours, theirs) = UnixStream::pair().unwrap();
        let events = theirs.try_clone().unwrap();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let mut w = theirs.try_clone().unwrap();
            let mut now = String::from("about:blank");
            for line in BufReader::new(theirs).lines() {
                let Ok(line) = line else { return };
                let req: Value = serde_json::from_str(&line).unwrap();
                if let Some(u) = req["url"].as_str() {
                    now = u.to_string();
                }
                let reply = match req["cmd"].as_str() {
                    Some("url") => json!({"ok": true, "url": now}),
                    _ => json!({"ok": true}),
                };
                let _ = writeln!(w, "{reply}");
                if tx.send(req).is_err() {
                    return;
                }
            }
        });
        (ours, rx, events)
    }

    /// The server's side of one page exchange: answer `ask` (a `0x04 {"op":"page"}`) with `body` as `b64`.
    fn push_page(c: &mut WebClient, ask: &Frame, body: &[u8]) -> Vec<Frame> {
        let ask = json_of(ask);
        assert_eq!(ask["op"], json!("page"), "{ask}");
        let mut reply = json!({"op": "page", "page": {"path": ask["path"], "size": body.len(),
            "mtime": 1_800_000_000 + body.len() as i64, "b64": fleetmux_proto::b64::encode(body)}});
        if let Some(seq) = ask.get("seq") {
            reply["seq"] = seq.clone();
        }
        c.on_server_line(&reply.to_string())
    }

    /// Server paths that do not exist on this (the viewer's) host.
    fn server_pages(tag: &str) -> (String, String) {
        let run = format!("/m4-srv-{tag}-{}/run", std::process::id());
        assert!(stat_file(Path::new(&run)).is_none(), "{run} must not exist here");
        (format!("{run}/history.html"), format!("{run}/sub/M0/history.html"))
    }

    fn jump_from(copy_url: &str, rel: &str, to: &str) -> String {
        let to = to.replace(':', "%3A").replace('/', "%2F");
        format!("{copy_url}?fleet-jump=k7-1&rel={rel}&to={to}")
    }

    /// M2.5 window, cross host: `page.select` to a child page never pushed before → the client exchanges that page
    /// and loads the **pushed copy** (never the server path); the reclaimed child's jump back to the root reaches
    /// the server under the server's own path; the server's `goto` back to the root pushes the root page again.
    #[test]
    fn m4_cross_host_m25_window_follows_page_select_and_jumps_back() {
        let d = tmp("m4x25");
        let web = d.join("web");
        let copy = web.join("page").join("history.html");
        let copy_url = file_url(&copy, "");
        let (root, child) = server_pages("m25");
        let (conn, seen, mut events) = xhost_window();
        let (mut c, mut ev) = WebClient::new(Path::new("/rt/tok"));
        c.set_pages(true);
        c.use_window(conn, web.clone());

        // web.open at start: the root page comes over and the copy loads.
        let out = c.on_server_line(&json!({"seq": 1, "op": "open", "url": format!("file://{root}")}).to_string());
        assert_eq!(json_of(&out[0])["have"], Value::Null);
        let out = push_page(&mut c, &out[0], b"<html>ROOT</html>");
        assert_eq!(json_of(&out[0]), json!({"seq": 1, "ok": true}));
        assert_eq!(seen.recv_timeout(Duration::from_secs(2)).unwrap(),
            json!({"cmd": "open", "url": copy_url, "pages": 1}));

        // page.select M0 → 0x84 goto <child history_url>, a page this client never got.
        let out = c.on_server_line(&json!({"seq": 2, "op": "goto", "url": format!("file://{child}")}).to_string());
        let ask = json_of(&out[0]);
        assert_eq!((ask["op"].clone(), ask["path"].clone(), ask["have"].clone()),
            (json!("page"), json!(child), Value::Null),
            "a goto to a server page that is not on this host must be exchanged first: {out:?}");
        let out = push_page(&mut c, &out[0], b"<html>CHILD</html>");
        assert_eq!(json_of(&out[0]), json!({"seq": 2, "ok": true}));
        let req = seen.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(req, json!({"cmd": "goto", "url": copy_url}), "the window loads the pushed copy");
        assert_eq!(std::fs::read(&copy).unwrap(), b"<html>CHILD</html>");
        let out = c.on_server_line(&json!({"seq": 3, "op": "url"}).to_string());
        assert_eq!(json_of(&out[0])["url"], json!(format!("file://{child}")), "web.url names the server path");
        let _ = seen.recv_timeout(Duration::from_secs(2));

        // The child is reclaimed; its page links back to the root: the window reports the jump.
        let jump = jump_from(&copy_url, "parent", &format!("file://{root}"));
        writeln!(events, "{}", json!({"event": "jump", "url": jump})).unwrap();
        assert_eq!(ev.blocking_recv(), Some(JUMP_WAKE));
        let suffix = split_url(&jump).1.to_string();
        assert_eq!(c.on_window_event(JUMP_WAKE).iter().map(json_of).collect::<Vec<_>>(),
            vec![json!({"event": "jump", "url": format!("file://{child}{suffix}")})],
            "the jump names the server's path of the page shown, never the local copy");

        // The server selects the root → goto <root history_url>: pushed again (the copy now holds the child).
        let out = c.on_server_line(&json!({"seq": 4, "op": "goto", "url": format!("file://{root}")}).to_string());
        let ask = json_of(&out[0]);
        assert_eq!((ask["path"].clone(), ask["have"].clone()), (json!(root), Value::Null), "{ask}");
        let out = push_page(&mut c, &out[0], b"<html>ROOT2</html>");
        assert_eq!(json_of(&out[0]), json!({"seq": 4, "ok": true}));
        assert_eq!(seen.recv_timeout(Duration::from_secs(2)).unwrap(), json!({"cmd": "goto", "url": copy_url}));
        assert_eq!(std::fs::read(&copy).unwrap(), b"<html>ROOT2</html>");
        assert!(c.has_window());
    }

    /// GUI client, cross host: the same switch as the M2.5 test, driven the way `attach/gui.rs` drives it — the
    /// `0x85` url of another directory goes through `load_history` (scope + page exchange) and the server's `goto`
    /// after `page.select` through `on_server_line`; both load the pushed copy, never the server path. A jump from
    /// the reclaimed child (`GuiEvent::Jump`, the window reports `file://` of its `fleetmux://page/` URL) reaches the
    /// server under the server's path.
    #[test]
    fn m4_cross_host_gui_follows_page_select_and_jumps_back() {
        let d = tmp("m4xgui");
        let web = d.join("web");
        let copy = web.join("page").join("history.html");
        let copy_url = file_url(&copy, "");
        let (root, child) = server_pages("gui");
        let (conn, seen, mut events) = xhost_window();
        let (mut c, _ev) = WebClient::new(Path::new("/rt/tok"));
        let mut gui = c.adopt_gui(conn, web.clone());
        c.set_pages(true);

        let out = c.load_history(&format!("file://{root}"));
        push_page(&mut c, &out[0], b"<html>ROOT</html>");
        assert_eq!(seen.recv_timeout(Duration::from_secs(2)).unwrap(),
            json!({"cmd": "open", "url": copy_url, "pages": 1}));

        // page.select M0: the 0x84 goto and the 0x85 with the child's url (another directory) both arrive.
        let out = c.on_server_line(&json!({"seq": 7, "op": "goto", "url": format!("file://{child}")}).to_string());
        let ask = json_of(&out[0]);
        assert_eq!((ask["op"].clone(), ask["path"].clone()), (json!("page"), json!(child)), "{out:?}");
        let out = push_page(&mut c, &out[0], b"<html>CHILD</html>");
        assert_eq!(json_of(&out[0]), json!({"seq": 7, "ok": true}));
        assert_eq!(seen.recv_timeout(Duration::from_secs(2)).unwrap(), json!({"cmd": "goto", "url": copy_url}));
        let out = c.load_history(&format!("file://{child}"));
        push_page(&mut c, &out[0], b"<html>CHILD</html>");
        assert_eq!(seen.recv_timeout(Duration::from_secs(2)).unwrap(),
            json!({"cmd": "open", "url": copy_url, "pages": 1}));
        assert_eq!(std::fs::read(&copy).unwrap(), b"<html>CHILD</html>");

        let jump = jump_from(&copy_url, "parent", &format!("file://{root}"));
        writeln!(events, "{}", json!({"event": "jump", "url": jump})).unwrap();
        let got = gui.blocking_recv();
        assert_eq!(got, Some(GuiEvent::Jump(jump.clone())));
        let suffix = split_url(&jump).1.to_string();
        assert_eq!(c.jump_frame(&jump).map(|f| json_of(&f)),
            Some(json!({"event": "jump", "url": format!("file://{child}{suffix}")})));

        let out = c.on_server_line(&json!({"seq": 8, "op": "goto", "url": format!("file://{root}")}).to_string());
        assert_eq!(json_of(&out[0])["path"], json!(root), "{out:?}");
        push_page(&mut c, &out[0], b"<html>ROOT2</html>");
        assert_eq!(seen.recv_timeout(Duration::from_secs(2)).unwrap(), json!({"cmd": "goto", "url": copy_url}));
        assert_eq!(std::fs::read(&copy).unwrap(), b"<html>ROOT2</html>");
    }
}
