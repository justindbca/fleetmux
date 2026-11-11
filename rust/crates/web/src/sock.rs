//! `web.sock` (PROTOCOL.md §15.2–§15.3): the window's only control channel, used by the attach client that
//! started it. Directory 0700, socket 0600, peer uid = own uid, path length checked; one JSON line per request,
//! one reply line each. The accept loop runs on a background thread and hands each command to the main thread
//! through the event-loop proxy (design.md §3.5.2: UI calls only on the main thread).
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt, FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use serde_json::{Value, json};
use tao::event_loop::EventLoopProxy;

use crate::window::UserEvent;

/// The socket's file name inside the web runtime directory.
pub const SOCK_NAME: &str = "web.sock";

/// Longest socket path this platform takes (`sockaddr_un.sun_path` minus the NUL; PROTOCOL.md §1.1).
pub const MAX_SOCK_PATH: usize = if cfg!(target_os = "macos") { 103 } else { 107 };

/// Longest request line (PROTOCOL.md §15.3).
const LINE_MAX: u64 = 1024 * 1024;

/// How long a connection waits for the main thread to answer one command. Loads answer when the page has
/// finished (or after the window's own 3 s load deadline), so this is only a backstop.
const ANSWER_TIMEOUT: Duration = Duration::from_secs(8);

/// One request line (PROTOCOL.md §15.3).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Cmd {
    Open(String),
    Goto(String),
    Url,
    Reload,
    Quit,
}

/// Decode one request line; `Err` holds the English sentence for `{"ok":false,"error":…}`.
pub fn parse_line(line: &str) -> Result<Cmd, String> {
    let v: Value = serde_json::from_str(line.trim())
        .map_err(|_| "the request is not a JSON object".to_string())?;
    let obj = v
        .as_object()
        .ok_or_else(|| "the request is not a JSON object".to_string())?;
    let cmd = obj
        .get("cmd")
        .and_then(Value::as_str)
        .ok_or_else(|| "the request has no \"cmd\" string".to_string())?;
    let url = || {
        obj.get("url")
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| format!("\"{cmd}\" needs a \"url\" string"))
    };
    match cmd {
        "open" => url().map(Cmd::Open),
        "goto" => url().map(Cmd::Goto),
        "url" => Ok(Cmd::Url),
        "reload" => Ok(Cmd::Reload),
        "quit" => Ok(Cmd::Quit),
        other => Err(format!("unknown command \"{other}\"")),
    }
}

/// M4 (design m4 §4.2 查看协议, PROTOCOL.md §15.3): an `open` request may carry `"pages":1` — the attach side's
/// server said `"pages":1` on its view `ok` line, so it handles `0x04 {"event":"jump"}`. From then on the window
/// stops a `?fleet-jump=` navigation and reports it as `{"event":"jump","url"}` on `web.sock` instead of loading
/// it. Never cleared: the window serves one client, whose server does not change.
static PAGES: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Whether an `open` with `"pages":1` came in (see [`PAGES`]).
pub fn pages() -> bool {
    PAGES.load(std::sync::atomic::Ordering::SeqCst)
}

/// `{"cmd":"open",…,"pages":1}` → `true`; anything else (no key, another value, another command) → `false`.
pub fn wants_pages(line: &str) -> bool {
    serde_json::from_str::<Value>(line.trim()).is_ok_and(|v| {
        v.get("cmd").and_then(Value::as_str) == Some("open") && v.get("pages") == Some(&json!(1))
    })
}

/// Set [`PAGES`] before the request reaches the event loop, so the load it starts is already intercepted.
fn note_pages(line: &str) {
    if wants_pages(line) {
        PAGES.store(true, std::sync::atomic::Ordering::SeqCst);
    }
}

/// M4: tell every `web.sock` client about a `?fleet-jump=` navigation the window stopped (`url` as a `file://`
/// URL with its query, so the attach side can hand it to the server).
pub(crate) fn notify_jump(url: &str) {
    notify_all(&json!({"event": "jump", "url": url}).to_string());
}

/// `<dir>/web.sock`.
pub fn sock_path(dir: &Path) -> PathBuf {
    dir.join(SOCK_NAME)
}

