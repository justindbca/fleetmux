//! `fleetmux attach` (design.md §2.1, PROTOCOL.md §6 / §9): the viewer client. Puts the outer terminal in raw
//! mode, sends the handshake, forwards input bytes and size changes, and writes `0x81` screen bytes as they
//! are; it never draws anything itself.
//!
//! B7 (attach-render). Detach (PROTOCOL.md §10 `d`, supervisor ruling D3): the client asks the server for its
//! prefix with a control `hello` (`prefix`, §4.1), runs the same input scanner the server runs
//! (`input::InputScanner`) over its stdin, and on prefix + `d` sends `0x03` and exits 0; the server gives `d`
//! no effect.
//!
//! `0x83` (open a path, PROTOCOL.md §6.3; B15): the client runs `clipboard::open_notice` off the event loop (the
//! opener may take a while); an empty payload or a failed open becomes a one-line notice written over the bottom
//! (status) row, and `NOTICE_FOR` (3 s) later the client sends a same-size `0x02` so the server redraws the screen.
//!
//! M2.5 (W3): `0x83` first goes to the web window (`web::WebClient::on_open_key`); the opener and the path notice
//! are the next steps, run as above. `0x84` lines go to `web::WebClient::on_server_line` and its `0x04` frames go
//! back to the server; a window the person closes and an unanswered page exchange come back through two more
//! `select!` arms. Every way out of the loop (detach, `0x82`, server gone, stdin closed) stops the window first.
use std::io::{self, BufRead as _, BufReader, Read as _, Write as _};
use std::os::unix::net::UnixStream as StdUnixStream;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::{Duration, Instant};

use fleetmux_proto::view::{self, Frame};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::signal::unix::{SignalKind, signal};

pub mod clipboard;
/// M2.6 `fleetmux gui` (brief-m26 W0 signatures, W3 bodies).
pub mod gui;
pub mod input;
/// fix-m25-2: kernel peer credentials of the client's own connections.
pub mod peer;
/// M2.5 web window, attach side (brief-m25 W0 signatures, W3 bodies).
pub mod web;

use input::{ClientAction, InputScanner};

/// How long an open notice stays on the bottom row before the client asks for a full redraw.
const NOTICE_FOR: Duration = Duration::from_secs(3);

/// `msg` on the bottom row (inverse video), cursor and attributes saved and restored around it. Control
/// characters (the payload comes from the server's kv) are shown as `?`.
fn notice_bytes(msg: &str, rows: u16) -> Vec<u8> {
    let clean: String = msg
        .chars()
        .map(|c| if c.is_control() { '?' } else { c })
        .collect();
    format!("\x1b7\x1b[{rows};1H\x1b[0m\x1b[2K\x1b[7m fleetmux: {clean} \x1b[0m\x1b8").into_bytes()
}

/// `fleetmux attach --runtime D [--no-mouse]`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Opts {
    pub runtime: PathBuf,
    /// Do not enable mouse reporting (`1002` + `1006`) on the outer terminal.
    pub no_mouse: bool,
}

const USAGE: &str = "usage: fleetmux attach --runtime D [--no-mouse]";

/// Parse the arguments after `attach`. `Err` = usage message (exit 2).
pub fn parse_opts(args: &[String]) -> Result<Opts, String> {
    let mut runtime = None;
    let mut no_mouse = false;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--runtime" => match it.next() {
                Some(d) if !d.is_empty() => runtime = Some(PathBuf::from(d)),
                _ => return Err(format!("--runtime needs a directory\n{USAGE}")),
            },
            "--no-mouse" => no_mouse = true,
            other => return Err(format!("unknown argument {other:?}\n{USAGE}")),
        }
    }
    match runtime {
        Some(runtime) => Ok(Opts { runtime, no_mouse }),
        None => Err(format!("--runtime is required\n{USAGE}")),
    }
}

/// Run the client. Exit 0 = detached or the server exited; 2 = usage; 3 = cannot connect (prints the last 20
/// lines of `server.log` to stderr).
pub fn run(args: &[String]) -> ExitCode {
    let opts = match parse_opts(args) {
        Ok(o) => o,
        Err(e) => {
            eprintln!("fleetmux attach: {e}");
            return ExitCode::from(2);
        }
    };
    let sock = opts.runtime.join(crate::paths::SOCKET_NAME);
    let prefix = match control_hello(&sock) {
        Ok(p) => p,
        Err(e) => {
            unreachable_server(&opts.runtime, &sock, &e);
            return ExitCode::from(3);
        }
    };
    let rt = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("fleetmux attach: cannot start: {e}");
            return ExitCode::from(1);
        }
    };
    let code = rt.block_on(client(&opts, &sock, prefix));
    // The stdin reader thread is blocked in `read`; do not wait for it.
    rt.shutdown_background();
    code
}

