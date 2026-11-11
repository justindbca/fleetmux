//! M2.6 `fleetmux gui --runtime D [--devtools]` (design.md §3.6.2, PROTOCOL.md §9.5): the graphical viewer. The
//! protocol half lives here, in the static `fleetmux` binary; the window half is `fleetmux-web --gui`
//! (PROTOCOL.md §15.4), which this client starts with two pipes (stdin: `0x81` / `0x85` / `0x86` in; stdout:
//! `0x01` / `0x02` / `0x05` / `0x06` out) and drives over its `web.sock`.
//!
//! Order (design.md §3.6.2): control `hello` (kernel peer credentials, as `attach`) + `kv.get history_url` →
//! [`gui_possible`] (exit 4 when not) → web runtime directory (as M2.5) → start `fleetmux-web --gui --dir …
//! [--run-dir …] --prefix … [--theme JSON] [--devtools]` (never `--test-hooks`) → wait for `web.sock` and the
//! first `0x02` (3 s each, else exit 4 with the `web.log` tail) → `WebClient::adopt_gui` → view handshake with
//! `"overlay":1,"scroll":1` at the size of that `0x02` → relay until detach / window closed / `0x82` / server gone,
//! always stopping the window first (`WebClient::shutdown`).
//!
//! Relay (W3, brief-m26 §3.4):
//! - server `0x81` / `0x85` / `0x86` go into the window pipe as they are, in order, through a bounded queue drained by a
//!   writer thread; while it is full the client stops reading the server socket (back-pressure, design.md §3.6.5);
//! - the first `url` of a `0x85`, or one in another run directory, → `scope` then the M2.5 page exchange that
//!   loads it into the embedded page; `pane` is remembered as the history pane; the window's `open` event goes to
//!   the server once that first load is done (or right away when there is nothing to load);
//! - `0x84` → `WebClient` as in `attach`; `0x83` (prefix `o`) → `pane.focus` the history pane + `web.sock` `focus`,
//!   or without a history pane the M2.5 fallbacks after the window (opener → path notice);
//! - `history_focus` from the window → `pane.focus` the history pane;
//! - window `0x01` → the attach input scanner (prefix + `d` detaches), then the server; `0x02` → the server;
//!   `0x05` (a scrollbar drag) → the server as it is; `0x06` (the page's titlebar inset, S1e) → the server only when
//!   its handshake reply said `"inset":1` (an old server would refuse the unknown type, PROTOCOL.md §6.2).
//!
//! Notices (the old-server hint, `0x82`, a page that cannot be shown here, …) are written into the pipe as a `0x81`
//! over the bottom row, then a same-size `0x02` 3 s later makes the server repaint it (as `attach`).
use std::collections::VecDeque;
use std::fs::OpenOptions;
use std::io::{self, BufRead as _, BufReader, Write as _};
use std::os::unix::fs::OpenOptionsExt as _;
use std::os::unix::net::UnixStream as StdUnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitCode, Stdio};
use std::time::{Duration, Instant};

use fleetmux_proto::view::{self, Frame, Overlay};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::signal::unix::{SignalKind, signal};
use tokio::sync::mpsc;

use super::input::{self, ClientAction, InputScanner};
use super::web::{self, GuiEvent, OpenEnv, WebClient};
use super::{NOTICE_FOR, notice_bytes, peer, send, unreachable_server_as};

/// `fleetmux gui` command line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Opts {
    /// `--runtime D`: the server's runtime directory (holds `ctl.sock`), as `fleetmux attach`.
    pub runtime: PathBuf,
    /// `--devtools`: passed on to `fleetmux-web --gui`.
    pub devtools: bool,
}

const USAGE: &str = "usage: fleetmux gui --runtime D [--devtools]";
/// `web.sock` accepts, and then the window's first `0x02` arrives, each within this (PROTOCOL.md §9.5).
const READY_WAIT: Duration = Duration::from_secs(3);
/// Frames waiting for the window pipe's writer thread; when full, the server socket is not read.
const PIPE_QUEUE: usize = 16;
/// S1e (plan §17.2): on macOS the page reports its titlebar inset right before its first size, and the window
/// process writes the two frames back to back; this is how long the handshake waits for that inset after the size
/// so the `hello` line can carry it. Without one (Linux, no traffic lights) the inset is 0.
const INSET_WAIT: Duration = Duration::from_millis(250);
/// After `quit` and EOF on its stdin the window has 1 s to exit (PROTOCOL.md §15.1); then it is killed.
const EXIT_WAIT: Duration = Duration::from_millis(1500);
/// No `0x85` this long after the first screen frame → the server predates M2.6 (design.md §3.6.3 ①).
const OVERLAY_WAIT: Duration = Duration::from_secs(2);
/// How many `web.log` lines an exit-4 reason quotes.
const LOG_TAIL: usize = 3;

