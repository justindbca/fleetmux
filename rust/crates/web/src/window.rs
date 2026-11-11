//! The window process (PROTOCOL.md §15.1): command line, tao event loop on the main thread, wry WebView
//! (Linux `window.default_vbox()` + `build_gtk`, macOS `build(&window)`), the stdin lifeline, the data directory.
use std::cell::RefCell;
use std::io::{self, Read, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::rc::Rc;
use std::sync::mpsc::Sender;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::json;
use tao::dpi::LogicalSize;
use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy};
use tao::window::WindowBuilder;
use wry::{PageLoadEvent, WebViewBuilder};

use crate::policy::{self, Decision};
use crate::quiet;
use crate::sock::{self, Cmd};

/// `fleetmux-web --dir D --run-dir R [--devtools]`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Opts {
    /// Web runtime directory (holds `web.sock`, `page/`, `web.log`; PROTOCOL.md §15.2).
    pub dir: PathBuf,
    /// The run directory whose `file://` pages the allow-list lets in.
    pub run_dir: PathBuf,
    /// WebView developer tools (default off).
    pub devtools: bool,
}

/// What the command line asks for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Invocation {
    /// `--version`: print [`version_line`].
    Version,
    Run(Opts),
    /// M2.6 `--gui …` (PROTOCOL.md §15.4).
    Gui(crate::gui::GuiOpts),
}

/// Events the event loop receives from other threads (through `EventLoopProxy`).
#[derive(Debug)]
pub enum UserEvent {
    /// A `web.sock` request; the reply line goes back on the sender.
    Command(Cmd, Sender<String>),
    /// stdin reached EOF: the client is gone, exit within 1 s.
    LifelineClosed,
}

/// An `open` / `goto` is answered when its page has finished loading (so a following `url` reads it back), or
/// after this long: the load was issued either way.
pub(crate) const LOAD_DEADLINE: Duration = Duration::from_secs(3);

/// How often a same-document `goto` (fragment only, no load events) checks whether the URL has moved.
const SAME_DOCUMENT_POLL: Duration = Duration::from_millis(10);

/// After stdin EOF the event loop gets this long to exit on its own before the lifeline thread ends the process.
pub(crate) const LIFELINE_GRACE: Duration = Duration::from_millis(500);

const USAGE: &str = "usage: fleetmux-web --dir <web runtime dir> --run-dir <run dir> [--devtools] | --gui --dir \
     <web runtime dir> [--run-dir <run dir>] --prefix <key> [--theme JSON] [--devtools] [--test-hooks] | --version";

/// Parse the arguments after the program name; `Err` = usage error (exit 2). `--gui` anywhere selects the GUI
/// mode (M2.6); without it the M2.5 command line is read exactly as before.
pub fn parse_args(args: &[String]) -> Result<Invocation, String> {
    if args.iter().any(|a| a == "--gui") {
        return parse_gui_args(args);
    }
    let mut dir = None;
    let mut run_dir = None;
    let mut devtools = false;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--version" => return Ok(Invocation::Version),
            "--devtools" => devtools = true,
            "--dir" | "--run-dir" => {
                let v = it
                    .next()
                    .filter(|v| !v.is_empty())
                    .ok_or_else(|| format!("{a} needs a directory"))?;
                let slot = if a == "--dir" { &mut dir } else { &mut run_dir };
                *slot = Some(PathBuf::from(v));
            }
            other => return Err(format!("unknown argument \"{other}\"")),
        }
    }
    Ok(Invocation::Run(Opts {
        dir: dir.ok_or("--dir is required")?,
        run_dir: run_dir.ok_or("--run-dir is required")?,
        devtools,
    }))
}

