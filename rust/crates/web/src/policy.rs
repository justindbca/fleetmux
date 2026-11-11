//! Navigation allow-list (PROTOCOL.md §15.2, design.md §3.5.7): what may load in the window. W1 calls [`allow`]
//! from `WebViewBuilder::with_navigation_handler` and hands `External` URLs to `open` / `xdg-open`.
use std::ffi::OsString;
use std::os::unix::ffi::OsStringExt;
use std::path::{Component, Path, PathBuf};

/// What to do with a navigation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    /// Load it in the window: `about:blank`, or a `file://` URL under the run directory or the window's `page/`
    /// directory (query string and fragment allowed).
    Allow,
    /// Do not load it; open it with the system opener (`http://` / `https://`).
    External,
    /// Do not load it at all.
    Deny,
}

/// The decision for `url` in a window whose run directory is `run_dir` and whose pushed-page directory is
/// `page_dir`.
pub fn allow(url: &str, run_dir: &Path, page_dir: &Path) -> Decision {
    let Some((scheme, rest)) = url.split_once(':') else {
        return Decision::Deny;
    };
    match scheme.to_ascii_lowercase().as_str() {
        "about" if rest.eq_ignore_ascii_case("blank") => Decision::Allow,
        "http" | "https" => Decision::External,
        "file" => match file_path(rest) {
            Some(p) if under(&p, run_dir) || under(&p, page_dir) => Decision::Allow,
            _ => Decision::Deny,
        },
        _ => Decision::Deny,
    }
}

/// Is `url` a sub-fleet jump marker (`?fleet-jump=…`, design.md §3.5.8)? Recognised only: until M4 such a URL
/// loads like any other allowed URL.
pub fn is_fleet_jump(url: &str) -> bool {
    let before_fragment = url.split('#').next().unwrap_or("");
    let Some((_, query)) = before_fragment.split_once('?') else {
        return false;
    };
    query
        .split('&')
        .any(|pair| pair.split('=').next() == Some("fleet-jump"))
}

/// The local path of a `file:` URL (the part after `file:`): host empty or `localhost`, query and fragment
/// dropped, percent-decoded, `.` / `..` resolved lexically (the file system is never consulted, so `/tmp` and
/// `/private/tmp` are different roots). `None` = not a usable local path.
fn file_path(rest: &str) -> Option<PathBuf> {
    let after_slashes = rest.strip_prefix("//")?;
    let slash = after_slashes.find('/')?;
    let host = &after_slashes[..slash];
    if !(host.is_empty() || host.eq_ignore_ascii_case("localhost")) {
        return None;
    }
    let path = after_slashes[slash..]
        .split(['?', '#'])
        .next()
        .unwrap_or("");
    let bytes = percent_decode(path)?;
    if bytes.contains(&0) {
        return None;
    }
    Some(normalize(Path::new(&OsString::from_vec(bytes))))
}

/// `%XX` → byte; a malformed escape makes the whole URL unusable.
fn percent_decode(s: &str) -> Option<Vec<u8>> {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            let hex = std::str::from_utf8(b.get(i + 1..i + 3)?).ok()?;
            out.push(u8::from_str_radix(hex, 16).ok()?);
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    Some(out)
}

/// Lexical `.` / `..` resolution of an absolute path (`..` at the root stays at the root).
fn normalize(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// `p` is `root` or below it, compared component by component (`/run-other` is not under `/run`).
fn under(p: &Path, root: &Path) -> bool {
    root.is_absolute() && p.starts_with(normalize(root))
}

/// M2.6 GUI mode (PROTOCOL.md §15.4): `fleetmux://app/index.html`, `about:blank` and `fleetmux://page/<path>`
/// whose path is under `run_dir` (when set) or `page_dir` → `Allow`; `http(s)` → `External`; `file:` and
/// everything else → `Deny`.
pub fn allow_gui(url: &str, run_dir: Option<&Path>, page_dir: &Path) -> Decision {
    if url == APP_INDEX || url.eq_ignore_ascii_case("about:blank") {
        return Decision::Allow;
    }
    if url.starts_with(PAGE_PREFIX) {
        return match page_path(url, run_dir, page_dir) {
            Some(_) => Decision::Allow,
            None => Decision::Deny,
        };
    }
    match url.split_once(':').map(|(s, _)| s.to_ascii_lowercase()) {
        Some(s) if s == "http" || s == "https" => Decision::External,
        _ => Decision::Deny,
    }
}

/// The terminal page, the only `fleetmux://app/` URL that navigates.
pub const APP_INDEX: &str = "fleetmux://app/index.html";

/// The scheme and host of embedded history pages.
const PAGE_PREFIX: &str = "fleetmux://page/";

/// `file:///a/b?q#h` → `fleetmux://page/a/b?q#h` (path, query and fragment kept byte for byte, percent escapes
/// included); `None` for anything that is not a local `file:` URL (host empty or `localhost`).
pub fn to_page_url(file_url: &str) -> Option<String> {
    let (scheme, rest) = file_url.split_once(':')?;
    if !scheme.eq_ignore_ascii_case("file") {
        return None;
    }
    let after_slashes = rest.strip_prefix("//")?;
    let slash = after_slashes.find('/')?;
    let host = &after_slashes[..slash];
    if !(host.is_empty() || host.eq_ignore_ascii_case("localhost")) {
        return None;
    }
    Some(format!("fleetmux://page{}", &after_slashes[slash..]))
}

/// The inverse of [`to_page_url`]: `fleetmux://page/a/b?q#h` → `file:///a/b?q#h`; `None` for anything else.
pub fn to_file_url(page_url: &str) -> Option<String> {
    let rest = page_url.strip_prefix(PAGE_PREFIX)?;
    Some(format!("file:///{rest}"))
}

/// The file a `fleetmux://page/…` request may be served from: the percent-decoded, lexically normalised path
/// (query and fragment dropped) when it is under `run_dir` (when set) or `page_dir`; else `None` (404).
pub fn page_path(page_url: &str, run_dir: Option<&Path>, page_dir: &Path) -> Option<PathBuf> {
    let rest = page_url.strip_prefix(PAGE_PREFIX)?;
    let p = file_path(&format!("///{rest}"))?;
    let ok = run_dir.is_some_and(|r| under(&p, r)) || under(&p, page_dir);
    ok.then_some(p)
}