/// Control `hello` (PROTOCOL.md §4.1), returns the server's prefix byte. Who may be trusted is decided on this
/// connection itself: the kernel's peer credentials of the local socket must be this user ([`peer::verify`];
/// fix-m25-2). The `uid` in the reply is the server machine's and is not compared: through the person's own
/// `ssh -L` forwarder it is another machine's uid (operator M2.5 C), and that end is checked by the server.
fn control_hello(sock: &Path) -> io::Result<Option<u8>> {
    let mut s = StdUnixStream::connect(sock)?;
    peer::verify(&s)?;
    s.set_read_timeout(Some(Duration::from_secs(5)))?;
    s.write_all(b"{\"id\":1,\"cmd\":\"hello\",\"protocol\":1}\n")?;
    let mut line = String::new();
    BufReader::new(&mut s).read_line(&mut line)?;
    let reply: Value = serde_json::from_str(&line)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, format!("hello: {e}")))?;
    if reply["ok"] != json!(true) {
        return Err(io::Error::other(format!(
            "hello refused: {}",
            line.trim_end()
        )));
    }
    let name = reply["prefix"].as_str().unwrap_or("C-b");
    Ok(input::prefix_byte(name))
}

/// Exit-3 message: why, then the last 20 lines of `server.log` (or that it is missing).
fn unreachable_server(runtime: &Path, sock: &Path, err: &io::Error) {
    unreachable_server_as("attach", runtime, sock, err);
}

/// [`unreachable_server`] for the subcommand `who` (`attach`, M2.6 `gui`).
fn unreachable_server_as(who: &str, runtime: &Path, sock: &Path, err: &io::Error) {
    eprintln!(
        "fleetmux {who}: cannot connect to {}: {err}",
        sock.display()
    );
    let log = runtime.join("server.log");
    match std::fs::read(&log) {
        Ok(bytes) => {
            let text = String::from_utf8_lossy(&bytes);
            let lines: Vec<&str> = text.lines().collect();
            eprintln!("--- last lines of {} ---", log.display());
            for l in &lines[lines.len().saturating_sub(20)..] {
                eprintln!("{l}");
            }
        }
        Err(e) => eprintln!("(no server.log at {}: {e})", log.display()),
    }
}

/// The outer terminal's size; 80×24 when stdout is not a terminal.
fn outer_size() -> (u16, u16) {
    match crossterm::terminal::size() {
        Ok((c, r)) if c > 0 && r > 0 => (c, r),
        _ => (80, 24),
    }
}

async fn send(w: &mut tokio::net::unix::OwnedWriteHalf, f: &Frame) -> io::Result<()> {
    let bytes = view::encode_frame(f).map_err(|e| io::Error::other(format!("{e:?}")))?;
    w.write_all(&bytes).await
}

/// Send the web client's `0x04` frames, then hand its status-line notes to the notice channel and run the
/// `0x83` steps after the window (opener, then the path) off the event loop.
async fn web_out(
    w: &mut tokio::net::unix::OwnedWriteHalf,
    frames: Vec<Frame>,
    client: &mut web::WebClient,
    notes: &tokio::sync::mpsc::UnboundedSender<String>,
) -> io::Result<()> {
    for f in &frames {
        send(w, f).await?;
    }
    for note in client.take_notes() {
        let _ = notes.send(note);
    }
    for (path, why) in client.take_fallbacks() {
        let tx = notes.clone();
        tokio::task::spawn_blocking(move || {
            if let Some(note) = clipboard::open_notice_after_window(&path, why.as_deref()) {
                let _ = tx.send(note);
            }
        });
    }
    Ok(())
}

