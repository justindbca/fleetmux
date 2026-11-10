//! M2.6 in-memory assets served as `fleetmux://app/<name>` (PROTOCOL.md §15.4): the terminal page
//! (`index.html`, `gui.js`, `gui.css`, `frame.js`) and the vendored xterm.js files (`xterm/<file>`, checked against
//! `MANIFEST.json`). Nothing is loaded from the network; everything is compiled into the binary.

const JS: &str = "text/javascript; charset=utf-8";
const CSS: &str = "text/css; charset=utf-8";
const HTML: &str = "text/html; charset=utf-8";

/// The iframe script (`frame.js`), also injected into every sub-frame by `gui::run`.
pub(crate) const FRAME_JS: &str = include_str!("../assets/app/frame.js");

/// `(mime, bytes)` for `fleetmux://app/<path>` (`path` without the leading `/`); `None` = 404.
pub fn get(path: &str) -> Option<(&'static str, &'static [u8])> {
    Some(match path {
        "index.html" => (HTML, include_bytes!("../assets/app/index.html")),
        "gui.js" => (JS, include_bytes!("../assets/app/gui.js")),
        "gui.css" => (CSS, include_bytes!("../assets/app/gui.css")),
        "frame.js" => (JS, FRAME_JS.as_bytes()),
        "xterm/xterm.js" => (JS, include_bytes!("../assets/xterm/xterm.js")),
        "xterm/xterm.css" => (CSS, include_bytes!("../assets/xterm/xterm.css")),
        "xterm/addon-fit.js" => (JS, include_bytes!("../assets/xterm/addon-fit.js")),
        "xterm/addon-webgl.js" => (JS, include_bytes!("../assets/xterm/addon-webgl.js")),
        "xterm/addon-unicode11.js" => (JS, include_bytes!("../assets/xterm/addon-unicode11.js")),
        _ => return None,
    })
}

/// `crates/web/assets/xterm/MANIFEST.json` (PROTOCOL.md §15.4), written by `assets/xterm/vendor.sh`.
pub const MANIFEST: &str = include_str!("../assets/xterm/MANIFEST.json");

/// `crates/web/assets/xterm/THIRD-PARTY-JS.txt` (PROTOCOL.md §15.4): appended to the `fleetmux-web` release's
/// `THIRD-PARTY-LICENSES`.
pub const THIRD_PARTY_JS: &str = include_str!("../assets/xterm/THIRD-PARTY-JS.txt");
