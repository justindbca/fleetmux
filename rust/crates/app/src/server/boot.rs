//! `fleetmux server` start-up (PROTOCOL.md §9.1): option parsing, the whole boot sequence and the event loop.
//! Signatures from stage A (brief-m03 §3.3); bodies from B6 (server-ctl).
//!
//! The event loop is the only owner of PTY events, SIGCHLD, the `?2026` synchronized-output deadlines and render
//! scheduling. It never polls: every wake-up comes from a PTY event, a signal, a one-shot deadline that some
//! output armed, or a dirty notification, so an idle server reports a constant `server.stats` `wakeups`.

use std::collections::{BTreeMap, HashMap};
use std::fs::OpenOptions;
use std::future::Future;
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use fleetmux_layout::Layout;
use fleetmux_proto::theme;
use fleetmux_term::Palette;
use tokio::sync::{Mutex, Notify, mpsc};
use tokio::task::JoinSet;

use super::pane::PaneEvent;
use super::{State, daemonize, handlers, pane, record, socket, stats, view, views};
use crate::paths;

/// Parsed `fleetmux server` flags. Defaults: `daemonize` false, `default_size` (240, 60), `record` none,
/// `scrollback` 10000, `prefix` `"C-b"`, `palette` [`Palette::CMUX`] (cmux 0.65.0).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Opts {
    /// `--runtime D`, required.
    pub runtime: PathBuf,
    /// `--session S`, required.
    pub session: String,
    /// `--daemonize`.
    pub daemonize: bool,
    /// `--default-size COLSxROWS`: virtual screen while no viewer is connected (design.md §2.5).
    pub default_size: (u16, u16),
    /// `--record DIR` (PROTOCOL.md §7).
    pub record: Option<PathBuf>,
    /// `--scrollback N`: rows kept per pane.
    pub scrollback: usize,
    /// `--prefix KEY`: prefix key name (PROTOCOL.md §10), reported by `hello.prefix`.
    pub prefix: String,
    /// `--theme FILE`: the palette answered to OSC 10 / 11 / 12 / 4 (PROTOCOL.md §9.1), reported by
    /// `hello.theme`. A file overrides only the keys it names; the rest stays [`Palette::CMUX`].
    pub palette: Palette,
}

const USAGE: &str = "usage: fleetmux server --runtime D --session S [--daemonize] [--default-size COLSxROWS] \
                     [--record DIR] [--scrollback N] [--prefix KEY] [--theme FILE]";

/// Parse the arguments after `server`. `Err` (a usage message) for an unknown flag, a missing `--runtime` /
/// `--session`, or a malformed value (e.g. `--default-size 100`, a 0 dimension, a non-numeric `--scrollback`).
pub fn parse_opts(args: &[String]) -> Result<Opts, String> {
    let mut runtime = None;
    let mut session = None;
    let mut o = Opts {
        runtime: PathBuf::new(),
        session: String::new(),
        daemonize: false,
        default_size: (240, 60),
        record: None,
        scrollback: 10_000,
        prefix: "C-b".into(),
        palette: Palette::CMUX,
    };
    let mut it = args.iter();
    while let Some(flag) = it.next() {
        let mut value = || {
            it.next()
                .cloned()
                .ok_or_else(|| format!("{flag} needs a value\n{USAGE}"))
        };
        match flag.as_str() {
            "--runtime" => runtime = Some(PathBuf::from(value()?)),
            "--session" => session = Some(value()?),
            "--daemonize" => o.daemonize = true,
            "--default-size" => {
                let v = value()?;
                o.default_size = parse_size(&v).ok_or_else(|| {
                    format!("--default-size {v:?}: expected COLSxROWS, each 1-65535\n{USAGE}")
                })?;
            }
            "--record" => o.record = Some(PathBuf::from(value()?)),
            "--scrollback" => {
                let v = value()?;
                o.scrollback = v
                    .parse()
                    .map_err(|_| format!("--scrollback {v:?}: expected a row count\n{USAGE}"))?;
            }
            "--prefix" => {
                let v = value()?;
                prefix_bytes(&v)
                    .ok_or_else(|| format!("--prefix {v:?}: expected C-a … C-z\n{USAGE}"))?;
                o.prefix = v;
            }
            "--theme" => {
                let v = value()?;
                o.palette = parse_theme_file(&v)?;
            }
            other => return Err(format!("unknown flag {other:?}\n{USAGE}")),
        }
    }
    o.runtime = runtime.ok_or_else(|| format!("--runtime is required\n{USAGE}"))?;
    o.session = session.ok_or_else(|| format!("--session is required\n{USAGE}"))?;
    Ok(o)
}

/// `--theme FILE` (PROTOCOL.md §9.1): a JSON object whose four keys are all optional, so a file may override just
/// `bg`. Every key it does name must be well-formed: `bg` / `fg` / `cursor` are `#rrggbb` (either case) and `ansi`
/// is exactly 16 of them. An unknown key, a bad value, a non-object or an unreadable file is a usage error —
/// nothing is silently dropped.
pub(super) fn parse_theme_file(path: &str) -> Result<Palette, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("--theme {path:?}: {e}\n{USAGE}"))?;
    let value: serde_json::Value = serde_json::from_str(&text)
        .map_err(|e| format!("--theme {path:?}: {e}\n{USAGE}"))?;
    let Some(obj) = value.as_object() else {
        return Err(format!(
            "--theme {path:?}: expected a JSON object\n{USAGE}"
        ));
    };
    let mut palette = Palette::CMUX;
    for (key, v) in obj {
        let colour = |name: &str, v: &serde_json::Value| -> Result<(u8, u8, u8), String> {
            let s = v.as_str().ok_or_else(|| {
                format!("--theme {path:?}: {name} must be a \"#rrggbb\" string\n{USAGE}")
            })?;
            theme::hex_rgb(s).ok_or_else(|| {
                format!("--theme {path:?}: {name} {s:?} is not #rrggbb\n{USAGE}")
            })
        };
        match key.as_str() {
            "bg" => palette.bg = colour("bg", v)?,
            "fg" => palette.fg = colour("fg", v)?,
            "cursor" => palette.cursor = colour("cursor", v)?,
            "ansi" => {
                let items = v.as_array().ok_or_else(|| {
                    format!("--theme {path:?}: ansi must be an array of 16 \"#rrggbb\" strings\n{USAGE}")
                })?;
                if items.len() != 16 {
                    return Err(format!(
                        "--theme {path:?}: ansi has {} colours, need 16\n{USAGE}",
                        items.len()
                    ));
                }
                let mut ansi = palette.ansi;
                for (i, item) in items.iter().enumerate() {
                    ansi[i] = colour(&format!("ansi[{i}]"), item)?;
                }
                palette.ansi = ansi;
            }
            other => {
                return Err(format!(
                    "--theme {path:?}: unknown key {other:?}\n{USAGE}"
                ));
            }
        }
    }
    Ok(palette)
}