/// M2.6: `--gui --dir D [--run-dir R] --prefix KEY [--theme JSON] [--devtools] [--test-hooks]` (or `--version`).
fn parse_gui_args(args: &[String]) -> Result<Invocation, String> {
    let mut dir = None;
    let mut run_dir = None;
    let mut prefix = None;
    let mut theme = None;
    let mut devtools = false;
    let mut test_hooks = false;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--version" => return Ok(Invocation::Version),
            "--gui" => {}
            "--devtools" => devtools = true,
            "--test-hooks" => test_hooks = true,
            "--dir" | "--run-dir" | "--prefix" | "--theme" => {
                let v = it
                    .next()
                    .filter(|v| !v.is_empty())
                    .ok_or_else(|| format!("{a} needs a value"))?;
                match a.as_str() {
                    "--dir" => dir = Some(PathBuf::from(v)),
                    "--run-dir" => run_dir = Some(PathBuf::from(v)),
                    // The server's palette, as `hello.theme` sent it (PROTOCOL.md §15.4); bad → usage (exit 2).
                    "--theme" => {
                        theme = Some(
                            fleetmux_proto::theme::Theme::parse_json(v)
                                .map_err(|e| format!("--theme: {e}"))?,
                        )
                    }
                    _ => prefix = Some(v.clone()),
                }
            }
            other => return Err(format!("unknown argument \"{other}\"")),
        }
    }
    Ok(Invocation::Gui(crate::gui::GuiOpts {
        dir: dir.ok_or("--dir is required")?,
        run_dir,
        prefix: prefix.ok_or("--prefix is required")?,
        theme,
        devtools,
        test_hooks,
    }))
}

/// `fleetmux-web <x.y.z> (protocol 1)`, the same version as `fleetmux`.
pub fn version_line() -> String {
    format!("fleetmux-web {} (protocol 1)", env!("CARGO_PKG_VERSION"))
}

/// The data directory for login state (PROTOCOL.md §15.2): `${XDG_DATA_HOME:-~/.local/share}/fleetmux-web/<token>/`.
pub fn data_dir(token: &str) -> PathBuf {
    let base = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .unwrap_or_else(|| {
            std::env::var_os("HOME")
                .map(PathBuf::from)
                .unwrap_or_default()
                .join(".local/share")
        });
    base.join("fleetmux-web").join(token)
}

/// An `open` / `goto` waiting for its page.
struct Pending {
    url: String,
    reply: Sender<String>,
    deadline: Instant,
    wait: Await,
}

/// What a pending `open` / `goto` still waits for. Its navigation is recognised by the load that *commits* with
/// the requested URL (wry `PageLoadEvent::Started` = commit, before any page script runs); the answer is that
/// navigation's `Finished`, whatever URL the page has rewritten itself to by then (the real history page does
/// `location.replace('#…')` while loading), so a load-finished event of another navigation is never taken for it.
#[derive(Debug, PartialEq, Eq)]
enum Await {
    /// Requested; its navigation has not committed yet.
    Commit,
    /// Committed; answered on the next `Finished`.
    Finish,
    /// A fragment-only change of the loaded document (no load events): answered once `url()` is no longer the
    /// URL from before the request (or already is the requested one).
    UrlChange(Option<String>),
}

/// `url` without its fragment: the document a navigation loads.
fn document(url: &str) -> &str {
    url.split('#').next().unwrap_or(url)
}

