//! The shared view state (PROTOCOL.md §10, §14): `layout.json` persistence and the prefix-key table.
//!
//! B16 views-multi owns this file. The M0 subset of [`on_prefix_key`] (`1`–`9`, `Tab` / `BTab`, the four
//! arrows) came with B7; B16 adds `z`, `[`, `<` `>` `+` `-` `=` and `o`; S1 adds `s` (toggle the fleet sidebar,
//! only while kv `sidebar` is set; the hidden flag is shared by every viewer and is not saved); S1b adds `(` `)` (the
//! sidebar's previous / next step, the same as clicking `←` / `→`) and `X` (the root card's `×`: open the close
//! confirmation box, plan §11.2). `d` and the
//! doubled prefix never reach this function (`view::on_input` handles them). Every layout change made here is shared by all viewers (the
//! layout lives in `State`) and rewrites `layout.json` (§8.3); [`load_ratios`] / [`save_layout`] are the one
//! implementation of that file that the mouse path (`view.rs`) and startup / handlers (ctl) also use.
use std::fs::{self, OpenOptions};
use std::io::{self, Write as _};
use std::os::unix::fs::OpenOptionsExt as _;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use fleetmux_layout::{Group, Layout};
use fleetmux_proto::view::Frame;

use super::State;
use super::scroll::PageMode;
use super::view::{self, ViewerId};

/// Ratio step of `<` `>` `+` `-` (PROTOCOL.md §10).
const RATIO_STEP: u8 = 5;

/// Read only the ratios `(h_ratio, v_ratio)` from `layout.json` (groups are not restored: panes do not survive
/// a server restart). Missing file → `None`; unreadable / malformed / out-of-range file → `None` and a `WARN`
/// line in `server.log`.
pub fn load_ratios(path: &Path) -> Option<(u8, u8)> {
    let text = match fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return None,
        Err(e) => {
            warn(&format!("{}: {e}; using default ratios", path.display()));
            return None;
        }
    };
    match Layout::from_json(&text) {
        Ok(old) => Some(old.ratios()),
        Err(e) => {
            warn(&format!("{}: {e}; using default ratios", path.display()));
            None
        }
    }
}

/// Write `layout.json` atomically (temp file + rename, no `.tmp` left behind).
pub fn save_layout(path: &Path, layout: &Layout) -> io::Result<()> {
    let name = path.file_name().ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidInput, "layout path has no file name")
    })?;
    let tmp = path.with_file_name(format!(
        ".{}.{}.tmp",
        name.to_string_lossy(),
        std::process::id()
    ));
    let write = || -> io::Result<()> {
        let mut f = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&tmp)?;
        f.write_all(layout.to_json().as_bytes())?;
        f.sync_all()?;
        fs::rename(&tmp, path)
    };
    write().inspect_err(|_| {
        let _ = fs::remove_file(&tmp);
    })
}

/// What an M0 prefix key does to the layout; `None` = not an M0 key.
fn m0_key(layout: &mut Layout, key: &[u8]) -> Option<()> {
    match key {
        [d @ b'1'..=b'9'] => layout.select_nth((d - b'1') as usize),
        b"\t" => layout.next_tab(),
        b"\x1b[Z" => layout.prev_tab(),
        // Left / Right: between the left column and `workers` (the left column is entered at the top).
        b"\x1b[D" | b"\x1bOD" => {
            if layout.focus() == Group::Workers {
                layout.focus_group(Group::Supervisor);
            }
        }
        b"\x1b[C" | b"\x1bOC" => layout.focus_group(Group::Workers),
        // Up / Down: between `supervisor` and `lower`.
        b"\x1b[A" | b"\x1bOA" => layout.focus_group(Group::Supervisor),
        b"\x1b[B" | b"\x1bOB" => layout.focus_group(Group::Lower),
        _ => return None,
    }
    Some(())
}