/// `hello.theme` (PROTOCOL.md §4.1): the palette in force as `{"bg","fg","cursor","ansi"}`, every colour
/// lowercase `#rrggbb`. Read from [`OPTS`] so a running server reports what it was started with; before the options
/// are set (unit tests) it is [`Palette::CMUX`].
pub(super) fn theme_json() -> serde_json::Value {
    let p = OPTS.get().map_or(Palette::CMUX, |o| o.palette);
    serde_json::json!({
        "bg": theme::rgb_hex(p.bg),
        "fg": theme::rgb_hex(p.fg),
        "cursor": theme::rgb_hex(p.cursor),
        "ansi": p.ansi.iter().map(|c| theme::rgb_hex(*c)).collect::<Vec<_>>(),
    })
}

/// `COLSxROWS`, each 1..=65535.
fn parse_size(s: &str) -> Option<(u16, u16)> {
    let (c, r) = s.split_once('x')?;
    let (c, r) = (c.parse::<u16>().ok()?, r.parse::<u16>().ok()?);
    (c >= 1 && r >= 1).then_some((c, r))
}

/// A prefix key name as the bytes the outer terminal sends: `C-a` … `C-z` (either case) → 0x01 … 0x1a, and
/// `C-@` `C-[` `C-\` `C-]` `C-^` `C-_` → 0x00, 0x1b … 0x1f.
pub(super) fn prefix_bytes(name: &str) -> Option<Vec<u8>> {
    let key = name.strip_prefix("C-")?;
    let &[k] = key.as_bytes() else { return None };
    match k.to_ascii_lowercase() {
        c @ b'a'..=b'z' => Some(vec![c - b'a' + 1]),
        b'@' => Some(vec![0]),
        c @ (b'[' | b'\\' | b']' | b'^' | b'_') => Some(vec![c - b'@']),
        _ => None,
    }
}

// ---------------------------------------------------------------- server-wide values that `State` does not hold

/// The parsed options, kept for the server's lifetime (`State` is frozen in stage A, so `session`, the prefix
/// *name* for `hello.prefix` and the rest live here; the default PTY size lives in the viewer hub).
static OPTS: OnceLock<Opts> = OnceLock::new();
/// `<runtime>/server.log`.
static LOG: OnceLock<PathBuf> = OnceLock::new();
/// `server.shutdown` was accepted: stop accepting connections.
static STOPPING: AtomicBool = AtomicBool::new(false);
static STOP_ACCEPT: Notify = Notify::const_new();
/// The `server.shutdown` reply has been written: clean up and exit.
static EXIT: Notify = Notify::const_new();

/// `hello.prefix`.
pub(super) fn prefix_name() -> &'static str {
    OPTS.get().map_or("C-b", |o| o.prefix.as_str())
}

/// Start the shutdown (PROTOCOL.md §4.16): no new connections from now on.
pub(super) fn begin_shutdown() {
    STOPPING.store(true, Ordering::SeqCst);
    STOP_ACCEPT.notify_one();
}

pub(super) fn stopping() -> bool {
    STOPPING.load(Ordering::SeqCst)
}

/// Resolves once [`begin_shutdown`] was called (used by the accept loop).
pub(super) async fn stop_requested() {
    if !stopping() {
        STOP_ACCEPT.notified().await;
    }
}

/// The `ok` reply to `server.shutdown` is on the wire: the server may exit.
pub(super) fn shutdown_replied() {
    EXIT.notify_one();
}

/// Append one `server.log` line: `<RFC 3339 UTC> <LEVEL> <message>` (PROTOCOL.md §8.5). Never fails the caller.
pub(super) fn log(level: &str, msg: &str) {
    let Some(path) = LOG.get() else { return };
    let line = format!("{} {level} {}\n", rfc3339_now(), msg.replace('\n', " "));
    if let Ok(mut f) = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(path)
    {
        let _ = f.write_all(line.as_bytes());
    }
}

/// Current UTC time as `YYYY-MM-DDTHH:MM:SS.mmmZ`.
fn rfc3339_now() -> String {
    let d = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = d.as_secs();
    let (days, rem) = ((secs / 86_400) as i64, secs % 86_400);
    // Civil-from-days (proleptic Gregorian), days since 1970-01-01.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}Z",
        rem / 3_600,
        rem % 3_600 / 60,
        rem % 60,
        d.subsec_millis()
    )
}

/// One `ERROR` line in `server.log`, the same sentence on stderr, and the exit code.
fn fail(code: u8, msg: &str) -> ExitCode {
    log("ERROR", msg);
    eprintln!("fleetmux server: {msg}");
    ExitCode::from(code)
}

// ---------------------------------------------------------------- boot

