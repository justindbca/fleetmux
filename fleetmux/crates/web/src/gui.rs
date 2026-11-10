//! M2.6 GUI mode, `fleetmux-web --gui` (design.md §3.6, PROTOCOL.md §15.4): one window with a full-window xterm.js
//! terminal page (`fleetmux://app/index.html`) showing the server's composed screen, and a sandboxed iframe laid
//! over the lower-left cell for the history page (`fleetmux://page/…`). Frames come in on stdin (`0x81` / `0x85`
//! / `0x86` only) and go out on stdout (`0x01` / `0x02` / `0x05`); `web.sock` takes the GUI command set
//! ([`crate::sock::GuiCmd`]).
//!
//! Threads: the tao event loop and the WebView live on the main thread (as M2.5). Background threads read stdin
//! (the frame pipe, which is also the lifeline), write stdout, serve `web.sock`, run the clipboard tools and read
//! page files for the custom protocol; they reach the main thread only through the [`UiEvent`] proxy.
//!
//! Data path (design.md §3.6.5): the stdin reader appends screen bytes, overlays and scroll states, in order, to
//! one shared batch and wakes the main thread once; the main thread sends the whole batch as
//! `fm.feed(seq, '<base64>', [overlays], [scrolls])` calls of ≤ 1 MiB raw bytes each. The page writes them with
//! `term.write(bytes, cb)`, applies the overlays and the scroll states in the callback and acknowledges `seq` over
//! ipc; the reader blocks while more than 4 MiB are unacknowledged, which
//! stalls the client's pipe and, through it, the server's per-viewer queue (whose slow-viewer rule takes over).
use std::collections::{HashMap, VecDeque};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Condvar, Mutex, RwLock};
use std::time::Instant;

use fleetmux_proto::view::{self, Frame, ScrollTo};
use serde_json::{Value, json};
use tao::dpi::LogicalSize;
use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy};
#[cfg(target_os = "macos")]
use tao::platform::macos::WindowBuilderExtMacOS;
use tao::window::WindowBuilder;
use wry::http::{Request, Response};
use wry::{NewWindowResponse, WebViewBuilder};

use crate::assets;
use crate::policy::{self, Decision};
use crate::quiet;
use crate::sock::{self, Cmd, GuiCmd};
use crate::window::{LIFELINE_GRACE, LOAD_DEADLINE, fail, log, open_external, prepare_data_dir};

/// `fleetmux-web --gui --dir D [--run-dir R] --prefix KEY [--theme JSON] [--devtools] [--test-hooks]`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GuiOpts {
    /// Web runtime directory (holds `web.sock`, `page/`, `web.log`; PROTOCOL.md §15.2).
    pub dir: PathBuf,
    /// The first run directory the page allow-list lets in; `web.sock` `scope` can set / change it later.
    pub run_dir: Option<PathBuf>,
    /// The prefix key name (`C-b`, PROTOCOL.md §9.1) the iframe's injected script catches.
    pub prefix: String,
    /// The palette the page paints with (`hello.theme`, PROTOCOL.md §4.1 / §15.4): xterm.js colours and the page
    /// background. `None` = xterm.js's own colours (the client sent no `--theme`).
    pub theme: Option<fleetmux_proto::theme::Theme>,
    /// WebView developer tools (default off).
    pub devtools: bool,
    /// Graphical tests only: enables `web.sock` `js` (PROTOCOL.md §15.4). `fleetmux gui` never passes it.
    pub test_hooks: bool,
}

/// Unacknowledged screen bytes (sent to the page or waiting for the main thread) above which the stdin reader
/// stops reading (design.md §3.6.5).
const INFLIGHT_MAX: usize = 4 << 20;

/// Raw screen bytes per `fm.feed` call at most (brief-m26 §3.2).
const FEED_MAX: usize = 1 << 20;

/// Largest history page served from disk (the `0x84` limit, design.md §3.5.3).
const PAGE_MAX: u64 = 32 << 20;

const APP_PREFIX: &str = "fleetmux://app/";
const PAGE_PREFIX: &str = "fleetmux://page/";

/// The body of every `fleetmux:` 404; `frame.js` recognises the meta tag and reports the load as failed.
const NOT_FOUND: &str = "<!doctype html><meta charset=\"utf-8\"><meta name=\"fleetmux-error\" content=\"404\">\
<title>not found</title><p>fleetmux: this page is not available.</p>";

/// A26 (user report A42-TOPBAR-ALIGN-01): the native window controls are the anchor of the topbar, so the window
/// does **not** move them: `with_traffic_light_inset` is gone and the macOS traffic lights keep the system default
/// place of a full-size-content window (measured on macOS 26.6.2 with this style mask: 14 px buttons at x = 9 / 32 /
/// 55, top = 9 px, i.e. centre 16 px below the window top; `swift` measurement recorded in
/// `impl-1/topbar-fix.md`). The page pads row 0 down so the toolbar and the fleet title/status share that centre
/// (gui.js `placeTopbar`); Linux draws its own native header row instead ([`Header`], A26 rev5), so it neither
/// pads nor reports an inset.
const CONTROL_CENTER: f64 = 16.0;

