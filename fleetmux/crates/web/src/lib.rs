//! fleetmux-web: the native web window (design v1.1 §3.5, PROTOCOL.md §15). One window per process: tao event loop
//! on the main thread + wry WebView (macOS WKWebView, Linux WebKitGTK). Public signatures from W0 (ctl-1,
//! brief-m25 §3.1); W1 (web-1) implements them.
//!
//! - [`window`]: command line, event loop, WebView, lifeline (stdin EOF), data directory.
//! - [`sock`]: `web.sock` — directory / socket permissions, peer uid, the JSON-line commands, the accept thread.
//! - [`policy`]: the navigation allow-list.
//! - [`quiet`]: `FLEETMUX_WEB_QUIET=1`, a window that never shows or takes the focus (test runs).
//! - [`gui`] / [`assets`]: M2.6 GUI mode (`--gui`, design v1.2 §3.6, PROTOCOL.md §15.4; brief-m26 W0 signatures, W1
//!   bodies).
pub mod assets;
pub mod gui;
pub mod policy;
pub mod quiet;
pub mod sock;
pub mod window;