/// `fleetmux server …`: parse options (usage error → 2), check the runtime directory's owner / mode
/// (`paths::check_runtime_perms`; → 5), log the logind warning (`reap::logind_warning`, Linux), refuse when the
/// socket already answers (→ 3) or its path is too long (→ 4), build `State`, bind, daemonize if asked, write
/// `server.json`, then run the event loop until `server.shutdown` (→ 0). Exits 3–5 write one `ERROR` line to
/// `server.log`.
///
/// The logind warning (P-11) goes through [`warn_logind`] (B17's `reap::logind_warning` does the parsing).
/// `fleetmux web …` (PROTOCOL.md §9.4, M2.5): find `fleetmux-web` (this binary's directory → `PATH` →
/// `$FLEETMUX_WEB_BIN`), refuse a different version, and `exec` it with `args`; `--ctl D CMD` sends one
/// `web.sock` command (§15.3) and prints the reply. Signature from W0 (ctl-1); W2 implements it.
pub fn web(args: &[String]) -> ExitCode {
    let fail = |code: u8, msg: &str| {
        eprintln!("fleetmux web: {msg}");
        ExitCode::from(code)
    };
    if args.first().map(String::as_str) == Some("--ctl") {
        let (Some(dir), Some(cmd)) = (args.get(1), args.get(2..).filter(|c| !c.is_empty())) else {
            return fail(
                2,
                "usage: fleetmux web --ctl <web runtime dir> <open URL | goto URL | url | reload | quit>",
            );
        };
        let request = match web_ctl_request(cmd) {
            Ok(r) => r,
            Err(msg) => return fail(2, &msg),
        };
        return match web_ctl(Path::new(dir), &request, WEB_CTL_TIMEOUT) {
            Ok((ok, line)) => {
                println!("{line}");
                ExitCode::from(if ok { 0 } else { 1 })
            }
            Err(msg) => fail(1, &msg),
        };
    }
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf));
    let bin = match locate_web(
        exe_dir.as_deref(),
        std::env::var_os("PATH").as_deref(),
        crate::attach::web::web_bin_env().as_deref(),
    ) {
        Ok(b) => b,
        Err(msg) => return fail(3, &msg),
    };
    let theirs = match web_version(&bin) {
        Ok(v) => v,
        Err(msg) => return fail(1, &msg),
    };
    if args.first().map(String::as_str) == Some("--version") && args.len() == 1 {
        println!("{theirs}");
        return ExitCode::SUCCESS;
    }
    let ours = web_version_line();
    if theirs != ours {
        return fail(
            1,
            &format!(
                "{} has a different version: \"{theirs}\", this fleetmux wants \"{ours}\"",
                bin.display()
            ),
        );
    }
    use std::os::unix::process::CommandExt;
    let err = std::process::Command::new(&bin).args(args).exec();
    fail(1, &format!("cannot run {}: {err}", bin.display()))
}

/// `$FLEETMUX_WEB_BIN`: the third place `fleetmux web` looks for `fleetmux-web` (PROTOCOL.md §9.4).
const WEB_BIN_VAR: &str = "FLEETMUX_WEB_BIN";
/// How long `fleetmux web --ctl` waits for the window's reply line.
const WEB_CTL_TIMEOUT: Duration = Duration::from_secs(3);

/// The version line a matching `fleetmux-web --version` prints: `fleetmux-web <x.y.z> (protocol 1)`.
fn web_version_line() -> String {
    format!(
        "fleetmux-web {} (protocol {})",
        env!("CARGO_PKG_VERSION"),
        fleetmux_proto::PROTOCOL
    )
}

/// `fleetmux-web` in this binary's directory, else the first on `PATH`, else `$FLEETMUX_WEB_BIN`.
fn locate_web(
    exe_dir: Option<&Path>,
    path: Option<&std::ffi::OsStr>,
    var: Option<&std::ffi::OsStr>,
) -> Result<PathBuf, String> {
    use std::os::unix::fs::PermissionsExt;
    let runnable = |p: &Path| {
        std::fs::metadata(p).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
    };
    let beside = exe_dir.map(|d| d.join("fleetmux-web"));
    let on_path = path.and_then(|p| {
        std::env::split_paths(p)
            .filter(|d| !d.as_os_str().is_empty())
            .map(|d| d.join("fleetmux-web"))
            .find(|c| runnable(c))
    });
    let from_var = var.filter(|v| !v.is_empty()).map(PathBuf::from);
    if let Some(b) = beside
        .iter()
        .chain(on_path.iter())
        .chain(from_var.iter())
        .find(|c| runnable(c))
    {
        return Ok(b.clone());
    }
    Err(format!(
        "fleetmux-web not found: looked in {}, on PATH, and in ${WEB_BIN_VAR} ({})",
        beside.map_or("this binary's directory (unknown)".into(), |b| b
            .display()
            .to_string()),
        from_var.map_or("unset".into(), |v| v.display().to_string()),
    ))
}

/// The first line `bin --version` prints.
fn web_version(bin: &Path) -> Result<String, String> {
    let out = std::process::Command::new(bin)
        .arg("--version")
        .stdin(std::process::Stdio::null())
        .output()
        .map_err(|e| format!("cannot run {} --version: {e}", bin.display()))?;
    let text = String::from_utf8_lossy(&out.stdout);
    match text.lines().next() {
        Some(l) if out.status.success() => Ok(l.trim_end().to_owned()),
        _ => Err(format!(
            "{} --version failed ({})",
            bin.display(),
            out.status
        )),
    }
}

/// The `web.sock` request line (PROTOCOL.md §15.3) for `--ctl … CMD`.
fn web_ctl_request(cmd: &[String]) -> Result<serde_json::Value, String> {
    use serde_json::json;
    let words: Vec<&str> = cmd.iter().map(String::as_str).collect();
    match words.as_slice() {
        [op @ ("open" | "goto"), url] => Ok(json!({"cmd": op, "url": url})),
        [op @ ("url" | "reload" | "quit")] => Ok(json!({"cmd": op})),
        _ => Err(format!(
            "unknown --ctl command {:?}: want open URL | goto URL | url | reload | quit",
            words.join(" ")
        )),
    }
}

