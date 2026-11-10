//! fix-m25-2 (operator M2.5 acceptance, scenario C): `fleetmux attach` checks who serves its **local** socket (the
//! kernel's peer credentials of its own connections), not the uid a server reports in `hello`. A server on another
//! machine, reached through the person's own `ssh -L` forwarder, reports that machine's uid (1000 on the VM vs 501
//! on the Mac); the client must still attach.
//!
//! The real binary runs in a PTY against a stand-in "server" listening as this user: `hello` answers with a
//! different uid, the view handshake is accepted, then `0x82` ends the session. Before the fix the client refused
//! with `server runs as another user` (exit 3).
mod common;
use common::*;

use std::os::unix::net::UnixListener;

/// Serve the control `hello` (with `uid`) and one view connection (handshake, then `0x82 reason`) on
/// `<dir>/ctl.sock`; returns the lines the client sent first on each connection.
fn stand_in_server(
    dir: &std::path::Path,
    uid: u64,
    reason: &'static str,
) -> std::thread::JoinHandle<Vec<String>> {
    let listener = UnixListener::bind(dir.join("ctl.sock")).expect("bind stand-in ctl.sock");
    std::thread::spawn(move || {
        let mut firsts = Vec::new();
        for _ in 0..2 {
            let (mut s, _) = listener.accept().expect("accept");
            let mut line = String::new();
            BufReader::new(s.try_clone().unwrap())
                .read_line(&mut line)
                .unwrap();
            let req: Value = serde_json::from_str(&line).expect("first line is JSON");
            firsts.push(line.trim_end().to_string());
            if req["cmd"] == json!("hello") {
                let reply = json!({"id": req["id"], "ok": true, "uid": uid, "pid": 4242, "epoch": "k7q2",
                                   "version": "0.1.0", "protocol": 1, "run": null, "prefix": "C-b"});
                s.write_all(format!("{reply}\n").as_bytes()).unwrap();
            } else {
                // View handshake (PROTOCOL.md §6.1), then 0x82 with a reason (§6.3): the client prints it, exits 0.
                s.write_all(b"{\"ok\":true,\"protocol\":1}\n").unwrap();
                let mut frame = vec![0x82];
                frame.extend_from_slice(&(reason.len() as u32).to_be_bytes());
                frame.extend_from_slice(reason.as_bytes());
                s.write_all(&frame).unwrap();
                std::thread::sleep(Duration::from_millis(500));
            }
        }
        firsts
    })
}

#[test]
fn attach_through_a_same_user_local_socket_works_when_the_server_reports_another_uid() {
    let tmp = TempDir::new();
    let rt = tmp.path().join("rt");
    std::fs::create_dir_all(&rt).unwrap();
    // SAFETY: geteuid has no preconditions and cannot fail.
    let remote_uid = unsafe { libc::geteuid() } as u64 + 499; // "the other machine's" uid
    let server = stand_in_server(&rt, remote_uid, "stand-in server for fix-m25-2");

    let rt_s = rt.to_str().expect("utf-8 runtime path");
    let mut p = PtyProc::spawn(&[BIN, "attach", "--runtime", rt_s], 100, 30, &[]);
    let code = p.wait_exit(Duration::from_secs(10));
    let out = p.output();
    assert!(
        !out.contains("another user"),
        "refused because of the server's hello.uid:\n{out}"
    );
    assert_eq!(code, Some(0), "attach did not end normally on 0x82:\n{out}");
    assert!(
        out.contains("stand-in server for fix-m25-2"),
        "0x82 reason not shown:\n{out}"
    );
    let firsts = server.join().unwrap();
    assert_eq!(
        firsts.len(),
        2,
        "control hello + view connection: {firsts:?}"
    );
    assert!(firsts[0].contains("\"hello\""), "{firsts:?}");
    assert!(firsts[1].contains("\"view\""), "{firsts:?}");
}
