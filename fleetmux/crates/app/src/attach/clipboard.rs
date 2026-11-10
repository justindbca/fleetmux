//! Copy and open helpers (PROTOCOL.md §10 `o`, §12): the OSC 52 sequence sent to viewers, the host clipboard
//! tool (`pbcopy` / `wl-copy` / `xclip`), and opening a path with the system opener.
//!
//! B15 (copy-scroll). Child processes get `/dev/null` for stdin (openers) / stdout / stderr: `xclip` and
//! `wl-copy` fork a background process that keeps serving the selection, and a pipe left open to it would
//! hold its reader forever; the attach client's terminal is in raw mode and must not get stray output.
use std::io::{self, Write as _};
use std::path::Path;
use std::process::{Command, Stdio};

/// `ESC ] 52 ; c ; <base64(text)> BEL`.
pub fn osc52(text: &str) -> Vec<u8> {
    let mut seq = b"\x1b]52;c;".to_vec();
    seq.extend_from_slice(fleetmux_proto::b64::encode(text.as_bytes()).as_bytes());
    seq.push(0x07);
    seq
}

/// The clipboard tool for this host: macOS `pbcopy`; else `wl-copy` when `WAYLAND_DISPLAY` is set; else
/// `xclip` when `DISPLAY` is set; else `None`.
pub fn local_tool() -> Option<&'static str> {
    let set = |var: &str| std::env::var_os(var).is_some_and(|v| !v.is_empty());
    if cfg!(target_os = "macos") {
        Some("pbcopy")
    } else if set("WAYLAND_DISPLAY") {
        Some("wl-copy")
    } else if set("DISPLAY") {
        Some("xclip")
    } else {
        None
    }
}

/// Pipe `text` into [`local_tool`]; returns the tool name used. No tool → `ErrorKind::NotFound`.
pub fn local_copy(text: &str) -> io::Result<&'static str> {
    let tool = local_tool().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::NotFound,
            "no clipboard tool (pbcopy / wl-copy / xclip)",
        )
    })?;
    let args: &[&str] = if tool == "xclip" {
        &["-selection", "clipboard"]
    } else {
        &[]
    };
    let mut child = Command::new(tool)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| io::Error::new(e.kind(), format!("{tool}: {e}")))?;
    // Close stdin after writing (drop) so the tool sees EOF, also when the write fails.
    let wrote = child.stdin.take().map(|mut w| w.write_all(text.as_bytes()));
    let status = child
        .wait()
        .map_err(|e| io::Error::new(e.kind(), format!("{tool}: {e}")))?;
    if let Some(Err(e)) = wrote {
        return Err(io::Error::new(
            e.kind(),
            format!("{tool}: writing the text: {e}"),
        ));
    }
    if !status.success() {
        return Err(io::Error::other(format!("{tool} {status}")));
    }
    Ok(tool)
}

/// The server's host-side copy (PROTOCOL.md §12) as one call: run [`local_copy`] and say what to log. `None` =
/// copied; `Some(("INFO", ..))` = no tool on this host, skipped; `Some(("WARN", ..))` = the tool failed.
/// Copying never fails the selection: the caller only logs the line. Blocks until the tool exits; see
/// [`host_copy_detached`] for the event loop.
pub fn host_copy(text: &str) -> Option<(&'static str, String)> {
    match local_copy(text) {
        Ok(_) => None,
        Err(e) if e.kind() == io::ErrorKind::NotFound && local_tool().is_none() => {
            Some(("INFO", format!("copy: {e}; host clipboard skipped")))
        }
        Err(e) => Some(("WARN", format!("copy: host clipboard failed: {e}"))),
    }
}

/// [`host_copy`] on its own thread (the server's event loop never waits on a clipboard tool); the resulting line,
/// if any, goes to `log(level, message)` (the server's `server.log` writer).
pub fn host_copy_detached(text: String, log: fn(&str, &str)) {
    let spawned = std::thread::Builder::new()
        .name("fleetmux-copy".into())
        .spawn(move || {
            if let Some((level, msg)) = host_copy(&text) {
                log(level, &msg);
            }
        });
    if let Err(e) = spawned {
        log(
            "WARN",
            &format!("copy: cannot start the clipboard thread: {e}"),
        );
    }
}