/// Open the GUI window and run the event loop until `quit`, stdin EOF or the window is closed. Exit 0 then, 1 on
/// a frame type other than `0x81` / `0x85` / `0x86` on stdin.
pub fn run(opts: &GuiOpts) -> ExitCode {
    let dir = opts.dir.clone();
    let page_dir = dir.join("page");
    let Some(prefix) = Prefix::parse(&opts.prefix) else {
        eprintln!(
            "fleetmux-web: --prefix {}: not a prefix key name (C-<letter>)",
            opts.prefix
        );
        return ExitCode::from(2);
    };
    let token = match Token::random() {
        Ok(t) => t,
        Err(e) => {
            eprintln!("fleetmux-web: no random source for the ipc token: {e}");
            return ExitCode::from(1);
        }
    };

    let mut event_loop = EventLoopBuilder::<UiEvent>::with_user_event().build();
    let quiet = quiet::on();
    if quiet {
        quiet::event_loop(&mut event_loop);
    }
    let proxy = event_loop.create_proxy();
    if let Err(e) = sock::serve_gui_with(&dir, proxy.clone(), opts.test_hooks) {
        eprintln!("fleetmux-web: {e}");
        return ExitCode::from(1);
    }
    log(
        &dir,
        &format!(
            "start {} --gui{}",
            crate::window::version_line(),
            if opts.test_hooks { " --test-hooks" } else { "" }
        ),
    );

    let scope: Arc<RwLock<Option<PathBuf>>> = Arc::new(RwLock::new(opts.run_dir.clone()));
    let flow: Arc<Flow> = Arc::default();
    spawn_pipe_reader(flow.clone(), proxy.clone(), dir.clone());
    let out = spawn_stdout_writer(proxy.clone());

    let builder = if quiet {
        quiet::builder(window_builder())
    } else {
        window_builder()
    };
    let window = match builder.build(&event_loop) {
        Ok(w) => w,
        Err(e) => return fail(&dir, &format!("cannot open a window: {e}")),
    };
    if quiet {
        quiet::window(&window);
    }
    // A26 rev5: Linux's one native header row, as the window's titlebar. Attached while the window is still
    // hidden: `gtk_window_set_titlebar` may not work on a window that is already visible (GTK3 docs).
    #[cfg(target_os = "linux")]
    let header = Header::attach(&window, prefix.byte, &proxy, opts.theme.as_ref());
    #[cfg(target_os = "linux")]
    window.set_visible(true);
    let data = prepare_data_dir(&dir);

    #[cfg(target_os = "linux")]
    let mut context = wry::WebContext::new(data.clone());
    #[cfg(target_os = "linux")]
    let builder = WebViewBuilder::new_with_web_context(&mut context);
    #[cfg(not(target_os = "linux"))]
    let builder = WebViewBuilder::new();

    // The token goes to the terminal page's main frame only, before its own script runs (wry lib.rs:1033); the
    // page reads and deletes `window.__fmBoot` at once.
    let boot = format!(
        "window.__fmBoot = {};",
        json!({"token": token.hex(), "prefixByte": prefix.byte, "mac": cfg!(target_os = "macos"),
               "controlCenter": CONTROL_CENTER,
               "testHooks": opts.test_hooks, "theme": opts.theme})
    );
    let frame_script = format!(
        "{}({});",
        assets::FRAME_JS.trim_end(),
        json!({"key": prefix.key, "code": prefix.code})
    );
    let builder = builder
        .with_url(policy::APP_INDEX)
        .with_devtools(opts.devtools)
        .with_initialization_script_for_main_only(boot, true)
        .with_initialization_script_for_main_only(frame_script, false)
        .with_asynchronous_custom_protocol(
            "fleetmux".into(),
            custom_protocol(scope.clone(), page_dir.clone()),
        )
        .with_ipc_handler(ipc_handler(token, proxy.clone(), opts.test_hooks))
        .with_navigation_handler(navigation_handler(&dir, scope.clone(), &page_dir))
        .with_new_window_req_handler({
            let dir = dir.clone();
            move |url: String, _features| {
                if matches!(scheme(&url).as_str(), "http" | "https") {
                    open_external(&dir, &url);
                } else {
                    log(&dir, &format!("new window refused: {url}"));
                }
                NewWindowResponse::Deny
            }
        });

    #[cfg(target_os = "macos")]
    let builder = match data.as_deref().map(crate::window::datastore_id) {
        Some(Ok(id)) => {
            use wry::WebViewBuilderExtDarwin;
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

    let mut ui = Ui {
        webview,
        window,
        dir,
        page_dir,
        scope,
        flow,
        out,
        proxy,
        ready: false,
        deferred: false,
        seq: 0,
        sizes: HashMap::new(),
        page_url: None,
        overlay_url: None,
        pending: None,
        inset: None,
        inset_cols: 0,
        last_sidebar_min: 0,
        #[cfg(target_os = "linux")]
        header,
        #[cfg(target_os = "linux")]
        focused: true,
        #[cfg(target_os = "linux")]
        last_caret: None,
        // A42-LINUX-IME-CANDIDATE-05 (M99): the client window's root origin at the last accepted push, so a move
        // that changes the root without changing the client-relative cell can still refresh the IM.
        #[cfg(target_os = "linux")]
        last_root: None,
        // A42-LINUX-IME-CANDIDATE-05 (M99): the next push is a re-assert (focus regain, geometry, composition), so
        // it writes even when the rect is identical — WebKit's own late write must be superseded.
        #[cfg(target_os = "linux")]
        force_caret: false,
        #[cfg(target_os = "linux")]
        pending_caret: None,
    };
    // A42-LINUX-IME-CANDIDATE-05: composition is where the candidate window is placed, and WebKit re-sends its own
    // cursor area there from its editor state (measured on the product binary, one font step behind the grid: the live
    // cell (80,95,10,19) vs WebKit's own (89,146,1,18), which then stays for the whole composition). Each preedit
    // update is the event that lets the retained caret be re-asserted after that write; the handler only forwards it to
    // the loop, so the re-assert runs outside WebKit's own emit. `preedit-changed` rather than `preedit-started`: the
    // start is followed by a change in the same emit (two identical pushes), and later updates are what keep ours last.
    #[cfg(target_os = "linux")]
    {
        use webkit2gtk::{InputMethodContextExt, WebViewExt};
        use wry::WebViewExtUnix;
        if let Some(im) = ui.webview.webview().input_method_context() {
            let proxy = ui.proxy.clone();
            im.connect_preedit_changed(move |_| {
                let _ = proxy.send_event(UiEvent::CaretRefresh);
            });
        }
    }
    event_loop.run(move |event, _, control_flow| {
        #[cfg(target_os = "linux")]
        let _ = &context; // the WebKit context lives as long as the web view
        match event {
            Event::UserEvent(e) => ui.on_event(e),
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => ui.close("window closed"),
            // A42-LINUX-IME-CANDIDATE-05: a focus loss must drop the cached caret (and never push one), so the same
            // cell is pushed again when the window comes back.
            Event::WindowEvent {
                event: WindowEvent::Focused(focused),
                ..
            } => {
                #[cfg(target_os = "linux")]
                ui.focus_changed(focused);
                #[cfg(not(target_os = "linux"))]
                let _ = focused;
            }
            // A42-LINUX-IME-CANDIDATE-05: a resize / maximize / restore can change the caret's root position, so the
            // retained caret is re-pushed (no timer: the event is the trigger).
            Event::WindowEvent {
                event: WindowEvent::Resized(_),
                ..
            } => {
                #[cfg(target_os = "linux")]
                ui.geometry_changed();
            }
            // A42-LINUX-IME-CANDIDATE-05 (M99): a move changes the client window's root origin without touching the
            // page, so the retained caret is re-asserted here too — otherwise IBus keeps the root it cached before
            // the move (measured: `move_bus: []`, IBus stayed at 37,83 where 157,143 was due).
            Event::WindowEvent {
                event: WindowEvent::Moved(_),
                ..
            } => {
                #[cfg(target_os = "linux")]
                ui.geometry_changed();
            }
            _ => {}
        }
        *control_flow = ui.tick();
    })
}

/// The window (S1e, plan §17.2): on macOS a transparent, title-less titlebar with the content view filling the
/// window, so row 0 of the grid — the server's toolbar — sits on the native traffic lights. Linux (WebKitGTK) draws
/// **one** native header row instead ([`Header`], A26 rev5 / `CURRENT_REQUIREMENTS` rev 6 `utm_topbar`): the
/// window's titlebar is a GTK headerbar, and the grid below it keeps no topbar row (`sidebar::topbar_cols`).
///
/// Both platforms therefore have exactly one topbar container whose native controls keep their OS-default place:
/// macOS overlays the traffic lights on grid row 0, Linux puts the headerbar row above the content.
fn window_builder() -> WindowBuilder {
    let builder = WindowBuilder::new()
        .with_title("fleetmux")
        .with_inner_size(LogicalSize::new(1200.0, 800.0));
    #[cfg(target_os = "macos")]
    let builder = builder
        .with_titlebar_transparent(true)
        .with_title_hidden(true)
        .with_fullsize_content_view(true);
    // A26 rev5: the Linux titlebar is a GTK headerbar, so the window stays hidden until it is attached — GTK3's
    // `gtk_window_set_titlebar` may not take effect on a window that is already visible. Decorations stay on: the
    // native close/minimize/maximize are required, and GTK draws them in that headerbar (client-side decorations,
    // the toolkit's own widgets at the `gtk-decoration-layout` default place).
    #[cfg(target_os = "linux")]
    let builder = builder.with_decorations(true).with_visible(false);
    builder
}

/// A26 rev5/rev7 (`CURRENT_REQUIREMENTS` rev 6 `utm_topbar`, rev 10 `top_container_alignment`): the Linux window
/// draws **one** native header row, split into the two containers of the approved layout.
///
/// The window's titlebar is a `GtkHeaderBar` (client-side decorations, `gtk_window_set_titlebar`; GTK then asks the
/// window manager for no titlebar of its own), so the same row carries:
///
/// - GTK's **own** close/minimize/maximize — native widgets at the OS-default top-right anchor of the
///   `gtk-decoration-layout` setting, with the toolkit's size, spacing and behaviour. The page draws no control and
///   no lookalike (`gui.js` has no `#wc` / `resize_window`), so no HTML can replace them;
/// - the **sidebar header** (right, above the mirrored Linux layout's sidebar band): a `│` separator on the grid's
///   own sidebar drag line, then the app's toolbar — sidebar toggle `◫`, new bash `+`, reopen fleet `↺`,
///   navigation `←` / `→`, in that order — right-aligned immediately beside those native buttons;
/// - the **workspace header** (left): the current fleet's title (bold) and `· line1` (grey), left-aligned with the
///   same comfortable padding, in that same single row (`0x85`'s `topbar` key, [`Ui::set_topbar`]).
///
/// Both containers fill the row, so they share its top edge, bottom edge, height and vertical centre by
/// construction. Their boundary is not a constant: [`header_split`] recomputes it from the headerbar's own
/// allocation and the `0x85` `topbar` geometry (`sidebar` / `cols`) whenever the row is allocated, so the separator
/// stays on the sidebar's drag line after a sidebar drag, a collapse, a window resize or a font change. The toolbar
/// keeps its minimum width even when a very wide grid makes the band narrower than the controls (then the divider
/// stops just left of them instead of squeezing them away).
///
/// The grid below keeps no topbar row (the server's `render::sidebar::topbar_cols` is 0 while the layout is
/// mirrored), so nothing duplicates the row. The buttons send the same prefix keys the row-0 cells would
/// (`server::views::on_prefix_key`: `^b s`, `^b c`, `^b r`, `^b (`, `^b )`), so one action implementation serves
/// both platforms and the terminal font never moves a native control.
#[cfg(target_os = "linux")]
struct Header {
    /// The one native `GtkHeaderBar` installed as the window's titlebar; its window buttons stay GTK's own.
    bar: gtk::HeaderBar,
    /// The current fleet's `title`, bold, left-aligned in the workspace header.
    title: gtk::Label,
    /// `· ` and the current fleet's `line1`, grey; empty while there is no fleet to show.
    status: gtk::Label,
    /// The five toolbar buttons in `◫ + ↺ ← →` order; [`Header::attach`] wires each to its prefix key.
    buttons: Vec<gtk::Button>,
    /// The row that is the headerbar's custom title: its allocation drives [`header_split`] (see [`header_row`]).
    row: header_row::HeaderRow,
    /// The last `0x85` `topbar` geometry (`sidebar` columns, viewer `cols`) plus the page's own cell width; the
    /// default = nothing reported yet.
    band: std::rc::Rc<std::cell::Cell<Band>>,
}

/// The geometry the Linux header's boundary is derived from, shared by the window and its header row.
///
/// `sidebar` / `cols` are the `0x85` `topbar` key (the grid's own sidebar band, in columns); `cell` is one cell's
/// width in px as the page really renders it (the `inset` ipc `cell` key). The page fits **whole cells**, so its
/// canvas is `cols · cell` px and can be a few px narrower than the headerbar (which fills the window): the grid's
/// divider column `cols − sidebar` has to be measured on the canvas, not on the headerbar (A42-LINUX-SIDEBAR-ALIGN-03).
/// `cell == 0.0` = the page has not reported it yet: the boundary falls back to the headerbar's own width.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct Band {
    /// Sidebar columns the grid draws ([`fleetmux_layout::Layout::sidebar_cols`]); 0 = no sidebar.
    pub sidebar: u16,
    /// The viewer's canvas width in columns; 0 = not sent yet.
    pub cols: u16,
    /// One cell's width in px as the page renders it; 0.0 = not reported yet.
    pub cell: f32,
}

/// A42-LINUX-MINWIDTH-01: the Linux header row as its own container.
///
/// The row has three children — the workspace heading, the 1 px boundary separator and the sidebar toolbar — and it
/// places them **from its own allocation inside `size_allocate`**, i.e. in the same layout pass. Its size request is
/// the toolbar's own intrinsic width (real GTK buttons) and nothing else, so where the heading and the separator go is
/// never turned into a size request. That is what used to lock the headerbar's minimum width: the workspace width was
/// written as a size request from the *current allocation* inside `size_allocate`, so the headerbar's
/// `WM_NORMAL_HINTS` minimum followed the biggest width the window ever had. Writing a margin or a request during
/// allocation cannot fix that either (GTK3 only applies it to a later pass), so the boundary is set by allocation.
#[cfg(target_os = "linux")]
mod header_row {
    use std::cell::{Cell, RefCell};
    use std::rc::Rc;

    use gtk::glib;
    use gtk::prelude::*;
    use gtk::subclass::prelude::*;

    use super::Band;

    /// The `0x85` `topbar` geometry plus the page's cell width, shared with [`super::Header`].
    type BandCell = Rc<Cell<Band>>;

    mod imp {
        use super::*;

        #[derive(Default)]
        pub struct HeaderRow {
            pub(super) bar: RefCell<Option<gtk::HeaderBar>>,
            pub(super) heading: RefCell<Option<gtk::Widget>>,
            pub(super) divider: RefCell<Option<gtk::Widget>>,
            pub(super) tools: RefCell<Option<gtk::Widget>>,
            pub(super) band: RefCell<Option<BandCell>>,
            /// A42-LINUX-SIDEBAR-ALIGN-03 (M81 refinement): set on every allocation of the row. The floor the window
            /// reports is derived from that allocation (and from GTK's native buttons), so a font/cell change, a
            /// theme change that resizes the native buttons, a sidebar drag or a window state change has to make
            /// the event loop recompute it instead of keeping an older value.
            pub(super) allocated: std::cell::Cell<bool>,
        }

        #[glib::object_subclass]
        impl ObjectSubclass for HeaderRow {
            const NAME: &'static str = "FleetmuxHeaderRow";
            type Type = super::HeaderRow;
            type ParentType = gtk::Container;
            type Interfaces = ();
        }

        impl ObjectImpl for HeaderRow {
            fn constructed(&self) {
                self.parent_constructed();
                // A pure layout container: it has no GdkWindow of its own (GTK3 widgets default to has_window = TRUE,
                // and the default realize would abort); its children draw into the headerbar's window.
                self.obj().set_has_window(false);
            }
        }

        impl WidgetImpl for HeaderRow {
            /// The row's floor is the toolbar's own request — real GTK buttons, never an allocation.
            fn preferred_width(&self) -> (i32, i32) {
                self.tools
                    .borrow()
                    .as_ref()
                    .map_or((0, 0), |tools| tools.preferred_width())
            }

            fn preferred_height(&self) -> (i32, i32) {
                let mut req = (0, 0);
                for child in [&self.heading, &self.tools] {
                    if let Some(child) = child.borrow().as_ref() {
                        let (min, natural) = child.preferred_height();
                        req = (req.0.max(min), req.1.max(natural));
                    }
                }
                req
            }

            fn size_allocate(&self, alloc: &gtk::Allocation) {
                let row = self.obj();
                row.set_allocation(alloc);
                // The reported floor is a function of this allocation (see `Header::sidebar_min`): mark it stale.
                self.allocated.set(true);
                let width = alloc.width().max(1);
                let height = alloc.height().max(1);
                // A42-LINUX-HEADER-DRIFT-02: the row is windowless, so — GTK3 — a child's allocation is in the
                // coordinates of the nearest ancestor GdkWindow, i.e. the window, not in this container's own
                // (x, y). Placing the three children at `(0, 0)` therefore painted them at the window's top-left
                // corner: up by the row's y (the CSD shadow plus the title box's own offset, 4-5 px restored and
                // 1 px maximized) and left by its x (10 px). That is the drift against GTK's native window
                // buttons, and it changed when the window was restored because the CSD shadow is not there while
                // maximized. Everything below is the row's origin plus an offset inside the row.
                let (x0, y0) = (alloc.x(), alloc.y());
                let want = {
                    let band = self.band.borrow();
                    let Band {
                        sidebar,
                        cols,
                        cell,
                    } = band.as_ref().map_or(Band::default(), |band| band.get());
                    let bar = self.bar.borrow();
                    let bar_x = bar.as_ref().map_or(0, |bar| bar.allocation().x());
                    let bar_w = bar.as_ref().map_or(width, |bar| bar.allocated_width());
                    // The page fits whole cells, so the grid it draws is `cols · cell` px — up to a few px narrower
                    // than the headerbar, which fills the window. The divider column `cols − sidebar` is a column
                    // of *that* canvas, so the boundary is measured on it; without the page's cell width the
                    // headerbar's own width is the only available denominator.
                    let grid_w = if cols > 0 && cell > 0.0 {
                        (f32::from(cols) * cell).round() as i32
                    } else {
                        bar_w
                    };
                    // `header_split` works in headerbar/page coordinates (`grid_w · (cols − sidebar) / cols` is the
                    // grid's divider column from the page's left edge), so the row's own offset inside the
                    // headerbar is what its `want` is measured against — not its window position.
                    let row_in_bar = x0 - bar_x;
                    let tools_min = self
                        .tools
                        .borrow()
                        .as_ref()
                        .map_or(0, |tools| tools.preferred_width().0);
                    super::super::header_split(grid_w, row_in_bar, width, sidebar, cols, tools_min)
                };
                // Tools fill the left sidebar header; the 1px separator and workspace heading follow.
                // All three share the row's height and GTK's native controls' vertical centre.
                let rest = (width - want - 1).max(1);
                if let Some(tools) = self.tools.borrow().as_ref() {
                    tools.size_allocate(&gtk::Allocation::new(x0, y0, want.max(1), height));
                }
                if let Some(divider) = self.divider.borrow().as_ref() {
                    divider.size_allocate(&gtk::Allocation::new(x0 + want, y0, 1, height));
                }
                if let Some(heading) = self.heading.borrow().as_ref() {
                    heading.size_allocate(&gtk::Allocation::new(x0 + want + 1, y0, rest, height));
                }
            }
        }

        impl ContainerImpl for HeaderRow {
            fn add(&self, widget: &gtk::Widget) {
                widget.set_parent(&*self.obj());
            }

            fn remove(&self, widget: &gtk::Widget) {
                widget.unparent();
            }

            fn forall(
                &self,
                _include_internals: bool,
                callback: &gtk::subclass::container::Callback,
            ) {
                for child in [&self.heading, &self.divider, &self.tools] {
                    if let Some(child) = child.borrow().as_ref() {
                        callback.call(child);
                    }
                }
            }
        }
    }

    glib::wrapper! {
        pub struct HeaderRow(ObjectSubclass<imp::HeaderRow>)
            @extends gtk::Widget, gtk::Container;
    }

    impl HeaderRow {
        pub fn new() -> Self {
            glib::Object::new()
        }

        /// The three children, in drawing order: the heading, the boundary separator on top of it, and the toolbar.
        pub fn set_parts(&self, heading: &gtk::Box, divider: &gtk::Separator, tools: &gtk::Box) {
            let imp = imp::HeaderRow::from_obj(self);
            *imp.heading.borrow_mut() = Some(heading.clone().upcast());
            *imp.divider.borrow_mut() = Some(divider.clone().upcast());
            *imp.tools.borrow_mut() = Some(tools.clone().upcast());
            self.add(heading);
            self.add(divider);
            self.add(tools);
        }

        /// The headerbar the row lives in: its allocated width is the window's content width.
        pub fn set_bar(&self, bar: &gtk::HeaderBar) {
            *imp::HeaderRow::from_obj(self).bar.borrow_mut() = Some(bar.clone());
        }

        /// The `0x85` `topbar` geometry and the page's cell width the boundary is derived from (see
        /// [`super::header_split`]).
        pub fn set_band(&self, band: &BandCell) {
            *imp::HeaderRow::from_obj(self).band.borrow_mut() = Some(band.clone());
        }

        /// A42-LINUX-SIDEBAR-ALIGN-03: whether the row has been allocated since the last call (the header floor is
        /// derived from that allocation, so the caller re-sends it when this is true). Clears the mark.
        pub fn take_allocated(&self) -> bool {
            imp::HeaderRow::from_obj(self).allocated.replace(false)
        }

        /// The workspace heading (`0..boundary`).
        pub fn heading(&self) -> Option<gtk::Widget> {
            imp::HeaderRow::from_obj(self).heading.borrow().clone()
        }

        /// The 1 px separator on the boundary.
        pub fn divider(&self) -> Option<gtk::Widget> {
            imp::HeaderRow::from_obj(self).divider.borrow().clone()
        }

        /// The sidebar toolbar (`boundary..row right edge`); its intrinsic width is the row's minimum width.
        pub fn tools(&self) -> Option<gtk::Widget> {
            imp::HeaderRow::from_obj(self).tools.borrow().clone()
        }
    }
}

/// A26 rev7 (`CURRENT_REQUIREMENTS` rev 10 `top_container_alignment`): the width the Linux headerbar's workspace
/// container takes, in px — i.e. where the `│` between the two header containers sits, measured from the row's own
/// left edge.
///
/// The left sidebar's divider stroke is at `grid_w · (sidebar + 0.5) / cols` px from the page's
/// left edge. `grid_w` is the page's canvas width in px (`cols · cell`: the page fits whole cells, so it can be a
/// few px narrower than the headerbar, which fills the window), `row_x` the row's offset inside the headerbar and
/// `row_w` the row's width, which ends at GTK's native window buttons; `tools_min` is the toolbar's minimum width.
/// Unknown geometry (`cols` or `sidebar` 0) and a collapsed sidebar keep only the toolbar width on the left; the
/// toolbar is never squeezed below `tools_min`.
#[cfg(any(target_os = "linux", test))]
fn header_split(grid_w: i32, row_x: i32, row_w: i32, sidebar: u16, cols: u16, tools_min: i32) -> i32 {
    let grid_w = i64::from(grid_w);
    let row_w = i64::from(row_w);
    let boundary = if cols > 0 && sidebar > 0 {
        let left = i64::from(sidebar.min(cols));
        // Align to the body border stroke's cell centre, not its left edge.
        (grid_w * (2 * left + 1) + i64::from(cols)) / (2 * i64::from(cols))
    } else {
        i64::from(row_x) + i64::from(tools_min)
    };
    let want = boundary - i64::from(row_x);
    let room = row_w.saturating_sub(1).max(0);
    want.clamp(i64::from(tools_min).max(0).min(room), room) as i32
}

/// Narrowest left sidebar: actual row left offset plus intrinsic toolbar width, divided by cell width.
/// Left-native controls are included in the offset; right-native controls do not widen the sidebar.
/// The existing server floor applies to body and header together, even across collapse/expand.
#[cfg(any(target_os = "linux", test))]
fn sidebar_floor(row_x: i32, tools_min: i32, cols: u16, cell: f32) -> u16 {
    if cols == 0 || cell <= 0.0 || !cell.is_finite() {
        return 0;
    }
    let cols = i64::from(cols);
    let cell = f64::from(cell);
    let tools_min = i64::from(tools_min.max(0));
    // Only native controls on the LEFT contribute to the measured row offset.
    let boundary = i64::from(row_x.max(0)) + tools_min;
    (boundary as f64 / cell).ceil().clamp(0.0, cols as f64) as u16
}

/// A26 rev7 (`CURRENT_REQUIREMENTS` rev 12 `linux_header_theme`): the CSS the window's own headerbar is painted
/// with, built from the palette the window already resolved (`hello.theme` → [`GuiOpts::theme`]): the page's
/// background, its foreground, a grey mixed from both for the status text and a border between them.
///
/// [`Header::attach`] adds the provider to the **headerbar's own** style context, so the row follows the page while
/// no GTK/GNOME theme setting is touched and no other widget changes. Nothing is hardcoded: a light palette gives a
/// light headerbar with dark text, a dark palette a dark one with light text, and a missing theme keeps GTK's own
/// look (the provider is only built when a palette was resolved).
#[cfg(any(target_os = "linux", test))]
fn header_css(theme: &fleetmux_proto::theme::Theme) -> String {
    use fleetmux_proto::theme::{hex_rgb, rgb_hex};
    // The palette was validated when it was parsed; the fallbacks only keep this total if it ever were not.
    let bg = hex_rgb(&theme.bg).unwrap_or((0x1e, 0x1e, 0x1e));
    let fg = hex_rgb(&theme.fg).unwrap_or((0xff, 0xff, 0xff));
    // `mix(w)` is `bg` blended `w/256` of the way to `fg`: grey status text, a quiet border and the two button states.
    let mix = |w: u32| {
        let f = |a: u8, b: u8| {
            ((u32::from(a) * (256 - w) + u32::from(b) * w + 128) >> 8) as u8
        };
        rgb_hex((f(bg.0, fg.0), f(bg.1, fg.1), f(bg.2, fg.2)))
    };
    let (muted, border, hover, active) = (mix(150), mix(40), mix(24), mix(48));
    let (bg, fg) = (rgb_hex(bg), rgb_hex(fg));
    format!(
        "headerbar, headerbar:backdrop {{ background-image: none; background-color: {bg}; color: {fg}; \
         border-bottom: 1px solid {border}; box-shadow: none; }}\n\
         headerbar label {{ color: {fg}; }}\n\
         headerbar label.dim-label {{ color: {muted}; }}\n\
         headerbar button {{ background-image: none; background-color: transparent; border: none; \
         box-shadow: none; text-shadow: none; -gtk-icon-shadow: none; color: {fg}; }}\n\
         headerbar button:hover {{ background-color: {hover}; }}\n\
         headerbar button:active, headerbar button:checked {{ background-color: {active}; }}\n\
         headerbar separator {{ background-color: {border}; min-width: 1px; }}\n"
    )
}

/// A42-LINUX-SIDEBAR-ALIGN-03: one tool cell's width in px (the approved layout's 28 px cells) and the style class
/// that keeps it that width — the theme's headerbar button padding would otherwise make every cell 50 px.
#[cfg(target_os = "linux")]
const TOOL_CELL: i32 = 28;
#[cfg(target_os = "linux")]
const TOOL_CLASS: &str = "fm-tool";

/// The CSS that gives the five custom tool cells their own width, scoped to [`TOOL_CLASS`] and added to the
/// buttons' **own** style contexts ([`Header::build`]). GTK's native window buttons are untouched: they are
/// `titlebutton`s of the same headerbar and keep the OS-default size, spacing and behaviour.
#[cfg(target_os = "linux")]
fn tool_css() -> String {
    format!(
        "headerbar button.{class} {{ min-width: {cell}px; min-height: {cell}px; padding: 0; border: none; \
         box-shadow: none; }}\n",
        class = TOOL_CLASS,
        cell = TOOL_CELL,
    )
}

#[cfg(target_os = "linux")]
impl Header {
    /// Attach the headerbar to `window` as its titlebar. Called while the window is still hidden
    /// (`window_builder`'s `with_visible(false)`): GTK3 may ignore a titlebar set on a visible window. `theme` is the
    /// palette the page uses; the headerbar is painted from it ([`header_css`]), so the one native row follows the
    /// fleetmux theme instead of the toolkit's own light bar (`CURRENT_REQUIREMENTS` rev 12 `linux_header_theme`).
    fn attach(
        window: &tao::window::Window,
        prefix: u8,
        proxy: &EventLoopProxy<UiEvent>,
        theme: Option<&fleetmux_proto::theme::Theme>,
    ) -> Header {
        use gtk::prelude::*;
        use tao::platform::unix::WindowExtUnix;
        let header = Header::build(theme);
        // The prefix keys of `◫ + ↺ ← →`, in that order: ^b s, ^b c, ^b r, ^b (, ^b ).
        for (button, key) in header.buttons.iter().zip(*b"scr()") {
            let proxy = proxy.clone();
            // The prefix byte then the key, in one `0x01` frame: the server's own prefix handling does the rest
            // (the same functions the toolbar cells call), so the buttons need no new protocol message.
            button.connect_clicked(move |_| {
                let _ = proxy.send_event(UiEvent::Ipc(IpcMsg::Input(vec![prefix, key])));
            });
        }
        window.gtk_window().set_titlebar(Some(&header.bar));
        header.bar.show_all();
        header
    }

    /// Build the one native header row: the workspace header (`title` + `· status`) on the left, the `│` on the
    /// grid's sidebar divider, and the toolbar (`◫ + ↺ ← →`) right-aligned against GTK's own window buttons.
    ///
    /// A42-LINUX-MINWIDTH-01: the boundary follows the **current allocation** but must not feed that allocation back
    /// into the headerbar's minimum width — which the window manager turns into `WM_NORMAL_HINTS`, and which used to
    /// grow with every resize (the old width request written from `want` inside `size_allocate`). So the row is
    /// [`header_row::HeaderRow`]: it positions its children from the allocation in `size_allocate`, and its own size
    /// request is the toolbar's intrinsic width, so a grown window cannot ratchet the headerbar's floor.
    fn build(theme: Option<&fleetmux_proto::theme::Theme>) -> Header {
        use gtk::prelude::*;
        let title = gtk::Label::new(None);
        title.set_xalign(0.0);
        title.set_ellipsize(gtk::pango::EllipsizeMode::End);
        title.set_margin_start(16);
        let status = gtk::Label::new(None);
        status.set_xalign(0.0);
        status.set_ellipsize(gtk::pango::EllipsizeMode::End);
        status.set_margin_end(12);
        // The theme's own "secondary text" class: grey status next to the bold title, like the grid's row 0.
        status.style_context().add_class("dim-label");
        // The workspace header: title then status, left-aligned, filling the row's height. The row allocates it over
        // `0..boundary`, so its right edge is where the sidebar header starts.
        let heading = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        heading.set_halign(gtk::Align::Fill);
        heading.set_valign(gtk::Align::Fill);
        heading.pack_start(&title, true, true, 0);
        heading.pack_start(&status, false, false, 0);
        // The sidebar toolbar's buttons, right-aligned against the native window buttons. `pack_start` keeps the
        // order left to right (`◫ + ↺ ← →`), never the reverse a `pack_end` loop produces.
        let buttons = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        buttons.set_halign(gtk::Align::Start);
        buttons.set_valign(gtk::Align::Center);
        buttons.set_margin_start(4);
        buttons.set_margin_end(4);
        // A42-LINUX-SIDEBAR-ALIGN-03: the toolbar's *real* floor is what the boundary is clamped against, so the
        // five cells must really be the 28 px the layout asks for — not the theme's padded button (Yaru's headerbar
        // button carries 16 px of horizontal padding and a 1 px border, which turned every cell into 50 px and the
        // whole toolbar into 274 px instead of 148 px). The rule is scoped to this class and attached to each
        // button's own style context: GTK's native window buttons and every other widget keep their theme.
        let tool_provider = gtk::CssProvider::new();
        if tool_provider
            .load_from_data(tool_css().as_bytes())
            .is_err()
        {
            eprintln!("fleetmux-web: the headerbar tool cells could not be sized");
        }
        let mut tools_buttons = Vec::new();
        for (glyph, tip) in [
            ("◫", "Fleet sidebar (^b s)"),
            ("+", "New bash (^b c)"),
            ("↺", "Reopen fleet (^b r)"),
            ("←", "Previous (^b ()"),
            ("→", "Next (^b ))"),
        ] {
            let button = gtk::Button::with_label(glyph);
            button.set_relief(gtk::ReliefStyle::None);
            button.set_focus_on_click(false);
            // The approved layout's 28 px tool cells: narrow enough that five tools plus GTK's window buttons fit
            // the sidebar band at the default font, so the boundary really is the sidebar's drag line.
            button.set_size_request(TOOL_CELL, -1);
            button.style_context().add_class(TOOL_CLASS);
            button
                .style_context()
                .add_provider(&tool_provider, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
            button.set_tooltip_text(Some(tip));
            buttons.pack_start(&button, false, false, 0);
            tools_buttons.push(button);
        }
        let tools = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        tools.set_halign(gtk::Align::Fill);
        tools.set_valign(gtk::Align::Fill);
        tools.pack_start(&buttons, false, false, 0);
        let divider = gtk::Separator::new(gtk::Orientation::Vertical);
        divider.set_halign(gtk::Align::Fill);
        divider.set_valign(gtk::Align::Fill);
        // The row positions its three children from its own allocation ([`header_row`]): its request is the toolbar's
        // intrinsic width, so nothing it allocates can come back as the headerbar's minimum width.
        let row = header_row::HeaderRow::new();
        row.set_halign(gtk::Align::Fill);
        row.set_hexpand(true);
        row.set_parts(&heading, &divider, &tools);
        let bar = gtk::HeaderBar::new();
        // A26 rev7 (CURRENT_REQUIREMENTS rev 12 `linux_header_theme`): paint this one headerbar from the fleetmux
        // palette. The provider is attached to the headerbar's own style context, so nothing outside it changes and
        // no GTK/GNOME setting is written; the native window buttons stay GTK's own widgets, only their colours
        // follow the palette (readable on a light and on a dark page alike).
        if let Some(theme) = theme {
            let provider = gtk::CssProvider::new();
            // The CSS is generated from a validated palette, so this parses; if it ever did not, GTK keeps its own
            // look for this row instead of a half-painted one.
            if provider.load_from_data(header_css(theme).as_bytes()).is_err() {
                eprintln!("fleetmux-web: the headerbar palette could not be applied");
            }
            bar.style_context()
                .add_provider(&provider, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
            divider.style_context()
                .add_provider(&provider, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
        }
        // GTK's native window buttons; their place comes from the OS setting (`gtk-decoration-layout`), never from
        // a literal here — that is what keeps them at the default anchor and out of the terminal font's reach.
        bar.set_show_close_button(true);
        bar.set_custom_title(Some(&row));
        row.set_bar(&bar);
        // GTK centres a custom title at its natural width. This row is the whole topbar, so it fills the space
        // between the headerbar's start and its window buttons: the workspace header then ends exactly on the
        // sidebar's drag line and the sidebar header runs from there to the native buttons.
        if let Some(wrap) = row.parent() {
            wrap.set_halign(gtk::Align::Fill);
            wrap.set_hexpand(true);
        }
        let band = std::rc::Rc::new(std::cell::Cell::new(Band::default()));
        row.set_band(&band);
        Header {
            bar,
            title,
            status,
            buttons: tools_buttons,
            row,
            band,
        }
    }

    /// The `0x85` `topbar` text: the current fleet's title (bold) and `· line1` (grey). Both empty = no fleet.
    fn set(&self, title: &str, status: &str) {
        use gtk::prelude::*;
        self.title
            .set_markup(&format!("<b>{}</b>", gtk::glib::markup_escape_text(title)));
        self.status.set_text(&if status.is_empty() {
            String::new()
        } else {
            format!("· {status}")
        });
    }

    /// The `0x85` `topbar` geometry: the sidebar band and the viewer's columns. A change re-runs the row's
    /// allocation, which is what moves the boundary separator onto the grid's sidebar divider.
    fn set_band(&self, sidebar: u16, cols: u16) {
        use gtk::prelude::*;
        let mut band = self.band.get();
        if (band.sidebar, band.cols) != (sidebar, cols) {
            band.sidebar = sidebar;
            band.cols = cols;
            self.band.set(band);
            self.row.queue_resize();
        }
    }

    /// The page's cell width in px (its `inset` ipc `cell` key): with it the boundary is measured on the grid's own
    /// canvas `cols · cell` instead of on the headerbar, which is a few px wider because the page fits whole cells.
    /// A change re-runs the row's allocation, like [`Header::set_band`] does.
    fn set_cell(&self, cell: f32) {
        use gtk::prelude::*;
        let mut band = self.band.get();
        if band.cell != cell {
            band.cell = cell;
            self.band.set(band);
            self.row.queue_resize();
        }
    }

    /// A42-LINUX-SIDEBAR-ALIGN-03: the narrowest sidebar band this window's own header controls need, in columns,
    /// from the row's real allocation, the toolbar's measured floor, GTK's window buttons' measured span and the
    /// page's cell width ([`sidebar_floor`]). The server keeps the shown sidebar at least this wide (`0x06`
    /// `sidebar_min`), so a drag below it moves the grid's divider and the header boundary together instead of
    /// clamping only the header. The floor is a property of the header's geometry, not of the band currently shown:
    /// while the sidebar is collapsed (`^b s`, 0 columns — rev 12) the window still reports it, so expanding comes
    /// back to the effective band straight away instead of one round trip below it. 0 = no geometry yet, or no cell
    /// width (a collapsed band keeps its floor; the shown width is still 0 because `Side::width` returns 0 while
    /// hidden).
    fn sidebar_min(&self) -> u16 {
        use gtk::prelude::*;
        let band = self.band.get();
        if band.cols == 0 || band.cell <= 0.0 {
            return 0;
        }
        let row_x = self
            .row
            .translate_coordinates(&self.bar, 0, 0)
            .map_or(0, |(x, _)| x);
        let tools_min = self
            .row
            .tools()
            .map_or(0, |tools| tools.preferred_width().0);
        sidebar_floor(row_x, tools_min, band.cols, band.cell)
    }

    /// A42-LINUX-SIDEBAR-ALIGN-03 (M81 refinement): has the row been allocated since the last check? The floor
    /// ([`Header::sidebar_min`]) is computed from the row's real allocation, the toolbar's intrinsic width and GTK's
    /// native buttons' allocations, so a font/theme/window-state change that reallocates the header can move it.
    /// [`Ui::tick`] asks this after every event and re-sends the floor when it is set, so no change is missed just
    /// because the page did not report a new `inset`.
    pub fn take_alloc_dirty(&self) -> bool {
        self.row.take_allocated()
    }

}

/// The 128-bit ipc token of one window process (PROTOCOL.md §15.4, design.md §3.6.7).
pub struct Token([u8; 16]);

impl Token {
    /// 16 bytes from the OS random source.
    pub fn random() -> io::Result<Token> {
        let mut b = [0u8; 16];
        std::fs::File::open("/dev/urandom")?.read_exact(&mut b)?;
        Ok(Token(b))
    }
    /// 32 lowercase hex digits.
    pub fn hex(&self) -> String {
        self.0.iter().map(|b| format!("{b:02x}")).collect()
    }
    /// Does `s` carry this token? Compared without an early exit.
    fn matches(&self, s: &str) -> bool {
        let want = self.hex();
        s.len() == want.len()
            && s.bytes()
                .zip(want.bytes())
                .fold(0u8, |acc, (a, b)| acc | (a ^ b))
                == 0
    }
}

/// One ipc message from the terminal page (PROTOCOL.md §15.4 "ipc").
#[derive(Debug, Clone, PartialEq)]
pub enum IpcMsg {
    /// `input {data: base64}` → `0x01`.
    Input(Vec<u8>),
    /// `resize {cols, rows}` (each 1..=65535) → `0x02`.
    Resize { cols: u16, rows: u16 },
    /// `ack {seq}`: the page has written batch `seq` (back-pressure).
    Ack(u64),
    /// `copy {text}`: put `text` on the system clipboard.
    Copy(String),
    /// `paste`: read the system clipboard into the terminal.
    Paste,
    /// `history_focus`: DOM focus went into the iframe.
    HistoryFocus,
    /// `page_url {url}`: the iframe's current `fleetmux://page/…` URL.
    PageUrl(String),
    /// `page_loaded {ok}`: the iframe finished loading (`ok` = not an error page).
    PageLoaded(bool),
    /// `scroll {pane, offset}`: scroll that pane to `offset` (the server clamps) → `0x05` (PROTOCOL.md §6.3).
    Scroll { pane: String, offset: u64 },
    /// `drag_window` (S1e, plan §17.2): the person pressed on a row-0 cell the server does not use — drag the
    /// window. The page sends it when the window has a titlebar-less top row (macOS, PROTOCOL.md §15.4).
    DragWindow,
    /// `zoom_window`: a double click on that same row-0 zone — `set_maximized(!is_maximized())`, the macOS habit.
    ZoomWindow,
    /// `inset {cols, cell?}` (0..=40): the columns row 0 leaves for the window controls, after the first fit, a
    /// font-size change or a window resize → `0x06` (PROTOCOL.md §15.4). The controls themselves are never moved.
    /// `cell` is the page's own cell width in px (A42-LINUX-SIDEBAR-ALIGN-03): it stays in the window and puts the
    /// Linux header's boundary on the grid's divider column of the canvas the page really draws.
    Inset { cols: u16, cell: Option<f32> },
    /// `caret {x, y, w, h}` / `caret` (A42-LINUX-IME-CANDIDATE-05): the focused terminal textarea's cell in the
    /// page's CSS px, or no rect at all when the terminal stopped being the real focus target. Linux only in
    /// effect; it never leaves the window process — no frame, no `0x06`, no server field.
    Caret(Option<Caret>),
}

/// A42-LINUX-IME-CANDIDATE-05: the focused terminal textarea's cell in the page's CSS px. The window hands it to
/// WebKit's own input method context unchanged ([`caret_to_widget`]): it is already client-window-relative.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Caret {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

// A26 rev3 / rev5: the page draws no window controls and no resize grips, and `win` / `resize_window` are unknown
// ipc messages. The native close/minimize/maximize stay native on both platforms: macOS overlays the traffic
// lights on grid row 0, Linux draws GTK's own window buttons in its one headerbar ([`Header`]).

/// The token-checked JSON object of one ipc body, and its `type`.
fn ipc_object<'a>(
    token: &Token,
    v: &'a Value,
) -> Option<(&'a serde_json::Map<String, Value>, &'a str)> {
    let obj = v.as_object()?;
    if !token.matches(obj.get("token")?.as_str()?) {
        return None;
    }
    Some((obj, obj.get("type")?.as_str()?))
}

/// Decode one ipc body: `None` when the token is missing or wrong, or the body is not one of the shapes of
/// PROTOCOL.md §15.4.
pub fn check_ipc(token: &Token, body: &str) -> Option<IpcMsg> {
    let v: Value = serde_json::from_str(body).ok()?;
    let (obj, ty) = ipc_object(token, &v)?;
    let str_key = |k: &str| obj.get(k).and_then(Value::as_str);
    let dim = |k: &str| {
        obj.get(k)
            .and_then(Value::as_u64)
            .filter(|n| (1..=65535).contains(n))
            .map(|n| n as u16)
    };
    // S1e: 0 is a real inset (Linux, or a window narrower than the toolbar), so it is not `dim`'s 1..=65535.
    let inset = |k: &str| {
        obj.get(k)
            .and_then(Value::as_u64)
            .filter(|n| *n <= u64::from(view::INSET_MAX))
            .map(|n| n as u16)
    };
    // A42-LINUX-SIDEBAR-ALIGN-03: the page's cell width in px rides the same metrics message. Absent = unknown
    // (an older page); a non-number, a non-finite number or one outside 1..=256 px is a malformed message.
    let cell_px = |k: &str| match obj.get(k) {
        None => Some(None),
        Some(v) => v
            .as_f64()
            .filter(|n| n.is_finite() && (1.0..=256.0).contains(n))
            .map(|n| Some(n as f32)),
    };
    // A42-LINUX-IME-CANDIDATE-05: the caret cell, four small integers in the page's CSS px. A message with none of
    // the four keys is the invalidation (the terminal lost the focus); a partial or out-of-range rect is malformed
    // like any other bad key, so a half-written rect never becomes a caret.
    let caret = |obj: &serde_json::Map<String, Value>| -> Option<Option<Caret>> {
        if !["x", "y", "w", "h"].iter().any(|k| obj.contains_key(*k)) {
            return Some(None);
        }
        let n = |k: &str| {
            obj.get(k)
                .and_then(Value::as_i64)
                .filter(|v| (-100_000..=100_000).contains(v))
                .map(|v| v as i32)
        };
        Some(Some(Caret {
            x: n("x")?,
            y: n("y")?,
            w: n("w")?,
            h: n("h")?,
        }))
    };
    Some(match ty {
        "input" => IpcMsg::Input(b64_decode(str_key("data")?)?),
        "resize" => IpcMsg::Resize {
            cols: dim("cols")?,
            rows: dim("rows")?,
        },
        "ack" => IpcMsg::Ack(obj.get("seq")?.as_u64()?),
        "copy" => IpcMsg::Copy(str_key("text")?.to_string()),
        "paste" => IpcMsg::Paste,
        "history_focus" => IpcMsg::HistoryFocus,
        "page_url" => IpcMsg::PageUrl(str_key("url")?.to_string()),
        "page_loaded" => IpcMsg::PageLoaded(obj.get("ok")?.as_bool()?),
        "scroll" => IpcMsg::Scroll {
            pane: str_key("pane")?.to_string(),
            offset: obj.get("offset")?.as_u64()?,
        },
        "drag_window" => IpcMsg::DragWindow,
        "zoom_window" => IpcMsg::ZoomWindow,
        "inset" => IpcMsg::Inset {
            cols: inset("cols")?,
            cell: cell_px("cell")?,
        },
        "caret" => IpcMsg::Caret(caret(obj)?),
        _ => return None,
    })
}

/// `--test-hooks` only: the terminal page's `window.close()` (gui.js) as `{"token","type":"test_close"}`. Not an
/// [`IpcMsg`]: without `--test-hooks` the ipc handler never looks for it, so it is dropped like any unknown type.
fn is_test_close(token: &Token, body: &str) -> bool {
    serde_json::from_str::<Value>(body)
        .ok()
        .as_ref()
        .and_then(|v| ipc_object(token, v))
        .is_some_and(|(obj, ty)| ty == "test_close" && obj.len() == 2)
}

/// May this frame come in on stdin? Only `Screen` (`0x81`), `Overlay` (`0x85`) and `Scroll` (`0x86`).
pub fn pipe_frame_ok(f: &Frame) -> bool {
    matches!(f, Frame::Screen(_) | Frame::Overlay(_) | Frame::Scroll(_))
}

/// Events the GUI event loop receives from other threads (through `EventLoopProxy`).
#[derive(Debug)]
pub enum UiEvent {
    /// A `web.sock` request; the reply line goes back on the sender.
    Gui(GuiCmd, Sender<String>),
    /// The stdin reader has added to the batch: send it to the page.
    Flush,
    /// A token-checked ipc message from the terminal page.
    Ipc(IpcMsg),
    /// `--test-hooks` `window.close()` from the terminal page's main frame: the same path as the person closing
    /// the window (PROTOCOL.md §15.4).
    TestClose,
    /// The clipboard tool's output for a `paste`, or why there is none.
    Pasted(Result<Vec<u8>, String>),
    /// A clipboard tool failed (`copy`).
    ClipboardError(String),
    /// A42-LINUX-IME-CANDIDATE-05: the GTK input method context changed its preedit (`preedit-changed`). WebKit's own
    /// cursor area is re-sent from its editor state at composition time and can be one font step behind the live grid,
    /// so the retained caret is re-asserted after it; measured, the re-assert is the last write from the next preedit
    /// update on. Linux only: macOS has no GTK input method context.
    #[cfg(target_os = "linux")]
    CaretRefresh,
    /// stdin reached EOF or stdout broke: the client is gone, exit within 1 s.
    LifelineClosed,
    /// A frame stdin may not carry (exit 1).
    BadFrame(String),
}

/// The prefix key: what `frame.js` matches and the byte the terminal page sends for it.
struct Prefix {
    key: String,
    code: Option<String>,
    byte: u8,
}

impl Prefix {
    /// `C-<letter>` (or `C-[`, `C-\`, `C-]`, `C-^`, `C-_`, `C-@`), as PROTOCOL.md §9.1 names keys.
    fn parse(name: &str) -> Option<Prefix> {
        let rest = name.strip_prefix("C-")?;
        let mut chars = rest.chars();
        let c = chars.next()?;
        if chars.next().is_some() {
            return None;
        }
        let lower = c.to_ascii_lowercase();
        let (byte, code) = match lower {
            'a'..='z' => (
                lower as u8 - b'a' + 1,
                Some(format!("Key{}", c.to_ascii_uppercase())),
            ),
            '@' => (0, None),
            '[' => (0x1b, Some("BracketLeft".into())),
            '\\' => (0x1c, Some("Backslash".into())),
            ']' => (0x1d, Some("BracketRight".into())),
            '^' => (0x1e, None),
            '_' => (0x1f, None),
            _ => return None,
        };
        Some(Prefix {
            key: lower.to_string(),
            code,
            byte,
        })
    }
}

/// One item of the stdin batch, in arrival order.
enum Item {
    Screen(Vec<u8>),
    /// A `0x85` payload, re-serialised (so it is a JSON object literal, safe to put into a script).
    Overlay(String),
    /// A `0x86` payload, re-serialised the same way.
    Scroll(String),
}

impl Item {
    fn size(&self) -> usize {
        match self {
            Item::Screen(b) => b.len(),
            Item::Overlay(s) => s.len(),
            Item::Scroll(s) => s.len(),
        }
    }
}

/// The batch shared by the stdin reader and the main thread, and the back-pressure count.
#[derive(Default)]
struct Flow {
    state: Mutex<FlowState>,
    acked: Condvar,
}

#[derive(Default)]
struct FlowState {
    items: VecDeque<Item>,
    /// A `Flush` is on its way to the main thread (one at a time, however many frames arrive meanwhile).
    flush_sent: bool,
    /// Bytes read from stdin and not yet acknowledged by the page.
    inflight: usize,
}

/// stdin: the frame pipe and the lifeline. EOF (or a read error) → `LifelineClosed`, and the process ends itself
/// after [`LIFELINE_GRACE`] if the loop has not (§15.1 "within 1 s"); a frame other than `0x81` / `0x85` / `0x86`
/// → exit 1.
fn spawn_pipe_reader(flow: Arc<Flow>, proxy: EventLoopProxy<UiEvent>, dir: PathBuf) {
    let _ = std::thread::Builder::new()
        .name("stdin frames".into())
        .spawn(move || {
            let mut stdin = io::stdin().lock();
            let (why, code) = loop {
                let frame = match view::read_frame(&mut stdin) {
                    Ok(Some(f)) => f,
                    Ok(None) | Err(view::FrameError::Io(_)) => break ("stdin closed", 0),
                    Err(e) => {
                        let _ = proxy.send_event(UiEvent::BadFrame(format!("{e:?}")));
                        break ("bad frame on stdin", 1);
                    }
                };
                if !pipe_frame_ok(&frame) {
                    let _ = proxy.send_event(UiEvent::BadFrame(format!(
                        "frame type 0x{:02x} is not allowed on stdin",
                        frame.type_byte()
                    )));
                    break ("bad frame type on stdin", 1);
                }
                let item = match frame {
                    Frame::Screen(b) => Item::Screen(b),
                    Frame::Overlay(s) => match serde_json::from_str::<Value>(&s) {
                        Ok(v @ Value::Object(_)) => Item::Overlay(v.to_string()),
                        _ => {
                            log(&dir, "0x85 payload is not a JSON object; ignored");
                            continue;
                        }
                    },
                    Frame::Scroll(s) => match serde_json::from_str::<Value>(&s) {
                        Ok(v @ Value::Object(_)) => Item::Scroll(v.to_string()),
                        _ => {
                            log(&dir, "0x86 payload is not a JSON object; ignored");
                            continue;
                        }
                    },
                    _ => unreachable!("pipe_frame_ok"),
                };
                let mut st = flow.state.lock().unwrap_or_else(|p| p.into_inner());
                while st.inflight > INFLIGHT_MAX {
                    st = flow.acked.wait(st).unwrap_or_else(|p| p.into_inner());
                }
                st.inflight += item.size();
                st.items.push_back(item);
                if !st.flush_sent {
                    st.flush_sent = true;
                    drop(st);
                    if proxy.send_event(UiEvent::Flush).is_err() {
                        break ("event loop gone", 0);
                    }
                }
            };
            if code == 0 {
                let _ = proxy.send_event(UiEvent::LifelineClosed);
            }
            std::thread::sleep(LIFELINE_GRACE);
            sock::remove(&dir);
            log(&dir, &format!("exit: {why} (forced)"));
            std::process::exit(code);
        });
}

/// stdout: `0x01` / `0x02` frames, written off the main thread. A failed write = the client is gone (§15.4).
fn spawn_stdout_writer(proxy: EventLoopProxy<UiEvent>) -> Sender<Frame> {
    let (tx, rx) = mpsc::channel::<Frame>();
    let _ = std::thread::Builder::new()
        .name("stdout frames".into())
        .spawn(move || {
            let mut out = io::stdout().lock();
            for f in rx {
                if view::write_frame(&mut out, &f).is_err() || out.flush().is_err() {
                    let _ = proxy.send_event(UiEvent::LifelineClosed);
                    return;
                }
            }
        });
    tx
}

/// `fleetmux://app/<asset>` from memory; `fleetmux://page/<path>` from disk (on a worker thread) when
/// `policy::page_path` allows it and it is a file of at most [`PAGE_MAX`]; everything else 404.
fn custom_protocol(
    scope: Arc<RwLock<Option<PathBuf>>>,
    page_dir: PathBuf,
) -> impl Fn(wry::WebViewId, Request<Vec<u8>>, wry::RequestAsyncResponder) + Send + Sync + 'static {
    move |_id, req: Request<Vec<u8>>, responder| {
        let uri = req.uri().to_string();
        if let Some(name) = uri.strip_prefix(APP_PREFIX) {
            let name = name.split(['?', '#']).next().unwrap_or("");
            responder.respond(match assets::get(name) {
                Some((mime, body)) => response(200, mime, body.to_vec()),
                None => not_found(),
            });
            return;
        }
        if !uri.starts_with(PAGE_PREFIX) {
            responder.respond(not_found());
            return;
        }
        let run_dir = scope.read().unwrap_or_else(|p| p.into_inner()).clone();
        let page_dir = page_dir.clone();
        let _ = std::thread::Builder::new()
            .name("page file".into())
            .spawn(move || {
                let body = policy::page_path(&uri, run_dir.as_deref(), &page_dir).and_then(|p| {
                    let md = std::fs::metadata(&p).ok()?;
                    (md.is_file() && md.len() <= PAGE_MAX).then_some(())?;
                    std::fs::read(&p).ok()
                });
                responder.respond(match body {
                    Some(b) => response(200, "text/html; charset=utf-8", b),
                    None => not_found(),
                });
            });
    }
}

fn response(status: u16, mime: &str, body: Vec<u8>) -> Response<Vec<u8>> {
    Response::builder()
        .status(status)
        .header("Content-Type", mime)
        .header("Cache-Control", "no-store")
        .body(body)
        .unwrap_or_else(|_| Response::new(Vec::new()))
}

fn not_found() -> Response<Vec<u8>> {
    response(
        404,
        "text/html; charset=utf-8",
        NOT_FOUND.as_bytes().to_vec(),
    )
}

/// Every ipc body: token-checked ([`check_ipc`]); on macOS it must also come from `fleetmux://app/` (wry reports
/// the URL of the frame that sent it, and the iframe can reach the handler: WP probe P4). Everything else is
/// dropped without an answer.
fn ipc_handler(
    token: Token,
    proxy: EventLoopProxy<UiEvent>,
    test_hooks: bool,
) -> impl Fn(Request<String>) + 'static {
    move |req: Request<String>| {
        if cfg!(target_os = "macos") && !req.uri().to_string().starts_with(APP_PREFIX) {
            return;
        }
        let body = req.body();
        if let Some(m) = check_ipc(&token, body) {
            let _ = proxy.send_event(UiEvent::Ipc(m));
        } else if test_hooks && is_test_close(&token, body) {
            let _ = proxy.send_event(UiEvent::TestClose);
        }
    }
}

/// The GUI allow-list as wry's navigation handler (main frame and the iframe alike).
fn navigation_handler(
    dir: &Path,
    scope: Arc<RwLock<Option<PathBuf>>>,
    page_dir: &Path,
) -> impl Fn(String) -> bool + 'static {
    let (dir, page_dir) = (dir.to_path_buf(), page_dir.to_path_buf());
    move |url: String| {
        let run_dir = scope.read().unwrap_or_else(|p| p.into_inner()).clone();
        // M4 (design m4 §5.3): with a server that has pages a `?fleet-jump=` link in the embedded page never
        // loads; it goes to the server as a `file://` URL (the attach side maps a pushed copy back), and the
        // server switches the page. Checked before the allow-list: the scope follows the shown page, so a link
        // back to the parent fleet is outside it.
        if sock::pages()
            && policy::is_fleet_jump(&url)
            && let Some(file) = policy::to_file_url(&url)
        {
            log(&dir, &format!("fleet-jump reported to the server: {file}"));
            sock::notify_jump(&file);
            return false;
        }
        match policy::allow_gui(&url, run_dir.as_deref(), &page_dir) {
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
}

fn scheme(url: &str) -> String {
    url.split_once(':')
        .map(|(s, _)| s.to_ascii_lowercase())
        .unwrap_or_default()
}

/// `url` without its fragment.
fn document(url: &str) -> &str {
    url.split('#').next().unwrap_or(url)
}

/// An `open` / `goto` waiting for the iframe.
struct PendingLoad {
    /// The requested `fleetmux://page/…` URL.
    want: String,
    reply: Sender<String>,
    deadline: Instant,
    /// Fragment-only change of the loaded document: answered on the URL change, there is no load.
    same_doc: Option<Option<String>>,
}

/// A42-LINUX-IME-CANDIDATE-05: the page's caret cell (CSS px, page coordinates) → the rect
/// `WebKitInputMethodContext::notify_cursor_area` takes, which is **client-window-relative** logical px.
///
/// No space term is added here. WebKit hands the GTK input method the webview's own `GdkWindow`
/// (`WebKitInputMethodContextImplGtk.cpp`, parent-provided), ibus's GTK3 client turns that rect into root
/// coordinates with `gdk_window_get_root_coords(client_window, …)` and only then multiplies by the window's scale
/// factor (`gtk3/ibusimcontext.c`, `_set_cursor_location_internal`), and the page's CSS origin coincides with that
/// client window (M100, independent probe on the same GTK/WebKit: IBus received the caret plus a duplicated
/// `(5, 51)` — the window origin this function used to add — while the true caret root was 51 px above it; the
/// DOM viewport and the client window share their origin). So the page cell *is* the whole conversion: adding the
/// window origin here would be added a second time by GTK, and adding a scale/DPR factor would be added a second
/// time by ibus.
///
/// `None` for a rect that is not a real cell, or that is not **entirely** inside the webview — the whole extent is
/// checked, not only the top-left corner: nothing outside the widget is ever handed to the input method.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn caret_to_widget(css: Caret, view: (i32, i32)) -> Option<(i32, i32, i32, i32)> {
    let (vw, vh) = view;
    if css.w < 1 || css.h < 1 || vw < 1 || vh < 1 {
        return None;
    }
    if css.x < 0 || css.y < 0 {
        return None;
    }
    // The far edges: a cell that would end outside the widget is dropped, not clipped, so the IM never gets a rect
    // that claims to be somewhere the webview does not draw.
    if css.x.checked_add(css.w)? > vw || css.y.checked_add(css.h)? > vh {
        return None;
    }
    Some((css.x, css.y, css.w, css.h))
}

/// A42-LINUX-IME-CANDIDATE-05 (M99): the client window's root origin as a cache key, from the three elements
/// `gdk` 0.18's `Window::origin()` really returns.
///
/// The binding is `(ret, x, y)`: `gdk_window_get_origin`'s boolean success result **first**, then the coordinates
/// (`gdk-0.18.2/src/auto/window.rs`, `pub fn origin(&self) -> (i32, i32, i32)`). Reading the tuple as `(x, y, _)`
/// takes `ret` for the origin's x and silently drops the real y. `ret == 0` means the window is not on screen, so
/// the coordinates are meaningless: the key is `None` (unknown), never an origin of `(0, 0)`.
///
/// The scale factor is part of the key because a scale change re-converts the root even when the root's logical
/// numbers stay the same. This key is **only** a cache: the rect handed to `notify_cursor_area` stays the page's
/// raw logical CSS cell, with no root and no scale folded into it.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn caret_origin_key(ret: i32, x: i32, y: i32, scale: i32) -> Option<(i32, i32, i32)> {
    if ret == 0 {
        return None;
    }
    Some((x, y, scale))
}