/// Open the window and run the event loop until `quit`, lifeline EOF or the window is closed.
pub fn run(opts: &Opts) -> ExitCode {
    let dir = opts.dir.clone();
    let run_dir = opts.run_dir.clone();
    let page_dir = dir.join("page");

    let mut event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let quiet = quiet::on();
    if quiet {
        quiet::event_loop(&mut event_loop);
    }
    let proxy = event_loop.create_proxy();
    if let Err(e) = sock::serve(&dir, proxy.clone()) {
        eprintln!("fleetmux-web: {e}");
        return ExitCode::from(1);
    }
    log(
        &dir,
        &format!("start {} run dir {}", version_line(), run_dir.display()),
    );
    lifeline(proxy, dir.clone());

    let builder = WindowBuilder::new()
        .with_title("fleetmux history")
        .with_inner_size(LogicalSize::new(1100.0, 800.0));
    let builder = if quiet {
        quiet::builder(builder)
    } else {
        builder
    };
    let window = match builder.build(&event_loop) {
        Ok(w) => w,
        Err(e) => return fail(&dir, &format!("cannot open a window: {e}")),
    };
    if quiet {
        quiet::window(&window);
    }

    let pending: Rc<RefCell<Option<Pending>>> = Rc::new(RefCell::new(None));
    let data = prepare_data_dir(&dir);

    #[cfg(target_os = "linux")]
    let mut context = wry::WebContext::new(data.clone());
    #[cfg(target_os = "linux")]
    let builder = WebViewBuilder::new_with_web_context(&mut context);
    #[cfg(not(target_os = "linux"))]
    let builder = WebViewBuilder::new();

    let builder = builder
        .with_devtools(opts.devtools)
        .with_navigation_handler(navigation_handler(&dir, &run_dir, &page_dir))
        .with_on_page_load_handler({
            let pending = pending.clone();
            move |event, url| {
                let mut slot = pending.borrow_mut();
                let Some(p) = slot.as_mut() else { return };
                match event {
                    PageLoadEvent::Started
                        if p.wait == Await::Commit && document(&url) == document(&p.url) =>
                    {
                        p.wait = Await::Finish;
                    }
                    PageLoadEvent::Finished if p.wait == Await::Finish => {
                        let p = slot.take().expect("checked above");
                        let _ = p.reply.send(json!({"ok": true}).to_string());
                    }
                    _ => {}
                }
            }
        });

    #[cfg(target_os = "macos")]
    let builder = match data.as_deref().map(datastore_id) {
        Some(Ok(id)) => {
            use wry::WebViewBuilderExtDarwin;
            // macOS 14+ uses this store; wry falls back to the default store on older systems.
            builder.with_data_store_identifier(id)
        }
        Some(Err(e)) => {
            log(
                &dir,
                &format!("datastore.uuid: {e}; using the default data store"),
            );
            builder
        }
        None => builder,
    };
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    let _ = &data;

    #[cfg(target_os = "linux")]
    let built = {
        use tao::platform::unix::WindowExtUnix;
        use wry::WebViewBuilderExtUnix;
        match window.default_vbox() {
            Some(vbox) => builder.build_gtk(vbox),
            None => return fail(&dir, "the window has no GTK box"),
        }
    };
    #[cfg(not(target_os = "linux"))]
    let built = builder.build(&window);

    let webview = match built {
        Ok(w) => w,
        Err(e) => return fail(&dir, &format!("cannot create the web view: {e}")),
    };

    // wry's macOS `url()` unwraps the WebView's URL, which is nil until something has been loaded: read it only
    // after the first load (the blank start reads as `about:blank`).
    let mut loaded = false;
    event_loop.run(move |event, _, control_flow| {
        #[cfg(target_os = "linux")]
        let _ = &context; // the WebKit context lives as long as the web view
        let mut exit = None;
        match event {
            Event::UserEvent(UserEvent::Command(cmd, reply)) => match cmd {
                Cmd::Open(url) | Cmd::Goto(url)
                    if policy::allow(&url, &run_dir, &page_dir) != Decision::Allow =>
                {
                    let _ = reply.send(sock::error_line(&format!(
                        "{url} is not allowed in this window"
                    )));
                }
                cmd @ (Cmd::Open(_) | Cmd::Goto(_)) => {
                    let front = matches!(cmd, Cmd::Open(_));
                    let (Cmd::Open(url) | Cmd::Goto(url)) = cmd else {
                        unreachable!()
                    };
                    let before = if loaded { webview.url().ok() } else { None };
                    // Same document, new fragment: WebKit scrolls instead of loading (no load events).
                    let same_document = url.contains('#')
                        && before
                            .as_deref()
                            .is_some_and(|b| document(b) == document(&url));
                    if webview.load_url(&url).inspect(|()| loaded = true).is_err() {
                        let _ = reply.send(sock::error_line(&format!("loading {url} failed")));
                    } else {
                        let wait = if same_document {
                            Await::UrlChange(before)
                        } else {
                            Await::Commit
                        };
                        if let Some(old) = pending.borrow_mut().replace(Pending {
                            url,
                            reply,
                            deadline: Instant::now() + LOAD_DEADLINE,
                            wait,
                        }) {
                            let _ = old.reply.send(json!({"ok": true}).to_string());
                        }
                        if front && !quiet {
                            window.set_visible(true);
                            window.set_focus();
                        }
                    }
                }
                Cmd::Url => {
                    let current = if loaded {
                        webview.url()
                    } else {
                        Ok("about:blank".to_string())
                    };
                    let line = match current {
                        Ok(url) => json!({"ok": true, "url": url}).to_string(),
                        Err(e) => sock::error_line(&format!("cannot read the URL: {e}")),
                    };
                    let _ = reply.send(line);
                }
                Cmd::Reload => {
                    let line = match webview.reload() {
                        Ok(()) => json!({"ok": true}).to_string(),
                        Err(e) => sock::error_line(&format!("reload failed: {e}")),
                    };
                    let _ = reply.send(line);
                }
                // The connection thread already answered (§15.3 "reply, then exit").
                Cmd::Quit => exit = Some("quit"),
            },
            Event::UserEvent(UserEvent::LifelineClosed) => exit = Some("stdin closed"),
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => {
                sock::notify_all(&json!({"event": "window", "state": "closed"}).to_string());
                exit = Some("window closed");
            }
            _ => {}
        }
        if let Some(why) = exit {
            sock::remove(&dir);
            log(&dir, &format!("exit: {why}"));
            *control_flow = ControlFlow::Exit;
            return;
        }
        let mut slot = pending.borrow_mut();
        let now = Instant::now();
        let (answer, next) = match slot.as_ref() {
            None => (false, ControlFlow::Wait),
            Some(p) if now >= p.deadline => (true, ControlFlow::Wait),
            Some(p) => match &p.wait {
                Await::UrlChange(before) => {
                    let current = webview.url().ok(); // `loaded`: a same-document wait follows a load
                    if current != *before || current.as_deref() == Some(p.url.as_str()) {
                        (true, ControlFlow::Wait)
                    } else {
                        (
                            false,
                            ControlFlow::WaitUntil((now + SAME_DOCUMENT_POLL).min(p.deadline)),
                        )
                    }
                }
                Await::Commit | Await::Finish => (false, ControlFlow::WaitUntil(p.deadline)),
            },
        };
        if answer {
            let p = slot.take().expect("pending checked above");
            let _ = p.reply.send(json!({"ok": true}).to_string());
        }
        *control_flow = next;
    })
}

