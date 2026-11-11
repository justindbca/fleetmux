//! Runtime paths, socket length, runtime-dir permissions and pane ids (design.md §2.2, §2.4, §5;
//! plan.md §3.3). Signatures from phase 0; bodies from B6 (server-ctl).
//!
//! Contract pinned by `tests/paths.rs`:
//! - `token(session)`: every char outside ASCII `[A-Za-z0-9_-]` becomes `_`, then the first 40 chars are
//!   kept; an empty result becomes `"fleet"` (same as `wezterm.py`'s `_token`, but ASCII-only).
//! - `runtime_dir(state_dir, session)` = `<state_dir>/fleetmux/<token(session)>`; nothing else goes in.
//! - `socket_path(runtime)` = `<runtime>/ctl.sock`, or `PathError::TooLong` when its byte length exceeds
//!   [`SOCKET_PATH_MAX`]; there is no `/tmp` fallback.
//! - `check_runtime_perms(dir)`: the directory must be owned by the effective uid (else `NotOwner`, and
//!   nothing is changed); group / other bits are cleared to 0700.
//! - `pane_id(epoch, seq)` = `<epoch>-<seq>`; `new_epoch()` = 4 random chars of `[0-9a-z]`.

use std::fmt;
use std::fs;
use std::io::{self, Read};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

/// Longest Unix socket path in bytes: `sockaddr_un.sun_path` minus the terminating NUL
/// (104 bytes on macOS, 108 on Linux).
#[cfg(target_os = "macos")]
pub const SOCKET_PATH_MAX: usize = 103;
#[cfg(not(target_os = "macos"))]
pub const SOCKET_PATH_MAX: usize = 107;

/// File name of the control socket inside the runtime directory.
pub const SOCKET_NAME: &str = "ctl.sock";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PathError {
    /// `<runtime>/ctl.sock` is `len` bytes, more than `max` (= [`SOCKET_PATH_MAX`]).
    TooLong { len: usize, max: usize },
}

impl fmt::Display for PathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PathError::TooLong { len, max } => write!(
                f,
                "socket path is {len} bytes, over the {max}-byte limit (use a shorter state directory)"
            ),
        }
    }
}

impl std::error::Error for PathError {}

/// What [`check_runtime_perms`] had to do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fixed {
    /// Already owner-only (no group / other bits); nothing changed.
    Unchanged,
    /// Permission bits were `from` and are now `to` (0o700).
    Chmod { from: u32, to: u32 },
}

#[derive(Debug)]
pub enum PermError {
    /// The directory belongs to `owner`, not to us (`me`); it is left untouched.
    NotOwner { owner: u32, me: u32 },
    /// The path is not a directory (a symlink counts as not a directory: checked with `lstat`).
    NotDir,
    /// Could not stat or chmod the directory (e.g. it does not exist).
    Io(io::Error),
}

impl fmt::Display for PermError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PermError::NotOwner { owner, me } => {
                write!(
                    f,
                    "runtime directory is owned by uid {owner}, not by uid {me}"
                )
            }
            PermError::NotDir => write!(f, "runtime path is not a directory"),
            PermError::Io(e) => write!(f, "runtime directory: {e}"),
        }
    }
}

impl std::error::Error for PermError {}

/// Session name → directory-safe token (see the module docs).
pub fn token(session: &str) -> String {
    let t: String = session
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                c
            } else {
                '_'
            }
        })
        .take(40)
        .collect();
    if t.is_empty() { "fleet".into() } else { t }
}

/// `<state_dir>/fleetmux/<token(session)>`.
pub fn runtime_dir(state_dir: &Path, session: &str) -> PathBuf {
    state_dir.join("fleetmux").join(token(session))
}

/// `<runtime>/ctl.sock`, refused when longer than [`SOCKET_PATH_MAX`] bytes.
pub fn socket_path(runtime: &Path) -> Result<PathBuf, PathError> {
    let sock = runtime.join(SOCKET_NAME);
    let len = sock.as_os_str().len();
    if len > SOCKET_PATH_MAX {
        return Err(PathError::TooLong {
            len,
            max: SOCKET_PATH_MAX,
        });
    }
    Ok(sock)
}

/// Verify the runtime directory is ours and owner-only, fixing the mode when it is ours but too open.
pub fn check_runtime_perms(dir: &Path) -> Result<Fixed, PermError> {
    let meta = fs::symlink_metadata(dir).map_err(PermError::Io)?;
    if !meta.is_dir() {
        return Err(PermError::NotDir);
    }
    // SAFETY: geteuid has no preconditions and cannot fail.
    let me = unsafe { libc::geteuid() };
    if meta.uid() != me {
        return Err(PermError::NotOwner {
            owner: meta.uid(),
            me,
        });
    }
    let from = meta.mode() & 0o777;
    if from & 0o077 == 0 {
        return Ok(Fixed::Unchanged);
    }
    let to = from & 0o700;
    fs::set_permissions(dir, fs::Permissions::from_mode(to)).map_err(PermError::Io)?;
    Ok(Fixed::Chmod { from, to })
}

/// Pane id unique across server generations: `<epoch>-<seq>`, e.g. `k7q2-12`.
pub fn pane_id(epoch: &str, seq: u32) -> String {
    format!("{epoch}-{seq}")
}

/// Random 4-char base36 (`[0-9a-z]`) tag of one server generation.
pub fn new_epoch() -> String {
    const DIGITS: &[u8; 36] = b"0123456789abcdefghijklmnopqrstuvwxyz";
    let mut seed = [0u8; 8];
    let random = fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut seed));
    let mut n = if random.is_ok() {
        u64::from_ne_bytes(seed)
    } else {
        // No /dev/urandom: clock nanoseconds mixed with the pid, still different per server start.
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_nanos() as u64);
        nanos ^ (u64::from(std::process::id()) << 32)
    };
    (0..4)
        .map(|_| {
            let c = DIGITS[(n % 36) as usize] as char;
            n /= 36;
            c
        })
        .collect()
}
