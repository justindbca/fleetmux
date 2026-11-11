//! The control socket: bind, accept, peer check and per-connection dispatch (PROTOCOL.md §1, §3, §6.1).
//! Signatures from stage A (brief-m03 §3.3); bodies from B6 (server-ctl).

use std::io;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::Ordering;

use fleetmux_proto::view::{self, HelloReply};
use fleetmux_proto::{
    Command, ErrorCode, MAX_LINE, Reply, decode_line, encode_reply, peer_allowed,
};
use serde_json::Value;
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::Mutex;

use super::boot;
use super::{State, daemonize, handlers};
use crate::paths::{self, SOCKET_PATH_MAX};

/// Why [`bind`] refused.
#[derive(Debug)]
pub enum BindError {
    /// Something already answers on the socket (another live server; `fleetmux server` exits 3).
    AlreadyAnswering,
    /// The socket path is `len` bytes, more than `max` (`paths::SOCKET_PATH_MAX`; exit 4). No `/tmp` fallback.
    TooLong { len: usize, max: usize },
    /// Any other OS error.
    Io(io::Error),
}

/// `true` when a connection to `path` succeeds right now (a live server is behind it). A missing path or a
/// stale socket file nobody listens on is `false`.
pub fn is_answering(path: &Path) -> bool {
    std::os::unix::net::UnixStream::connect(path).is_ok()
}

/// Bind the control socket at `path` with mode 0600. Refuses a too-long path and a socket that answers; a stale
/// socket file that does not answer is removed first.
///
/// Single instance without any lock (fix-m0-2; `start.lock` belongs to starters): the server listens on a private
/// name in the same directory first, then publishes it with `link(2)`, which fails with `EEXIST` atomically if
/// anything is already at `path`. So `path` only ever appears as an already-listening socket, and of two racing
/// servers exactly one publishes; the other finds the winner answering and gets `AlreadyAnswering`. A path that
/// exists but does not answer is removed only while it is still the same socket inode that was probed.
pub async fn bind(path: &Path) -> Result<UnixListener, BindError> {
    let len = path.as_os_str().len();
    if len > SOCKET_PATH_MAX {
        return Err(BindError::TooLong {
            len,
            max: SOCKET_PATH_MAX,
        });
    }
    if is_answering(path) {
        return Err(BindError::AlreadyAnswering);
    }
    let dir = path.parent().unwrap_or(Path::new("."));
    let (listener, temp) = bind_private(dir)?;
    let published = publish(&temp, path);
    // The listener keeps its inode; only the private name goes (also on every failure path).
    let _ = std::fs::remove_file(&temp);
    published.map(|()| listener)
}

/// Listen on a fresh private name in `dir`, mode 0600 before it is ever published. The name is no longer than
/// `ctl.sock`, so it fits wherever the real path was accepted.
fn bind_private(dir: &Path) -> Result<(UnixListener, std::path::PathBuf), BindError> {
    for _ in 0..8 {
        let temp = dir.join(format!(
            ".s{}{}",
            paths::new_epoch(),
            &paths::new_epoch()[..2]
        ));
        match UnixListener::bind(&temp) {
            Ok(l) => {
                if let Err(e) = daemonize::chmod(&temp, 0o600) {
                    let _ = std::fs::remove_file(&temp);
                    return Err(BindError::Io(e));
                }
                return Ok((l, temp));
            }
            // A leftover private name, not a live server: try another one.
            Err(e) if e.kind() == io::ErrorKind::AddrInUse => {}
            Err(e) => return Err(BindError::Io(e)),
        }
    }
    Err(BindError::Io(io::Error::other(format!(
        "no free private socket name in {}",
        dir.display()
    ))))
}