/// Open `path` with `open` (macOS) / `xdg-open` (others). `Err` carries the path to show instead when the
/// file is not on this machine or the opener fails.
///
/// An absolute path that does not exist here is not handed to the opener (design.md §3.3: the viewer may be on
/// another host than the server); anything else (a relative path, a URL) is. Off macOS without `DISPLAY` /
/// `WAYLAND_DISPLAY` no opener runs at all (`attach::web::has_display`): the path is shown instead. A path
/// starting with `-` is passed as `./-…` so the opener cannot take it for an option.
pub fn open_local(path: &str) -> Result<(), String> {
    open_with(path, std::env::consts::OS, |k| std::env::var(k).ok(), None)
}

/// [`open_local`] for host `os` with environment `var`; `opener` replaces the platform opener (tests only).
fn open_with(
    path: &str,
    os: &str,
    var: impl Fn(&str) -> Option<String>,
    opener: Option<&str>,
) -> Result<(), String> {
    if path.is_empty() || (path.starts_with('/') && !Path::new(path).exists()) {
        return Err(path.to_string());
    }
    // fix-m25-3: without a graphical session (Linux and the like: no `DISPLAY` / `WAYLAND_DISPLAY`, e.g.
    // `ssh -T`) `xdg-open` cannot show anything here, and may start a text-mode handler on the raw terminal or
    // report success without effect; skip it and show the path (design.md §3.5.6 fallback 1).
    if !super::web::has_display(os, &var) {
        return Err(path.to_string());
    }
    let opener = opener.unwrap_or(if os == "macos" { "open" } else { "xdg-open" });
    let arg = if path.starts_with('-') {
        format!("./{path}")
    } else {
        path.to_string()
    };
    match Command::new(opener)
        .arg(arg)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
    {
        Ok(s) if s.success() => Ok(()),
        _ => Err(path.to_string()),
    }
}

/// What the attach client shows for one `0x83` payload (PROTOCOL.md §6.3), after trying to open it: `None` =
/// opened; else the one-line notice — `no history url` for an empty payload, otherwise the path.
pub fn open_notice(path: &str) -> Option<String> {
    notice(path, open_local)
}

fn notice(path: &str, open: impl FnOnce(&str) -> Result<(), String>) -> Option<String> {
    if path.is_empty() {
        return Some(String::from("no history url"));
    }
    open(path).err().map(|p| format!("cannot open here: {p}"))
}

