//! fix-m0-6 regression (supervisor [M41]): after `exec`, a pane's child holds only its own PTY slave on fds
//! 0 / 1 / 2 — never the server's listening socket, an accepted control / view connection, or another pane's PTY.
//!
//! The descriptors are read from outside the child, over its whole descriptor table (no fixed fd range), and
//! the pane runs `sleep` exec'd directly (no shell, so no script descriptor of its own):
//! - Linux: `/proc/<pid>/fd` (each entry's link target).
//! - macOS: `proc_pidinfo(PROC_PIDLISTFDS)` for the table and `proc_pidfdinfo(PROC_PIDFDVNODEPATHINFO)` for each
//!   vnode's path. libc 0.2 has the calls but not that flavor's result struct, so it is declared here and the
//!   returned size is checked against it (a mismatch fails the test instead of passing it).
//!
//! "After exec" = the child's executable is no longer the server binary (a forked child runs the server image
//! until `exec`). The same reader applied to the server must find its sockets and PTY masters, so an empty
//! result for the children cannot come from a reader that sees nothing.
mod common;
use common::*;

/// What one descriptor refers to.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Kind {
    Socket,
    /// A file or device by path (`/dev/ttys012`, `/dev/pts/3`, `/dev/ptmx`, …).
    Path(String),
    Other(String),
}

#[derive(Debug, Clone)]
struct Fd {
    fd: i32,
    kind: Kind,
}

/// Every entry of `/proc/<pid>/fd`. No entry is skipped: a listing or name error fails the test, and an fd that
/// closes between the listing and its `readlink` is kept as `Other("closed while reading")`, which can never
/// match a child's expected `[0, 1, 2]` on its own tty (so a read problem cannot turn into a pass).
#[cfg(target_os = "linux")]
fn fds(pid: i32) -> Vec<Fd> {
    let dir = format!("/proc/{pid}/fd");
    let mut out: Vec<Fd> = std::fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("read {dir}: {e}"))
        .map(|e| {
            let e = e.unwrap_or_else(|err| panic!("list {dir}: {err}"));
            let name = e.file_name();
            let fd: i32 = name
                .to_str()
                .and_then(|n| n.parse().ok())
                .unwrap_or_else(|| panic!("{dir}: entry {name:?} is not an fd number"));
            let kind = match std::fs::read_link(e.path()) {
                Ok(target) => {
                    let t = target.to_string_lossy().into_owned();
                    if t.starts_with("socket:") {
                        Kind::Socket
                    } else if t.starts_with('/') {
                        Kind::Path(t)
                    } else {
                        Kind::Other(t)
                    }
                }
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                    Kind::Other("closed while reading".into())
                }
                Err(err) => panic!("readlink {dir}/{fd}: {err}"),
            };
            Fd { fd, kind }
        })
        .collect();
    out.sort_by_key(|f| f.fd);
    out
}

#[cfg(target_os = "linux")]
fn exe(pid: i32) -> Option<String> {
    std::fs::read_link(format!("/proc/{pid}/exe"))
        .ok()
        .map(|p| p.to_string_lossy().into_owned())
}

#[cfg(target_os = "macos")]
mod mac {
    use std::ffi::{CStr, c_int, c_void};
    use std::mem::{size_of, zeroed};

    /// `struct proc_fileinfo` (sys/proc_info.h).
    #[repr(C)]
    struct ProcFileInfo {
        fi_openflags: u32,
        fi_status: u32,
        fi_offset: i64,
        fi_type: i32,
        fi_guardflags: u32,
    }

    /// `struct vnode_fdinfowithpath` (sys/proc_info.h), the result of `PROC_PIDFDVNODEPATHINFO`.
    #[repr(C)]
    struct VnodeFdInfoWithPath {
        pfi: ProcFileInfo,
        pvip: libc::vnode_info_path,
    }

    const PROC_PIDFDVNODEPATHINFO: c_int = 2;

    pub fn fds(pid: i32) -> Vec<super::Fd> {
        let need =
            unsafe { libc::proc_pidinfo(pid, libc::PROC_PIDLISTFDS, 0, std::ptr::null_mut(), 0) };
        assert!(
            need > 0,
            "proc_pidinfo(PROC_PIDLISTFDS) size for pid {pid}: {need}"
        );
        let cap = need as usize / size_of::<libc::proc_fdinfo>() + 16;
        let mut buf: Vec<libc::proc_fdinfo> = vec![unsafe { zeroed() }; cap];
        let got = unsafe {
            libc::proc_pidinfo(
                pid,
                libc::PROC_PIDLISTFDS,
                0,
                buf.as_mut_ptr() as *mut c_void,
                (cap * size_of::<libc::proc_fdinfo>()) as c_int,
            )
        };
        assert!(
            got > 0,
            "proc_pidinfo(PROC_PIDLISTFDS) for pid {pid}: {got}"
        );
        buf.truncate(got as usize / size_of::<libc::proc_fdinfo>());
        let mut out: Vec<super::Fd> = buf
            .iter()
            .map(|f| {
                let kind = match f.proc_fdtype as c_int {
                    libc::PROX_FDTYPE_SOCKET => super::Kind::Socket,
                    libc::PROX_FDTYPE_VNODE => match vnode_path(pid, f.proc_fd) {
                        Some(p) => super::Kind::Path(p),
                        None => super::Kind::Other("vnode without path".into()),
                    },
                    t => super::Kind::Other(format!("fd type {t}")),
                };
                super::Fd {
                    fd: f.proc_fd,
                    kind,
                }
            })
            .collect();
        out.sort_by_key(|f| f.fd);
        out
    }