/// What an M1 layout key (`z < > + - =`) does to the layout; `None` = not one of them.
fn m1_key(layout: &mut Layout, key: &[u8]) -> Option<()> {
    let (h, v) = layout.ratios();
    match key {
        b"z" => {
            // The focused pane is the zoomed one while zoomed, so `z` again restores the normal view.
            if let Some(id) = focused_pane(layout) {
                layout.toggle_zoom(id);
            }
        }
        b"<" => layout.set_ratios(h.saturating_sub(RATIO_STEP), v),
        b">" => layout.set_ratios(h.saturating_add(RATIO_STEP), v),
        b"+" => layout.set_ratios(h, v.saturating_add(RATIO_STEP)),
        b"-" => layout.set_ratios(h, v.saturating_sub(RATIO_STEP)),
        b"=" => layout.reset_ratios(),
        _ => return None,
    }
    Some(())
}

/// The pane prefix keys act on: the zoomed pane while zoomed, else the focused group's selected tab.
fn focused_pane(layout: &Layout) -> Option<String> {
    layout.zoom().or(layout.selected(layout.focus())).cloned()
}

/// `0x83` payload for `o`: kv `history_url` without `file://`, empty when unset. M4 (design m4 §4.2 kv row): the
/// current page's `history_url`.
fn history_path(state: &State) -> String {
    let url = super::handlers::current_page_kv(state, "history_url").unwrap_or_default();
    url.strip_prefix("file://").unwrap_or(&url).to_string()
}

/// Apply the key that followed the prefix (PROTOCOL.md §10 key table: `1`–`9`, `Tab` / `BTab`, arrows, `z`,
/// `[`, `<` `>` `+` `-` `=`, `o`, `s`, `(`, `)`, `X`, `r`, `c`, `d`, prefix twice). Returns frames for `viewer` (e.g.
/// `0x83` for `o`).
pub fn on_prefix_key(state: &mut State, viewer: ViewerId, key: &[u8]) -> Vec<Frame> {
    let _ = viewer;
    match key {
        b"o" => return vec![Frame::Open(history_path(state))],
        // S1b (plan §11.2): `^b (` / `^b )` are the `←` / `→` buttons, the same function the clicks call.
        b"(" => {
            view::side_step(state, false);
            return Vec::new();
        }
        b")" => {
            view::side_step(state, true);
            return Vec::new();
        }
        // S1b (plan §11.2): `^b X` is the root card's `×` — open the close confirmation box (the same function the
        // click calls; it does nothing while the card has no `×`).
        b"X" => {
            view::open_confirm(state);
            return Vec::new();
        }
        // S2b (plan §13.6-2): `^b r` restores the newest closed fleet — the same function as the topbar's `↺`.
        b"r" => {
            view::reopen(state);
            return Vec::new();
        }
        // S1c (plan §14.2): `^b c` opens a bash page, the same function the toolbar's `+` calls.
        b"c" => {
            view::new_bash(state);
            return Vec::new();
        }
        // S1: toggle the fleet sidebar; without kv `sidebar` there is no sidebar and nothing happens.
        b"s" => {
            if state.side.data.is_some() {
                state.side.hidden = !state.side.hidden;
                view::sync_sidebar(state);
            }
            return Vec::new();
        }
        b"[" => {
            if let Some(pane) = focused_pane(&state.layout) {
                state.page = Some(PageMode {
                    pane,
                    query: String::new(),
                    typing: false,
                });
                state.viewers.mark_dirty();
            }
            return Vec::new();
        }
        _ => {}
    }
    let before = state.layout.clone();
    let hit = m0_key(&mut state.layout, key).or_else(|| m1_key(&mut state.layout, key));
    if hit.is_some() {
        view::layout_changed(state);
        if state.layout != before {
            persist(state);
        }
    }
    // Unknown keys: nothing happens.
    Vec::new()
}

