//! The few libc calls the probe needs that std and crossterm do not offer: `signal`, `poll`, `write`.
//! `sigaction` is not used: `signal` is enough for two handlers that only write one byte.
use libc::{SIGTERM, SIGWINCH, c_int, c_void};
use std::io;
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::Duration;

/// Write end of the self-pipe; -1 until [`SignalPipe::install`].
static WAKE_FD: AtomicI32 = AtomicI32::new(-1);

extern "C" fn on_signal(sig: c_int) {
    let byte: u8 = if sig == SIGTERM { b'T' } else { b'W' };
    let fd = WAKE_FD.load(Ordering::Relaxed);
    if fd >= 0 {
        // Async-signal-safe; a full pipe just drops the byte (one pending byte is enough to wake us).
        unsafe {
            libc::write(fd, &byte as *const u8 as *const c_void, 1);
        }
    }
}

/// SIGWINCH / SIGTERM turned into bytes on a socket pair the main loop polls (`W` / `T`).
pub struct SignalPipe {
    rx: UnixStream,
    _tx: UnixStream,
}

/// What the signals since the last [`SignalPipe::drain`] were.
#[derive(Default, Debug, Clone, Copy)]
pub struct Signals {
    pub winch: bool,
    pub term: bool,
}

impl SignalPipe {
    pub fn install() -> io::Result<SignalPipe> {
        let (rx, tx) = UnixStream::pair()?;
        rx.set_nonblocking(true)?;
        tx.set_nonblocking(true)?;
        WAKE_FD.store(tx.as_raw_fd(), Ordering::Relaxed);
        for sig in [SIGWINCH, SIGTERM] {
            let handler = on_signal as extern "C" fn(c_int) as libc::sighandler_t;
            if unsafe { libc::signal(sig, handler) } == libc::SIG_ERR {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(SignalPipe { rx, _tx: tx })
    }

    pub fn fd(&self) -> RawFd {
        self.rx.as_raw_fd()
    }

    pub fn drain(&self) -> Signals {
        use std::io::Read;
        let mut got = Signals::default();
        let mut buf = [0u8; 64];
        while let Ok(n) = (&self.rx).read(&mut buf) {
            if n == 0 {
                break;
            }
            for b in &buf[..n] {
                match b {
                    b'T' => got.term = true,
                    _ => got.winch = true,
                }
            }
        }
        got
    }
}

/// Waits until one of `fds` is readable or `timeout` passes (`None` = forever). Returns which fds are
/// readable; an interrupted wait (a signal) counts as a wake with nothing readable.
pub fn wait_readable(fds: &[RawFd], timeout: Option<Duration>) -> io::Result<Vec<bool>> {
    let mut pfds: Vec<libc::pollfd> = fds
        .iter()
        .map(|&fd| libc::pollfd { fd, events: libc::POLLIN, revents: 0 })
        .collect();
    let ms: c_int = match timeout {
        None => -1,
        Some(d) => d.as_millis().min(c_int::MAX as u128) as c_int,
    };
    let r = unsafe { libc::poll(pfds.as_mut_ptr(), pfds.len() as libc::nfds_t, ms) };
    if r < 0 {
        let err = io::Error::last_os_error();
        if err.kind() == io::ErrorKind::Interrupted {
            return Ok(vec![false; fds.len()]);
        }
        return Err(err);
    }
    // POLLHUP / POLLERR also count: the following read reports EOF or the error.
    Ok(pfds.iter().map(|p| p.revents != 0).collect())
}