/// Send one request to `<dir>/web.sock` and read its reply line (event lines are skipped): `(ok, line)`.
fn web_ctl(
    dir: &Path,
    request: &serde_json::Value,
    wait: Duration,
) -> Result<(bool, String), String> {
    use std::io::BufRead;
    let sock = dir.join("web.sock");
    let mut stream = std::os::unix::net::UnixStream::connect(&sock)
        .map_err(|e| format!("cannot connect to {}: {e}", sock.display()))?;
    stream
        .set_read_timeout(Some(wait))
        .map_err(|e| format!("{}: {e}", sock.display()))?;
    stream
        .write_all(format!("{request}\n").as_bytes())
        .map_err(|e| format!("cannot write to {}: {e}", sock.display()))?;
    let mut reader = io::BufReader::new(stream);
    loop {
        let mut line = String::new();
        match reader.read_line(&mut line) {
            Ok(0) => return Err(format!("{} closed without a reply", sock.display())),
            Ok(_) => {}
            Err(e) => {
                return Err(format!(
                    "the window did not answer on {}: {e}",
                    sock.display()
                ));
            }
        }
        let line = line.trim_end().to_owned();
        let v: serde_json::Value = serde_json::from_str(&line).unwrap_or_default();
        if v.get("event").is_some() {
            continue;
        }
        return Ok((v.get("ok") == Some(&serde_json::Value::Bool(true)), line));
    }
}

pub fn run(args: &[String]) -> ExitCode {
    let opts = match parse_opts(args) {
        Ok(o) => o,
        Err(msg) => {
            eprintln!("fleetmux server: {msg}");
            return ExitCode::from(2);
        }
    };
    if opts.daemonize {
        // fix-m0-6: before anything of ours is open, so only the starter's descriptors go (stdio stays).
        daemonize::close_inherited_fds();
    }
    let runtime = opts.runtime.clone();
    let _ = LOG.set(runtime.join("server.log"));

    if let Err(e) = paths::check_runtime_perms(&runtime) {
        return fail(5, &format!("runtime directory {}: {e}", runtime.display()));
    }
    // P-11: after the log path and the runtime-directory check, before bind / daemonize, so the stderr copy
    // still reaches the starting terminal.
    warn_logind();
    let sock = match paths::socket_path(&runtime) {
        Ok(s) => s,
        Err(e) => {
            return fail(
                4,
                &format!("{}: {e}", runtime.join(paths::SOCKET_NAME).display()),
            );
        }
    };

    // `start.lock` belongs to starters (design.md §2.3): one may hold it right now while it waits for this
    // server's `hello`, so the server never locks it (fix-m0-2); it only makes sure the file exists (§8.4).
    if let Err(e) = daemonize::touch_start_lock(&runtime) {
        log(
            "WARN",
            &format!("{}: {e}", runtime.join("start.lock").display()),
        );
    }
    // Bind (single instance via `socket::bind`'s atomic publish) on a throwaway single-threaded runtime:
    // `daemonize` forks, which is only sound while the process has one thread, and kqueue / epoll registrations
    // do not survive a fork anyway.
    let bound = {
        let rt = match tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
        {
            Ok(rt) => rt,
            Err(e) => return fail(1, &format!("tokio runtime: {e}")),
        };
        let r = rt.block_on(async {
            socket::bind(&sock)
                .await
                .and_then(|l| l.into_std().map_err(socket::BindError::Io))
        });
        drop(rt);
        r
    };
    let listener = match bound {
        Err(socket::BindError::AlreadyAnswering) => {
            return fail(
                3,
                &format!(
                    "{} already answers: another server is running",
                    sock.display()
                ),
            );
        }
        Err(socket::BindError::TooLong { len, max }) => {
            return fail(
                4,
                &format!(
                    "{}: socket path is {len} bytes, over the {max}-byte limit",
                    sock.display()
                ),
            );
        }
        Err(socket::BindError::Io(e)) => {
            return fail(1, &format!("bind {}: {e}", sock.display()));
        }
        Ok(l) => l,
    };

    if opts.daemonize
        && let Err(e) = daemonize::daemonize(&runtime.join("server.log"))
    {
        let _ = std::fs::remove_file(&sock);
        return fail(1, &format!("daemonize: {e}"));
    }
    let _ = OPTS.set(opts);

    let rt = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            let _ = std::fs::remove_file(&sock);
            return fail(1, &format!("tokio runtime: {e}"));
        }
    };
    let code = rt.block_on(serve_until_shutdown(listener, sock));
    // PTY reader threads may still block in read(2); the runtime must not wait for them.
    rt.shutdown_background();
    code
}