/// `link(temp, path)`; on `EEXIST` a live server wins (`AlreadyAnswering`), a dead socket is cleared and the link
/// retried (bounded), anything that is not a socket is refused.
fn publish(temp: &Path, path: &Path) -> Result<(), BindError> {
    for _ in 0..3 {
        match std::fs::hard_link(temp, path) {
            Ok(()) => return Ok(()),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(BindError::Io(e)),
        }
        let seen = daemonize::socket_identity(path).map_err(BindError::Io)?;
        if is_answering(path) {
            return Err(BindError::AlreadyAnswering);
        }
        match daemonize::remove_stale_socket(path, seen) {
            Ok(true) => {}
            Ok(false) => {
                return Err(BindError::Io(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    format!("{} exists and is not a socket", path.display()),
                )));
            }
            Err(e) => return Err(BindError::Io(e)),
        }
    }
    if is_answering(path) {
        return Err(BindError::AlreadyAnswering);
    }
    Err(BindError::Io(io::Error::other(format!(
        "{} keeps reappearing without answering",
        path.display()
    ))))
}

/// Accept connections until shutdown. Each connection: `peer_allowed(euid, peer uid)` before reading anything
/// (refused → close without a byte, one `WARN` line); then the first line decides control (`handlers::handle`
/// per line, replies in order) or view (`view::Hello`, then frames) per PROTOCOL.md §1.1.
pub async fn serve(listener: UnixListener, state: Arc<Mutex<State>>) -> io::Result<()> {
    // SAFETY: geteuid has no preconditions and cannot fail.
    let me = unsafe { libc::geteuid() };
    loop {
        let stream = tokio::select! {
            r = listener.accept() => match r {
                Ok((s, _)) => s,
                // Out of descriptors and the like: the next accept may work; one bad peer never stops the server.
                Err(e) => {
                    boot::log("WARN", &format!("accept: {e}"));
                    // A lasting error (no descriptors left) keeps the listener readable: do not spin on it.
                    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                    continue;
                }
            },
            () = boot::stop_requested() => return Ok(()),
        };
        if boot::stopping() {
            return Ok(());
        }
        match stream.peer_cred() {
            Ok(cred) if peer_allowed(me, cred.uid()) => {}
            Ok(cred) => {
                boot::log(
                    "WARN",
                    &format!(
                        "refused a connection from uid {} (server uid {me})",
                        cred.uid()
                    ),
                );
                continue;
            }
            Err(e) => {
                boot::log(
                    "WARN",
                    &format!("refused a connection whose peer uid is unreadable: {e}"),
                );
                continue;
            }
        }
        tokio::spawn(connection(stream, state.clone()));
    }
}

/// What one bounded line read produced.
enum Line {
    /// A whole line (its `\n` included), or the final bytes before EOF.
    Some(Vec<u8>),
    /// Over `MAX_LINE` bytes without a `\n`.
    Oversize,
    Eof,
}

async fn read_line<R: AsyncBufRead + Unpin>(r: &mut R) -> io::Result<Line> {
    let mut buf = Vec::new();
    // At most MAX_LINE bytes of content plus the `\n`.
    let n = r
        .take(MAX_LINE as u64 + 1)
        .read_until(b'\n', &mut buf)
        .await?;
    Ok(if n == 0 {
        Line::Eof
    } else if buf.last() != Some(&b'\n') && buf.len() > MAX_LINE {
        Line::Oversize
    } else {
        Line::Some(buf)
    })
}