/// The binary's entry point: [`parse_args`], then `--version` or [`run`].
pub fn main(args: &[String]) -> ExitCode {
    match parse_args(args) {
        Ok(Invocation::Version) => {
            println!("{}", version_line());
            ExitCode::SUCCESS
        }
        Ok(Invocation::Run(opts)) => run(&opts),
        Ok(Invocation::Gui(opts)) => crate::gui::run(&opts),
        Err(e) => {
            eprintln!("fleetmux-web: {e}\n{USAGE}");
            ExitCode::from(2)
        }
    }
}

/// The allow-list as wry's navigation handler: `Allow` loads (a `?fleet-jump=` is reported to a server with pages
/// instead, M4; without pages it is only noted and loads),
/// `External` goes to the system opener, everything else is dropped.
fn navigation_handler(
    dir: &Path,
    run_dir: &Path,
    page_dir: &Path,
) -> impl Fn(String) -> bool + 'static {
    let (dir, run_dir, page_dir) = (
        dir.to_path_buf(),
        run_dir.to_path_buf(),
        page_dir.to_path_buf(),
    );
    move |url: String| match policy::allow(&url, &run_dir, &page_dir) {
        // M4 (design m4 §5.3): with a server that has pages the marked URL never loads; the server switches the
        // page and sends this window the target page itself (`0x84 goto`).
        _ if sock::pages() && policy::is_fleet_jump(&url) && url.starts_with("file:") => {
            log(&dir, &format!("fleet-jump reported to the server: {url}"));
            sock::notify_jump(&url);
            false
        }
        Decision::Allow => {
            if policy::is_fleet_jump(&url) {
                log(
                    &dir,
                    &format!("fleet-jump seen (the server has no pages; loads as is): {url}"),
                );
            }
            true
        }
        Decision::External => {
            open_external(&dir, &url);
            false
        }
        Decision::Deny => {
            log(&dir, &format!("navigation refused: {url}"));
            false
        }
    }
}