/// A42-LINUX-IME-CANDIDATE-05 (M99): a strictly smaller, still non-empty sub-rect of the caret cell that keeps the
/// anchor (`x`, `y`).
///
/// It exists for one case only: ibus's GTK3 client drops a rectangle it already holds **before** it converts the
/// rect to root coordinates (`ibtusimcontext.c` `ibus_im_context_set_cursor_location`), so when a window move
/// changes the client window's root origin while the client-relative cell stays the same, re-notifying the same
/// rect never refreshes IBus (`move_bus: []` in the product trace: IBus kept `37,83` where `157,143` was due).
/// Writing a different rectangle first makes the held value change, so the real rect written immediately after it,
/// in the same handler, is converted again.
///
/// The value is always a sub-rect of the cell - same corner, never wider or taller, never empty - so it cannot
/// claim an area the caret does not occupy, and it never reaches past an edge the cell itself is inside.
/// The width is shrunk by one when it is wider than a pixel, otherwise the height; a 1x1 cell has no such
/// sub-rect and yields `None` rather than a widened or otherwise faked rectangle.
///
/// What an input method makes of a narrower box in the instant before the real rect follows (an underline, a
/// candidate-window anchor) is the IM's own behaviour and is not claimed here; the real rect replaces it in the
/// same handler, which is the point of this value and what the real-IBus regression has to confirm.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn caret_invalidator(rect: (i32, i32, i32, i32)) -> Option<(i32, i32, i32, i32)> {
    let (x, y, w, h) = rect;
    if w > 1 {
        Some((x, y, w - 1, h))
    } else if h > 1 {
        Some((x, y, w, h - 1))
    } else {
        None
    }
}

