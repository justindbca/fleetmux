//! Detaching, `server.json` and `start.lock` (design.md §2.3; PROTOCOL.md §8.1, §8.4). Signatures from stage A
//! (brief-m03 §3.3); bodies from B6 (server-ctl).

use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

/// Fork twice and `setsid`; the intermediate process exits at once, the grandchild returns `Ok` with stdin from
/// `/dev/null` and stdout / stderr appended to `log` (`server.log`). Nothing is written under `$HOME`.
pub fn daemonize(log: &Path) -> io::Result<()> {
    // Open both targets before forking so a failure is reported to the caller in the foreground.
    let null = OpenOptions::new().read(true).open("/dev/null")?;
    let out = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(log)?;
    // SAFETY: called before the tokio runtime exists (boot.rs), so the process has a single thread and the
    // child may keep running Rust code. The parents leave with `_exit`, running no destructors.
    unsafe {
        match libc::fork() {
            -1 => return Err(io::Error::last_os_error()),
            0 => {}
            _ => libc::_exit(0),
        }
        if libc::setsid() == -1 {
            return Err(io::Error::last_os_error());
        }
        match libc::fork() {
            -1 => return Err(io::Error::last_os_error()),
            0 => {}
            _ => libc::_exit(0),
        }
        for (from, to) in [
            (null.as_raw_fd(), libc::STDIN_FILENO),
            (out.as_raw_fd(), libc::STDOUT_FILENO),
            (out.as_raw_fd(), libc::STDERR_FILENO),
        ] {
            if libc::dup2(from, to) == -1 {
                return Err(io::Error::last_os_error());
            }
        }
    }
    Ok(())
}

/// Close every descriptor above 2 this process inherited from its starter (fix-m0-6): with `--daemonize` a
/// starter's pipe, file, listener or connection must not live as long as the daemon.
///
/// Called first thing in `fleetmux server --daemonize` (boot.rs), while the process is single-threaded and has
/// opened nothing of its own yet: every descriptor above 2 is the starter's, none is owned by a Rust value
/// (no double close), and the control listener, `server.log` and tokio's process-wide descriptors (created by
/// the bind runtime before the fork and needed again after it) do not exist yet. The list is taken first and the
/// enumeration's own descriptor is closed before anything else is, so its (by then free) number only gets
/// `EBADF`, which is ignored; nothing is opened in between, so no number is reused.
pub(super) fn close_inherited_fds() {
    for fd in open_fds() {
        if fd > 2 {
            // SAFETY: no Rust value owns `fd` (see above); EBADF / EINTR leave nothing to do.
            unsafe { libc::close(fd) };
        }
    }
}

/// The open descriptors: `/dev/fd` (macOS fdesc; on Linux a link to `/proc/self/fd`), else every number below
/// the `RLIMIT_NOFILE` soft limit (at most 2^20) that `fcntl(F_GETFD)` accepts.
fn open_fds() -> Vec<RawFd> {
    if let Ok(dir) = fs::read_dir("/dev/fd") {
        let fds: Vec<RawFd> = dir
            .filter_map(|e| e.ok()?.file_name().to_str()?.parse().ok())
            .collect();
        if !fds.is_empty() {
            return fds; // `dir` was consumed above: its descriptor is closed by now
        }
    }
    // SAFETY: getrlimit fills the struct; fcntl(F_GETFD) only queries.
    let mut lim = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    let max = if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut lim) } == 0 {
        lim.rlim_cur.min(1 << 20) as RawFd
    } else {
        1024
    };
    (0..max)
        .filter(|&fd| unsafe { libc::fcntl(fd, libc::F_GETFD) } != -1)
        .collect()
}