/// One accepted, peer-checked connection.
async fn connection(stream: UnixStream, state: Arc<Mutex<State>>) {
    let (rd, mut wr) = stream.into_split();
    let mut rd = BufReader::new(rd);
    let first = match read_line(&mut rd).await {
        Ok(Line::Some(l)) => l,
        Ok(Line::Oversize) => {
            let _ = wr.write_all(&encode_reply(&oversize_reply())).await;
            return;
        }
        Ok(Line::Eof) | Err(_) => return,
    };
    if is_view_handshake(&first) {
        let hello = match view::Hello::decode(&first) {
            Ok(h) => h,
            Err(e) => {
                let _ = wr
                    .write_all(
                        &HelloReply::Err {
                            code: e.code,
                            error: e.message,
                        }
                        .encode(),
                    )
                    .await;
                return;
            }
        };
        if hello.view != view::VIEW_VERSION {
            let reply = HelloReply::Err {
                code: ErrorCode::ProtocolVersion,
                error: format!(
                    "view {} is not the server's view {}",
                    hello.view,
                    view::VIEW_VERSION
                ),
            };
            let _ = wr.write_all(&reply.encode()).await;
            return;
        }
        // M2.6: the optional `overlay` key (PROTOCOL.md §6.1); a bad value is an invalid handshake like any other.
        let overlay = match view::wants_overlay(&first) {
            Ok(o) => o,
            Err(e) => {
                let reply = HelloReply::Err {
                    code: e.code,
                    error: e.message,
                };
                let _ = wr.write_all(&reply.encode()).await;
                return;
            }
        };
        // gui-scrollbar: the optional `scroll` key, same rule (PROTOCOL.md §6.1).
        let scroll = match view::wants_scroll(&first) {
            Ok(o) => o,
            Err(e) => {
                let reply = HelloReply::Err {
                    code: e.code,
                    error: e.message,
                };
                let _ = wr.write_all(&reply.encode()).await;
                return;
            }
        };
        // S1e: the optional `inset` key, the gui's traffic-light columns (PROTOCOL.md §6.1, plan §17.3); a bad
        // value is an invalid handshake like any other.
        let inset = match view::wants_inset(&first) {
            Ok(n) => n,
            Err(e) => {
                let reply = HelloReply::Err {
                    code: e.code,
                    error: e.message,
                };
                let _ = wr.write_all(&reply.encode()).await;
                return;
            }
        };
        let native_header = match view::wants_native_header(&first) {
            Ok(value) => value,
            Err(e) => {
                let reply = HelloReply::Err { code: e.code, error: e.message };
                let _ = wr.write_all(&reply.encode()).await;
                return;
            }
        };
        let epoch = state.lock().await.epoch.clone();
        let ok = HelloReply::Ok {
            epoch,
            protocol: fleetmux_proto::PROTOCOL,
        };
        // S1e (PROTOCOL.md §6.1): the `ok` line carries `"inset":1`, so the page only sends `0x06` to servers
        // that understand it. M4 (design m4 §4.2 查看协议): and `"pages":1`, so a window only reports
        // `0x04 {"event":"jump"}` to a server that handles it.
        if wr.write_all(&with_pages(ok.encode_with_inset())).await.is_err() {
            return;
        }
        // The same BufReader: input frames sent right behind the handshake may already sit in its buffer.
        let opts = super::view::ViewOpts {
            overlay,
            scroll,
            inset,
            native_header,
        };
        if let Err(e) = super::view::serve_view_opts(state, hello, opts, rd, wr).await {
            boot::log("INFO", &format!("view connection ended: {e}"));
        }
        return;
    }
    let mut line = first;
    // M4 (PROTOCOL.md §2 addendum): the page this connection is bound to by its first `hello {page}`.
    let mut page: Option<String> = None;
    loop {
        let (reply, shutdown) = control_line(&state, &line, &mut page).await;
        if wr.write_all(&encode_reply(&reply)).await.is_err() {
            return;
        }
        if shutdown {
            let _ = wr.flush().await;
            boot::shutdown_replied();
            return;
        }
        line = match read_line(&mut rd).await {
            Ok(Line::Some(l)) => l,
            Ok(Line::Oversize) => {
                let _ = wr.write_all(&encode_reply(&oversize_reply())).await;
                return;
            }
            Ok(Line::Eof) | Err(_) => return,
        };
    }
}

/// A first line is a view handshake when it is a JSON object with a top-level `view` key (PROTOCOL.md §6.1);
/// everything else, bad JSON included, is a control line.
fn is_view_handshake(line: &[u8]) -> bool {
    matches!(serde_json::from_slice::<Value>(line), Ok(Value::Object(o)) if o.contains_key("view"))
}

fn oversize_reply() -> Reply {
    Reply::Err {
        id: None,
        code: ErrorCode::BadRequest,
        error: format!("request line over {MAX_LINE} bytes"),
    }
}

/// Decode and execute one control line; `true` alongside an accepted `server.shutdown`.
async fn control_line(
    state: &Arc<Mutex<State>>,
    line: &[u8],
    page: &mut Option<String>,
) -> (Reply, bool) {
    let req = match decode_line(line) {
        Ok(req) => req,
        Err(e) => {
            state
                .lock()
                .await
                .counters
                .wakeups
                .fetch_add(1, Ordering::Relaxed);
            return (e.into_reply(), false);
        }
    };
    // Answering `server.stats` is not a wake-up (PROTOCOL.md §4.17).
    if req.cmd != Command::ServerStats {
        state
            .lock()
            .await
            .counters
            .wakeups
            .fetch_add(1, Ordering::Relaxed);
    }
    let shutdown = matches!(req.cmd, Command::ServerShutdown { .. });
    let reply = handlers::handle(state, req, page).await;
    let accepted = shutdown && matches!(reply, Reply::Ok { .. });
    (reply, accepted)
}