/// Build `State`, write `server.json`, start the event loop and serve until `server.shutdown`.
async fn serve_until_shutdown(
    listener: std::os::unix::net::UnixListener,
    sock: PathBuf,
) -> ExitCode {
    let opts = OPTS
        .get()
        .expect("options are set before the runtime starts");
    let runtime = opts.runtime.clone();
    let pid = std::process::id();
    let Some(start_time) = super::pty::start_time(pid) else {
        let _ = std::fs::remove_file(&sock);
        return fail(
            1,
            "cannot read this server's own start time (PROTOCOL.md §8.6)",
        );
    };
    let epoch = paths::new_epoch();
    let recorder = opts.record.clone().map(record::Recorder::new);
    let mut state = State {
        panes: pane::PaneStore::with_palette(
            epoch.clone(),
            opts.scrollback,
            recorder,
            opts.palette,
        ),
        layout: initial_layout(&runtime),
        kv: BTreeMap::new(),
        viewers: view::ViewerHub::default(),
        page: None,
        counters: stats::Counters::default(),
        runtime: runtime.clone(),
        epoch: epoch.clone(),
        start_time,
        prefix: prefix_bytes(&opts.prefix).expect("validated by parse_opts"),
        web: Default::default(),
        side: Default::default(),
        resume: Default::default(),
        fleet_pages: Default::default(),
    };
    state.viewers.set_default_size(opts.default_size);
    let events = state
        .panes
        .take_events()
        .expect("a new PaneStore hands out its events once");
    let dirty = state.viewers.dirty_notify();
    let state = Arc::new(Mutex::new(state));

    if let Err(e) = write_server_json(&runtime, pid, start_time, &epoch) {
        let _ = std::fs::remove_file(&sock);
        return fail(1, &format!("server.json: {e}"));
    }
    let listener = match listener
        .set_nonblocking(true)
        .and_then(|()| tokio::net::UnixListener::from_std(listener))
    {
        Ok(l) => l,
        Err(e) => return fail(1, &format!("listen {}: {e}", sock.display())),
    };
    log(
        "INFO",
        &format!(
            "server pid {pid} epoch {epoch} session {:?} listening on {}",
            opts.session,
            sock.display()
        ),
    );

    let mut core: JoinSet<&'static str> = JoinSet::new();
    let reap = Arc::new(Notify::new());
    let st = state.clone();
    let wake = reap.clone();
    core.spawn(async move {
        pty_events(st, events, wake).await;
        "PTY event task"
    });
    core.spawn(async move {
        child_exits(reap).await;
        "SIGCHLD task"
    });
    let st = state.clone();
    core.spawn(async move {
        render(st, dirty).await;
        "render task"
    });
    // The closed-fleet reaper returns once it started the shutdown, so it must not be one of the `core` tasks.
    let st = state.clone();
    tokio::spawn(closed_reaper(st));
    // Dual tmux views (fleetmux design §44): the tmux client in view keeps the window size (view::tmux_watch_tick).
    let st = state.clone();
    tokio::spawn(tmux_watch(st));

    // `serve` returns once `server.shutdown` was accepted (or the listener failed).
    match supervise(socket::serve(listener, state.clone()), &mut core).await {
        Served::Done(Ok(())) => {}
        Served::Done(Err(e)) => log("ERROR", &format!("accept loop: {e}")),
        Served::CoreFailed(why) => {
            // Dropping the accept loop closes the listener, and returning ends the process, so no client sees
            // a healthy `hello` from a server that no longer reads PTYs, reaps or draws. The panes keep
            // running; ctl.sock, server.json and panes.json stay for identity checks and `fleetmux reap`.
            return fail(
                1,
                &format!("{why}; server stopped, panes.json kept for `fleetmux reap`"),
            );
        }
    }
    // From here on the core tasks are cancelled on purpose (dropped with `core`), which is not a fault.
    if stopping() {
        // The `ok` reply goes out from its connection task; give it, and the `0x82` frames to viewers, a
        // moment to reach the peers. Bounded waits, during shutdown only.
        let _ = tokio::time::timeout(Duration::from_secs(2), EXIT.notified()).await;
        let deadline = Instant::now() + Duration::from_millis(500);
        while Instant::now() < deadline && !state.lock().await.viewers.is_empty() {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }
    for name in [paths::SOCKET_NAME, "server.json", "panes.json"] {
        let _ = std::fs::remove_file(runtime.join(name));
    }
    log("INFO", "server exiting");
    ExitCode::SUCCESS
}

/// PROTOCOL.md §8.7 default; `$FLEETMUX_LOGIND_CONF` overrides it.
const LOGIND_CONF: &str = "/etc/systemd/logind.conf";

/// The logind config to check: `$FLEETMUX_LOGIND_CONF` when set and non-empty, else [`LOGIND_CONF`].
fn logind_conf(env: Option<std::ffi::OsString>) -> PathBuf {
    env.filter(|v| !v.is_empty())
        .map_or_else(|| PathBuf::from(LOGIND_CONF), PathBuf::from)
}

/// P-11 on Linux: when the logind config has an uncommented `KillUserProcesses=yes`, one `WARN` line in
/// `server.log` and the same sentence, with `WARN`, on stderr. `reap::logind_warning` (server-1, B17) owns the
/// parsing and returns the sentence without timestamp or level; it never changes the system.
fn warn_logind() {
    if !cfg!(target_os = "linux") {
        return;
    }
    let conf = logind_conf(std::env::var_os("FLEETMUX_LOGIND_CONF"));
    if let Some(msg) = super::reap::logind_warning(&conf) {
        log("WARN", &msg);
        eprintln!("WARN {msg}");
    }
}

/// Explicit legacy/test content mirror; both platforms default to stack left, workers right.
fn content_mirror() -> bool {
    match std::env::var("FLEETMUX_LAYOUT_MIRROR") {
        Ok(v) => match v.trim().to_ascii_lowercase().as_str() {
            "1" | "true" | "yes" => true,
            "0" | "false" | "no" => false,
            _ => false,
        },
        Err(_) => false,
    }
}

/// A new layout, reusing only the ratios of an earlier `layout.json` (PROTOCOL.md §8.3) through the shared
/// `views::load_ratios`: its groups, selection and zoom name panes of an earlier epoch and are dropped. A bad
/// file is a `WARN` (from `load_ratios`), never fatal.
///
/// Ordering and native chrome are re-derived on boot, never read from layout.json.
/// `server.json` (PROTOCOL.md §8.1): `daemonize::write_server_json`'s keys plus M4's capability `"pages": 1`
/// (design m4 §4.2 `server.json` row), written the same atomic way.
fn write_server_json(runtime: &Path, pid: u32, start_time: u64, epoch: &str) -> std::io::Result<()> {
    let body = serde_json::json!({
        "pid": pid,
        "start_time": start_time,
        "version": env!("CARGO_PKG_VERSION"),
        "protocol": fleetmux_proto::PROTOCOL,
        "epoch": epoch,
        "pages": 1,
    });
    daemonize::write_atomic(&runtime.join("server.json"), body.to_string().as_bytes())
}

fn initial_layout(runtime: &Path) -> Layout {
    let mut layout = Layout::for_platform();
    layout.set_mirror(content_mirror());
    let native = match std::env::var("FLEETMUX_NATIVE_HEADER").as_deref() {
        Ok("0" | "false" | "no") => false,
        Ok("1" | "true" | "yes") => true,
        _ => cfg!(target_os = "linux"),
    };
    layout.set_native_header(native);
    layout.set_header(if native { fleetmux_layout::HEADER_GAP } else { 0 });
    if let Some((h, v)) = views::load_ratios(&runtime.join("layout.json")) {
        layout.set_ratios(h, v);
    }
    layout
}

// ---------------------------------------------------------------- event loop

/// How serving ended: the accept loop returned (shutdown or listener failure), or a core task ended first.
#[derive(Debug)]
enum Served {
    Done(io::Result<()>),
    /// The `ERROR` sentence: which core task ended, and how.
    CoreFailed(String),
}

/// Run the accept loop while watching the core tasks (PTY events, SIGCHLD, render). None of them ends on its
/// own while the server runs, so the first one to return or panic is a fault and wins over the accept loop.
async fn supervise(
    serve: impl Future<Output = io::Result<()>>,
    core: &mut JoinSet<&'static str>,
) -> Served {
    tokio::select! {
        r = serve => Served::Done(r),
        Some(joined) = core.join_next() => Served::CoreFailed(match joined {
            Ok(name) => format!("{name} ended unexpectedly"),
            Err(e) if e.is_panic() => {
                let payload = e.into_panic();
                let msg = payload
                    .downcast_ref::<&str>()
                    .map(|s| (*s).to_string())
                    .or_else(|| payload.downcast_ref::<String>().cloned())
                    .unwrap_or_else(|| "non-string panic".to_string());
                format!("core task panicked: {msg}")
            }
            Err(e) => format!("core task cancelled: {e}"),
        }),
    }
}

/// `?2026` synchronized output (PROTOCOL.md §6.3 / design.md §2.7): the parser holds the screen while a
/// synchronized update is open and gives up after 150 ms. The parser only notices that timeout when it is fed,
/// so for each pane that enters a synchronized update this loop arms **one** deadline 150 ms after the entry;
/// output that arrives meanwhile does not move it. When it fires the pane is fed an empty slice (which runs the
/// parser's timers) and redrawn. If the parser still reports a synchronized update then (a nested or new `BSU`
/// restarted its own 150 ms), one more one-shot deadline is armed 150 ms later; there is no retry cap, so a held
/// screen is always released by the parser's own timeout. Output that ends the update, or the pane going away,
/// drops the deadline. With no pane synchronized there is no timer at all.
const SYNC_TIMEOUT: Duration = Duration::from_millis(150);

/// How many already-sent PTY events the SIGCHLD turn applies under the state lock before it reaps. The batch is what
/// gives the parser the exiting pane's last output; it is capped so that an unrelated pane which keeps producing
/// output cannot hold the lock (or postpone the reap) indefinitely (P-04-REAP). Whatever is left stays queued and is
/// applied by the normal event path.
const REAP_DRAIN_EVENTS: usize = 256;

/// One PTY event, applied under the state lock. `Closed` reaps too: its EOF is what lets a reaped child be
/// finalised (P-04-REAP).
fn apply_event(st: &mut State, sync: &mut HashMap<String, Instant>, event: PaneEvent) {
    match event {
        PaneEvent::Output { pane, bytes } => {
            // `contains`, not `info`: a pane whose child is still being reaped keeps its parser so its last output
            // is still fed (A42-LINUX-RESTORE-FAILURE-04).
            if bytes.is_empty() || !st.panes.contains(&pane) {
                return;
            }
            st.panes.feed_output(&pane, &bytes);
            st.counters
                .parse_bytes
                .fetch_add(bytes.len() as u64, Ordering::Relaxed);
            if in_sync(st, &pane) {
                sync.entry(pane).or_insert(Instant::now() + SYNC_TIMEOUT);
            } else {
                sync.remove(&pane);
            }
            st.viewers.mark_dirty();
        }
        PaneEvent::Closed { pane } => {
            sync.remove(&pane);
            reap_and_settle(st);
        }
    }
}

/// PTY output and EOF, plus the synchronized-output deadlines they arm and the SIGCHLD wakeup.
///
/// A child that exits while its PTY stays open (a background grandchild holds the slave) must still be reported:
/// the SIGCHLD task only wakes this loop, which first feeds a bounded batch of the events its reader threads already
/// sent (so the parser has the pane's last output) and then reaps — one dispatcher, no sleep, no wait for the PTY to
/// close (P-04-REAP). Output that arrives after that is dropped on purpose; the pane is gone.
async fn pty_events(state: Arc<Mutex<State>>, mut events: mpsc::Receiver<PaneEvent>, reap: Arc<Notify>) {
    let mut sync: HashMap<String, Instant> = HashMap::new();
    loop {
        let next = sync.values().min().copied();
        let mut event = None;
        let mut exited = false;
        tokio::select! {
            ev = events.recv() => match ev {
                Some(ev) => event = Some(ev),
                None => return,
            },
            () = sleep_until_opt(next) => {}
            () = reap.notified() => exited = true,
        }
        let mut st = state.lock().await;
        st.counters.wakeups.fetch_add(1, Ordering::Relaxed);
        // A pane killed or reaped elsewhere loses its deadline here; its PTY EOF usually arrives as `Closed`.
        sync.retain(|pane, _| st.panes.contains(pane));
        if exited {
            // A bounded batch, not a drain to empty: another pane that keeps producing output would otherwise keep
            // `try_recv` non-empty and postpone the reap (and the lock release) for as long as it keeps writing. What
            // is still queued stays in the channel and is applied by the normal path of the next iterations.
            for _ in 0..REAP_DRAIN_EVENTS {
                let Ok(ev) = events.try_recv() else { break };
                apply_event(&mut st, &mut sync, ev);
            }
            reap_and_settle(&mut st);
            continue;
        }
        match event {
            Some(ev) => apply_event(&mut st, &mut sync, ev),
            None => {
                let now = Instant::now();
                let due: Vec<String> = sync
                    .iter()
                    .filter(|(_, at)| **at <= now)
                    .map(|(p, _)| p.clone())
                    .collect();
                for pane in due {
                    sync.remove(&pane);
                    if st.panes.info(&pane).is_none() {
                        continue;
                    }
                    // An empty feed only runs the parser's timers: not output, not counted in parse_bytes.
                    st.panes.feed_output(&pane, &[]);
                    if in_sync(&st, &pane) {
                        sync.insert(pane, now + SYNC_TIMEOUT);
                    }
                }
                st.viewers.mark_dirty();
            }
        }
    }
}

fn in_sync(st: &State, pane: &str) -> bool {
    st.panes.term(pane).is_some_and(|t| t.modes().sync_update)
}

async fn sleep_until_opt(at: Option<Instant>) {
    match at {
        Some(at) => tokio::time::sleep_until(at.into()).await,
        None => std::future::pending().await,
    }
}

/// SIGCHLD: a child exited (its PTY may or may not have reported EOF). This only wakes the PTY event loop, which
/// feeds a bounded batch of what the reader threads already sent and then reaps in that same turn (P-04-REAP) — so a
/// launcher that exits nonzero while a background child still holds the PTY slave fails the restore attempt promptly
/// instead of waiting for the PTY to close, and a pane that never stops writing cannot starve that wakeup.
async fn child_exits(reap: Arc<Notify>) {
    let mut sigchld = match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::child()) {
        Ok(s) => s,
        Err(e) => {
            log("ERROR", &format!("SIGCHLD handler: {e}"));
            return;
        }
    };
    while sigchld.recv().await.is_some() {
        reap.notify_one();
    }
}