/// The main thread's state.
struct Ui {
    webview: wry::WebView,
    window: tao::window::Window,
    dir: PathBuf,
    page_dir: PathBuf,
    scope: Arc<RwLock<Option<PathBuf>>>,
    flow: Arc<Flow>,
    out: Sender<Frame>,
    proxy: EventLoopProxy<UiEvent>,
    /// The terminal page has reported its first size (its `fm` exists).
    ready: bool,
    /// A `Flush` came before the page was ready.
    deferred: bool,
    seq: u64,
    /// Bytes of each batch not yet acknowledged.
    sizes: HashMap<u64, usize>,
    /// The iframe's current address (from the page), `None` before the first load.
    page_url: Option<String>,
    /// M4 (design m4 §3.4 gui row, §5.3): the `url` of the last `0x85` that had one. When the next one names
    /// another document (the server selected another fleet page), the iframe follows it ([`Ui::follow_overlay`]).
    overlay_url: Option<String>,
    pending: Option<PendingLoad>,
    /// S1e: an `inset` (plan §17.2) that came before the first size, held until that `0x02` is out: the first frame
    /// on stdout stays the page's first fit (PROTOCOL.md §15.4, and the graphical tests assert it).
    inset: Option<Frame>,
    /// A42-LINUX-SIDEBAR-ALIGN-03: the last `inset` columns the page reported (0 on Linux), so a header-floor update
    /// can be sent on the same `0x06` line without waiting for the page.
    inset_cols: u16,
    /// The last `sidebar_min` sent to the server (0 = none yet): a new `0x06` goes out only when the floor moves.
    last_sidebar_min: u16,
    /// A26 rev5: the Linux window's one native header row (its title/status labels), attached in [`run`].
    #[cfg(target_os = "linux")]
    header: Header,
    /// A42-LINUX-IME-CANDIDATE-05: the window currently holds the focus (tao's `Focused` event). A caret is only
    /// pushed while it does; a focus loss drops the cache so a refocus re-pushes the same cell.
    #[cfg(target_os = "linux")]
    focused: bool,
    /// A42-LINUX-IME-CANDIDATE-05: the last caret rect pushed to the input method context (webview widget px), so an
    /// identical rect is not pushed twice. `None` after a `caret` without a rect, a focus loss or an invalid rect.
    #[cfg(target_os = "linux")]
    last_caret: Option<(i32, i32, i32, i32)>,
    /// A42-LINUX-IME-CANDIDATE-05 (M99): the client window's root origin and scale at the last accepted push, as
    /// [`caret_origin_key`] returns them (`(x, y, scale)`; `None` while gdk cannot confirm the window is on screen).
    /// ibus's GTK3 client drops an identical rect before converting it to root, so a move that leaves the
    /// client-relative cell unchanged needs [`caret_invalidator`] to make the IM recompute the anchor.
    #[cfg(target_os = "linux")]
    last_root: Option<(i32, i32, i32)>,
    /// A42-LINUX-IME-CANDIDATE-05 (M99): the current push is a re-assert, so it must write even an identical rect
    /// (WebKit's own composition-time write has to be superseded). Cleared by the push that consumes it.
    #[cfg(target_os = "linux")]
    force_caret: bool,
    /// A42-LINUX-IME-CANDIDATE-05: the latest caret the page reported. It is kept even when the push is not possible
    /// yet, because the page's own dedupe will not report that cell again and tao's `Focused` event can arrive after
    /// the page's `window focus` ipc message (two independent sources for the same GTK focus-in) — a regain re-pushes
    /// this value instead of waiting for a report that never comes.
    #[cfg(target_os = "linux")]
    pending_caret: Option<Caret>,
}