async fn client(opts: &Opts, sock: &Path, prefix: Option<u8>) -> ExitCode {
    // The view connection is a new connection: its peer is checked on its own (fix-m25-2), before any byte.
    let stream = match UnixStream::connect(sock)
        .await
        .and_then(|s| peer::verify(&s).map(|()| s))
    {
        Ok(s) => s,
        Err(e) => {
            unreachable_server(&opts.runtime, sock, &e);
            return ExitCode::from(3);
        }
    };
    let (rd, mut wr) = stream.into_split();
    let mut rd = tokio::io::BufReader::new(rd);
    let (cols, rows) = outer_size();
    let hello = json!({
        "view": view::VIEW_VERSION,
        "cols": cols,
        "rows": rows,
        "term": std::env::var("TERM").unwrap_or_default(),
        "colorterm": std::env::var("COLORTERM").unwrap_or_default(),
    });
    let mut line = String::new();
    let shake = async {
        wr.write_all(format!("{hello}\n").as_bytes()).await?;
        rd.read_line(&mut line).await
    };
    if let Err(e) = shake.await {
        unreachable_server(&opts.runtime, sock, &e);
        return ExitCode::from(3);
    }
    // M4 (PROTOCOL.md §6.1): `"pages":1` on the `ok` line — the server handles the window's jump events.
    let pages = match serde_json::from_str::<Value>(&line) {
        Ok(v) if v["ok"] == json!(true) => v["pages"] == json!(1),
        _ => {
            eprintln!(
                "fleetmux attach: view handshake refused: {}",
                line.trim_end()
            );
            return ExitCode::from(3);
        }
    };

    let guard = match input::enter_raw(!opts.no_mouse) {
        Ok(g) => g,
        Err(e) => {
            eprintln!("fleetmux attach: cannot put the terminal in raw mode: {e}");
            return ExitCode::from(1);
        }
    };
    // Stdin on its own thread: a blocking read never holds up the event loop or the exit.
    let (in_tx, mut in_rx) = tokio::sync::mpsc::channel::<Vec<u8>>(64);
    std::thread::spawn(move || {
        let mut stdin = io::stdin().lock();
        let mut b = vec![0u8; 64 * 1024];
        loop {
            match stdin.read(&mut b) {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    if in_tx.blocking_send(b[..n].to_vec()).is_err() {
                        break;
                    }
                }
            }
        }
    });
    let mut winch = signal(SignalKind::window_change()).ok();
    let mut scanner = InputScanner::new(prefix);
    let mut buf: Vec<u8> = Vec::with_capacity(256 * 1024);
    let mut out = io::stdout();
    let (note_tx, mut note_rx) = tokio::sync::mpsc::unbounded_channel::<String>();
    let mut redraw_at: Option<tokio::time::Instant> = None;
    let (mut webc, mut web_events) = web::WebClient::new(&opts.runtime);
    webc.set_pages(pages);

    let reason: String = 'conn: loop {
        loop {
            match view::decode_frame(&buf) {
                Ok(Some((frame, used))) => {
                    buf.drain(..used);
                    match input::on_frame(frame) {
                        ClientAction::Write(b) => {
                            if out.write_all(&b).and_then(|_| out.flush()).is_err() {
                                break 'conn String::new();
                            }
                        }
                        ClientAction::Open(path) => {
                            // Window first; the opener and the path notice come back as fallbacks.
                            let frames = webc.on_open_key(&path);
                            if web_out(&mut wr, frames, &mut webc, &note_tx).await.is_err() {
                                break 'conn String::new();
                            }
                        }
                        ClientAction::Exit(r) => break 'conn r,
                        ClientAction::Web(line) => {
                            let frames = webc.on_server_line(&line);
                            if web_out(&mut wr, frames, &mut webc, &note_tx).await.is_err() {
                                break 'conn String::new();
                            }
                        }
                        ClientAction::Overlay(_) => {} // terminal attach never asks for it
                        ClientAction::Scroll(_) => {}  // terminal attach never asks for it
                    }
                }
                Ok(None) => break,
                Err(e) => break 'conn format!("protocol error: {e:?}"),
            }
        }
        let deadline = redraw_at;
        let web_deadline = webc.deadline().map(tokio::time::Instant::from_std);
        tokio::select! {
            r = rd.read_buf(&mut buf) => match r {
                Ok(0) | Err(_) => break String::new(), // server gone
                Ok(_) => {}
            },
            chunk = in_rx.recv() => {
                let Some(chunk) = chunk else { break String::new() }; // stdin closed
                let o = input::client_input(&mut scanner, &chunk, Instant::now());
                if !o.forward.is_empty() && send(&mut wr, &Frame::Input(o.forward)).await.is_err() {
                    break String::new();
                }
                if o.detach {
                    let _ = send(&mut wr, &Frame::Detach).await;
                    let _ = wr.shutdown().await;
                    break String::from("detached");
                }
            },
            _ = async { match winch.as_mut() { Some(s) => { s.recv().await; } None => std::future::pending().await } } => {
                let (c, r) = outer_size();
                if send(&mut wr, &Frame::Resize { cols: c, rows: r }).await.is_err() {
                    break String::new();
                }
            },
            Some(note) = note_rx.recv() => {
                let (_, rows) = outer_size();
                if out.write_all(&notice_bytes(&note, rows)).and_then(|_| out.flush()).is_err() {
                    break String::new();
                }
                redraw_at = Some(tokio::time::Instant::now() + NOTICE_FOR);
            },
            _ = async move { match deadline { Some(t) => tokio::time::sleep_until(t).await, None => std::future::pending().await } } => {
                // A same-size `0x02` resets the server's diff base: the next frame repaints the notice row.
                redraw_at = None;
                let (c, r) = outer_size();
                if send(&mut wr, &Frame::Resize { cols: c, rows: r }).await.is_err() {
                    break String::new();
                }
            },
            Some(generation) = web_events.recv() => {
                // The person closed the window (or it died): tell the server.
                let frames = webc.on_window_event(generation);
                if web_out(&mut wr, frames, &mut webc, &note_tx).await.is_err() {
                    break String::new();
                }
            },
            _ = async move { match web_deadline { Some(t) => tokio::time::sleep_until(t).await, None => std::future::pending().await } } => {
                // A page exchange the server left unanswered.
                let frames = webc.on_deadline(Instant::now());
                if web_out(&mut wr, frames, &mut webc, &note_tx).await.is_err() {
                    break String::new();
                }
            },
        }
    };
    // Detach, `0x82`, server gone, stdin closed: `quit` the window and close its lifeline before leaving.
    webc.shutdown();
    drop(guard);
    if !reason.is_empty() {
        eprintln!("[fleetmux: {reason}]");
    }
    ExitCode::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn parse_opts_runtime_and_no_mouse() {
        assert_eq!(
            parse_opts(&args(&["--runtime", "/r", "--no-mouse"])),
            Ok(Opts {
                runtime: "/r".into(),
                no_mouse: true
            })
        );
        assert_eq!(
            parse_opts(&args(&["--runtime", "/r"])),
            Ok(Opts {
                runtime: "/r".into(),
                no_mouse: false
            })
        );
        assert!(parse_opts(&args(&[])).is_err());
        assert!(parse_opts(&args(&["--runtime"])).is_err());
        assert!(parse_opts(&args(&["--runtime", "/r", "--bogus"])).is_err());
    }

    #[test]
    fn notice_goes_to_the_bottom_row_and_masks_control_characters() {
        let b = String::from_utf8(notice_bytes("cannot open here: /x\x1b]0;t\x07", 40)).unwrap();
        assert!(b.starts_with("\x1b7\x1b[40;1H"), "{b:?}");
        assert!(b.ends_with("\x1b8"), "{b:?}");
        assert!(
            b.contains(" fleetmux: cannot open here: /x?]0;t? "),
            "{b:?}"
        );
        assert_eq!(b.matches('\x07').count(), 0);
    }

    #[test]
    fn missing_server_prints_log_tail() {
        let dir = std::env::temp_dir().join(format!("fm-attach-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let sock = dir.join("ctl.sock");
        assert!(control_hello(&sock).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }
    // ---- fix-m25-2: the uid check is the local socket's peer, not the server's hello.uid ----

    /// A one-shot local "server" on `<dir>/ctl.sock` that answers the control hello with `reply`.
    fn fake_hello_server(name: &str, reply: Value) -> (PathBuf, std::thread::JoinHandle<()>) {
        let dir = std::env::temp_dir().join(format!("fm-hello-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let sock = dir.join("ctl.sock");
        let listener = std::os::unix::net::UnixListener::bind(&sock).unwrap();
        let t = std::thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut line = String::new();
            BufReader::new(s.try_clone().unwrap())
                .read_line(&mut line)
                .unwrap();
            assert!(line.contains("\"hello\""), "{line}");
            s.write_all(format!("{reply}\n").as_bytes()).unwrap();
        });
        (sock, t)
    }

    #[test]
    fn hello_from_a_same_user_local_socket_is_accepted_whatever_uid_the_server_reports() {
        // `ssh -L` to a server on another machine: the local socket is our own forwarder, the server's uid is that
        // machine's (operator M2.5 C: 1000 on the VM vs 501 on the Mac).
        let remote_uid = unsafe { libc::geteuid() } as u64 + 499;
        let reply = json!({"id": 1, "ok": true, "uid": remote_uid, "pid": 4242, "epoch": "k7q2",
                           "version": "0.1.0", "protocol": 1, "run": null, "prefix": "C-a"});
        let (sock, t) = fake_hello_server("remote-uid", reply);
        let prefix = control_hello(&sock).expect("a same-user local socket is accepted");
        assert_eq!(prefix, input::prefix_byte("C-a"));
        t.join().unwrap();
        std::fs::remove_dir_all(sock.parent().unwrap()).unwrap();
    }

    #[test]
    fn a_refused_hello_is_still_an_error() {
        let reply =
            json!({"id": 1, "ok": false, "code": "bad_request", "error": "unsupported protocol"});
        let (sock, t) = fake_hello_server("refused", reply);
        let err = control_hello(&sock).unwrap_err();
        assert!(err.to_string().contains("hello refused"), "{err}");
        t.join().unwrap();
        std::fs::remove_dir_all(sock.parent().unwrap()).unwrap();
    }
}