/// Reap every exited child, take its pane out of the layout, and bring mirrors, PTY sizes, runtime files and
/// viewers up to date (PROTOCOL.md §4.0: no "dead pane" state).
fn reap_and_settle(st: &mut State) {
    let gone = st.panes.reap_exited();
    if gone.is_empty() {
        return;
    }
    for exited in &gone {
        // M4: the pane leaves whichever page holds it.
        handlers::remove_everywhere(st, &exited.pane);
        log(
            "INFO",
            &format!(
                "pane {} exited{}",
                exited.pane,
                if exited.pty_open {
                    " (its PTY was still open: later output is not included)"
                } else {
                    ""
                }
            ),
        );
        // A42-LINUX-RESTORE-FAILURE-04: a sidebar restore attempt is matched by the pane id it spawned (never by a
        // title); its exit decides whether `正在恢复…` may stay up.
        view::resume_exited(st, exited);
    }
    handlers::after_layout_change(st, true);
}

/// [`view::tmux_watch_tick`] every [`view::TMUX_WATCH_TICK`] until the server stops.
async fn tmux_watch(state: Arc<Mutex<State>>) {
    loop {
        tokio::time::sleep(view::TMUX_WATCH_TICK).await;
        if stopping() {
            return;
        }
        let mut st = state.lock().await;
        view::tmux_watch_tick(&mut st, Instant::now());
    }
}