impl Ui {
    fn on_event(&mut self, e: UiEvent) {
        match e {
            UiEvent::Gui(cmd, reply) => self.command(cmd, reply),
            UiEvent::Flush => self.flush(),
            UiEvent::Ipc(m) => self.ipc(m),
            UiEvent::TestClose => self.close("window closed (test hook)"),
            UiEvent::Pasted(Ok(bytes)) => {
                let js = format!("fm.paste('{}')", b64_encode(&bytes));
                self.eval(&js);
            }
            UiEvent::Pasted(Err(why)) | UiEvent::ClipboardError(why) => {
                log(&self.dir, &why);
                self.eval(&format!("fm.notice({})", json!(why)));
            }
            UiEvent::LifelineClosed => self.exit(0, "stdin closed"),
            UiEvent::BadFrame(why) => self.exit(1, &why),
            #[cfg(target_os = "linux")]
            UiEvent::CaretRefresh => self.reassert_caret(),
        }
    }

    /// The person closed the window (or, with `--test-hooks`, the terminal page called `window.close()`).
    fn close(&mut self, why: &str) {
        sock::notify_all(&json!({"event": "window", "state": "closed"}).to_string());
        self.exit(0, why);
    }

    fn exit(&mut self, code: i32, why: &str) {
        if let Some(p) = self.pending.take() {
            let _ = p.reply.send(sock::error_line("the window is closing"));
        }
        sock::remove(&self.dir);
        log(&self.dir, &format!("exit: {why}"));
        std::process::exit(code);
    }

    fn eval(&self, js: &str) {
        if let Err(e) = self.webview.evaluate_script(js) {
            log(&self.dir, &format!("evaluate_script: {e}"));
        }
    }

    /// Evaluate `expr` in the page and answer with `make(value)`; a page that cannot answer gets an error line.
    fn eval_reply(&self, expr: &str, reply: Sender<String>, make: fn(Value) -> Value) {
        let js = format!("(() => {{ try {{ return {expr}; }} catch (e) {{ return null; }} }})()");
        let r = reply.clone();
        let sent = self.webview.evaluate_script_with_callback(&js, move |res| {
            let line = match serde_json::from_str::<Value>(&res) {
                Ok(v) if !v.is_null() => make(v).to_string(),
                _ => sock::error_line("the terminal page is not ready"),
            };
            let _ = r.send(line);
        });
        if let Err(e) = sent {
            let _ = reply.send(sock::error_line(&format!("evaluate_script: {e}")));
        }
    }

    fn command(&mut self, cmd: GuiCmd, reply: Sender<String>) {
        let ok = || json!({"ok": true}).to_string();
        match cmd {
            GuiCmd::Base(Cmd::Open(url)) => self.load(url, true, reply),
            GuiCmd::Base(Cmd::Goto(url)) => self.load(url, false, reply),
            GuiCmd::Base(Cmd::Url) => {
                let url = self
                    .page_url
                    .as_deref()
                    .and_then(policy::to_file_url)
                    .unwrap_or_else(|| "about:blank".into());
                let _ = reply.send(json!({"ok": true, "url": url}).to_string());
            }
            GuiCmd::Base(Cmd::Reload) => {
                self.eval("fm.reloadPage()");
                let _ = reply.send(ok());
            }
            // The connection thread already answered (§15.3 "reply, then exit").
            GuiCmd::Base(Cmd::Quit) => self.exit(0, "quit"),
            GuiCmd::Scope(dir) => {
                *self.scope.write().unwrap_or_else(|p| p.into_inner()) = Some(dir);
                let _ = reply.send(ok());
            }
            GuiCmd::Focus => {
                self.eval("fm.focusPage()");
                let _ = reply.send(ok());
            }
            GuiCmd::Text => {
                self.eval_reply("fm.text()", reply, |rows| json!({"ok": true, "rows": rows}))
            }
            GuiCmd::Overlay => self.eval_reply("fm.overlay()", reply, |mut v| {
                v["ok"] = json!(true);
                v
            }),
            GuiCmd::Js(code) => {
                // The completion value as JSON (`null` for undefined / not serialisable); a throw is an error.
                let js = format!(
                    "(() => {{ try {{ const v = (0, eval)({}); \
                     try {{ return {{v: v === undefined ? null : JSON.parse(JSON.stringify(v))}}; }} \
                     catch (e) {{ return {{v: String(v)}}; }} }} catch (e) {{ return {{e: String(e)}}; }} }})()",
                    json!(code)
                );
                let r = reply.clone();
                let sent = self.webview.evaluate_script_with_callback(&js, move |res| {
                    let v: Value = serde_json::from_str(&res).unwrap_or(Value::Null);
                    let line = match (v.get("e"), v.get("v")) {
                        (Some(e), _) => sock::error_line(&format!(
                            "the script threw: {}",
                            e.as_str().unwrap_or("?")
                        )),
                        (None, Some(v)) => json!({"ok": true, "result": v}).to_string(),
                        (None, None) => json!({"ok": true, "result": null}).to_string(),
                    };
                    let _ = r.send(line);
                });
                if let Err(e) = sent {
                    let _ = reply.send(sock::error_line(&format!("evaluate_script: {e}")));
                }
            }
        }
    }

    /// M4 (design m4 §3.4 gui row): a `0x85` whose `url` names another document than the last one (the server
    /// switched the fleet page) loads it into the iframe — only once a page was loaded at all (the first `url` is
    /// the attach side's load, through `scope` and the page exchange), only inside the GUI allow-list and only when
    /// the file is on this host (a cross-host page comes through the attach side's page exchange instead). Not
    /// through [`Ui::load`]: nothing waits for an answer, and a pending `open` keeps its own.
    fn follow_overlay(&mut self, overlay: &str) {
        let Some(url) = serde_json::from_str::<Value>(overlay)
            .ok()
            .and_then(|v| v.get("url").and_then(Value::as_str).map(str::to_string))
        else {
            return;
        };
        let Some(prev) = self.overlay_url.replace(url.clone()) else {
            return;
        };
        if document(&prev) == document(&url) || self.page_url.is_none() {
            return;
        }
        let Some(page) = policy::to_page_url(&url) else {
            return;
        };
        if self
            .page_url
            .as_deref()
            .is_some_and(|cur| document(cur) == document(&page))
        {
            return;
        }
        let run_dir = self.scope.read().unwrap_or_else(|p| p.into_inner()).clone();
        let local = policy::page_path(&page, run_dir.as_deref(), &self.page_dir)
            .is_some_and(|p| p.is_file());
        if !local || policy::allow_gui(&page, run_dir.as_deref(), &self.page_dir) != Decision::Allow {
            return;
        }
        log(&self.dir, &format!("history page follows the server: {page}"));
        let _ = self
            .webview
            .evaluate_script(&format!("fm.load({})", json!(page)));
    }

    /// `open` / `goto`: a `file://` URL → `fleetmux://page/…` → GUI allow-list → `fm.load`; answered when the
    /// iframe has loaded it (or, for a fragment-only change, when its URL moves), within [`LOAD_DEADLINE`].
    fn load(&mut self, url: String, front: bool, reply: Sender<String>) {
        let Some(page) = policy::to_page_url(&url) else {
            let _ = reply.send(sock::error_line(&format!("{url} is not a local file: URL")));
            return;
        };
        let run_dir = self.scope.read().unwrap_or_else(|p| p.into_inner()).clone();
        if policy::allow_gui(&page, run_dir.as_deref(), &self.page_dir) != Decision::Allow {
            let _ = reply.send(sock::error_line(&format!(
                "{url} is not allowed in this window"
            )));
            return;
        }
        let same_doc = (page.contains('#')
            && self
                .page_url
                .as_deref()
                .is_some_and(|cur| document(cur) == document(&page)))
        .then(|| self.page_url.clone());
        if let Err(e) = self
            .webview
            .evaluate_script(&format!("fm.load({})", json!(page)))
        {
            let _ = reply.send(sock::error_line(&format!("loading {url} failed: {e}")));
            return;
        }
        if let Some(old) = self.pending.replace(PendingLoad {
            want: page,
            reply,
            deadline: Instant::now() + LOAD_DEADLINE,
            same_doc,
        }) {
            let _ = old.reply.send(json!({"ok": true}).to_string());
        }
        if front && !quiet::on() {
            self.window.set_visible(true);
            self.window.set_focus();
        }
    }

    fn answer_pending(&mut self) {
        if let Some(p) = self.pending.take() {
            let _ = p.reply.send(json!({"ok": true}).to_string());
        }
    }

    fn ipc(&mut self, m: IpcMsg) {
        match m {
            IpcMsg::Input(bytes) => {
                let _ = self.out.send(Frame::Input(bytes));
            }
            IpcMsg::Resize { cols, rows } => {
                let _ = self.out.send(Frame::Resize { cols, rows });
                if !self.ready {
                    self.ready = true;
                    // S1e: the page's inset, held back so the first fit stays the first frame (see `Ui::inset`).
                    if let Some(f) = self.inset.take() {
                        let _ = self.out.send(f);
                    }
                    if std::mem::take(&mut self.deferred) {
                        self.flush();
                    }
                }
            }
            IpcMsg::Ack(seq) => {
                if let Some(n) = self.sizes.remove(&seq) {
                    self.release(n);
                }
            }
            IpcMsg::Copy(text) => clipboard_copy(text, self.proxy.clone()),
            IpcMsg::Paste => clipboard_paste(self.proxy.clone()),
            IpcMsg::HistoryFocus => {
                sock::notify_all(&json!({"event": "history_focus"}).to_string());
            }
            IpcMsg::PageUrl(url) => {
                let answer = self.pending.as_ref().is_some_and(|p| match &p.same_doc {
                    Some(before) => before.as_deref() != Some(url.as_str()) || url == p.want,
                    None => false,
                });
                self.page_url = Some(url);
                if answer {
                    self.answer_pending();
                }
            }
            IpcMsg::PageLoaded(_ok) => {
                let answer = self.pending.as_ref().is_some_and(|p| {
                    p.same_doc.is_none()
                        && self
                            .page_url
                            .as_deref()
                            .is_some_and(|cur| document(cur) == document(&p.want))
                });
                if answer {
                    self.answer_pending();
                }
            }
            IpcMsg::Scroll { pane, offset } => {
                let _ = self
                    .out
                    .send(Frame::ScrollTo(ScrollTo { pane, offset }.encode()));
            }
            // S1e (plan §17.2): row 0's drag zone. tao moves the window itself (wry has no drag-region property),
            // and a double click toggles the maximised state as macOS does.
            IpcMsg::DragWindow => {
                let _ = self.window.drag_window();
            }
            IpcMsg::ZoomWindow => {
                let zoomed = self.window.is_maximized();
                let _ = self.window.set_maximized(!zoomed);
            }
            // S1e (plan §17.3): the page's control columns → `0x06`, the same pipe as `0x05` (the client
            // forwards it to the server, which only knows the frame if its `ok` line carried `"inset":1`).
            // A26: the window controls are the anchor and are never moved from their system default.
            IpcMsg::Inset { cols, cell } => {
                // A42-LINUX-SIDEBAR-ALIGN-03: the cell width is the window's own metric (the grid's canvas is
                // `cols · cell` px); it never goes to the server, only into the header row's boundary.
                self.inset_cols = cols;
                #[cfg(target_os = "linux")]
                if let Some(px) = cell {
                    self.header.set_cell(px);
                }
                #[cfg(not(target_os = "linux"))]
                let _ = cell;
                // The window's own header floor rides the same `0x06` line (`sidebar_min`, optional): the server
                // keeps the shown sidebar at least that wide, so the grid's divider moves with the header boundary
                // instead of a header-only clamp when the user drags below the controls' real intrinsic width.
                #[cfg(target_os = "linux")]
                let sidebar_min = self.header.sidebar_min();
                #[cfg(not(target_os = "linux"))]
                let sidebar_min = 0;
                self.last_sidebar_min = sidebar_min;
                let f = Frame::Inset(
                    view::Inset {
                        cols,
                        sidebar_min,
                    }
                    .encode(),
                );
                if self.ready {
                    let _ = self.out.send(f);
                } else {
                    self.inset = Some(f);
                }
            }
            // A42-LINUX-IME-CANDIDATE-05: the focused terminal's caret cell, re-asserted to WebKit's own input
            // method context on Linux. Window-local: no frame, no `0x06`, no server field (macOS keeps WebKit's
            // native behaviour and drops it here).
            IpcMsg::Caret(caret) => {
                #[cfg(target_os = "linux")]
                self.push_caret_area(caret);
                #[cfg(not(target_os = "linux"))]
                let _ = caret;
            }
        }
    }

