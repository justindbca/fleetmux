//! fix-m25-2: who serves the Unix socket this client connected to (design.md §1 / §5: "对端 uid 必须相同").
//!
//! The attach client checks the **kernel's peer credentials of its own connection** (macOS / BSD `getpeereid`,
//! Linux `SO_PEERCRED`): the process at the other end of the local socket must run as this client's effective
//! uid, and a failed query is a refusal. That is the local server itself, or — when the person attaches to a
//! server on another machine — their own `ssh -L` forwarder, whose remote end the server checks the same way
//! (`server::socket`). The server's `hello.uid` is that machine's uid and is not compared with ours: uids are
//! per machine, so comparing them only blocked the cross-host case (operator M2.5 acceptance, scenario C).

use std::io;
use std::os::fd::AsRawFd;

/// The effective uid of the process at the other end of the connected Unix socket `fd` (the kernel's record from
/// connect / listen time; the peer cannot claim another).
#[cfg(any(
    target_os = "macos",
    target_os = "freebsd",
    target_os = "openbsd",
    target_os = "netbsd"
))]
pub fn peer_uid(fd: &impl AsRawFd) -> io::Result<u32> {
    let mut uid: libc::uid_t = 0;
    let mut gid: libc::gid_t = 0;
    // SAFETY: getpeereid only writes the two out-pointers, which point to live locals.
    if unsafe { libc::getpeereid(fd.as_raw_fd(), &mut uid, &mut gid) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(uid)
}

/// The effective uid of the process at the other end of the connected Unix socket `fd` (`SO_PEERCRED`).
#[cfg(not(any(
    target_os = "macos",
    target_os = "freebsd",
    target_os = "openbsd",
    target_os = "netbsd"
)))]
pub fn peer_uid(fd: &impl AsRawFd) -> io::Result<u32> {
    // SAFETY: ucred is plain integers; all-zero is a valid value.
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: getsockopt writes at most `len` bytes into `cred`, both live and sized for SO_PEERCRED.
    let rc = unsafe {
        libc::getsockopt(
            fd.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut cred as *mut libc::ucred).cast(),
            &mut len,
        )
    };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    if len as usize != std::mem::size_of::<libc::ucred>() {
        return Err(io::Error::other("SO_PEERCRED returned a short answer"));
    }
    Ok(cred.uid)
}

/// `Ok` when the peer is this user (`peer == euid`); a different uid or an unreadable peer is refused
/// (`PermissionDenied`, with the reason).
pub fn check_peer(peer: io::Result<u32>, euid: u32) -> io::Result<()> {
    match peer {
        Ok(uid) if uid == euid => Ok(()),
        Ok(uid) => Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!("the local socket is served by uid {uid}, not by this user (uid {euid})"),
        )),
        Err(e) => Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!("cannot read who serves the local socket: {e}"),
        )),
    }
}

/// [`check_peer`] of [`peer_uid`] against this process's effective uid.
pub fn verify(fd: &impl AsRawFd) -> io::Result<()> {
    // SAFETY: geteuid has no preconditions and cannot fail.
    check_peer(peer_uid(fd), unsafe { libc::geteuid() })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::{UnixListener, UnixStream};

    fn euid() -> u32 {
        // SAFETY: geteuid has no preconditions and cannot fail.
        unsafe { libc::geteuid() }
    }

    #[test]
    fn same_machine_same_user_socket_is_accepted() {
        let dir = std::env::temp_dir().join(format!("fm-peer-same-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("s.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let client = UnixStream::connect(&path).unwrap();
        let (server_side, _) = listener.accept().unwrap();
        assert_eq!(
            peer_uid(&client).unwrap(),
            euid(),
            "the listener's uid, seen from the client"
        );
        assert_eq!(
            peer_uid(&server_side).unwrap(),
            euid(),
            "and the other way round"
        );
        verify(&client).expect("same user on this machine");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn another_uid_is_refused() {
        let other = euid().wrapping_add(1);
        let err = check_peer(Ok(other), euid()).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::PermissionDenied);
        assert!(err.to_string().contains(&other.to_string()), "{err}");
        // root on the other end is another user too.
        if euid() != 0 {
            assert!(check_peer(Ok(0), euid()).is_err());
        }
    }

    #[test]
    fn unreadable_peer_credentials_are_refused() {
        let err =
            check_peer(Err(io::Error::from_raw_os_error(libc::ENOTCONN)), euid()).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::PermissionDenied);
        // A real failure: a pipe is not a socket, the kernel cannot say who is on the other end.
        let (r, _w) = std::io::pipe().unwrap();
        assert!(peer_uid(&r).is_err());
        assert_eq!(
            verify(&r).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }
}