/// How often the fleet-less reaper looks at the server (plan §13.4).
const REAPER_TICK: Duration = Duration::from_secs(1);
/// Consecutive quiet ticks before a fleet-less server exits: 10 s with 0 panes and 0 viewers.
const REAPER_TICKS: u32 = 10;

/// A fleet's server outlives its fleets: kv `sidebar` is set (plan §18.4-5 / §18.7-6) and no card is still
/// `running` / `paused` / `stopping` — the history list alone is enough. Once no pane and no viewer is left, the
/// server waits [`REAPER_TICKS`] ticks (10 s, so a gui reload or a re-attach does not lose the fleet) and then exits
/// exactly like `server.shutdown` does. Any pane or viewer resets the count. A bare `fleetmux server` (no kv
/// `sidebar`) and a server whose sidebar still lists a live fleet are never reaped, so no existing test server exits
/// by itself. Started with `tokio::spawn` on purpose: it must not sit in the `core` `JoinSet`, where a task ending
/// first means `CoreFailed` and exit code 1 (plan §13.6-6).
async fn closed_reaper(state: Arc<Mutex<State>>) {
    let mut quiet: u32 = 0;
    loop {
        tokio::time::sleep(REAPER_TICK).await;
        if stopping() {
            return;
        }
        let (reap, idle) = {
            let st = state.lock().await;
            let reap = st
                .side
                .data
                .as_ref()
                .is_some_and(|d| !crate::render::sidebar::has_live_card(d));
            (reap, st.panes.list().is_empty() && st.viewers.is_empty())
        };
        if !reap || !idle {
            quiet = 0;
            continue;
        }
        quiet += 1;
        if quiet < REAPER_TICKS {
            continue;
        }
        match handlers::shutdown(&state, false).await {
            Ok(()) => {
                // What a `server.shutdown` connection task does after its `ok` went out: without it the exit
                // path waits its full 2 s for a reply nobody is going to send (plan §13.6-6).
                shutdown_replied();
                return;
            }
            // A pane or a viewer arrived between the check above and the lock inside `shutdown`: start over.
            Err(e) => {
                log(
                    "INFO",
                    &format!("closed-fleet reaper: shutdown refused ({}); waiting again", e.1),
                );
                quiet = 0;
            }
        }
    }
}