/// Write `bytes` to `path` atomically (PROTOCOL.md §8): `<path>.tmp` in the same directory with mode 0600,
/// `fsync`, then `rename` over `path`.
pub(super) fn write_atomic(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".tmp");
    let tmp = Path::new(&tmp);
    let result = (|| {
        let mut f = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
        fs::rename(tmp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(tmp);
    }
    result
}

/// Atomically write `<runtime>/server.json` = `{pid, start_time, version, protocol, epoch}` (PROTOCOL.md §8.1),
/// mode 0600, via `server.json.tmp` + `fsync` + `rename`.
pub fn write_server_json(runtime: &Path, pid: u32, start_time: u64, epoch: &str) -> io::Result<()> {
    let body = serde_json::json!({
        "pid": pid,
        "start_time": start_time,
        "version": env!("CARGO_PKG_VERSION"),
        "protocol": fleetmux_proto::PROTOCOL,
        "epoch": epoch,
    });
    write_atomic(&runtime.join("server.json"), body.to_string().as_bytes())
}

/// Run `f` while holding `flock(LOCK_EX)` on `<runtime>/start.lock` (created if missing, never deleted).
///
/// This is the starter's side of PROTOCOL.md §8.4 (design.md §2.3: lock → `hello` again → clear a dead socket →
/// start → wait for `hello`). `fleetmux server` itself never takes the lock (fix-m0-2): a starter holds it while
/// it waits for the server's `hello`, so a server that waited for it would deadlock. The server keeps a single
/// instance through [`super::socket::bind`] instead.
pub fn with_start_lock<T>(runtime: &Path, f: impl FnOnce() -> T) -> io::Result<T> {
    let path = runtime.join("start.lock");
    let lock: File = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(&path)?;
    loop {
        // SAFETY: flock on a file descriptor we own for the whole call.
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } == 0 {
            break;
        }
        let e = io::Error::last_os_error();
        if e.kind() != io::ErrorKind::Interrupted {
            return Err(e);
        }
    }
    let out = f();
    // Closing the descriptor releases the lock; the file itself stays (PROTOCOL.md §8.4).
    drop(lock);
    Ok(out)
}

/// Create `<runtime>/start.lock` if it is missing (mode 0600; content meaningless, never deleted — PROTOCOL.md
/// §8.4). Opening never contends with a starter's `flock`; the server does not lock it.
pub(super) fn touch_start_lock(runtime: &Path) -> io::Result<()> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(runtime.join("start.lock"))
        .map(drop)
}

/// What [`socket_identity`] saw at a path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum PathKind {
    Missing,
    /// A socket inode: `(st_dev, st_ino, st_ctime, st_ctime_nsec)`. The inode number alone is not an identity:
    /// ext4 hands a just-freed inode number to the next file created in the directory (fix-m0-5), so the change
    /// time is part of it.
    Socket(u64, u64, i64, i64),
    Other,
}

/// `lstat` the path: missing, a socket (with its identity) or something else.
pub(super) fn socket_identity(path: &Path) -> io::Result<PathKind> {
    use std::os::unix::fs::{FileTypeExt, MetadataExt};
    match fs::symlink_metadata(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(PathKind::Missing),
        Err(e) => Err(e),
        Ok(m) if m.file_type().is_socket() => Ok(PathKind::Socket(
            m.dev(),
            m.ino(),
            m.ctime(),
            m.ctime_nsec(),
        )),
        Ok(_) => Ok(PathKind::Other),
    }
}

/// Remove `path` only if it is still the socket `seen` (observed before the caller found it not answering) and
/// still does not answer; a path that changed meanwhile — e.g. another server just published its live socket
/// there — is left alone. The identity check covers a different inode or change time; the second probe covers a
/// live socket that happens to share both (coarse timestamps). Neither closes the gap between this check and
/// the `unlink` (see the fix-m0-2 report). `false` when the path is something other than a socket.
pub(super) fn remove_stale_socket(path: &Path, seen: PathKind) -> io::Result<bool> {
    let now = socket_identity(path)?;
    match now {
        PathKind::Missing => Ok(true),
        PathKind::Other => Ok(false),
        PathKind::Socket(..) if now != seen || super::socket::is_answering(path) => Ok(true),
        PathKind::Socket(..) => match fs::remove_file(path) {
            Err(e) if e.kind() != io::ErrorKind::NotFound => Err(e),
            _ => Ok(true),
        },
    }
}

/// `chmod(path, mode)` on a path that may be a socket (std's `set_permissions` works on any inode too, but the
/// explicit call keeps the intent visible next to `bind`).
pub(super) fn chmod(path: &Path, mode: u32) -> io::Result<()> {
    let c = CString::new(path.as_os_str().as_bytes())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "path contains NUL"))?;
    // SAFETY: `c` is a valid NUL-terminated path for the duration of the call.
    if unsafe { libc::chmod(c.as_ptr(), mode as libc::mode_t) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}