/// Rewrite `runtime/layout.json` after a prefix-key change; a failure is a `WARN`, never fatal. M4 (design m4
/// R11): the file holds the root page whichever page is current, with the tree's shared ratios brought over first.
fn persist(state: &mut State) {
    super::handlers::spread_shared(state);
    let path = state.runtime.join("layout.json");
    if let Err(e) = save_layout(&path, super::handlers::root_layout(state)) {
        warn(&format!("{}: {e}", path.display()));
    }
}

/// One `WARN` line for `server.log` (PROTOCOL.md §8.5 format: `<RFC 3339 UTC> WARN <message>` on stderr).
fn warn(msg: &str) {
    eprintln!("{} WARN {msg}", rfc3339(SystemTime::now()));
}

/// `YYYY-MM-DDTHH:MM:SSZ` (civil date from days since 1970-01-01, H. Hinnant's algorithm).
fn rfc3339(t: SystemTime) -> String {
    let secs = t
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let (days, rem) = (secs / 86_400, secs % 86_400);
    let z = days as i64 + 719_468;
    let (era, doe) = (z.div_euclid(146_097), z.rem_euclid(146_097));
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        rem / 3600,
        rem / 60 % 60,
        rem % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use fleetmux_layout::PaneType;

    fn tmpdir(tag: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("fm-views-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn save_then_load_keeps_only_ratios_and_leaves_no_tmp() {
        let dir = tmpdir("save");
        let path = dir.join("layout.json");
        let mut l = Layout::new();
        l.insert("p1".into(), PaneType::Worker);
        l.set_ratios(65, 40);
        save_layout(&path, &l).unwrap();
        save_layout(&path, &l).unwrap(); // overwrite in place
        assert_eq!(load_ratios(&path), Some((65, 40)));
        let names: Vec<_> = fs::read_dir(&dir)
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert_eq!(
            names,
            vec![std::ffi::OsString::from("layout.json")],
            "leftovers: {names:?}"
        );
        use std::os::unix::fs::PermissionsExt as _;
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn load_ratios_missing_or_bad_is_none() {
        let dir = tmpdir("load");
        assert_eq!(load_ratios(&dir.join("absent.json")), None);
        let bad = dir.join("bad.json");
        fs::write(&bad, "{not json").unwrap();
        assert_eq!(load_ratios(&bad), None);
        let mut l = Layout::new();
        l.set_ratios(50, 60);
        let out = dir.join("range.json");
        fs::write(
            &out,
            l.to_json().replace("\"h_ratio\":50", "\"h_ratio\":95"),
        )
        .unwrap();
        assert_eq!(
            load_ratios(&out),
            None,
            "out-of-range ratio must be ignored"
        );
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn m1_keys_zoom_and_ratios() {
        let mut l = Layout::new();
        l.insert("w1".into(), PaneType::Worker);
        l.select("w1".into());
        l.focus_group(Group::Workers);
        m1_key(&mut l, b"z").unwrap();
        assert_eq!(l.zoom().map(String::as_str), Some("w1"));
        m1_key(&mut l, b"z").unwrap();
        assert_eq!(l.zoom(), None);
        for k in [b">", b"<", b"<"] {
            m1_key(&mut l, k).unwrap();
        }
        m1_key(&mut l, b"+").unwrap();
        assert_eq!(l.ratios(), (45, 65));
        for _ in 0..20 {
            m1_key(&mut l, b"-").unwrap();
        }
        assert_eq!(l.ratios(), (45, 10), "v clamps at 10");
        m1_key(&mut l, b"=").unwrap();
        assert_eq!(l.ratios(), (50, 60));
        assert!(m1_key(&mut l, b"x").is_none());
    }

    #[test]
    fn rfc3339_formats_utc() {
        assert_eq!(
            rfc3339(UNIX_EPOCH + std::time::Duration::from_secs(951_782_400)),
            "2000-02-29T00:00:00Z"
        );
    }
}