/// Draw on demand: wait for a dirty notification, keep to `MAX_FPS` with one sleep when a frame came too soon,
/// then draw every viewer (`render_all` counts the frames).
async fn render(state: Arc<Mutex<State>>, dirty: Arc<Notify>) {
    let mut throttle = crate::render::draw::Throttle::new(crate::render::draw::MAX_FPS);
    loop {
        dirty.notified().await;
        let now = Instant::now();
        if !throttle.ready(now) {
            tokio::time::sleep(throttle.until_ready(now)).await;
            throttle.ready(Instant::now());
        }
        let mut st = state.lock().await;
        st.counters.wakeups.fetch_add(1, Ordering::Relaxed);
        view::render_all(&mut st);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn core_with(f: impl Future<Output = &'static str> + Send + 'static) -> JoinSet<&'static str> {
        let mut core = JoinSet::new();
        core.spawn(std::future::pending());
        core.spawn(f);
        core
    }

    #[test]
    fn logind_conf_env_overrides_default_unless_empty() {
        assert_eq!(logind_conf(None), PathBuf::from("/etc/systemd/logind.conf"));
        assert_eq!(
            logind_conf(Some("".into())),
            PathBuf::from("/etc/systemd/logind.conf")
        );
        assert_eq!(
            logind_conf(Some("/tmp/l.conf".into())),
            PathBuf::from("/tmp/l.conf")
        );
    }

    #[tokio::test]
    async fn supervise_core_task_return_is_a_fault() {
        let mut core = core_with(async { "render task" });
        let out = supervise(std::future::pending(), &mut core).await;
        assert!(
            matches!(&out, Served::CoreFailed(m) if m == "render task ended unexpectedly"),
            "{out:?}"
        );
    }

    #[tokio::test]
    async fn supervise_core_task_panic_is_a_fault() {
        let mut core = core_with(async { panic!("feed_output exploded") });
        let out = supervise(std::future::pending(), &mut core).await;
        assert!(
            matches!(&out, Served::CoreFailed(m) if m == "core task panicked: feed_output exploded"),
            "{out:?}"
        );
    }

    #[tokio::test]
    async fn supervise_shutdown_with_live_core_tasks_is_not_a_fault() {
        let mut core = core_with(std::future::pending());
        let out = supervise(async { Ok(()) }, &mut core).await;
        assert!(matches!(out, Served::Done(Ok(()))), "{out:?}");
        // Cancelling the core tasks afterwards (normal shutdown) is not reported anywhere.
        core.abort_all();
    }

    // ---------------------------------------------------------------- M2.5 W2: `fleetmux web`

    fn exe(dir: &Path, name: &str, body: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        std::fs::create_dir_all(dir).unwrap();
        let p = dir.join(name);
        std::fs::write(&p, body).unwrap();
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
        p
    }

    fn scratch(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("fm-w2-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn web_locate_order_beside_then_path_then_var() {
        let d = scratch("locate");
        let (beside, on_path, var) = (d.join("bin"), d.join("p1"), d.join("v"));
        let b = exe(&beside, "fleetmux-web", "#!/bin/sh\n");
        let p = exe(&on_path, "fleetmux-web", "#!/bin/sh\n");
        let v = exe(&var, "fleetmux-web", "#!/bin/sh\n");
        let path = std::env::join_paths([d.join("empty"), on_path.clone()]).unwrap();
        assert_eq!(
            locate_web(Some(&beside), Some(&path), Some(v.as_os_str())),
            Ok(b)
        );
        assert_eq!(
            locate_web(Some(&d.join("none")), Some(&path), Some(v.as_os_str())),
            Ok(p)
        );
        assert_eq!(
            locate_web(Some(&d.join("none")), None, Some(v.as_os_str())),
            Ok(v)
        );
        // Not executable, or the variable empty: not found, and the message names the three places.
        exe(&d.join("noexec"), "fleetmux-web", "x");
        std::fs::set_permissions(
            d.join("noexec/fleetmux-web"),
            std::os::unix::fs::PermissionsExt::from_mode(0o644),
        )
        .unwrap();
        let err = locate_web(Some(&d.join("noexec")), None, Some("".as_ref())).unwrap_err();
        assert!(
            err.contains("noexec") && err.contains("PATH") && err.contains(WEB_BIN_VAR),
            "{err}"
        );
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn web_version_line_matches_fleetmux() {
        assert_eq!(
            web_version_line(),
            format!("fleetmux-web {} (protocol 1)", env!("CARGO_PKG_VERSION"))
        );
        let d = scratch("version");
        let bin = exe(
            &d,
            "fleetmux-web",
            "#!/bin/sh\necho 'fleetmux-web 9.9.9 (protocol 1)'\n",
        );
        assert_eq!(
            web_version(&bin).unwrap(),
            "fleetmux-web 9.9.9 (protocol 1)"
        );
        let bad = exe(&d, "broken", "#!/bin/sh\nexit 4\n");
        assert!(web_version(&bad).is_err());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn web_ctl_requests_follow_protocol_15_3() {
        let words = |s: &str| s.split(' ').map(String::from).collect::<Vec<_>>();
        let req = |s: &str| web_ctl_request(&words(s)).map(|v| v.to_string());
        assert_eq!(
            req("open file:///r/h.html").unwrap(),
            r#"{"cmd":"open","url":"file:///r/h.html"}"#
        );
        assert_eq!(
            req("goto about:blank").unwrap(),
            r#"{"cmd":"goto","url":"about:blank"}"#
        );
        for c in ["url", "reload", "quit"] {
            assert_eq!(req(c).unwrap(), format!(r#"{{"cmd":"{c}"}}"#));
        }
        for bad in ["open", "url x", "eval 1", "goto a b"] {
            assert!(req(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn web_ctl_sends_one_line_and_reads_the_reply() {
        use std::io::{BufRead, BufReader};
        let d = scratch("ctl");
        let listener = std::os::unix::net::UnixListener::bind(d.join("web.sock")).unwrap();
        let server = std::thread::spawn(move || {
            let mut got = Vec::new();
            for reply in [
                "{\"event\":\"window\",\"state\":\"closed\"}\n{\"ok\":true,\"url\":\"about:blank\"}\n",
                "{\"ok\":false,\"error\":\"refused\"}\n",
            ] {
                let (s, _) = listener.accept().unwrap();
                let mut line = String::new();
                BufReader::new(&s).read_line(&mut line).unwrap();
                got.push(line);
                (&s).write_all(reply.as_bytes()).unwrap();
            }
            got
        });
        let url = web_ctl(
            &d,
            &serde_json::json!({"cmd": "url"}),
            Duration::from_secs(3),
        )
        .unwrap();
        assert_eq!(url, (true, r#"{"ok":true,"url":"about:blank"}"#.into()));
        let no = web_ctl(
            &d,
            &serde_json::json!({"cmd": "goto", "url": "ftp://x"}),
            Duration::from_secs(3),
        )
        .unwrap();
        assert_eq!(no, (false, r#"{"ok":false,"error":"refused"}"#.into()));
        assert_eq!(
            server.join().unwrap(),
            [
                "{\"cmd\":\"url\"}\n",
                "{\"cmd\":\"goto\",\"url\":\"ftp://x\"}\n"
            ]
        );
        // Nobody listening: an error (exit 1), not a hang.
        let _ = std::fs::remove_file(d.join("web.sock"));
        assert!(
            web_ctl(
                &d,
                &serde_json::json!({"cmd": "url"}),
                Duration::from_millis(200)
            )
            .is_err()
        );
        let _ = std::fs::remove_dir_all(&d);
    }
}