    fn vnode_path(pid: i32, fd: i32) -> Option<String> {
        let mut info: VnodeFdInfoWithPath = unsafe { zeroed() };
        let n = unsafe {
            libc::proc_pidfdinfo(
                pid,
                fd,
                PROC_PIDFDVNODEPATHINFO,
                &mut info as *mut _ as *mut c_void,
                size_of::<VnodeFdInfoWithPath>() as c_int,
            )
        };
        if n <= 0 {
            return None; // closed meanwhile
        }
        assert_eq!(
            n as usize,
            size_of::<VnodeFdInfoWithPath>(),
            "PROC_PIDFDVNODEPATHINFO result size differs from the struct declared in this test"
        );
        let raw = &info.pvip.vip_path as *const _ as *const std::ffi::c_char;
        Some(
            unsafe { CStr::from_ptr(raw) }
                .to_string_lossy()
                .into_owned(),
        )
    }

    pub fn exe(pid: i32) -> Option<String> {
        let mut buf = vec![0u8; libc::PROC_PIDPATHINFO_MAXSIZE as usize];
        let n =
            unsafe { libc::proc_pidpath(pid, buf.as_mut_ptr() as *mut c_void, buf.len() as u32) };
        (n > 0).then(|| String::from_utf8_lossy(&buf[..n as usize]).into_owned())
    }
}

#[cfg(target_os = "macos")]
use mac::{exe, fds};

fn is_ptmx(k: &Kind) -> bool {
    matches!(k, Kind::Path(p) if p == "/dev/ptmx" || p == "/dev/pts/ptmx")
}

#[test]
fn pane_children_hold_only_their_own_pty_after_exec() {
    let srv = Server::start();
    let server_pid = srv.ok("hello", json!({}))["pid"]
        .as_i64()
        .expect("hello.pid") as i32;
    let server_exe = exe(server_pid).expect("server executable path");

    // Held for the whole test: a view connection and one more control connection after every spawn, so later
    // panes are spawned while more accepted sockets (and earlier panes' PTY masters) are open in the server.
    let _view = Viewer::attach(&srv, 120, 40);
    let mut held = vec![srv.client()];
    held[0].ok("hello", json!({}));
    let mut panes: Vec<(String, i32, String)> = Vec::new(); // (pane, pid, tty)
    for (i, ty) in ["supervisor", "worker", "worker", "tab"].iter().enumerate() {
        let r = srv.spawn(&format!("fd-{i}"), ty, &["sleep", "300"], json!({}));
        panes.push((
            r["pane"].as_str().expect("pane").to_string(),
            r["pid"].as_i64().expect("pid") as i32,
            r["tty"].as_str().expect("tty").to_string(),
        ));
        let mut c = srv.client();
        c.ok("hello", json!({}));
        held.push(c);
    }

    // The reader sees what the children must not have: the server's listener + held connections, and masters.
    let server_fds = fds(server_pid);
    let sockets = server_fds.iter().filter(|f| f.kind == Kind::Socket).count();
    let masters = server_fds.iter().filter(|f| is_ptmx(&f.kind)).count();
    assert!(
        sockets >= held.len() + 2 && masters >= panes.len(),
        "the descriptor reader must see the server's listener, {} control + 1 view connections and {} PTY \
         masters; it saw {sockets} sockets and {masters} masters: {server_fds:?}",
        held.len(),
        panes.len()
    );

    let mut fails = Vec::new();
    for (pane, pid, tty) in &panes {
        let image = wait_until(
            &format!("pane {pane} (pid {pid}) to finish exec"),
            Duration::from_secs(10),
            || exe(*pid).filter(|p| *p != server_exe),
        );
        let got = fds(*pid);
        let numbers: Vec<i32> = got.iter().map(|f| f.fd).collect();
        let own_tty = got.iter().all(|f| f.kind == Kind::Path(tty.clone()));
        if numbers != [0, 1, 2] || !own_tty {
            fails.push(format!(
                "pane {pane} pid {pid} ({image}), tty {tty}: expected exactly fds 0/1/2 on {tty}, got {got:?}"
            ));
        }
    }
    assert!(
        fails.is_empty(),
        "pane children hold descriptors they must not:\n{}",
        fails.join("\n")
    );
}