/// Parse the arguments after `gui`; `Err` = the usage error (exit 2).
pub fn parse_opts(args: &[String]) -> Result<Opts, String> {
    let mut runtime = None;
    let mut devtools = false;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--runtime" => match it.next() {
                Some(d) if !d.is_empty() => runtime = Some(PathBuf::from(d)),
                _ => return Err(format!("--runtime needs a directory\n{USAGE}")),
            },
            "--devtools" => devtools = true,
            other => return Err(format!("unknown argument {other:?}\n{USAGE}")),
        }
    }
    match runtime {
        Some(runtime) => Ok(Opts { runtime, devtools }),
        None => Err(format!("--runtime is required\n{USAGE}")),
    }
}

/// The exit-4 line: the reason, then the attach hint (PROTOCOL.md §9.5).
fn exit4_line(reason: &str, runtime: &str) -> String {
    format!("fleetmux gui: {reason}; use: fleetmux attach --runtime {runtime}")
}

/// Why `env` cannot show the GUI (`version` = the version check's own words, when it ran and failed).
fn why_not(env: &OpenEnv, version: Option<&str>) -> Option<String> {
    if !env.display {
        Some(String::from(
            "no graphical session (no DISPLAY / WAYLAND_DISPLAY)",
        ))
    } else if env.web_bin.is_none() {
        Some(format!(
            "fleetmux-web not found (next to fleetmux, on PATH, ${})",
            web::WEB_BIN_ENV
        ))
    } else if !env.version_ok {
        Some(version.map(str::to_string).unwrap_or_else(|| {
            format!(
                "fleetmux-web is not the matching version (want {})",
                web::expected_version()
            )
        }))
    } else {
        None
    }
}

/// Run `fleetmux gui`: exit 0 on detach / window closed / server exit, 2 usage, 3 cannot connect, 4 cannot
/// open the window (PROTOCOL.md §9.5).
pub fn run(args: &[String]) -> ExitCode {
    let opts = match parse_opts(args) {
        Ok(o) => o,
        Err(e) => {
            eprintln!("fleetmux gui: {e}");
            return ExitCode::from(2);
        }
    };
    let sock = opts.runtime.join(crate::paths::SOCKET_NAME);
    let (prefix_name, history_url, theme) = match hello_and_history(&sock) {
        Ok(v) => v,
        Err(e) => {
            unreachable_server_as("gui", &opts.runtime, &sock, &e);
            return ExitCode::from(3);
        }
    };
    let runtime_shown = opts.runtime.display().to_string();
    let (env, version_why) = web::gui_env();
    if let Some(why) = why_not(&env, version_why.as_deref()) {
        eprintln!("{}", exit4_line(&why, &runtime_shown));
        return ExitCode::from(4);
    }
    let rt = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("fleetmux gui: cannot start: {e}");
            return ExitCode::from(1);
        }
    };
    let bin = env.web_bin.clone().expect("checked by why_not");
    let code = rt.block_on(gui(&opts, &sock, &bin, &prefix_name, history_url, theme));
    rt.shutdown_background();
    code
}

/// Can this host show the GUI? `Ok` when there is a display and a matching `fleetmux-web`; `Err` = the exit-4
/// line for stderr (the reason, then `use: fleetmux attach --runtime D`).
pub fn gui_possible(env: &OpenEnv) -> Result<(), String> {
    match why_not(env, None) {
        Some(why) => Err(exit4_line(&why, "D")),
        None => Ok(()),
    }
}

/// The run directory of a `file://` `history_url` (the directory holding the page), for `web.sock` `scope`;
/// `None` for anything that is not an absolute `file://` URL.
pub fn scope_for(history_url: &str) -> Option<PathBuf> {
    let path = web::file_url_path(history_url)?;
    path.parent().map(Path::to_path_buf)
}

/// One control request on a fresh connection to `ctl.sock`, whose kernel peer must be this user (as `attach`,
/// fix-m25-2); the reply line.
fn control(sock: &Path, req: Value) -> io::Result<Value> {
    let mut s = StdUnixStream::connect(sock)?;
    peer::verify(&s)?;
    s.set_read_timeout(Some(Duration::from_secs(5)))?;
    s.write_all(format!("{req}\n").as_bytes())?;
    let mut line = String::new();
    BufReader::new(&mut s).read_line(&mut line)?;
    serde_json::from_str(&line)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, format!("{}: {e}", req["cmd"])))
}

/// Control `hello` (the prefix key's name, and the server's palette when it sends one) and `kv.get history_url`.
fn hello_and_history(sock: &Path) -> io::Result<(String, Option<String>, Option<String>)> {
    let hello = control(sock, json!({"id": 1, "cmd": "hello", "protocol": 1}))?;
    if hello["ok"] != json!(true) {
        return Err(io::Error::other(format!("hello refused: {hello}")));
    }
    let prefix = hello["prefix"].as_str().unwrap_or("C-b").to_string();
    // `hello.theme` is an optional key (PROTOCOL.md §4.1): the object's own JSON goes to the window verbatim, so
    // the page paints with exactly the palette the server answers OSC queries from. No key (an older server) = no
    // `--theme`, and the page keeps xterm.js's own colours.
    let theme = hello
        .get("theme")
        .filter(|t| t.is_object())
        .map(Value::to_string);
    let kv = control(
        sock,
        json!({"id": 2, "cmd": "kv.get", "key": "history_url"}),
    )?;
    let url = kv["value"]
        .as_str()
        .filter(|u| !u.is_empty())
        .map(str::to_string);
    Ok((prefix, url, theme))
}

