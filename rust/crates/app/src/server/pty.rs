//! PTY children (design.md §2.4): spawn with argv passed straight through, a new session and process group with
//! the PTY as controlling terminal; read the OS start time (PROTOCOL.md §8.6); signal a process group.
//!
//! Package B5 `server-pty`.

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::io::{self, Read, Write};
use std::path::Path;

use portable_pty::{CommandBuilder, PtySize, native_pty_system};

/// A spawned PTY child. `pid == pgid`: the child is a session and process-group leader.
pub struct PtyChild {
    pub pid: u32,
    pub pgid: u32,
    /// Full device path of the PTY slave (`/dev/ttys012`, `/dev/pts/3`).
    pub tty: String,
    /// The child's start time as the OS records it, integer Unix seconds (PROTOCOL.md §8.6).
    pub start_time: u64,
    master: Box<dyn portable_pty::MasterPty + Send>,
    child: Box<dyn portable_pty::Child + Send + Sync>,
}

/// What `waitpid(pid, WNOHANG)` said about a pane's child. `status` is `None` when the OS gives no usable status
/// (unknown, never "success"): only an explicit nonzero exit may be read as a failure.
#[derive(Debug, Clone)]
pub(crate) enum Reaped {
    /// Still running.
    No,
    /// Gone (and reaped).
    Yes {
        status: Option<portable_pty::ExitStatus>,
    },
}

/// Why a pane could not be spawned. Every variant is `spawn_failed` on the wire (PROTOCOL.md §4.4, §5).
#[derive(Debug)]
pub enum SpawnError {
    /// `argv` was empty.
    EmptyArgv,
    /// `cwd` is not an existing directory.
    BadCwd(String),
    /// PTY allocation, fork or exec failed.
    Io(io::Error),
    /// The child started but its start time could not be read (it is killed; no unverifiable pane is kept).
    StartTime,
}

fn pty_err(e: impl std::fmt::Display) -> SpawnError {
    SpawnError::Io(io::Error::other(e.to_string()))
}