/// Check the web runtime directory: it exists, is a directory owned by this user, mode exactly 0700.
pub fn check_dir(dir: &Path) -> io::Result<()> {
    let md = std::fs::symlink_metadata(dir)?;
    if !md.is_dir() {
        return Err(refused(dir, "is not a directory"));
    }
    if md.uid() != own_uid() {
        return Err(refused(dir, "belongs to another user"));
    }
    let mode = md.permissions().mode() & 0o777;
    if mode != 0o700 {
        return Err(refused(dir, &format!("has mode {mode:o}, not 700")));
    }
    Ok(())
}

/// Create `dir` (0700) if missing, check it, and bind `<dir>/web.sock` with mode 0600. A socket path longer than
/// [`MAX_SOCK_PATH`] is an error (never truncated).
pub fn bind(dir: &Path) -> io::Result<UnixListener> {
    let path = sock_path(dir);
    let len = path.as_os_str().len();
    if len > MAX_SOCK_PATH {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!(
                "socket path {} is {len} bytes, over the {MAX_SOCK_PATH}-byte limit",
                path.display()
            ),
        ));
    }
    if !dir.exists() {
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(dir)?;
    }
    check_dir(dir)?;
    clear_stale(&path)?;
    let listener = UnixListener::bind(&path)?;
    // The directory is 0700 and ours, so nobody else can reach the socket between bind and chmod.
    if let Err(e) = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)) {
        let _ = std::fs::remove_file(&path);
        return Err(e);
    }
    Ok(listener)
}

/// A connection is served only when its peer uid is the window's own uid.
pub fn peer_allowed(peer_uid: u32, own_uid: u32) -> bool {
    peer_uid == own_uid
}

/// Bind `<dir>/web.sock` and start the accept thread: each request becomes `UserEvent::Command` on `proxy`,
/// whose reply line is written back on the same connection.
pub fn serve(dir: &Path, proxy: EventLoopProxy<UserEvent>) -> io::Result<JoinHandle<()>> {
    let listener = bind(dir)?;
    std::thread::Builder::new()
        .name("web.sock".into())
        .spawn(move || accept_loop(listener, proxy))
}

/// Write `line` to every connected client (the unsolicited `{"event":"window",…}` lines, §15.1).
pub(crate) fn notify_all(line: &str) {
    let clients = CLIENTS.lock().unwrap_or_else(|p| p.into_inner());
    for c in clients.iter() {
        let mut s = c.lock().unwrap_or_else(|p| p.into_inner());
        let _ = writeln!(s, "{line}");
        let _ = s.flush();
    }
}

/// Remove `<dir>/web.sock` (every exit path; the process may leave through `process::exit`, so no `Drop`).
pub(crate) fn remove(dir: &Path) {
    let _ = std::fs::remove_file(sock_path(dir));
}

/// The writer half of every connection, for [`notify_all`].
static CLIENTS: Mutex<Vec<Arc<Mutex<UnixStream>>>> = Mutex::new(Vec::new());

fn accept_loop(listener: UnixListener, proxy: EventLoopProxy<UserEvent>) {
    let me = own_uid();
    for stream in listener.incoming() {
        let Ok(stream) = stream else { continue };
        match peer_uid(&stream) {
            Ok(uid) if peer_allowed(uid, me) => {}
            // Another user, or an unreadable peer: closed without a reply (§15.2).
            _ => continue,
        }
        let proxy = proxy.clone();
        let _ = std::thread::Builder::new()
            .name("web.sock conn".into())
            .spawn(move || serve_conn(stream, proxy));
    }
}