/// Hand an `http(s)` URL to the system opener, never blocking the UI thread; the child is reaped on a thread.
pub(crate) fn open_external(dir: &Path, url: &str) {
    let opener = if cfg!(target_os = "macos") {
        "open"
    } else {
        "xdg-open"
    };
    match Command::new(opener)
        .arg(url)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(mut child) => {
            let _ = std::thread::Builder::new()
                .name("opener".into())
                .spawn(move || child.wait());
        }
        Err(e) => log(dir, &format!("{opener} {url}: {e}")),
    }
}

/// stdin is the lifeline pipe from the attach client: on EOF ask the loop to exit, and end the process
/// ourselves if it has not after [`LIFELINE_GRACE`] (well inside the 1 s of §15.1).
fn lifeline(proxy: EventLoopProxy<UserEvent>, dir: PathBuf) {
    let _ = std::thread::Builder::new()
        .name("lifeline".into())
        .spawn(move || {
            let mut buf = [0u8; 256];
            let mut stdin = io::stdin();
            loop {
                match stdin.read(&mut buf) {
                    Ok(0) => break,
                    Ok(_) => {}
                    Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                    Err(_) => break,
                }
            }
            let _ = proxy.send_event(UserEvent::LifelineClosed);
            std::thread::sleep(LIFELINE_GRACE);
            sock::remove(&dir);
            log(&dir, "exit: stdin closed (forced)");
            std::process::exit(0);
        });
}

pub(crate) fn fail(dir: &Path, msg: &str) -> ExitCode {
    eprintln!("fleetmux-web: {msg}");
    log(dir, msg);
    sock::remove(dir);
    ExitCode::from(1)
}

/// `<data_dir(token)>` created 0700; `token` = the web runtime directory's name without its `-<pid>` suffix, so
/// the directory survives re-attach. `None` (logged) = WebKit's default location.
pub(crate) fn prepare_data_dir(dir: &Path) -> Option<PathBuf> {
    let name = dir.file_name()?.to_string_lossy().into_owned();
    let token = match name.rsplit_once('-') {
        Some((t, pid))
            if !t.is_empty() && !pid.is_empty() && pid.bytes().all(|b| b.is_ascii_digit()) =>
        {
            t
        }
        _ => name.as_str(),
    };
    let data = data_dir(token);
    let made = std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&data)
        .and_then(|()| std::fs::set_permissions(&data, std::fs::Permissions::from_mode(0o700)));
    match made {
        Ok(()) => Some(data),
        Err(e) => {
            log(dir, &format!("data directory {}: {e}", data.display()));
            None
        }
    }
}

/// The 16-byte WKWebsiteDataStore identifier kept as a UUID string in `<data dir>/datastore.uuid` (0600).
#[cfg(target_os = "macos")]
pub(crate) fn datastore_id(data: &Path) -> io::Result<[u8; 16]> {
    let file = data.join("datastore.uuid");
    if let Some(id) = std::fs::read_to_string(&file)
        .ok()
        .and_then(|s| parse_uuid(s.trim()))
    {
        return Ok(id);
    }
    let mut id = [0u8; 16];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut id)?;
    id[6] = (id[6] & 0x0f) | 0x40; // version 4
    id[8] = (id[8] & 0x3f) | 0x80; // RFC 4122 variant
    let hex: String = id.iter().map(|b| format!("{b:02x}")).collect();
    let text = format!(
        "{}-{}-{}-{}-{}\n",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    );
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&file)?;
    f.write_all(text.as_bytes())?;
    Ok(id)
}

#[cfg(target_os = "macos")]
fn parse_uuid(s: &str) -> Option<[u8; 16]> {
    let hex: String = s.chars().filter(|c| *c != '-').collect();
    if hex.len() != 32 {
        return None;
    }
    let mut id = [0u8; 16];
    for (i, b) in id.iter_mut().enumerate() {
        *b = u8::from_str_radix(hex.get(2 * i..2 * i + 2)?, 16).ok()?;
    }
    Some(id)
}

/// Append one line to `<dir>/web.log` (0600). Failures are ignored: the log is for people, not for control.
pub(crate) fn log(dir: &Path, msg: &str) {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .append(true)
        .create(true)
        .mode(0o600)
        .open(dir.join("web.log"))
    {
        let _ = writeln!(f, "{secs} {msg}");
    }
}