/// M4 (PROTOCOL.md §6.1): add `"pages":1` to an `ok` handshake line; an error line is left as it is.
fn with_pages(line: Vec<u8>) -> Vec<u8> {
    match serde_json::from_slice::<Value>(&line) {
        Ok(Value::Object(mut o)) if o.get("ok") == Some(&Value::Bool(true)) => {
            o.insert("pages".into(), Value::from(1));
            let mut out = serde_json::to_vec(&Value::Object(o)).unwrap_or(line);
            if out.last() != Some(&b'\n') {
                out.push(b'\n');
            }
            out
        }
        _ => line,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
    use std::path::PathBuf;
    use std::sync::atomic::AtomicU32;

    /// Short private dir (socket path limit), removed on drop.
    struct Tmp(PathBuf);
    impl Tmp {
        fn new() -> Tmp {
            static N: AtomicU32 = AtomicU32::new(0);
            let p = std::env::temp_dir().join(format!(
                "fsb{}-{}",
                std::process::id(),
                N.fetch_add(1, Ordering::SeqCst)
            ));
            let _ = std::fs::remove_dir_all(&p);
            std::fs::DirBuilder::new().mode(0o700).create(&p).unwrap();
            Tmp(p)
        }
    }
    impl Drop for Tmp {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    /// A stale `ctl.sock`: an AF_UNIX stream socket bound at `path` that never listens, then closed (fix-m0-6).
    /// Binding a `UnixListener` and dropping it is not a dead socket while another test in this process is
    /// spawning a child: the child holds a copy of every fd between its fork and exec, so the dropped listener
    /// keeps listening — and answering — for that window. A socket that never listened refuses connections no
    /// matter who holds it. The fd is close-on-exec and owned, so it is closed on every path, panics included.
    fn dead_socket(path: &Path) {
        use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
        use std::os::unix::ffi::OsStrExt;

        #[cfg(target_os = "linux")]
        let ty = libc::SOCK_STREAM | libc::SOCK_CLOEXEC;
        #[cfg(not(target_os = "linux"))]
        let ty = libc::SOCK_STREAM;
        // SAFETY: plain socket(2) call.
        let raw = unsafe { libc::socket(libc::AF_UNIX, ty, 0) };
        assert!(raw >= 0, "socket: {}", io::Error::last_os_error());
        // SAFETY: `raw` is a fresh descriptor nobody else owns.
        let fd = unsafe { OwnedFd::from_raw_fd(raw) };
        #[cfg(not(target_os = "linux"))]
        // SAFETY: fcntl on a descriptor we own.
        assert_eq!(
            unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_SETFD, libc::FD_CLOEXEC) },
            0,
            "FD_CLOEXEC: {}",
            io::Error::last_os_error()
        );
        // SAFETY: sockaddr_un is plain data; all-zero is a valid value.
        let mut addr: libc::sockaddr_un = unsafe { std::mem::zeroed() };
        addr.sun_family = libc::AF_UNIX as libc::sa_family_t;
        let bytes = path.as_os_str().as_bytes();
        assert!(
            bytes.len() < addr.sun_path.len(),
            "{} too long",
            path.display()
        );
        for (d, b) in addr.sun_path.iter_mut().zip(bytes) {
            *d = *b as libc::c_char;
        }
        let len = std::mem::size_of::<libc::sockaddr_un>() as libc::socklen_t;
        // SAFETY: `addr` is a valid sockaddr_un of `len` bytes for the duration of the call.
        let r = unsafe { libc::bind(fd.as_raw_fd(), (&raw const addr).cast(), len) };
        assert_eq!(
            r,
            0,
            "bind {}: {}",
            path.display(),
            io::Error::last_os_error()
        );
        // Never listen(2); dropping `fd` closes it.
    }

    /// Everything in the dir except `ctl.sock`: private names must never be left behind.
    fn leftovers(dir: &Path) -> Vec<String> {
        std::fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .filter(|n| n != "ctl.sock")
            .collect()
    }

    #[tokio::test]
    async fn bind_publishes_listening_socket_0600_and_leaves_no_private_name() {
        let t = Tmp::new();
        let path = t.0.join("ctl.sock");
        let _l = bind(&path).await.expect("bind");
        assert!(is_answering(&path));
        let mode = std::fs::symlink_metadata(&path)
            .unwrap()
            .permissions()
            .mode();
        assert_eq!(mode & 0o777, 0o600, "{mode:o}");
        assert_eq!(leftovers(&t.0), Vec::<String>::new());
    }

    #[tokio::test]
    async fn bind_refuses_when_a_live_server_answers() {
        let t = Tmp::new();
        let path = t.0.join("ctl.sock");
        let _live = bind(&path).await.expect("first bind");
        assert!(matches!(
            bind(&path).await,
            Err(BindError::AlreadyAnswering)
        ));
        assert!(is_answering(&path), "the live socket must stay published");
        assert_eq!(leftovers(&t.0), Vec::<String>::new());
    }

    #[tokio::test]
    async fn bind_replaces_a_stale_socket_file() {
        let t = Tmp::new();
        let path = t.0.join("ctl.sock");
        // A socket file nobody listens on (its listener is gone).
        dead_socket(&path);
        assert!(path.exists() && !is_answering(&path));
        let _l = bind(&path).await.expect("bind over a stale socket");
        assert!(is_answering(&path));
        assert_eq!(leftovers(&t.0), Vec::<String>::new());
    }

    #[tokio::test]
    async fn bind_refuses_a_path_that_is_not_a_socket() {
        let t = Tmp::new();
        let path = t.0.join("ctl.sock");
        std::fs::write(&path, b"x").unwrap();
        assert!(matches!(bind(&path).await, Err(BindError::Io(_))));
        assert_eq!(
            std::fs::read(&path).unwrap(),
            b"x",
            "a regular file is never removed"
        );
        assert_eq!(leftovers(&t.0), Vec::<String>::new());
    }

    #[test]
    fn remove_stale_socket_skips_a_path_that_changed_since_it_was_probed() {
        let t = Tmp::new();
        let path = t.0.join("ctl.sock");
        drop(std::os::unix::net::UnixListener::bind(&path).unwrap());
        let seen = daemonize::socket_identity(&path).unwrap();
        // Another server publishes a new socket inode at the path before we unlink.
        std::fs::remove_file(&path).unwrap();
        let _live = std::os::unix::net::UnixListener::bind(&path).unwrap();
        assert!(daemonize::remove_stale_socket(&path, seen).unwrap());
        assert!(
            is_answering(&path),
            "a socket that replaced the probed one must not be unlinked"
        );
    }

    /// fix-m0-5: the probed dead socket itself is still removed (the tighter identity does not stop real cleanup).
    #[test]
    fn remove_stale_socket_removes_the_probed_dead_socket() {
        let t = Tmp::new();
        let path = t.0.join("ctl.sock");
        dead_socket(&path);
        let seen = daemonize::socket_identity(&path).unwrap();
        assert!(matches!(seen, daemonize::PathKind::Socket(..)), "{seen:?}");
        assert!(daemonize::remove_stale_socket(&path, seen).unwrap());
        assert!(
            !path.exists(),
            "the dead socket that was probed must be removed"
        );
    }

    /// fix-m0-5: a live socket is never unlinked even when its identity equals the probed one (same inode and
    /// change time, as ext4 inode reuse within one coarse timestamp tick can produce).
    #[test]
    fn remove_stale_socket_keeps_a_live_socket_with_the_probed_identity() {
        let t = Tmp::new();
        let path = t.0.join("ctl.sock");
        let _live = std::os::unix::net::UnixListener::bind(&path).unwrap();
        let seen = daemonize::socket_identity(&path).unwrap();
        assert!(daemonize::remove_stale_socket(&path, seen).unwrap());
        assert!(is_answering(&path), "a live socket must not be unlinked");
    }
}