fn serve_conn(stream: UnixStream, proxy: EventLoopProxy<UserEvent>) {
    let Ok(writer) = stream.try_clone() else {
        return;
    };
    let writer = Arc::new(Mutex::new(writer));
    CLIENTS
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .push(writer.clone());
    let mut reader = BufReader::new(stream);
    loop {
        let mut buf = Vec::new();
        match (&mut reader).take(LINE_MAX + 1).read_until(b'\n', &mut buf) {
            Ok(0) | Err(_) => break,
            Ok(_) => {}
        }
        if buf.last() != Some(&b'\n') && buf.len() as u64 > LINE_MAX {
            reply(&writer, &error_line("the request line is over 1 MiB"));
            break;
        }
        let line = String::from_utf8_lossy(&buf);
        if line.trim().is_empty() {
            continue;
        }
        note_pages(&line);
        let cmd = match parse_line(&line) {
            Ok(c) => c,
            Err(e) => {
                reply(&writer, &error_line(&e));
                continue;
            }
        };
        let (tx, rx) = mpsc::channel();
        if cmd == Cmd::Quit {
            // "reply, then exit" (§15.3): the reply is on the wire before the main thread can end the process.
            reply(&writer, &json!({"ok": true}).to_string());
            let _ = proxy.send_event(UserEvent::Command(cmd, tx));
            break;
        }
        if proxy.send_event(UserEvent::Command(cmd, tx)).is_err() {
            reply(&writer, &error_line("the window is closing"));
            break;
        }
        let answer = rx
            .recv_timeout(ANSWER_TIMEOUT)
            .unwrap_or_else(|_| error_line("the window did not answer"));
        reply(&writer, &answer);
    }
    CLIENTS
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .retain(|c| !Arc::ptr_eq(c, &writer));
}

fn reply(writer: &Mutex<UnixStream>, line: &str) {
    let mut s = writer.lock().unwrap_or_else(|p| p.into_inner());
    let _ = writeln!(s, "{line}");
    let _ = s.flush();
}

pub(crate) fn error_line(msg: &str) -> String {
    json!({"ok": false, "error": msg}).to_string()
}

fn refused(dir: &Path, why: &str) -> io::Error {
    io::Error::new(
        io::ErrorKind::PermissionDenied,
        format!("web runtime directory {} {why}", dir.display()),
    )
}

fn clear_stale(path: &Path) -> io::Result<()> {
    let md = match std::fs::symlink_metadata(path) {
        Ok(md) => md,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    if !md.file_type().is_socket() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            format!("{} exists and is not a socket", path.display()),
        ));
    }
    if UnixStream::connect(path).is_ok() {
        return Err(io::Error::new(
            io::ErrorKind::AddrInUse,
            format!("{} already answers (another window)", path.display()),
        ));
    }
    std::fs::remove_file(path)
}

fn own_uid() -> u32 {
    // SAFETY: getuid has no preconditions and cannot fail.
    unsafe { libc::getuid() }
}

#[cfg(any(
    target_os = "macos",
    target_os = "freebsd",
    target_os = "openbsd",
    target_os = "netbsd"
))]
fn peer_uid(s: &UnixStream) -> io::Result<u32> {
    let mut uid: libc::uid_t = 0;
    let mut gid: libc::gid_t = 0;
    // SAFETY: a valid socket fd and two out-pointers to live locals.
    if unsafe { libc::getpeereid(s.as_raw_fd(), &mut uid, &mut gid) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(uid)
}

#[cfg(not(any(
    target_os = "macos",
    target_os = "freebsd",
    target_os = "openbsd",
    target_os = "netbsd"
)))]
fn peer_uid(s: &UnixStream) -> io::Result<u32> {
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: a valid socket fd; `cred` / `len` are live and sized for SO_PEERCRED.
    let rc = unsafe {
        libc::getsockopt(
            s.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut cred as *mut libc::ucred).cast(),
            &mut len,
        )
    };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(cred.uid)
}

/// M2.6: one `web.sock` request line in GUI mode (PROTOCOL.md §15.4). `Cmd` / [`parse_line`] stay as they are.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GuiCmd {
    /// `open` / `goto` / `url` / `reload` / `quit`, acting on the embedded page.
    Base(Cmd),
    /// `scope {run_dir}` (absolute path).
    Scope(PathBuf),
    /// `focus`: keyboard to the embedded page.
    Focus,
    /// `text`: the terminal's visible rows.
    Text,
    /// `overlay`: the overlay's current state.
    Overlay,
    /// `js {code}`: only with `--test-hooks`.
    Js(String),
}