    fn release(&self, n: usize) {
        let mut st = self.flow.state.lock().unwrap_or_else(|p| p.into_inner());
        st.inflight = st.inflight.saturating_sub(n);
        self.flow.acked.notify_all();
    }

    /// A42-LINUX-IME-CANDIDATE-05: hand the focused terminal textarea's cell to WebKit's own input method context,
    /// so GTK/ibus receives a fresh cursor area after a caret move, a fed screen (pane switch), a font change, a
    /// resize or a zoom. WebKit itself only refreshes it on focus and selection changes — a focused textarea moved
    /// by CSS produces no `gtk_im_context_set_cursor_location` at all (measured, `workspace/impl-1/probe-gdk2` and
    /// build-1's ordered validation), which is what put the candidate window at a stale cell.
    ///
    /// The conversion is [`caret_to_widget`] (the page's CSS cell, client-window-relative logical px, no DPR factor).
    /// Nothing is pushed while the window is unfocused or the webview is not mapped, an identical rect is not pushed
    /// twice, and a `caret` without a rect only drops the cache (never a zero rect: the IM keeps the last real one
    /// until the next focus).
    #[cfg(target_os = "linux")]
    fn push_caret_area(&mut self, caret: Option<Caret>) {
        use gtk::prelude::*;
        use webkit2gtk::{InputMethodContextExt, WebViewExt};
        use wry::WebViewExtUnix;
        let Some(caret) = caret else {
            self.pending_caret = None;
            self.last_caret = None;
            self.last_root = None;
            self.force_caret = false;
            return;
        };
        self.pending_caret = Some(caret);
        let view = self.webview.webview();
        if !self.focused || !view.is_mapped() {
            self.last_caret = None; // kept in `pending_caret`, pushed again when the window regains the focus
            self.force_caret = false;
            return;
        }
        let alloc = view.allocation();
        // M100: the IM's client window is the webview's own GdkWindow and the page's CSS origin coincides with it, so
        // the page cell is already the client-window-relative rect. GTK/ibus adds the window origin and the scale
        // when it converts this to root coordinates; adding either here would duplicate it (measured: the caret plus
        // a duplicated (5, 51)).
        let Some(rect) = caret_to_widget(caret, (alloc.width(), alloc.height())) else {
            self.last_caret = None;
            return;
        };
        // M99/M105: the client window's root origin, read at every push. A move changes it while the client-relative
        // cell stays the same, and ibus's GTK3 client drops an identical rect before it converts it to root
        // coordinates - so that one case needs the invalidator below, not just another identical notify. `origin()`
        // returns `(ret, x, y)` (gdk 0.18); an unknown origin (`ret == 0`: the window is not on screen) is never
        // counted as "unchanged", because gdk could not confirm where the client window is.
        let root = view.window().and_then(|w| {
            let (ret, x, y) = w.origin();
            caret_origin_key(ret, x, y, w.scale_factor())
        });
        let same = self.last_caret == Some(rect);
        let root_moved = root.is_none() || self.last_root != root;
        let forced = std::mem::take(&mut self.force_caret);
        if same && !forced && !root_moved {
            return;
        }
        let Some(im) = view.input_method_context() else {
            return;
        };
        if same && root_moved {
            // The rect is bit-identical, so IBus would keep its cached root: write a smaller sub-rect first (same
            // anchor, width shrunk by one, else the height) and then the real rect, inside this same handler. A 1x1
            // cell has no smaller sub-rect, so nothing is faked for it - the canonical write below still happens.
            if let Some(inv) = caret_invalidator(rect) {
                im.notify_cursor_area(inv.0, inv.1, inv.2, inv.3);
            }
        }
        im.notify_cursor_area(rect.0, rect.1, rect.2, rect.3);
        self.last_caret = Some(rect);
        self.last_root = root;
    }

    /// A42-LINUX-IME-CANDIDATE-05: re-assert the retained caret, bypassing the rect cache. Used by the events that can
    /// change what the IM holds **without** a new page report: a window geometry change (the caret's root position
    /// moves even though the client-window-relative cell does not), a focus regain (the page's report and tao's
    /// `Focused` are not ordered) and a composition start (WebKit re-sends its own area from the helper textarea,
    /// which can be one font size behind the grid). No timer, no refocus, no synthesized selection: the value is the
    /// last one the page itself reported.
    #[cfg(target_os = "linux")]
    fn reassert_caret(&mut self) {
        if self.pending_caret.is_none() {
            return;
        }
        // A re-assert must reach the IM even when the rect is unchanged (WebKit's own composition-time write has to be
        // superseded); a move additionally goes through the invalidator in `push_caret_area`, because IBus would
        // otherwise keep the root it cached before the client window moved.
        self.force_caret = true;
        self.push_caret_area(self.pending_caret);
    }

    /// A42-LINUX-IME-CANDIDATE-05: the window's geometry changed (resize, maximize/restore or a move). The caret's
    /// root position can move with it while the client-window-relative cell stays the same, so the retained caret is
    /// re-pushed; the push decides whether that needs the invalidator (root moved, rect identical) or a plain write.
    #[cfg(target_os = "linux")]
    fn geometry_changed(&mut self) {
        self.reassert_caret();
    }

    /// A42-LINUX-IME-CANDIDATE-05: the window's focus state. A focus loss drops the caret cache, so the same cell
    /// is pushed again after a refocus. On a regain the **already reported** caret is re-pushed: the page's
    /// `window focus` ipc message and this tao event are two independent notifications of the same GTK focus-in and
    /// are not ordered, so the page can have reported while `focused` was still false (the push was dropped and the
    /// page's dedupe will not repeat it). No timer, no refocus, no synthesized selection: just the retained value.
    #[cfg(target_os = "linux")]
    fn focus_changed(&mut self, focused: bool) {
        self.focused = focused;
        self.last_caret = None;
        if focused {
            self.reassert_caret();
        }
    }

    /// Send everything the stdin reader has queued, in order, as `fm.feed` calls of at most [`FEED_MAX`] raw
    /// bytes; an overlay or a scroll state ends its call, so it is applied right after the screen bytes that came
    /// before it.
    fn flush(&mut self) {
        if !self.ready {
            self.deferred = true;
            return;
        }
        let items: Vec<Item> = {
            let mut st = self.flow.state.lock().unwrap_or_else(|p| p.into_inner());
            st.flush_sent = false;
            st.items.drain(..).collect()
        };
        let mut bytes: Vec<u8> = Vec::new();
        let mut overlays: Vec<String> = Vec::new();
        let mut scrolls: Vec<String> = Vec::new();
        for item in items {
            match item {
                Item::Screen(b) => {
                    if !overlays.is_empty()
                        || !scrolls.is_empty()
                        || (!bytes.is_empty() && bytes.len() + b.len() > FEED_MAX)
                    {
                        self.feed(
                            std::mem::take(&mut bytes),
                            std::mem::take(&mut overlays),
                            std::mem::take(&mut scrolls),
                        );
                    }
                    bytes.extend_from_slice(&b);
                }
                Item::Overlay(o) => {
                    self.follow_overlay(&o);
                    // A26 rev5: the `0x85` `topbar` key is the native headerbar's title/status (Linux).
                    #[cfg(target_os = "linux")]
                    self.set_topbar(&o);
                    overlays.push(o);
                }
                Item::Scroll(s) => scrolls.push(s),
            }
        }
        if !bytes.is_empty() || !overlays.is_empty() || !scrolls.is_empty() {
            self.feed(bytes, overlays, scrolls);
        }
    }

    fn feed(&mut self, bytes: Vec<u8>, overlays: Vec<String>, scrolls: Vec<String>) {
        self.seq += 1;
        let size = bytes.len()
            + overlays.iter().map(String::len).sum::<usize>()
            + scrolls.iter().map(String::len).sum::<usize>();
        let js = format!(
            "fm.feed({},'{}',[{}],[{}])",
            self.seq,
            b64_encode(&bytes),
            overlays.join(","),
            scrolls.join(",")
        );
        match self.webview.evaluate_script(&js) {
            Ok(()) => {
                self.sizes.insert(self.seq, size);
            }
            Err(e) => {
                log(&self.dir, &format!("evaluate_script (feed): {e}"));
                self.release(size);
            }
        }
    }

    /// A26 rev5/rev7 (`CURRENT_REQUIREMENTS` rev 6 `utm_topbar`, rev 10 `top_container_alignment`): fill the native
    /// headerbar from a `0x85` payload's `topbar` key — its `title`/`status` text and its `sidebar`/`cols` geometry,
    /// which puts the workspace/sidebar container boundary on the grid's sidebar drag line. The server sends it only
    /// to a mirrored (Linux) layout, where the grid keeps no topbar row; a `0x85` without the key (an older server)
    /// leaves the headerbar's labels empty rather than inventing a title, and its geometry at the fallback.
    #[cfg(target_os = "linux")]
    fn set_topbar(&mut self, overlay: &str) {
        let Ok(v) = serde_json::from_str::<Value>(overlay) else {
            return;
        };
        match v.get("topbar").and_then(Value::as_object) {
            Some(t) => {
                self.header.set(
                    t.get("title").and_then(Value::as_str).unwrap_or(""),
                    t.get("status").and_then(Value::as_str).unwrap_or(""),
                );
                let band = |key: &str| {
                    t.get(key)
                        .and_then(Value::as_u64)
                        .filter(|n| *n <= u64::from(u16::MAX))
                        .map_or(0, |n| n as u16)
                };
                self.header.set_band(band("sidebar"), band("cols"));
                // A42-LINUX-SIDEBAR-ALIGN-03: the server's effective sidebar is now known; report the band this
                // window's controls need so the next drag below it moves the grid's divider, not just the header.
                self.send_sidebar_min();
            }
            None => self.header.set("", ""),
        }
    }

    /// A42-LINUX-SIDEBAR-ALIGN-03: tell the server the narrowest sidebar band this window's header controls need
    /// (`0x06` `sidebar_min`), when it differs from the last one sent. The line also carries the page's last
    /// `inset` columns, so it is a valid `0x06` for an older server too (the extra key is optional there).
    #[cfg(target_os = "linux")]
    fn send_sidebar_min(&mut self) {
        let sidebar_min = self.header.sidebar_min();
        if sidebar_min == self.last_sidebar_min {
            return;
        }
        self.last_sidebar_min = sidebar_min;
        let f = Frame::Inset(
            view::Inset {
                cols: self.inset_cols,
                sidebar_min,
            }
            .encode(),
        );
        if self.ready {
            let _ = self.out.send(f);
        } else {
            self.inset = Some(f);
        }
    }

    /// After every event: answer a load whose deadline passed; wake up for the next deadline.
    fn tick(&mut self) -> ControlFlow {
        // A42-LINUX-SIDEBAR-ALIGN-03 (M81 refinement): the header was allocated since the last tick — a font or
        // theme change, a native window-state change or a band change moved the geometry the floor is derived
        // from. Recompute and send it if it really moved (`send_sidebar_min` dedupes), so the effective sidebar
        // follows every such change and not only the page's `inset` reports.
        #[cfg(target_os = "linux")]
        if self.header.take_alloc_dirty() {
            self.send_sidebar_min();
        }
        match self.pending.as_ref().map(|p| p.deadline) {
            Some(d) if Instant::now() >= d => {
                self.answer_pending();
                ControlFlow::Wait
            }
            Some(d) => ControlFlow::WaitUntil(d),
            None => ControlFlow::Wait,
        }
    }
}