/// Spawn `argv` (exec'd directly, never typed into a shell) on a new `cols` × `rows` PTY. `cwd` absent → the
/// server's cwd; `env` is set on top of the server's environment, plus `TERM=xterm-256color` and
/// `COLORTERM=truecolor` unless `env` sets them (PROTOCOL.md §4.4).
pub fn spawn(
    argv: &[String],
    cwd: Option<&Path>,
    env: &BTreeMap<String, String>,
    cols: u16,
    rows: u16,
) -> Result<PtyChild, SpawnError> {
    if argv.is_empty() {
        return Err(SpawnError::EmptyArgv);
    }
    // portable-pty silently falls back to $HOME for a missing / absent cwd: check it here, and pass the server's
    // own cwd explicitly when none is given (R11).
    let dir = match cwd {
        Some(d) if d.is_dir() => d.to_path_buf(),
        Some(d) => return Err(SpawnError::BadCwd(d.display().to_string())),
        None => std::env::current_dir().map_err(SpawnError::Io)?,
    };

    let pair = native_pty_system()
        .openpty(PtySize {
            rows,
            cols,
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(pty_err)?;

    let mut cmd = CommandBuilder::from_argv(argv.iter().map(OsString::from).collect());
    cmd.cwd(&dir);
    for (k, v) in env {
        cmd.env(k, v);
    }
    if !env.contains_key("TERM") {
        cmd.env("TERM", "xterm-256color");
    }
    if !env.contains_key("COLORTERM") {
        cmd.env("COLORTERM", "truecolor");
    }

    let tty = pair
        .master
        .tty_name()
        .map(|p| p.display().to_string())
        .ok_or_else(|| pty_err("cannot name the PTY slave"))?;
    let mut child = pair.slave.spawn_command(cmd).map_err(pty_err)?;
    // Only the child keeps the slave open: EOF on the master then means the child side is gone.
    drop(pair.slave);

    let Some(pid) = child.process_id() else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(pty_err("spawned child has no pid"));
    };
    let Some(start_time) = start_time(pid) else {
        let _ = signal_group(pid, libc::SIGKILL);
        let _ = child.wait();
        return Err(SpawnError::StartTime);
    };
    Ok(PtyChild {
        pid,
        pgid: pid,
        tty,
        start_time,
        master: pair.master,
        child,
    })
}

impl PtyChild {
    /// A reader of the PTY master (what the child writes). EOF / EIO = the slave side is closed.
    pub(crate) fn reader(&self) -> io::Result<Box<dyn Read + Send>> {
        self.master.try_clone_reader().map_err(io::Error::other)
    }

    /// The writer of the PTY master (the child's input); can be taken once.
    pub(crate) fn writer(&self) -> io::Result<Box<dyn Write + Send>> {
        self.master.take_writer().map_err(io::Error::other)
    }

    /// The PTY's foreground process group (`tcgetpgrp` on the master, plan §14.2): the shell's own pgid while it
    /// sits at its prompt, the foreground job's pgid while a program runs. `None` when the ioctl reports no group
    /// (the PTY is gone or was never a terminal).
    pub(crate) fn process_group_leader(&self) -> Option<u32> {
        let pgid = self.master.process_group_leader()?;
        (pgid > 0).then_some(pgid as u32)
    }

    /// `TIOCSWINSZ`.
    pub(crate) fn resize(&self, cols: u16, rows: u16) -> io::Result<()> {
        self.master
            .resize(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(io::Error::other)
    }

    /// `waitpid(pid, WNOHANG)`: [`Reaped::Yes`] once the child has exited and been reaped. The exit status is kept
    /// for the caller (A42-LINUX-RESTORE-FAILURE-04: a `start --resume` pane that failed must be tellable from one
    /// that exited 0); a status the OS does not give is reported as unknown, never as success.
    pub(crate) fn try_reap(&mut self) -> Reaped {
        match self.child.try_wait() {
            Ok(Some(status)) => Reaped::Yes {
                status: Some(status),
            },
            Ok(None) | Err(_) => Reaped::No,
        }
    }
}

/// Start time of `pid` per PROTOCOL.md §8.6 (macOS `p_starttime.tv_sec`; Linux ⌊btime + starttime ÷ CLK_TCK⌋).
/// `None` = unverifiable (process gone, unreadable, unparsable): callers must not signal.
pub fn start_time(pid: u32) -> Option<u64> {
    if pid == 0 || pid > i32::MAX as u32 {
        return None;
    }
    os_start_time(pid)
}

/// macOS: `proc_pidinfo(PROC_PIDTBSDINFO).pbi_start_tvsec`, which the kernel fills from the same `p_start` as
/// `kinfo_proc.kp_proc.p_starttime` (libc has no `kinfo_proc`); `ps -o lstart=` prints the same second.
#[cfg(target_os = "macos")]
fn os_start_time(pid: u32) -> Option<u64> {
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
    let n = unsafe {
        libc::proc_pidinfo(
            pid as libc::c_int,
            libc::PROC_PIDTBSDINFO,
            0,
            &mut info as *mut _ as *mut libc::c_void,
            size,
        )
    };
    (n == size && info.pbi_pid == pid && info.pbi_start_tvsec > 0).then_some(info.pbi_start_tvsec)
}

#[cfg(target_os = "linux")]
fn os_start_time(pid: u32) -> Option<u64> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // Fields after the `)` that ends the command name: state is field 3, starttime field 22.
    let rest = &stat[stat.rfind(')')? + 1..];
    let ticks: u64 = rest.split_whitespace().nth(19)?.parse().ok()?;
    let btime: u64 = std::fs::read_to_string("/proc/stat")
        .ok()?
        .lines()
        .find_map(|l| l.strip_prefix("btime "))?
        .trim()
        .parse()
        .ok()?;
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    (hz > 0).then(|| btime + ticks / hz as u64)
}

/// The command name of `pid` (macOS `pbi_comm`, Linux `/proc/<pid>/comm`); `None` when it cannot be read.
pub fn process_name(pid: u32) -> Option<String> {
    if pid == 0 || pid > i32::MAX as u32 {
        return None;
    }
    os_process_name(pid)
}

#[cfg(target_os = "macos")]
fn os_process_name(pid: u32) -> Option<String> {
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
    let n = unsafe {
        libc::proc_pidinfo(
            pid as libc::c_int,
            libc::PROC_PIDTBSDINFO,
            0,
            &mut info as *mut _ as *mut libc::c_void,
            size,
        )
    };
    if n != size || info.pbi_pid != pid {
        return None;
    }
    let bytes: Vec<u8> = info
        .pbi_comm
        .iter()
        .take_while(|&&c| c != 0)
        .map(|&c| c as u8)
        .collect();
    Some(String::from_utf8_lossy(&bytes).into_owned())
}

#[cfg(target_os = "linux")]
fn os_process_name(pid: u32) -> Option<String> {
    let comm = std::fs::read_to_string(format!("/proc/{pid}/comm")).ok()?;
    Some(comm.trim_end().to_string())
}

/// `kill(-pgid, sig)`.
pub fn signal_group(pgid: u32, sig: i32) -> io::Result<()> {
    if pgid <= 1 || pgid > i32::MAX as u32 {
        return Err(io::Error::from(io::ErrorKind::InvalidInput));
    }
    if unsafe { libc::kill(-(pgid as i32), sig) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

/// Whether any process of group `pgid` still exists (`kill(-pgid, 0)` succeeds).
pub fn group_alive(pgid: u32) -> bool {
    match signal_group(pgid, 0) {
        Ok(()) => true,
        // A member we may not signal still exists.
        Err(e) => e.raw_os_error() == Some(libc::EPERM),
    }
}