/// [`open_notice`] for the steps after the web window (M2.5, PROTOCOL.md §6.3 order: window → opener → path):
/// `why` = the reason the window step was skipped or failed, shown on this client's status line only
/// (design.md §3.5.6) — after the opener's notice when there is one, alone when the opener worked.
pub fn open_notice_after_window(path: &str, why: Option<&str>) -> Option<String> {
    match (open_notice(path), why) {
        (Some(note), Some(why)) => Some(format!("{note} ({why})")),
        (Some(note), None) => Some(note),
        (None, Some(why)) => Some(why.to_string()),
        (None, None) => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn notice_after_window_adds_the_reason() {
        let missing = "/fleetmux-w3-not-on-this-host/history.html";
        assert_eq!(
            open_notice_after_window("", None),
            Some("no history url".to_string())
        );
        assert_eq!(
            open_notice_after_window(missing, Some("web: fleetmux-web not found")),
            Some(format!(
                "cannot open here: {missing} (web: fleetmux-web not found)"
            ))
        );
        assert_eq!(
            open_notice_after_window(missing, None),
            Some(format!("cannot open here: {missing}"))
        );
    }

    #[test]
    fn osc52_is_base64_between_c_and_bel() {
        assert_eq!(osc52("hi"), b"\x1b]52;c;aGk=\x07");
        assert_eq!(osc52(""), b"\x1b]52;c;\x07");
        assert_eq!(
            osc52("SEL-ONE\nSEL-TWO"),
            b"\x1b]52;c;U0VMLU9ORQpTRUwtVFdP\x07"
        );
    }

    #[test]
    fn local_tool_follows_platform_then_display_vars() {
        // Read-only on the process environment: tests run in parallel, so no set_var here.
        let tool = local_tool();
        if cfg!(target_os = "macos") {
            assert_eq!(tool, Some("pbcopy"));
        } else {
            let set = |v: &str| std::env::var_os(v).is_some_and(|x| !x.is_empty());
            let want = if set("WAYLAND_DISPLAY") {
                Some("wl-copy")
            } else if set("DISPLAY") {
                Some("xclip")
            } else {
                None
            };
            assert_eq!(tool, want);
        }
    }

    #[test]
    fn open_missing_or_empty_path_returns_it_without_running_an_opener() {
        let missing = "/fleetmux-b15-not-on-this-host/history.html";
        assert_eq!(open_local(missing), Err(missing.to_string()));
        assert_eq!(open_local(""), Err(String::new()));
        assert_eq!(open_notice(""), Some("no history url".to_string()));
        assert_eq!(
            open_notice(missing),
            Some(format!("cannot open here: {missing}"))
        );
    }

    // ---------------------------------------------------------------- fix-m25-3: no display, no opener

    /// A fake opener that records its argument in `<dir>/ran` and exits `code`; and an existing page to open.
    fn fake_opener(tag: &str, code: i32) -> (std::path::PathBuf, String, String) {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("fm-fix-m25-3-{tag}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let ran = dir.join("ran");
        let _ = std::fs::remove_file(&ran);
        let bin = dir.join("xdg-open");
        std::fs::write(
            &bin,
            format!(
                "#!/bin/sh\necho \"$1\" > '{}'\nexit {code}\n",
                ran.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
        let page = dir.join("history.html");
        std::fs::write(&page, "<html></html>").unwrap();
        (
            dir,
            bin.to_str().unwrap().to_owned(),
            page.to_str().unwrap().to_owned(),
        )
    }

    fn vars(set: &'static [(&'static str, &'static str)]) -> impl Fn(&str) -> Option<String> {
        move |k: &str| {
            set.iter()
                .find(|(n, _)| *n == k)
                .map(|(_, v)| v.to_string())
        }
    }

    #[test]
    fn open_without_display_never_runs_the_opener_and_shows_the_path() {
        let (dir, bin, page) = fake_opener("nodisplay", 0);
        for env in [
            &[][..],
            &[("DISPLAY", "")][..],
            &[("DISPLAY", ""), ("WAYLAND_DISPLAY", "")][..],
        ] {
            let env: &'static [(&'static str, &'static str)] = env;
            assert_eq!(
                open_with(&page, "linux", vars(env), Some(&bin)),
                Err(page.clone()),
                "{env:?}"
            );
            assert_eq!(
                notice(&page, |p| open_with(p, "linux", vars(env), Some(&bin))),
                Some(format!("cannot open here: {page}")),
                "{env:?}"
            );
            assert!(
                !dir.join("ran").exists(),
                "the opener ran without a display ({env:?}), though it exits 0"
            );
        }
        // Relative paths and URLs are handed to an opener only when there is a display, too.
        assert_eq!(
            open_with("history.html", "linux", vars(&[]), Some(&bin)),
            Err("history.html".into())
        );
        assert!(!dir.join("ran").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn open_with_display_or_on_macos_still_runs_the_opener() {
        let (dir, bin, page) = fake_opener("display", 0);
        let ran = dir.join("ran");
        for (os, env) in [
            ("linux", &[("DISPLAY", ":0")][..]),
            ("linux", &[("WAYLAND_DISPLAY", "wayland-0")][..]),
            ("macos", &[][..]),
        ] {
            let env: &'static [(&'static str, &'static str)] = env;
            let _ = std::fs::remove_file(&ran);
            assert_eq!(
                open_with(&page, os, vars(env), Some(&bin)),
                Ok(()),
                "{os} {env:?}"
            );
            assert_eq!(
                notice(&page, |p| open_with(p, os, vars(env), Some(&bin))),
                None
            );
            assert_eq!(
                std::fs::read_to_string(&ran).unwrap().trim_end(),
                page,
                "{os} {env:?}"
            );
        }
        // With a display, an opener that fails still falls back to showing the path (unchanged).
        let (dir2, failing, page2) = fake_opener("display-fails", 1);
        assert_eq!(
            open_with(&page2, "linux", vars(&[("DISPLAY", ":0")]), Some(&failing)),
            Err(page2.clone())
        );
        assert!(dir2.join("ran").exists(), "the opener ran");
        // A path that is not on this host is still never handed to the opener.
        let _ = std::fs::remove_file(&ran);
        let missing = "/fleetmux-fix-m25-3-not-on-this-host/history.html";
        assert_eq!(
            open_with(missing, "macos", vars(&[]), Some(&bin)),
            Err(missing.into())
        );
        assert!(!ran.exists());
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::remove_dir_all(&dir2);
    }
}