/// Decode one GUI request line; `js` is an unknown command unless `test_hooks`. `Err` holds the English sentence
/// for `{"ok":false,"error":…}`.
pub fn parse_gui_line(line: &str, test_hooks: bool) -> Result<GuiCmd, String> {
    let v: Value = serde_json::from_str(line.trim())
        .map_err(|_| "the request is not a JSON object".to_string())?;
    let obj = v
        .as_object()
        .ok_or_else(|| "the request is not a JSON object".to_string())?;
    let cmd = obj
        .get("cmd")
        .and_then(Value::as_str)
        .ok_or_else(|| "the request has no \"cmd\" string".to_string())?;
    match cmd {
        "scope" => {
            let dir = obj
                .get("run_dir")
                .and_then(Value::as_str)
                .ok_or_else(|| "\"scope\" needs a \"run_dir\" string".to_string())?;
            let dir = PathBuf::from(dir);
            if !dir.is_absolute() {
                return Err(format!("run_dir {} is not an absolute path", dir.display()));
            }
            Ok(GuiCmd::Scope(dir))
        }
        "focus" => Ok(GuiCmd::Focus),
        "text" => Ok(GuiCmd::Text),
        "overlay" => Ok(GuiCmd::Overlay),
        "js" if test_hooks => obj
            .get("code")
            .and_then(Value::as_str)
            .map(|c| GuiCmd::Js(c.to_string()))
            .ok_or_else(|| "\"js\" needs a \"code\" string".to_string()),
        // open / goto / url / reload / quit as M2.5; everything else (`js` without --test-hooks, `eval`, …) is
        // an unknown command there.
        _ => parse_line(line).map(GuiCmd::Base),
    }
}

/// GUI mode's [`serve`]: bind `<dir>/web.sock` (same rules) and start the accept thread; each request goes to the
/// event loop through `proxy` as [`crate::gui::UiEvent::Gui`].
pub fn serve_gui(dir: &Path, proxy: EventLoopProxy<crate::gui::UiEvent>) -> io::Result<()> {
    serve_gui_with(dir, proxy, false)
}

/// [`serve_gui`] with `js` accepted (`--test-hooks`).
pub(crate) fn serve_gui_with(
    dir: &Path,
    proxy: EventLoopProxy<crate::gui::UiEvent>,
    test_hooks: bool,
) -> io::Result<()> {
    let listener = bind(dir)?;
    std::thread::Builder::new()
        .name("web.sock".into())
        .spawn(move || {
            let me = own_uid();
            for stream in listener.incoming() {
                let Ok(stream) = stream else { continue };
                match peer_uid(&stream) {
                    Ok(uid) if peer_allowed(uid, me) => {}
                    _ => continue, // closed without a reply (§15.2)
                }
                let proxy = proxy.clone();
                let _ = std::thread::Builder::new()
                    .name("web.sock conn".into())
                    .spawn(move || serve_gui_conn(stream, proxy, test_hooks));
            }
        })?;
    Ok(())
}

fn serve_gui_conn(
    stream: UnixStream,
    proxy: EventLoopProxy<crate::gui::UiEvent>,
    test_hooks: bool,
) {
    use crate::gui::UiEvent;
    let Ok(writer) = stream.try_clone() else {
        return;
    };
    let writer = Arc::new(Mutex::new(writer));
    CLIENTS
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .push(writer.clone());
    let mut reader = BufReader::new(stream);
    loop {
        let mut buf = Vec::new();
        match (&mut reader).take(LINE_MAX + 1).read_until(b'\n', &mut buf) {
            Ok(0) | Err(_) => break,
            Ok(_) => {}
        }
        if buf.last() != Some(&b'\n') && buf.len() as u64 > LINE_MAX {
            reply(&writer, &error_line("the request line is over 1 MiB"));
            break;
        }
        let line = String::from_utf8_lossy(&buf);
        if line.trim().is_empty() {
            continue;
        }
        note_pages(&line);
        let cmd = match parse_gui_line(&line, test_hooks) {
            Ok(c) => c,
            Err(e) => {
                reply(&writer, &error_line(&e));
                continue;
            }
        };
        let (tx, rx) = mpsc::channel();
        if cmd == GuiCmd::Base(Cmd::Quit) {
            reply(&writer, &json!({"ok": true}).to_string()); // "reply, then exit" (§15.3)
            let _ = proxy.send_event(UiEvent::Gui(cmd, tx));
            break;
        }
        if proxy.send_event(UiEvent::Gui(cmd, tx)).is_err() {
            reply(&writer, &error_line("the window is closing"));
            break;
        }
        let answer = rx
            .recv_timeout(ANSWER_TIMEOUT)
            .unwrap_or_else(|_| error_line("the window did not answer"));
        reply(&writer, &answer);
    }
    CLIENTS
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .retain(|c| !Arc::ptr_eq(c, &writer));
}