/// `pane.focus <pane>` over the control socket; why not, if it failed.
fn focus_pane(sock: &Path, pane: &str) -> Result<(), String> {
    match control(sock, json!({"id": 3, "cmd": "pane.focus", "pane": pane})) {
        Ok(r) if r["ok"] == json!(true) => Ok(()),
        Ok(r) => Err(format!("pane.focus refused: {r}")),
        Err(e) => Err(format!("pane.focus: {e}")),
    }
}

/// The last `n` non-empty lines of `web.log`, joined with ` | ` (one stderr line).
fn log_tail(log: &Path, n: usize) -> String {
    let text = std::fs::read(log)
        .map(|b| String::from_utf8_lossy(&b).into_owned())
        .unwrap_or_default();
    let lines: Vec<&str> = text
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .collect();
    lines[lines.len().saturating_sub(n)..].join(" | ")
}

/// Wait for our own child: up to [`EXIT_WAIT`] (its stdin already closed or about to be), then kill it.
fn reap(child: &mut Child) {
    let started = Instant::now();
    while started.elapsed() < EXIT_WAIT {
        if let Ok(Some(_)) = child.try_wait() {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let _ = child.kill();
    let _ = child.wait();
}

/// What the window's stdout reader thread passes on.
enum FromWindow {
    Frame(Frame),
    /// A frame type the window may not send, or a broken frame: protocol error.
    Bad(String),
}

/// The started window: the child, the queue into its stdin, the channel of its stdout frames.
struct Window {
    child: Child,
    pipe: mpsc::Sender<Frame>,
    frames: mpsc::UnboundedReceiver<FromWindow>,
}

impl Window {
    /// Close the pipe (EOF on its stdin once the writer thread has written what it holds) and reap the child.
    fn stop(self) {
        let Window {
            mut child,
            pipe,
            frames,
        } = self;
        drop(pipe);
        drop(frames);
        reap(&mut child);
    }
}

/// Start `fleetmux-web --gui` in `dir` with its two pipes and stderr into `web.log`.
fn start_window(
    bin: &Path,
    dir: &Path,
    run_dir: Option<&Path>,
    prefix: &str,
    theme: Option<&str>,
    devtools: bool,
) -> Result<Window, String> {
    let _ = std::fs::remove_file(dir.join("web.sock")); // a stale socket in our own directory
    let log_path = dir.join("web.log");
    let log = OpenOptions::new()
        .append(true)
        .create(true)
        .mode(0o600)
        .open(&log_path)
        .map_err(|e| format!("cannot open {}: {e}", log_path.display()))?;
    let mut cmd = Command::new(bin);
    cmd.arg("--gui").arg("--dir").arg(dir);
    if let Some(r) = run_dir {
        cmd.arg("--run-dir").arg(r);
    }
    cmd.arg("--prefix").arg(prefix);
    if let Some(t) = theme {
        cmd.arg("--theme").arg(t);
    }
    if devtools {
        cmd.arg("--devtools");
    }
    let mut child = cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(log)
        .spawn()
        .map_err(|e| format!("cannot start {}: {e}", bin.display()))?;
    let stdin = child.stdin.take().expect("piped");
    let mut stdout = child.stdout.take().expect("piped");

    // Writer: the bounded queue → the window's stdin. Ends when the queue closes (stdin dropped = EOF) or the
    // window stops reading (write error).
    let (pipe, mut queue) = mpsc::channel::<Frame>(PIPE_QUEUE);
    std::thread::Builder::new()
        .name("fleetmux-gui-pipe".into())
        .spawn(move || {
            let mut stdin = stdin;
            while let Some(f) = queue.blocking_recv() {
                if view::write_frame(&mut stdin, &f).is_err() || stdin.flush().is_err() {
                    break;
                }
            }
        })
        .map_err(|e| format!("cannot start the pipe writer: {e}"))?;
    // Reader: the window's stdout → only `0x01` / `0x02` / `0x05` / `0x06` (PROTOCOL.md §15.4); EOF closes the
    // channel.
    let (tx, frames) = mpsc::unbounded_channel();
    std::thread::Builder::new()
        .name("fleetmux-gui-stdout".into())
        .spawn(move || {
            loop {
                match view::read_frame(&mut stdout) {
                    Ok(Some(
                        f @ (Frame::Input(_)
                        | Frame::Resize { .. }
                        | Frame::ScrollTo(_)
                        | Frame::Inset(_)),
                    )) => {
                        if tx.send(FromWindow::Frame(f)).is_err() {
                            return;
                        }
                    }
                    Ok(Some(f)) => {
                        let _ = tx.send(FromWindow::Bad(format!(
                            "window sent frame type 0x{:02x}",
                            f.type_byte()
                        )));
                        return;
                    }
                    Ok(None) => return,
                    Err(e) => {
                        let _ = tx.send(FromWindow::Bad(format!("window pipe: {e:?}")));
                        return;
                    }
                }
            }
        })
        .map_err(|e| format!("cannot start the pipe reader: {e}"))?;
    Ok(Window {
        child,
        pipe,
        frames,
    })
}

/// Wait for `web.sock` to accept and for the window's first `0x02` (each within [`READY_WAIT`]). Window input that
/// comes before the `0x02` is kept for after the handshake; an `inset` (`0x06`) that comes before it is the one the
/// handshake carries (S1e, plan §17.2: the page reports it right before its first size).
async fn window_ready(
    win: &mut Window,
    dir: &Path,
) -> Result<(StdUnixStream, (u16, u16), Vec<Vec<u8>>, u16), String> {
    let sock = dir.join("web.sock");
    let started = Instant::now();
    let conn = loop {
        if let Ok(Some(status)) = win.child.try_wait() {
            return Err(format!("fleetmux-web exited ({status})"));
        }
        if let Ok(c) = StdUnixStream::connect(&sock) {
            break c;
        }
        if started.elapsed() >= READY_WAIT {
            return Err(String::from(
                "the window's web.sock did not answer within 3 s",
            ));
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    };
    let end = tokio::time::Instant::now() + READY_WAIT;
    let mut early = Vec::new();
    let mut inset = 0u16;
    let mut got_inset = false;
    loop {
        match tokio::time::timeout_at(end, win.frames.recv()).await {
            Err(_) => return Err(String::from("the page did not report its size within 3 s")),
            Ok(None) => return Err(String::from("the window closed before reporting its size")),
            Ok(Some(FromWindow::Bad(why))) => return Err(why),
            Ok(Some(FromWindow::Frame(Frame::Resize { cols, rows }))) => {
                // S1e (plan §17.2 / §17.4-3): the handshake carries the page's titlebar inset. The page sends it
                // just before its first size and the window process forwards it right after that `0x02`, so it is
                // one frame behind; wait for it here (macOS only — the page sends none on Linux). A frame that
                // never comes leaves 0: a later `0x06` still updates the server.
                if cfg!(target_os = "macos") {
                    let until = tokio::time::Instant::now() + INSET_WAIT;
                    while !got_inset {
                        match tokio::time::timeout_at(until, win.frames.recv()).await {
                            Ok(Some(FromWindow::Frame(Frame::Inset(line)))) => {
                                match view::Inset::decode(&line) {
                                    Ok(i) => {
                                        inset = i.cols;
                                        got_inset = true;
                                    }
                                    Err(e) => {
                                        return Err(format!("window sent a bad inset: {e:?}"));
                                    }
                                }
                            }
                            Ok(Some(FromWindow::Frame(Frame::Input(b)))) => early.push(b),
                            Ok(Some(FromWindow::Bad(why))) => return Err(why),
                            Ok(Some(_)) => {}
                            Ok(None) => {
                                return Err(String::from("the window closed before the handshake"));
                            }
                            Err(_) => break,
                        }
                    }
                }
                return Ok((conn, (cols, rows), early, inset));
            }
            Ok(Some(FromWindow::Frame(Frame::Input(b)))) => early.push(b),
            Ok(Some(FromWindow::Frame(Frame::Inset(line)))) => match view::Inset::decode(&line) {
                Ok(i) => {
                    inset = i.cols;
                    got_inset = true;
                }
                Err(e) => return Err(format!("window sent a bad inset: {e:?}")),
            },
            Ok(Some(FromWindow::Frame(_))) => {}
        }
    }
}

/// Hand `frames` (`0x04`) to the server, then the client's notes and its page fallbacks (GUI: shown as notices;
/// the embedded page is the window, so no system opener runs for them).
async fn web_out(
    w: &mut tokio::net::unix::OwnedWriteHalf,
    frames: Vec<Frame>,
    client: &mut WebClient,
    notes: &mpsc::UnboundedSender<String>,
) -> io::Result<()> {
    for f in &frames {
        send(w, f).await?;
    }
    for note in client.take_notes() {
        let _ = notes.send(note);
    }
    for (path, why) in client.take_fallbacks() {
        let note = match why {
            Some(why) => format!("cannot show the history page: {path} ({why})"),
            None => format!("cannot show the history page here: {path}"),
        };
        let _ = notes.send(note);
    }
    Ok(())
}

/// Why the relay ended.
enum End {
    /// Detach (prefix + `d`): `0x03` already sent.
    Detached,
    /// The window went away (closed by the person, or died): `window closed` + `0x03` still to send.
    WindowGone,
    /// The server said why (`0x82`), already shown in the window for [`NOTICE_FOR`].
    ServerExit(String),
    /// Server gone, signal, protocol error.
    Other(String),
}

async fn gui(
    opts: &Opts,
    sock: &Path,
    bin: &Path,
    prefix_name: &str,
    history_url: Option<String>,
    theme: Option<String>,
) -> ExitCode {
    let runtime_shown = opts.runtime.display().to_string();
    let fail4 = |why: &str| {
        eprintln!("{}", exit4_line(why, &runtime_shown));
        ExitCode::from(4)
    };
    let (mut webc, mut web_events) = WebClient::new(&opts.runtime);
    let dir = match webc.gui_dir() {
        Ok(d) => d,
        Err(e) => return fail4(&e),
    };
    let first_scope = history_url.as_deref().and_then(scope_for);
    let mut win = match start_window(
        bin,
        &dir,
        first_scope.as_deref(),
        prefix_name,
        theme.as_deref(),
        opts.devtools,
    ) {
        Ok(w) => w,
        Err(e) => return fail4(&e),
    };
    let (conn, (cols, rows), early, inset) = match window_ready(&mut win, &dir).await {
        Ok(r) => r,
        Err(e) => {
            let _ = win.child.kill();
            win.stop();
            let tail = log_tail(&dir.join("web.log"), LOG_TAIL);
            let why = if tail.is_empty() {
                e
            } else {
                format!("{e} (web.log: {tail})")
            };
            return fail4(&why);
        }
    };
    let mut gui_events = webc.adopt_gui(conn, dir.clone());

    // The view connection: its own peer check, then the handshake at the page's first size, with "overlay":1,
    // "scroll":1 and (S1e, plan §17.2 / §17.3) the page's titlebar `inset`.
    let stream = match UnixStream::connect(sock)
        .await
        .and_then(|s| peer::verify(&s).map(|()| s))
    {
        Ok(s) => s,
        Err(e) => {
            webc.shutdown();
            win.stop();
            unreachable_server_as("gui", &opts.runtime, sock, &e);
            return ExitCode::from(3);
        }
    };
    let (rd, mut wr) = stream.into_split();
    let mut rd = tokio::io::BufReader::new(rd);
    let hello = json!({
        "view": view::VIEW_VERSION,
        "cols": cols,
        "rows": rows,
        "term": "xterm-256color",
        "colorterm": "truecolor",
        "overlay": 1,
        "native_header": u8::from(cfg!(target_os = "linux")),
        "scroll": 1,
        "inset": inset,
    });
    let mut line = String::new();
    let shake = async {
        wr.write_all(format!("{hello}\n").as_bytes()).await?;
        rd.read_line(&mut line).await
    };
    // S1e (plan §17.3): `true` when the `ok` line carries `"inset":1` — the server knows the handshake key and the
    // `0x06` frame, so the window's later insets (a font-size change) may go to it; an old server must never see
    // one (unknown type = connection closed, PROTOCOL.md §6.2).
    let shaken = match shake.await {
        Ok(_) => match serde_json::from_str::<Value>(&line) {
            Ok(v) if v["ok"] == json!(true) => Ok((v["inset"] == json!(1), v["pages"] == json!(1))),
            _ => Err(format!("view handshake refused: {}", line.trim_end())),
        },
        Err(e) => Err(format!("view handshake: {e}")),
    };
    let inset_ok = match shaken {
        // M4 (PROTOCOL.md §6.1): `"pages":1` — the window's `?fleet-jump=` links go to the server.
        Ok((flag, pages)) => {
            webc.set_pages(pages);
            flag
        }
        Err(why) => {
            webc.shutdown();
            win.stop();
            eprintln!("fleetmux gui: {why}");
            return ExitCode::from(3);
        }
    };

    let (note_tx, note_rx) = mpsc::unbounded_channel::<String>();
    let mut relay = Relay {
        sock: sock.to_path_buf(),
        rows,
        cols,
        scanner: InputScanner::new(input::prefix_byte(prefix_name)),
        hist_pane: None,
        loaded_scope: None,
        loaded: false,
        open_reported: false,
        inset_ok,
        stash: VecDeque::new(),
        note_tx,
    };
    let end = relay
        .run(
            &mut rd,
            &mut wr,
            &mut win,
            &mut webc,
            &mut web_events,
            &mut gui_events,
            note_rx,
            early,
        )
        .await;
    if let End::WindowGone = end {
        let _ = send(&mut wr, &Frame::Detach).await;
    }
    let _ = wr.shutdown().await;
    // Every way out: `quit` the window (its `web.sock`), close its stdin, wait for it — no orphan.
    webc.shutdown();
    win.stop();
    match end {
        End::Detached | End::WindowGone => {}
        End::ServerExit(r) | End::Other(r) => {
            if !r.is_empty() {
                eprintln!("[fleetmux: {r}]");
            }
        }
    }
    ExitCode::SUCCESS
}

/// The relay's own state (beside the `WebClient`).
struct Relay {
    sock: PathBuf,
    /// The window's size (its latest `0x02`); notices go on its bottom row.
    cols: u16,
    rows: u16,
    scanner: InputScanner,
    /// The history pane (`pane` of the latest `0x85`).
    hist_pane: Option<String>,
    /// The run directory last sent with `scope` for a load; `loaded` once a load of `history_url` started.
    loaded_scope: Option<PathBuf>,
    loaded: bool,
    /// The window's `{"event":"window","state":"open"}` went to the server (see [`Relay::report_open`]).
    open_reported: bool,
    /// S1e (plan §17.3): the `ok` line carried `"inset":1`, so the window's `0x06` frames may be forwarded.
    inset_ok: bool,
    /// Pipe frames that found the queue full, in order; while any wait, the server socket is not read.
    stash: VecDeque<Frame>,
    note_tx: mpsc::UnboundedSender<String>,
}

/// A `select!` arm that never fires when `at` is `None`.
async fn sleep_until(at: Option<tokio::time::Instant>) {
    match at {
        Some(t) => tokio::time::sleep_until(t).await,
        None => std::future::pending().await,
    }
}

impl Relay {
    /// Queue a frame for the window pipe, or stash it when the queue is full (the caller then stops reading the
    /// server). `false` = the pipe is gone (the window died).
    fn queue_pipe(&mut self, win: &Window, f: Frame) -> bool {
        if !self.stash.is_empty() {
            self.stash.push_back(f);
            return true;
        }
        match win.pipe.try_send(f) {
            Ok(()) => true,
            Err(mpsc::error::TrySendError::Full(f)) => {
                self.stash.push_back(f);
                true
            }
            Err(mpsc::error::TrySendError::Closed(_)) => false,
        }
    }

    /// A `0x85`: remember the history pane; the first url, or a url in another run directory, is scoped and
    /// loaded into the embedded page (scope first, PROTOCOL.md §15.4).
    async fn on_overlay(
        &mut self,
        line: &str,
        wr: &mut tokio::net::unix::OwnedWriteHalf,
        webc: &mut WebClient,
    ) -> io::Result<()> {
        let Ok(o) = Overlay::decode(line) else {
            return Ok(()); // the window ignores it as well; nothing to load
        };
        self.hist_pane = o.pane.clone();
        let Some(url) = o.url else { return Ok(()) };
        let scope = scope_for(&url);
        if self.loaded && scope == self.loaded_scope {
            return Ok(());
        }
        self.loaded = true;
        self.loaded_scope = scope.clone();
        if let Some(run_dir) = &scope
            && let Err(e) = webc.set_scope(run_dir)
        {
            let _ = self.note_tx.send(format!("web: scope: {e}"));
        }
        let frames = webc.load_history(&url);
        web_out(wr, frames, webc, &self.note_tx).await
    }

    /// Report the window's `open` (from `adopt_gui`) so `web.*` counts it as a window — only once the first load
    /// of `history_url` has finished (no page exchange pending), so a `web.goto` from fleet can never land before
    /// that load and be overwritten by it. `force`: report now (no `0x85` came: old server, nothing to load).
    async fn report_open(
        &mut self,
        wr: &mut tokio::net::unix::OwnedWriteHalf,
        webc: &mut WebClient,
        force: bool,
    ) -> io::Result<()> {
        if self.open_reported || (!force && webc.deadline().is_some()) {
            return Ok(());
        }
        self.open_reported = true;
        web_out(wr, webc.take_frames(), webc, &self.note_tx).await
    }

    /// Prefix `o` (`0x83`): the history pane and the embedded page when there is one, else the M2.5 steps after
    /// the window (opener → path notice), off the event loop.
    fn on_open_key(&mut self, path: String, webc: &mut WebClient) {
        if let Some(pane) = self.hist_pane.clone() {
            if let Err(e) = focus_pane(&self.sock, &pane) {
                let _ = self.note_tx.send(e);
            }
            if let Err(e) = webc.focus_page() {
                let _ = self.note_tx.send(format!("web: focus: {e}"));
            }
            return;
        }
        let tx = self.note_tx.clone();
        tokio::task::spawn_blocking(move || {
            if let Some(note) = super::clipboard::open_notice(&path) {
                let _ = tx.send(note);
            }
        });
    }

    #[allow(clippy::too_many_arguments)]
    async fn run(
        &mut self,
        rd: &mut tokio::io::BufReader<tokio::net::unix::OwnedReadHalf>,
        wr: &mut tokio::net::unix::OwnedWriteHalf,
        win: &mut Window,
        webc: &mut WebClient,
        web_events: &mut mpsc::UnboundedReceiver<u64>,
        gui_events: &mut mpsc::UnboundedReceiver<GuiEvent>,
        mut note_rx: mpsc::UnboundedReceiver<String>,
        early: Vec<Vec<u8>>,
    ) -> End {
        let ioerr = |_| End::Other(String::from("server gone"));
        for b in early {
            if let Some(end) = self.window_input(wr, &b).await {
                return end;
            }
        }
        let mut term = signal(SignalKind::terminate()).ok();
        let mut hup = signal(SignalKind::hangup()).ok();
        let mut int = signal(SignalKind::interrupt()).ok();
        let mut buf: Vec<u8> = Vec::with_capacity(256 * 1024);
        let mut redraw_at: Option<tokio::time::Instant> = None;
        let mut seen_screen = false;
        let mut seen_overlay = false;
        let mut overlay_check: Option<tokio::time::Instant> = None;
        loop {
            // Decode only while nothing waits for the pipe: frames keep their order.
            while self.stash.is_empty() {
                let frame = match view::decode_frame(&buf) {
                    Ok(Some((frame, used))) => {
                        buf.drain(..used);
                        frame
                    }
                    Ok(None) => break,
                    Err(e) => return End::Other(format!("protocol error: {e:?}")),
                };
                match input::on_frame(frame) {
                    ClientAction::Write(b) => {
                        if !seen_screen {
                            seen_screen = true;
                            overlay_check = Some(tokio::time::Instant::now() + OVERLAY_WAIT);
                        }
                        if !self.queue_pipe(win, Frame::Screen(b)) {
                            return End::WindowGone;
                        }
                    }
                    ClientAction::Overlay(line) => {
                        seen_overlay = true;
                        overlay_check = None;
                        if !self.queue_pipe(win, Frame::Overlay(line.clone())) {
                            return End::WindowGone;
                        }
                        if let Err(e) = self.on_overlay(&line, wr, webc).await {
                            return ioerr(e);
                        }
                        if let Err(e) = self.report_open(wr, webc, false).await {
                            return ioerr(e);
                        }
                    }
                    ClientAction::Scroll(line) => {
                        // A `0x86`: into the window pipe as it is; the page keeps the state.
                        if !self.queue_pipe(win, Frame::Scroll(line.clone())) {
                            return End::WindowGone;
                        }
                    }
                    ClientAction::Open(path) => self.on_open_key(path, webc),
                    ClientAction::Web(line) => {
                        let frames = webc.on_server_line(&line);
                        if let Err(e) = web_out(wr, frames, webc, &self.note_tx).await {
                            return ioerr(e);
                        }
                        if let Err(e) = self.report_open(wr, webc, false).await {
                            return ioerr(e);
                        }
                    }
                    ClientAction::Exit(reason) => {
                        // Shown on the window's bottom row for 3 s, then leave (PROTOCOL.md §9.5 exit 0).
                        let note = Frame::Screen(notice_bytes(&reason, self.rows));
                        if self.queue_pipe(win, note) {
                            let until = tokio::time::Instant::now() + NOTICE_FOR;
                            while let Some(f) = self.stash.pop_front() {
                                match tokio::time::timeout_at(until, win.pipe.send(f)).await {
                                    Ok(Ok(())) => {}
                                    _ => break,
                                }
                            }
                            tokio::time::sleep_until(until).await;
                        }
                        return End::ServerExit(reason);
                    }
                }
            }
            let stashed = !self.stash.is_empty();
            let web_deadline = webc.deadline().map(tokio::time::Instant::from_std);
            tokio::select! {
                r = rd.read_buf(&mut buf), if !stashed => match r {
                    Ok(0) | Err(_) => return End::Other(String::from("server gone")),
                    Ok(_) => {}
                },
                permit = win.pipe.reserve(), if stashed => match permit {
                    Ok(p) => p.send(self.stash.pop_front().expect("stashed")),
                    Err(_) => return End::WindowGone,
                },
                f = win.frames.recv() => match f {
                    None => return End::WindowGone, // stdout EOF: the window exited (closed by the person)
                    Some(FromWindow::Bad(why)) => return End::Other(format!("protocol error: {why}")),
                    Some(FromWindow::Frame(Frame::Input(b))) => {
                        if let Some(end) = self.window_input(wr, &b).await {
                            return end;
                        }
                    }
                    Some(FromWindow::Frame(Frame::Resize { cols, rows })) => {
                        self.cols = cols;
                        self.rows = rows;
                        if let Err(e) = send(wr, &Frame::Resize { cols, rows }).await {
                            return ioerr(e);
                        }
                    }
                    Some(FromWindow::Frame(Frame::ScrollTo(line))) => {
                        // A scrollbar drag: `0x05` to the server unchanged; no reply, the next `0x86` is it.
                        if let Err(e) = send(wr, &Frame::ScrollTo(line)).await {
                            return ioerr(e);
                        }
                    }
                    Some(FromWindow::Frame(Frame::Inset(line))) => {
                        // S1e (plan §17.3): the page's inset after a font-size change, `0x06` to the server — only
                        // when its `ok` line said `"inset":1`; an old server closes the connection on an unknown
                        // type (§6.2), so the frame is dropped instead.
                        if self.inset_ok
                            && let Err(e) = send(wr, &Frame::Inset(line)).await
                        {
                            return ioerr(e);
                        }
                    }
                    Some(FromWindow::Frame(_)) => {}
                },
                Some(generation) = web_events.recv() => {
                    let frames = webc.on_window_event(generation);
                    if !frames.is_empty() {
                        // The window closed (`window closed` goes to the server, then `0x03`).
                        let _ = web_out(wr, frames, webc, &self.note_tx).await;
                        return End::WindowGone;
                    }
                },
                Some(ev) = gui_events.recv() => match ev {
                    GuiEvent::HistoryFocus => {
                        if let Some(pane) = self.hist_pane.clone()
                            && let Err(e) = focus_pane(&self.sock, &pane)
                        {
                            let _ = self.note_tx.send(e);
                        }
                    }
                    // M4 (design m4 §5.3): the embedded page followed a `?fleet-jump=` link; the server switches.
                    GuiEvent::Jump(url) => {
                        if let Some(f) = webc.jump_frame(&url)
                            && let Err(e) = web_out(wr, vec![f], webc, &self.note_tx).await
                        {
                            return ioerr(e);
                        }
                    }
                },
                Some(note) = note_rx.recv() => {
                    if !self.queue_pipe(win, Frame::Screen(notice_bytes(&note, self.rows))) {
                        return End::WindowGone;
                    }
                    redraw_at = Some(tokio::time::Instant::now() + NOTICE_FOR);
                },
                _ = sleep_until(redraw_at) => {
                    // A same-size `0x02` resets the server's diff base: the next frame repaints the notice row.
                    redraw_at = None;
                    let f = Frame::Resize { cols: self.cols, rows: self.rows };
                    if let Err(e) = send(wr, &f).await {
                        return ioerr(e);
                    }
                },
                _ = sleep_until(web_deadline) => {
                    let frames = webc.on_deadline(Instant::now());
                    if let Err(e) = web_out(wr, frames, webc, &self.note_tx).await {
                        return ioerr(e);
                    }
                    if let Err(e) = self.report_open(wr, webc, false).await {
                        return ioerr(e);
                    }
                },
                _ = sleep_until(overlay_check) => {
                    overlay_check = None;
                    if !seen_overlay {
                        let _ = self.note_tx.send(String::from(
                            "this server does not support the embedded history page",
                        ));
                        if let Err(e) = self.report_open(wr, webc, true).await {
                            return ioerr(e);
                        }
                    }
                },
                _ = async { match term.as_mut() { Some(s) => { s.recv().await; } None => std::future::pending().await } } => {
                    return End::Other(String::from("terminated"));
                },
                _ = async { match hup.as_mut() { Some(s) => { s.recv().await; } None => std::future::pending().await } } => {
                    return End::Other(String::from("hangup"));
                },
                _ = async { match int.as_mut() { Some(s) => { s.recv().await; } None => std::future::pending().await } } => {
                    return End::Other(String::from("interrupted"));
                },
            }
        }
    }

    /// Window `0x01`: the attach input scanner (prefix + `d` = detach), the rest to the server.
    async fn window_input(
        &mut self,
        wr: &mut tokio::net::unix::OwnedWriteHalf,
        bytes: &[u8],
    ) -> Option<End> {
        let o = input::client_input(&mut self.scanner, bytes, Instant::now());
        if !o.forward.is_empty() && send(wr, &Frame::Input(o.forward)).await.is_err() {
            return Some(End::Other(String::from("server gone")));
        }
        if o.detach {
            let _ = send(wr, &Frame::Detach).await;
            return Some(End::Detached);
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn parse_opts_runtime_and_devtools() {
        assert_eq!(
            parse_opts(&args(&["--runtime", "/r", "--devtools"])),
            Ok(Opts {
                runtime: "/r".into(),
                devtools: true
            })
        );
        assert_eq!(
            parse_opts(&args(&["--runtime", "/r"])),
            Ok(Opts {
                runtime: "/r".into(),
                devtools: false
            })
        );
        assert!(parse_opts(&args(&[])).is_err());
        assert!(parse_opts(&args(&["--runtime"])).is_err());
        assert!(parse_opts(&args(&["--runtime", "/r", "--test-hooks"])).is_err());
    }

    #[test]
    fn scope_is_the_page_directory_of_a_file_url() {
        assert_eq!(
            scope_for("file:///r/run%201/history.html?x=1#h"),
            Some(PathBuf::from("/r/run 1"))
        );
        assert_eq!(
            scope_for("file://localhost/r/history.html"),
            Some(PathBuf::from("/r"))
        );
        assert_eq!(scope_for("https://example.com/history.html"), None);
        assert_eq!(scope_for("file://host/r/history.html"), None);
        assert_eq!(scope_for(""), None);
    }

    #[test]
    fn exit_4_line_has_reason_and_attach_hint() {
        let line = exit4_line("no graphical session", "/run/x");
        assert_eq!(
            line,
            "fleetmux gui: no graphical session; use: fleetmux attach --runtime /run/x"
        );
        assert!(!line.contains('\n'));
    }
}