/// The clipboard tool for this session: macOS `pbcopy` / `pbpaste`; Linux `wl-copy` / `wl-paste -n` under
/// Wayland, else `xclip -selection clipboard [-o]` under X11.
fn clipboard_tool(paste: bool) -> Option<(&'static str, &'static [&'static str])> {
    let set = |k: &str| std::env::var_os(k).is_some_and(|v| !v.is_empty());
    if cfg!(target_os = "macos") {
        Some(if paste {
            ("pbpaste", &[])
        } else {
            ("pbcopy", &[])
        })
    } else if set("WAYLAND_DISPLAY") {
        Some(if paste {
            ("wl-paste", &["-n"])
        } else {
            ("wl-copy", &[])
        })
    } else if set("DISPLAY") {
        Some(if paste {
            ("xclip", &["-selection", "clipboard", "-o"])
        } else {
            ("xclip", &["-selection", "clipboard"])
        })
    } else {
        None
    }
}

/// Put `text` on the system clipboard from a worker thread (never blocks the UI).
fn clipboard_copy(text: String, proxy: EventLoopProxy<UiEvent>) {
    let _ = std::thread::Builder::new()
        .name("clipboard copy".into())
        .spawn(move || {
            let Some((tool, args)) = clipboard_tool(false) else {
                let _ = proxy.send_event(UiEvent::ClipboardError(
                    "copy: no clipboard in this session".into(),
                ));
                return;
            };
            let run = Command::new(tool)
                .args(args)
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .and_then(|mut child| {
                    if let Some(mut stdin) = child.stdin.take() {
                        stdin.write_all(text.as_bytes())?;
                    }
                    child.wait()
                });
            match run {
                Ok(st) if st.success() => {}
                Ok(st) => {
                    let _ = proxy.send_event(UiEvent::ClipboardError(format!("copy: {tool} {st}")));
                }
                Err(e) => {
                    let _ = proxy.send_event(UiEvent::ClipboardError(format!("copy: {tool}: {e}")));
                }
            }
        });
}

/// Read the system clipboard on a worker thread; the text comes back as [`UiEvent::Pasted`].
fn clipboard_paste(proxy: EventLoopProxy<UiEvent>) {
    let _ = std::thread::Builder::new()
        .name("clipboard paste".into())
        .spawn(move || {
            let result = match clipboard_tool(true) {
                None => Err("paste: no clipboard in this session".to_string()),
                Some((tool, args)) => match Command::new(tool)
                    .args(args)
                    .stdin(Stdio::null())
                    .stderr(Stdio::null())
                    .output()
                {
                    Ok(o) if o.status.success() => Ok(o.stdout),
                    Ok(o) => Err(format!("paste: {tool} {}", o.status)),
                    Err(e) => Err(format!("paste: {tool}: {e}")),
                },
            };
            let _ = proxy.send_event(UiEvent::Pasted(result));
        });
}

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Standard base64 with padding (PROTOCOL.md §4.7).
fn b64_encode(data: &[u8]) -> String {
    let mut s = String::with_capacity(data.len().div_ceil(3) * 4);
    for c in data.chunks(3) {
        let n = (c[0] as u32) << 16
            | (*c.get(1).unwrap_or(&0) as u32) << 8
            | *c.get(2).unwrap_or(&0) as u32;
        s.push(B64[(n >> 18) as usize & 63] as char);
        s.push(B64[(n >> 12) as usize & 63] as char);
        s.push(if c.len() > 1 {
            B64[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        s.push(if c.len() > 2 {
            B64[n as usize & 63] as char
        } else {
            '='
        });
    }
    s
}

/// Strict standard base64 (padding required, no whitespace); `None` on anything else.
fn b64_decode(s: &str) -> Option<Vec<u8>> {
    let b = s.as_bytes();
    if !b.len().is_multiple_of(4) {
        return None;
    }
    let val = |c: u8| B64.iter().position(|&x| x == c).map(|v| v as u32);
    let mut out = Vec::with_capacity(b.len() / 4 * 3);
    for (i, q) in b.chunks(4).enumerate() {
        let last = i == b.len() / 4 - 1;
        let pad = q.iter().rev().take_while(|&&c| c == b'=').count();
        if pad > 2 || (pad > 0 && !last) {
            return None;
        }
        let mut n = 0u32;
        for &c in &q[..4 - pad] {
            n = n << 6 | val(c)?;
        }
        n <<= 6 * pad as u32;
        out.push((n >> 16) as u8);
        if pad < 2 {
            out.push((n >> 8) as u8);
        }
        if pad < 1 {
            out.push(n as u8);
        }
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_roundtrip_and_strict() {
        for data in [
            &b""[..],
            b"x",
            b"xy",
            b"xyz",
            b"hello world",
            &[0, 255, 1, 2, 254],
        ] {
            assert_eq!(b64_decode(&b64_encode(data)).as_deref(), Some(data));
        }
        assert_eq!(b64_decode("eA=="), Some(b"x".to_vec()));
        for bad in ["not base64!", "eA=", "e===", "eA==eA==", "eA= =", "eA\n=="] {
            assert_eq!(b64_decode(bad), None, "{bad:?}");
        }
    }

    #[test]
    fn prefix_names() {
        let p = Prefix::parse("C-b").unwrap();
        assert_eq!(
            (p.key.as_str(), p.code.as_deref(), p.byte),
            ("b", Some("KeyB"), 2)
        );
        assert_eq!(Prefix::parse("C-a").unwrap().byte, 1);
        assert_eq!(Prefix::parse("C-]").unwrap().byte, 0x1d);
        for bad in ["b", "C-", "C-bb", "M-b", "C-1"] {
            assert!(Prefix::parse(bad).is_none(), "{bad}");
        }
    }

    /// S1e (plan §17.2 / §17.4-1): the titlebar drag zone, the double-click zoom and the page's inset report are
    /// three ipc messages, all token-checked like every other one.
    #[test]
    fn check_ipc_accepts_drag_zoom_and_inset() {
        let t = Token::random().unwrap();
        let with = |v: Value| {
            let mut o = v.as_object().cloned().unwrap_or_default();
            o.insert("token".into(), json!(t.hex()));
            Value::Object(o).to_string()
        };
        assert_eq!(
            check_ipc(&t, &with(json!({"type": "drag_window"}))),
            Some(IpcMsg::DragWindow),
            "drag_window from the row-0 drag zone (plan §17.2)"
        );
        assert_eq!(
            check_ipc(&t, &with(json!({"type": "zoom_window"}))),
            Some(IpcMsg::ZoomWindow),
            "zoom_window from a double click on that zone (plan §17.2)"
        );
        for cols in [0u16, 10, 22, 40] {
            assert_eq!(
                check_ipc(&t, &with(json!({"type": "inset", "cols": cols}))),
                Some(IpcMsg::Inset { cols, cell: None }),
                "inset cols {cols} is the page's control columns (plan §17.2)"
            );
        }
        // A42-LINUX-SIDEBAR-ALIGN-03: the page's cell width in px rides the same message, and is optional so an
        // older page (or the macOS page before this change) still validates.
        assert_eq!(
            check_ipc(
                &t,
                &with(json!({"type": "inset", "cols": 0, "cell": 8.0}))
            ),
            Some(IpcMsg::Inset {
                cols: 0,
                cell: Some(8.0)
            }),
            "the Linux page's cell width is the grid's own px per column"
        );
        assert_eq!(
            check_ipc(
                &t,
                &with(json!({"type": "inset", "cols": 10, "cell": 8.3984375}))
            ),
            Some(IpcMsg::Inset {
                cols: 10,
                cell: Some(8.3984375)
            }),
            "a fractional cell width is a real font metric, not a bad message"
        );
        for bad in [
            json!({"type": "inset"}),
            json!({"type": "inset", "cols": 41}),
            json!({"type": "inset", "cols": -1}),
            json!({"type": "inset", "cols": 10.0}),
            json!({"type": "inset", "cols": "10"}),
            json!({"type": "inset", "cols": 0, "cell": 0}),
            json!({"type": "inset", "cols": 0, "cell": -8}),
            json!({"type": "inset", "cols": 0, "cell": 257}),
            json!({"type": "inset", "cols": 0, "cell": "8"}),
            json!({"type": "inset", "cols": 0, "cell": null}),
        ] {
            assert_eq!(
                check_ipc(&t, &with(bad.clone())),
                None,
                "inset {bad} is not a valid inset (0-40, plan §17.3)"
            );
        }
        assert_eq!(
            check_ipc(&t, &json!({"type": "drag_window"}).to_string()),
            None,
            "drag_window without the token is dropped like every other ipc message"
        );
    }

    /// A42-LINUX-IME-CANDIDATE-05: the caret message. Four integers are the focused terminal textarea's cell in the
    /// page's CSS px; a message with none of them is the invalidation (the terminal stopped being the focus target);
    /// a partial, fractional or absurd rect is malformed like any other bad key. It is a window-local message: no
    /// frame is built from it (see `gui_linux_caret_push_is_window_local` in tests/gui.rs).
    #[test]
    fn check_ipc_accepts_the_caret_cell_and_its_invalidation() {
        let t = Token::random().unwrap();
        let with = |v: Value| {
            let mut o = v.as_object().cloned().unwrap_or_default();
            o.insert("token".into(), json!(t.hex()));
            Value::Object(o).to_string()
        };
        for (x, y, w, h) in [(0, 0, 1, 1), (448, 176, 8, 16), (1272, 664, 8, 16)] {
            assert_eq!(
                check_ipc(
                    &t,
                    &with(json!({"type": "caret", "x": x, "y": y, "w": w, "h": h}))
                ),
                Some(IpcMsg::Caret(Some(Caret { x, y, w, h }))),
                "caret {x},{y} {w}x{h} is the page's own cell in CSS px"
            );
        }
        assert_eq!(
            check_ipc(&t, &with(json!({"type": "caret"}))),
            Some(IpcMsg::Caret(None)),
            "a caret line with no rect is the invalidation, not an error"
        );
        for bad in [
            json!({"type": "caret", "x": 448, "y": 176, "w": 8}),
            json!({"type": "caret", "x": 448, "y": 176, "h": 16}),
            json!({"type": "caret", "x": 448, "y": 176, "w": 8, "h": 16.5}),
            json!({"type": "caret", "x": "448", "y": 176, "w": 8, "h": 16}),
            json!({"type": "caret", "x": 448, "y": 176, "w": 8, "h": 100_001}),
            json!({"type": "caret", "x": null, "y": 176, "w": 8, "h": 16}),
        ] {
            assert_eq!(
                check_ipc(&t, &with(bad.clone())),
                None,
                "caret {bad} is not a real cell"
            );
        }
        assert_eq!(
            check_ipc(&t, &json!({"type": "caret"}).to_string()),
            None,
            "caret without the token is dropped like every other ipc message"
        );
    }

    /// A42-LINUX-IME-CANDIDATE-05: the conversion itself, the page's CSS cell unchanged. This pins the arithmetic
    /// (no window origin, no scale) and the bounds only — the space is the measured one in `caret_to_widget`'s docs
    /// (M100: IBus received the caret plus a duplicated `(5, 51)`, so the client-window-relative rect is the page
    /// cell itself). Nothing here claims a scale factor or a DPR multiplication.
    #[test]
    fn caret_to_widget_keeps_the_page_cell_in_client_window_space() {
        let cell = |x, y, w, h| Caret { x, y, w, h };
        // The M100 case: the caret cell at CSS (448, 176) 8x16 is handed over unchanged — the window origin the code
        // used to add is what ibus then added a second time.
        assert_eq!(
            caret_to_widget(cell(448, 176, 8, 16), (1275, 623)),
            Some((448, 176, 8, 16)),
            "the page cell is already client-window-relative: no window offset"
        );
        // Same numbers at a scale-2 window: ibus multiplies by the client window's scale factor, so the rect stays
        // GTK-logical here.
        assert_eq!(
            caret_to_widget(cell(448, 176, 8, 16), (1275, 623)),
            Some((448, 176, 8, 16)),
            "devicePixelRatio never enters this function"
        );
        // The first cell of a webview at the window origin.
        assert_eq!(
            caret_to_widget(cell(0, 0, 8, 16), (1280, 674)),
            Some((0, 0, 8, 16))
        );
        // The whole extent must be inside the webview: a cell that would end past the edge is dropped, not clipped.
        assert_eq!(
            caret_to_widget(cell(1280, 0, 8, 16), (1280, 674)),
            None,
            "a top-left on the far edge is outside the webview"
        );
        assert_eq!(
            caret_to_widget(cell(1276, 0, 8, 16), (1280, 674)),
            None,
            "a cell whose far edge ends past the widget is dropped, even though its top-left is inside"
        );
        assert_eq!(
            caret_to_widget(cell(1272, 658, 8, 16), (1280, 674)),
            Some((1272, 658, 8, 16)),
            "the bottom-right cell ends exactly on the widget's edge and is a real caret cell"
        );
        assert_eq!(
            caret_to_widget(cell(0, 660, 8, 16), (1280, 674)),
            None,
            "a cell ending below the widget is dropped"
        );
        assert_eq!(caret_to_widget(cell(-8, 176, 8, 16), (1275, 623)), None);
        assert_eq!(caret_to_widget(cell(448, -16, 8, 16), (1275, 623)), None);
        // Not a cell at all (xterm's 0x0 default), or a degenerate view.
        assert_eq!(caret_to_widget(cell(448, 176, 0, 16), (1275, 623)), None);
        assert_eq!(caret_to_widget(cell(448, 176, 8, 0), (1275, 623)), None);
        assert_eq!(caret_to_widget(cell(448, 176, 8, 16), (0, 674)), None);
    }

    /// A42-LINUX-IME-CANDIDATE-05 (M105): gdk 0.18's `Window::origin()` is `(ret, x, y)` — the `gboolean` result
    /// first, then the coordinates. The cache key must read that order (the M99 code read `(x, y, _)`, i.e. `ret` as
    /// x and the real y dropped), keep the scale with the root, and treat `ret == 0` (window not on screen: the
    /// coordinates are meaningless) as unknown instead of as an origin of `(0, 0)`.
    #[test]
    fn caret_origin_key_reads_the_gdk_tuple_order_and_result() {
        assert_eq!(caret_origin_key(1, 157, 143, 1), Some((157, 143, 1)));
        assert_eq!(
            caret_origin_key(1, 5, 51, 2),
            Some((5, 51, 2)),
            "the middle element is x and the last one y, never (ret, x)"
        );
        assert_eq!(
            caret_origin_key(0, 157, 143, 1),
            None,
            "ret == 0: gdk could not confirm the window is on screen"
        );
        assert_ne!(
            caret_origin_key(1, 157, 143, 1),
            caret_origin_key(1, 157, 143, 2),
            "a scale change refreshes the anchor even when the root's logical numbers stay the same"
        );
    }

    /// A42-LINUX-IME-CANDIDATE-05 (M99): the invalidator that makes ibus recompute the root after a window move. It
    /// keeps the anchor (`x`, `y`) and returns a strictly smaller, non-empty sub-rect: the width shrinks by one when
    /// it is wider than a pixel, otherwise the height; a 1x1 cell has none (`None`) and is never widened.
    #[test]
    fn caret_invalidator_is_a_strictly_smaller_real_sub_rect() {
        assert_eq!(caret_invalidator((448, 176, 8, 16)), Some((448, 176, 7, 16)));
        assert_eq!(caret_invalidator((1272, 658, 8, 16)), Some((1272, 658, 7, 16)));
        assert_eq!(
            caret_invalidator((448, 176, 1, 16)),
            Some((448, 176, 1, 15)),
            "a one-pixel-wide cell shrinks the height instead of widening the box"
        );
        assert_eq!(
            caret_invalidator((1279, 658, 1, 16)),
            Some((1279, 658, 1, 15)),
            "the last column of a 1280 px webview stays inside it: a widened box would end 1 px outside"
        );
        assert_eq!(
            caret_invalidator((448, 176, 1, 1)),
            None,
            "a 1x1 cell has no smaller non-empty sub-rect: nothing is faked"
        );
        // Property, over the boundary shapes: a sub-rect of the cell (same corner, never wider/taller, never
        // empty) that differs from it — and only a 1x1 cell has no such value.
        for rect in [(0, 0, 1, 1), (0, 0, 2, 16), (7, 9, 1, 2), (1272, 658, 8, 16), (1279, 0, 1, 1)] {
            let (x, y, w, h) = rect;
            match caret_invalidator(rect) {
                Some((ix, iy, iw, ih)) => {
                    assert_eq!((ix, iy), (x, y), "the anchor must not move: {rect:?}");
                    assert!(iw >= 1 && ih >= 1, "never an empty rect: {rect:?}");
                    assert!(iw <= w && ih <= h, "never wider or taller than the cell: {rect:?}");
                    assert!((iw, ih) != (w, h), "must differ or ibus drops it: {rect:?}");
                }
                None => assert_eq!((w, h), (1, 1), "only a 1x1 cell has no sub-rect: {rect:?}"),
            }
        }
    }

    /// A26 rev3 (CURRENT_REQUIREMENTS rev 4): the page draws no window controls and no resize grips — the native
    /// Linux window buttons must not be replaced by HTML lookalikes — so `win` / `resize_window` are unknown ipc
    /// messages and are dropped like any other bad shape.
    #[test]
    fn check_ipc_drops_the_removed_html_window_controls() {
        let t = Token::random().unwrap();
        let with = |v: Value| {
            let mut o = v.as_object().cloned().unwrap_or_default();
            o.insert("token".into(), json!(t.hex()));
            Value::Object(o).to_string()
        };
        for bad in [
            json!({"type": "win", "action": "close"}),
            json!({"type": "win", "action": "minimize"}),
            json!({"type": "win", "action": "maximize"}),
            json!({"type": "resize_window", "dir": "e"}),
            json!({"type": "resize_window", "dir": "nw"}),
        ] {
            assert_eq!(check_ipc(&t, &with(bad.clone())), None, "{bad}");
        }
    }

    /// A26: the macOS topbar geometry — the measured native controls are the anchor, the first toolbar column sits
    /// past them, row 0 is padded to their centre, and the page draws no controls of its own. Linux's one header
    /// row is the GTK headerbar (A26 rev5, `gui_linux_headerbar_is_the_one_header_row` in tests/gui.rs), so the
    /// page only pads and reports an inset on macOS.
    #[test]
    fn gui_assets_have_the_unified_topbar_geometry() {
        let (_, js) = assets::get("gui.js").expect("the terminal page script is an asset");
        let js = std::str::from_utf8(js).expect("gui.js is UTF-8");
        for (needle, why) in [
            (
                "CONTROL = 14",
                "the window controls are the measured 14 px system default",
            ),
            (
                "CONTROL_GAP = 9",
                "9 px between them, as the measured native traffic lights",
            ),
            (
                "CONTROL_X = 9",
                "a 9 px left margin, as the measured native traffic lights",
            ),
            (
                "INSET_PX = CONTROLS_RIGHT + 12",
                "the toolbar starts 12 px right of the controls",
            ),
            (
                "controlCenter",
                "row 0 is padded to the measured control centre",
            ),
            (
                "placeTopbar",
                "the page pads row 0 so B/C share the native controls' centre",
            ),
            (
                "IS_MAC",
                "only macOS pads row 0 for the controls (Linux's row is the GTK headerbar)",
            ),
            (
                "'calc(100% - '",
                "the pad is taken off the grid's height, so the last row is never clipped",
            ),
        ] {
            assert!(js.contains(needle), "gui.js must contain {needle:?}: {why}");
        }
        assert!(
            !js.contains("78"),
            "the fixed 78 px inset is gone: the controls are centred on row 0 and the toolbar follows them"
        );
        for gone in ["customControls", "'win'", "resize_window", "#wc", "rg-n"] {
            assert!(
                !js.contains(gone),
                "gui.js must not draw window controls any more: {gone:?}"
            );
        }
        let (_, css) = assets::get("gui.css").expect("the terminal page style is an asset");
        let css = std::str::from_utf8(css).expect("gui.css is UTF-8");
        assert!(
            !css.contains("#wc") && !css.contains(".rg"),
            "gui.css must not style page-drawn window controls or resize grips"
        );
    }

    /// A26 rev7 (`CURRENT_REQUIREMENTS` rev 10 `top_container_alignment`) / A42-LINUX-SIDEBAR-ALIGN-03: the Linux
    /// headerbar's workspace/sidebar boundary is the grid's sidebar divider, expressed as the workspace container's
    /// width, and it never squeezes the toolbar. The first argument is the **page's** canvas width (the grid the
    /// page really draws, `cols · cell`), which is why the last case differs from the headerbar's own width. The
    /// sizes are the rev 5 allocation dump's (1210×856 window, 1200 px headerbar, row at x = 10, 1070 px wide up to
    /// GTK's window buttons).
    #[test]
    fn header_split_puts_the_boundary_on_the_grids_sidebar_divider() {
        assert_eq!(header_split(1200, 10, 1070, 36, 150, 160), 282);
        assert_eq!(header_split(600, 0, 530, 36, 75, 160), 292);
        assert_eq!(header_split(1200, 10, 1070, 36, 300, 160), 160);
        // Collapsing never removes the toolbar; the remaining row belongs to the heading.
        assert_eq!(header_split(1200, 10, 1070, 0, 150, 160), 160);
        assert_eq!(header_split(1200, 10, 1070, 36, 0, 160), 160);
        assert_eq!(header_split(200, 0, 100, 36, 150, 160), 99);
        assert_eq!(header_split(1280, 6, 1154, 34, 160, 148), 270);
        assert_eq!(header_split(1286, 6, 1154, 34, 160, 148), 271);
        // The body glyph occupies x=34*8+4, not the cell's x=34*8 edge (native screenshot regression).
        assert_eq!(header_split(1280, 0, 1280, 34, 160, 148), 276);
        assert_eq!(header_split(1280, 6, 1154, 8, 160, 148), 148);
        assert_eq!(header_split(0, 0, 0, 0, 0, 148), 0);
        // OS controls on the left shift the row, not the grid divider.
        assert_eq!(header_split(1280, 120, 1030, 34, 160, 148), 156);
    }

    /// A42-LINUX-SIDEBAR-ALIGN-03: the shared constraint for a band that cannot hold the toolbar. The floor is
    /// derived from the row's real end, the toolbar's measured intrinsic width, GTK's window buttons' measured span
    /// and the page's cell width, so the server can keep the grid's divider and the header boundary on the same
    /// column instead of clamping only the header. The live geometry needs 34 columns; a legal narrower request (24)
    /// therefore keeps the band at 34, while a wider request is never shrunk. The numbers are measurements of this
    /// geometry (the M81 operator boundary's own two examples among them), not a fixed column rule.
    #[test]
    fn sidebar_floor_is_the_shared_constraint_for_the_header_controls() {
        // Right-side native buttons no longer occupy the sidebar. Only the row's left offset matters.
        assert_eq!(sidebar_floor(6, 148, 160, 8.0), 20);
        assert_eq!(sidebar_floor(6, 148, 160, 8.0).max(24), 24);
        assert_eq!(sidebar_floor(6, 148, 160, 7.0), 22);
        assert_eq!(sidebar_floor(6, 148, 160, 7.9), 20);
        assert_eq!(sidebar_floor(6, 274, 160, 8.0), 35);
        // A system theme with native buttons on the left is respected, not overridden.
        assert_eq!(sidebar_floor(120, 148, 160, 8.0), 34);
        assert_eq!(sidebar_floor(6, 148, 160, 2.0), 77);
        assert_eq!(sidebar_floor(6, 148, 0, 8.0), 0);
        for cell in [0.0, -1.0, f32::NAN, f32::INFINITY] {
            assert_eq!(sidebar_floor(6, 148, 160, cell), 0);
        }
    }

    /// A26 rev7 (`CURRENT_REQUIREMENTS` rev 12 `linux_header_theme`): the Linux headerbar is painted from the
    /// fleetmux palette — the page background, its foreground, a grey mixed from both and a border between them — so
    /// the one native header row follows the theme instead of being a white system bar, and a light palette stays
    /// light (nothing is hardcoded to one scheme).
    #[test]
    fn header_css_follows_the_fleetmux_palette() {
        let dark = fleetmux_proto::theme::Theme {
            bg: "#1e1e1e".into(),
            fg: "#e6e6e6".into(),
            cursor: "#98989d".into(),
            ansi: vec!["#000000".into(); 16],
        };
        let css = header_css(&dark);
        assert!(css.contains("background-color: #1e1e1e"), "{css}");
        assert!(css.contains("color: #e6e6e6"), "{css}");
        assert!(
            css.contains("border-bottom: 1px solid #") && !css.contains("solid #1e1e1e"),
            "the boundary is a colour mixed from the palette, not the background itself: {css}"
        );
        // A light palette gives a light header with dark text and its own border: the CSS is derived, not fixed.
        let light = fleetmux_proto::theme::Theme {
            bg: "#fafafa".into(),
            fg: "#20242b".into(),
            ..dark.clone()
        };
        let light_css = header_css(&light);
        assert!(light_css.contains("background-color: #fafafa"), "{light_css}");
        assert!(light_css.contains("color: #20242b"), "{light_css}");
        assert!(
            !light_css.contains("#1e1e1e") && !light_css.contains("#e6e6e6"),
            "a light palette must not inherit the dark one: {light_css}"
        );
        // Status text, the separator and the button states are all in the same rule set.
        for needle in [
            "headerbar label.dim-label",
            "headerbar separator",
            "headerbar button:hover",
            "headerbar button:active",
        ] {
            assert!(css.contains(needle), "the headerbar CSS lacks {needle:?}: {css}");
        }
        // Scoped to the headerbar: no rule may leak into the rest of the app or the global GTK theme.
        for rule in css.lines().filter(|l| !l.trim().is_empty()) {
            assert!(
                rule.starts_with("headerbar"),
                "a rule is not headerbar-scoped: {rule}"
            );
        }
    }

    #[test]
    fn test_close_needs_the_token_and_exact_shape() {
        let t = Token::random().unwrap();
        let ok = json!({"token": t.hex(), "type": "test_close"}).to_string();
        assert!(is_test_close(&t, &ok));
        assert!(!is_test_close(
            &t,
            &json!({"type": "test_close"}).to_string()
        ));
        assert!(!is_test_close(
            &t,
            &json!({"token": "0".repeat(32), "type": "test_close"}).to_string()
        ));
        assert!(!is_test_close(
            &t,
            &json!({"token": t.hex(), "type": "test_close", "x": 1}).to_string()
        ));
        assert_eq!(check_ipc(&t, &ok), None, "not an IpcMsg");
    }

    /// A42-LINUX-MINWIDTH-01: the Linux header row's boundary follows the row's allocation, but that allocation must
    /// never come back as a size request that feeds the headerbar's minimum width (what the window manager reports as
    /// `WM_NORMAL_HINTS`). This runs the real [`Header::build`] tree — the same widgets and the same [`header_row`]
    /// container `attach` installs — in a real GTK window: grow it far past its content and shrink it again. The
    /// minimum width must stay put, the window must follow the shrink, the separator must stay on the grid's sidebar
    /// divider and the tools must stay against the row's right edge.
    ///
    /// It also pins the vertical/horizontal placement of the row's children against GTK's native window buttons
    /// (A42-LINUX-HEADER-DRIFT-02, the last section): a windowless container's children are allocated in the
    /// window's coordinates, so they must be placed at the row's origin, not at `(0, 0)`.
    ///
    /// Needs a display; the Linux gate runs this under `xvfb-run` (the `MEASURE` lines in the log prove it ran).
    #[cfg(target_os = "linux")]
    #[test]
    fn linux_header_minimum_width_does_not_follow_the_allocation() {
        use gtk::prelude::*;
        if gtk::init().is_err() {
            assert!(
                std::env::var_os("DISPLAY").is_none(),
                "GTK failed with DISPLAY set"
            );
            eprintln!("SKIP Linux header: no display");
            return;
        }
        let settings = gtk::Settings::default().expect("GTK settings");
        let original_layout = settings.gtk_decoration_layout();
        let ctx = gtk::glib::MainContext::default();
        let pump = || {
            for _ in 0..60 {
                while ctx.pending() {
                    ctx.iteration(false);
                }
                std::thread::sleep(std::time::Duration::from_millis(2));
            }
        };
        fn native_buttons(w: &gtk::Widget, out: &mut Vec<gtk::Widget>) {
            if let Some(c) = w.downcast_ref::<gtk::Container>() {
                c.forall(|child| native_buttons(child, out));
            }
            if w.downcast_ref::<gtk::Button>().is_some()
                && w.style_context().has_class("titlebutton")
            {
                out.push(w.clone());
            }
        }
        // These are process-local GTK settings, not changes to the desktop/system setting.
        // Both legal OS arrangements must remain native and at their own edge.
        for decoration in [":minimize,maximize,close", "close,minimize,maximize:"] {
            settings.set_gtk_decoration_layout(Some(decoration));
            let palette = fleetmux_proto::theme::Theme {
                bg: "#1e1e1e".into(), fg: "#ffffff".into(), cursor: "#98989d".into(),
                ansi: vec!["#000000".into(); 16],
            };
            let header = Header::build(Some(&palette));
            let window = gtk::Window::new(gtk::WindowType::Toplevel);
            window.set_titlebar(Some(&header.bar));
            window.set_default_size(1286, 721);
            window.show_all();
            header.set_band(34, 160);
            header.set_cell(8.0);
            pump();
            let minimum = window.preferred_width().0;
            assert!(minimum <= 600, "minimum width is not intrinsic: {minimum}");
            let mut native = Vec::new();
            native_buttons(header.bar.upcast_ref(), &mut native);
            assert_eq!(native.len(), 3, "native window controls missing");
            let native_sizes: Vec<_> = native
                .iter()
                .map(|b| {
                    let a = b.allocation();
                    (a.width(), a.height())
                })
                .collect();
            let tools = header.row.tools().unwrap();
            let heading = header.row.heading().unwrap();
            let divider = header.row.divider().unwrap();
            let separator_color = divider.style_context()
                .style_property_for_state("background-color", gtk::StateFlags::NORMAL)
                .get::<gtk::gdk::RGBA>().expect("separator background RGBA");
            assert!(separator_color.red() > 0.2 && separator_color.alpha() > 0.9,
                "separator must use the visible palette border, not the near-background GTK default: {separator_color:?}");
            assert!(!header.row.has_window());
            assert_eq!(header.row.window(), window.window());
            let mut previous_floor = None;
            for (width, requested, cols, cell) in [
                (1286, 34, 160, 8.0),
                (1700, 60, 210, 8.0),
                (900, 24, 110, 8.0),
                (400, 24, 49, 8.0),
                (1286, 34, 160, 7.0),
                (1286, 34, 160, 7.9),
                (1286, 0, 160, 8.0),
                (1286, 24, 160, 8.0),
                (1286, 60, 600, 2.0),
                (1286, 34, 160, 8.0),
            ] {
                window.resize(width, 721);
                header.set_band(requested, cols);
                header.set_cell(cell);
                pump();
                let floor = header.sidebar_min();
                let shown = if requested == 0 {
                    0
                } else {
                    requested.max(floor)
                };
                header.set_band(shown, cols);
                pump();
                let row = header.row.allocation();
                let row_x = header
                    .row
                    .translate_coordinates(&header.bar, 0, 0)
                    .unwrap()
                    .0;
                let tools_min = tools.preferred_width().0;
                assert_eq!(tools_min, 5 * TOOL_CELL + 8);
                let button_row = header.buttons[0]
                    .parent()
                    .unwrap()
                    .downcast::<gtk::Box>()
                    .unwrap();
                assert_eq!(
                    (
                        button_row.spacing(),
                        button_row.margin_start(),
                        button_row.margin_end()
                    ),
                    (0, 4, 4)
                );
                assert_eq!(
                    header
                        .buttons
                        .iter()
                        .map(|b| b.label().unwrap().to_string())
                        .collect::<Vec<_>>(),
                    ["◫", "+", "↺", "←", "→"]
                );
                let canvas = (f32::from(cols) * cell).round() as i32;
                let want = header_split(canvas, row_x, row.width(), shown, cols, tools_min);
                let t = tools.allocation();
                let h = heading.allocation();
                let d = divider.allocation();
                eprintln!(
                    "MEASURE left-header decoration={decoration} width={width} shown={shown}                           floor={floor} row={row:?} tools={t:?} divider={d:?} heading={h:?}"
                );
                assert_eq!(
                    window.preferred_width().0,
                    minimum,
                    "resize ratcheted the minimum"
                );
                assert!(window.allocated_width() <= width + 32);
                assert_eq!(heading.width_request(), -1);
                assert_eq!(t.x(), row.x(), "tools must start at the left");
                assert_eq!(t.width(), want.max(1));
                assert_eq!(d.x(), row.x() + want);
                assert_eq!(d.width(), 1);
                assert_eq!(h.x(), d.x() + 1, "heading must follow the vertical divider");
                assert_eq!(h.x() + h.width(), row.x() + row.width());
                let row_in_window = header.row.translate_coordinates(&window, 0, 0).unwrap();
                for (child, offset) in [(&tools, 0), (&divider, want), (&heading, want + 1)] {
                    let a = child.allocation();
                    assert_eq!(a.y(), row.y());
                    assert_eq!(a.height(), row.height());
                    assert!(!child.has_window());
                    assert_eq!(child.window(), window.window());
                    assert_eq!(
                        child.translate_coordinates(&window, 0, 0).unwrap(),
                        (row_in_window.0 + offset, row_in_window.1),
                        "ancestor coordinate drift"
                    );
                }
                if shown > 0 {
                    let expected = (i64::from(canvas) * (2 * i64::from(shown) + 1) + i64::from(cols))
                        / (2 * i64::from(cols));
                    assert_eq!(
                        i64::from(row_x + want),
                        expected,
                        "header/body divider drift"
                    );
                } else {
                    assert_eq!(want, tools_min, "collapsed sidebar hid its tools");
                }
                let centre = row.y() + row.height() / 2;
                let mut previous_right = t.x();
                for button in &header.buttons {
                    let a = button.allocation();
                    assert_eq!(a.width(), TOOL_CELL);
                    assert!(a.x() >= previous_right && a.x() + a.width() <= d.x());
                    assert!((a.y() + a.height() / 2 - centre).abs() <= 1);
                    assert!(button.is_mapped() && button.is_sensitive());
                    assert!(button.tooltip_text().is_some());
                    previous_right = a.x() + a.width();
                }
                for (button, size) in native.iter().zip(&native_sizes) {
                    let a = button.allocation();
                    assert_eq!(
                        (a.width(), a.height()),
                        *size,
                        "native control resized with grid font"
                    );
                    assert!((a.y() + a.height() / 2 - centre).abs() <= 1);
                    if decoration.starts_with(':') {
                        assert!(a.x() >= row.x() + row.width(), "native controls moved left");
                    } else {
                        assert!(a.x() + a.width() <= row.x(), "native controls moved right");
                    }
                }
                if width == 1286 && cell == 8.0 {
                    if let Some(previous) = previous_floor {
                        assert_eq!(floor, previous);
                    }
                    previous_floor = Some(floor);
                }
                assert!(header.take_alloc_dirty());
                assert!(!header.take_alloc_dirty());
            }
            // Long text must ellipsize, never ratchet the window minimum (the previous width bug).
            header.set(&"Fleet <title> ".repeat(100), &"status ".repeat(100));
            window.resize(400, 721);
            pump();
            assert_eq!(window.preferred_width().0, minimum);
            assert!(window.allocated_width() <= 432);
            assert_eq!(header.title.ellipsize(), gtk::pango::EllipsizeMode::End);
            assert_eq!(header.status.ellipsize(), gtk::pango::EllipsizeMode::End);
            header.set("Fleet", "ready");
            window.resize(1286, 721);
            pump();
            // Real X clicks catch allocation/hit-window drift that emit_clicked cannot detect.
            let have_xdotool = std::process::Command::new("xdotool")
                .arg("version")
                .output()
                .is_ok_and(|o| o.status.success());
            if std::env::var_os("FLEETMUX_REQUIRE_X_CLICKS").is_some() {
                assert!(have_xdotool, "the native gate requires xdotool");
            }
            for (i, button) in header.buttons.iter().enumerate() {
                let count = std::rc::Rc::new(std::cell::Cell::new(0));
                let observed = count.clone();
                button.connect_clicked(move |_| observed.set(observed.get() + 1));
                if have_xdotool {
                    let a = button.allocation();
                    let (cx, cy) = button
                        .translate_coordinates(&window, a.width() / 2, a.height() / 2)
                        .unwrap();
                    let (wx, wy) = window.position();
                    let result = std::process::Command::new("xdotool")
                        .args([
                            "mousemove",
                            "--sync",
                            &(wx + cx).to_string(),
                            &(wy + cy).to_string(),
                            "click",
                            "1",
                        ])
                        .status()
                        .unwrap();
                    assert!(result.success());
                    pump();
                    assert_eq!(count.get(), 1, "real click missed tool {i}");
                    eprintln!("MEASURE X-click decoration={decoration} tool={i} hits=1");
                } else {
                    eprintln!("SKIP real X-click: xdotool unavailable");
                }
            }
            window.close();
            pump();
        }
        settings.set_gtk_decoration_layout(original_layout.as_deref());
    }
}
