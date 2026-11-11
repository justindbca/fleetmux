//! The fleet sidebar, pure part (plan §2.2 / §2.3 / §4.2; §11.1 for the S1b toolbar): parsing the kv `sidebar`
//! value the fleet daemon pushes, the title fit, the width the layout is asked for, the hit test and the character
//! drawing. No state, no I/O — `server/view.rs` keeps the parsed [`Side`] in `State` and calls these.
//!
//! kv `sidebar` (written by `fleetmux/fleetmux_sidebar.py`, plan §4.5) is a JSON object
//! `{"v":1,"current":"<id>","fleets":[{"id","title","state","line1","line2"},…],"close":{"title","body"}}`:
//! 1..=[`MAX_FLEETS`] fleets, every value a string of at most 200 characters, `current` one of the ids, and the
//! optional `close` object (two strings of at most 400 characters each) the close confirmation box shows. M4: a card
//! may carry a sixth key `page` (a string, the fleet page it shows; [`Entry::page`]). Anything
//! else is [`parse`]'s `Err` (one English sentence; the server logs it and draws no sidebar), unknown keys are
//! ignored.
//!
//! A fleet the sidebar closed keeps its card with `state: "closed"` (plan §13.3): the dot stays grey, `line1` says
//! `已关闭 · 可恢复` and its first row gets the restore `↺` in place of the `×`, which is [`SideHit::Restore`]. The
//! command that brings it back comes from kv `restorable` (plan §13.6 / §18.3),
//! `{"v":1,"runs":[{"id","title","closed_at","argv","cwd","env"},…]}` with at most [`MAX_RESTORABLE`] runs:
//! [`parse_restorable`] parses it, [`Side::restorable`] keeps it and [`Side::resuming`] says a restore pane is running.
//!
//! The close confirmation box (plan §2.5) is pure too: [`modal_text`] picks the daemon's texts or the default ones,
//! [`wrap`] breaks the body to the box's inner width, [`modal_rect`] / [`draw_modal`] / [`modal_hit`] are its
//! geometry, its drawing and its hit test.
//!
//! Colours are ANSI-16 only (plan §2): `Blue` for the selected card's bar, `LightGreen` / `Gray` / `Yellow` for the
//! state dot, `White` for the toolbar glyphs, the titles and a selected card's `×`, `Gray` for the subtitle lines,
//! an unselected card's `×`, `DarkGray` for the disabled arrows and the hidden rail's toggle.
//! Geometry (plan §20.2, row numbers 0-based): row 0 is the topbar, two segments split at the sidebar's drag line.
//! Its left part is `max(sb, inset + `[`TOOLBAR_W`])` columns wide and holds the toolbar `◫  +  ↺  ←  →` at
//! `k+1 / k+4 / k+7 / k+10 / k+13` (`k` = [`Side::inset`], where the gui's traffic lights sit); while the sidebar is
//! drawn the topbar puts `│` at column `sb` (the divider, right above the groups' left border) and the current fleet's
//! title + line1 from `sb + 2`; hidden (`sb == 0`) it draws only the toolbar, no divider. Cards start at `top` —
//! the layout's header band height, so the first card's first row is the workspace's topmost tab row on both
//! platforms (A26 rev7) — every 4 rows (3 rows + 1 blank) with the card's `×` on its first row
//! at column `sb − 3`, column `sb − 1` always blank as the gap to the groups' left border, the last 5 rows kept
//! free. `sb` is the sidebar's actual width (`Layout::sidebar_cols`, plan §16.2: the user drags it between 24 and
//! 60), so every position below is relative to it; [`SIDEBAR_W`] is only the default width. Hidden, [`width`] is 0
//! (the panes fill the screen from column 0) and [`topbar_w`] keeps the toolbar's own `inset + `[`TOOLBAR_W`]
//! columns for row 0 (plan §20.2: the buttons do not move when the sidebar collapses).

use std::collections::BTreeMap;

use fleetmux_layout::Rect as LRect;
use fleetmux_proto::PaneId;
use fleetmux_term::cell_width;
use ratatui::buffer::Buffer;
use ratatui::style::{Color, Modifier, Style};

use crate::render::draw::text_cols;

/// Columns the sidebar takes on the left while shown (plan §2.1).
pub const SIDEBAR_W: u16 = 34;
/// Columns the S1b hidden sidebar kept as a rail. No longer used for drawing or for [`width`] (plan §17.4-6: the
/// hidden sidebar is `inset + `[`TOOLBAR_W`] columns of toolbar now); kept for the plan's cross-references.
pub const RAIL_W: u16 = 1;
/// Columns the topbar's left part needs: `◫` at `k+1`, `+` at `k+4`, `↺` at `k+7`, `←` at `k+10`, `→` at `k+13`
/// and the blank column `k+14`, where `k` is [`Side::inset`] (plan §20.2). [`topbar_w`] is this wide plus `k`; the
/// hidden sidebar itself is 0 columns (plan §20.2).
pub const TOOLBAR_W: u16 = 15;
/// Most fleets one kv `sidebar` value may list (S3 shows the whole tree, flattened).
pub const MAX_FLEETS: usize = 16;
/// Most runs one kv `restorable` value may list (plan §18.3: at most [`MAX_FLEETS`] cards, so at most as many runs).
pub const MAX_RESTORABLE: usize = MAX_FLEETS;

/// One card: the fleet's id (the value `current` names), its title and the two grey lines under it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub id: String,
    pub title: String,
    pub state: String,
    pub line1: String,
    pub line2: String,
    /// M4 (design m4 R8): the optional sixth key, the fleet page this card shows (`""` = the root page). A click
    /// selects that page; a child card with a page has no `×` (R10). Absent = the S1 / S2 card as before.
    pub page: Option<String>,
}

/// The texts of the close confirmation box, from kv `sidebar`'s optional `close` (plan §11.1): the daemon writes
/// them (they name the workers and the child fleets it will stop). A missing or empty string falls back to the
/// default text ([`modal_text`]).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Close {
    pub title: String,
    pub body: String,
}

/// The parsed kv `sidebar` value: the fleet whose card is highlighted, the cards in display order and the close
/// box's texts when the daemon sent them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Data {
    pub current: String,
    pub fleets: Vec<Entry>,
    pub close: Option<Close>,
}

/// One run kv `restorable` can bring back (plan §18.3): the fleet's id (the card's `id`), its title, when it was
/// closed and the command that resumes it — the argv, the working directory and the environment the resume pane
/// spawns with.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestoreRun {
    pub id: String,
    pub title: String,
    pub closed_at: i64,
    pub argv: Vec<String>,
    pub cwd: String,
    pub env: BTreeMap<String, String>,
}

/// The parsed kv `restorable` value (plan §13.6-2 / §18.3): the closed fleets that can be restored, newest first
/// (`runs[0]` is what the topbar's `↺` and `^b r` bring back). Empty `runs` is legal — the key is set, but there is
/// nothing to restore, so the buttons only say so.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Restorable {
    pub runs: Vec<RestoreRun>,
}

/// What the `stop_request` in kv says (plan §11.1 / §11.3): the close request this server wrote, or the daemon's
/// answer to it. `None` = no request, so the card keeps its `×`; anything else hides the `×` and stays on the
/// status line until the state changes.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub enum Stop {
    #[default]
    None,
    /// `y` in the box wrote `{"v":1,"state":"requested","at":…}`; the daemon has not answered yet.
    Requested,
    /// The daemon took the request (`state: accepted`): the fleet is going down.
    Accepted,
    /// The daemon refused it (`state: refused`), with its reason.
    Refused(String),
}

/// The sidebar's runtime state (lives in `State`, shared by every viewer): the last good [`Data`], whether the
/// user hid it, the width the user dragged it to, the one-shot toolbar notice, whether the close box is open and
/// the `stop_request` state. No `data` = no kv `sidebar` = no sidebar at all (the prefix key and clicks do
/// nothing).
#[derive(Debug, Clone)]
pub struct Side {
    pub data: Option<Data>,
    /// The sidebar's bash tabs (plan §14.3 / §14.5-3; user 2026-11-09: tabs of the root page's worker area): one card
    /// after the fleet cards per pane, in creation order. `server/view.rs` fills it from the pane list
    /// (`type == "shell"`); the sidebar never writes it.
    pub bash: Vec<PaneId>,
    /// The bash tab the root page shows, when it is one of [`Side::bash`] (plan §14.2: the current view follows the
    /// layout, nothing else is stored): the zoomed pane, else the focused group's selected tab. `server/view.rs`
    /// mirrors it here; that card is then drawn selected and the fleet card is not.
    pub bash_sel: Option<PaneId>,
    /// The bash page a close box is open for (plan §14.2: `×` on a bash card asks first when a program runs in its
    /// foreground). Set with [`Side::confirm`]; while it names a page of [`Side::bash`] the box speaks about that page
    /// instead of the fleet, and `y` closes it.
    pub close_bash: Option<PaneId>,
    pub hidden: bool,
    /// The width the user dragged the edge to (plan §16.2, `SIDEBAR_MIN_W`..=`SIDEBAR_MAX_W`): the **requested**
    /// width, kept in kv. [`width`] returns the **effective** width (at least this, [`shown_min`] and the window's
    /// [`Side::sidebar_min`]); the layout, render, hit-test and drag all use that effective value. Starts at
    /// [`SIDEBAR_W`] (see the hand-written [`Default`]).
    pub width: u16,
    /// Columns the toolbar is pushed right by (plan §17.2), so the gui's traffic lights never sit on `◫`: the
    /// biggest `inset` any connected viewer reported, 0 when no gui is connected. Every viewer sees the toolbar at
    /// the same columns; [`topbar_w`] is `inset + `[`TOOLBAR_W`] (plan §20.2).
    pub inset: u16,
    /// A42-LINUX-SIDEBAR-ALIGN-03: the narrowest band (columns) a connected window needs for its own header
    /// controls, from the `0x06` `inset` line's optional `sidebar_min` (the Linux GTK headerbar's five tools beside
    /// GTK's window buttons). 0 = no viewer asked for one. [`width`] keeps the shown sidebar at least this wide, so
    /// the header boundary and the grid's divider are the same geometry even when the user drags below it; the
    /// requested width ([`Side::width`]) stays the user's value.
    pub sidebar_min: u16,
    /// The toolbar's one-shot notice on the status line (plan §11.1): what `+`, a grey arrow or a child fleet's `×`
    /// has to say. The next key press or mouse press clears it (`server/view.rs`).
    pub notice: Option<String>,
    /// The close confirmation box is open (plan §2.5 / §11.3): while it is, every key, paste and mouse press goes to
    /// the box and nothing reaches a pane (`server/view.rs`).
    pub confirm: bool,
    /// The close request's state (plan §11.1): shown on the status line, and the card's `×` goes away.
    pub stop: Stop,
    /// The closed fleets kv `restorable` lists (plan §13.6-2 / §18.3), `None` until the daemon or the closer wrote
    /// it; the topbar's `↺` restores `runs[0]`, a card's `↺` restores the run whose `id` is the card's `id`.
    pub restorable: Option<Restorable>,
    /// A restore pane is running (plan §13.3): the status line says `正在恢复…` until the daemon's cards come back.
    pub resuming: bool,
    /// The card list's scroll offset (like cmux): the merged-list index of the first card drawn. Shared by every
    /// viewer like the selection; read through [`first_card`], which clamps it to the cards there are, so a taller
    /// window or a closed card never leaves blank card slots.
    pub scroll: usize,
    /// The (selected card, card count) [`follow_selection`] last saw: the selected card is scrolled into view only
    /// when this pair changes, so a kv push or a layout change that moves no card leaves the user's scroll alone.
    pub followed: Option<(Option<usize>, usize)>,
}

/// A fresh sidebar: no kv `sidebar` yet, shown, at the default width [`SIDEBAR_W`] (plan §16.2: the server resets
/// the width to it when it really restarts, the width only lives in the service's memory).
impl Default for Side {
    fn default() -> Self {
        Self {
            data: None,
            bash: Vec::new(),
            bash_sel: None,
            close_bash: None,
            hidden: false,
            width: SIDEBAR_W,
            inset: 0,
            sidebar_min: 0,
            notice: None,
            confirm: false,
            stop: Stop::default(),
            restorable: None,
            resuming: false,
            scroll: 0,
            followed: None,
        }
    }
}

/// The toolbar row: `◫` toggle, `+` new bash, `↺` reopen, `←` previous fleet, `→` next fleet (plan §20.2).
const TOOL_ROW: u16 = 0;
/// Column of the sidebar toggle `◫` (plan §11.1); every toolbar column is shifted right by [`Side::inset`].
const TOGGLE_X: u16 = 1;
/// Column of the new-bash `+` (plan §11.1).
const NEW_X: u16 = 4;
/// Column of the reopen `↺` (plan §20.2: S2b wires the restore, this milestone only shows a notice).
const REOPEN_X: u16 = 7;
/// Column of the previous-fleet `←` (plan §20.2).
const PREV_X: u16 = 10;
/// Column of the next-fleet `→` (plan §20.2).
const NEXT_X: u16 = 13;
/// The shortest header band that still has the grid's own toolbar row (the macOS two-row topbar band,
/// `fleetmux_layout::TOPBAR_ROWS`): a shorter band means the window's native headerbar carries the toolbar, so the
/// sidebar has no row-0 buttons (the Linux GTK headerbar, A26 rev5/rev7).
const TOOLBAR_BAND: u16 = fleetmux_layout::TOPBAR_ROWS;
/// Rows per card slot: three card rows and one blank row between cards.
const CARD_STEP: u16 = 4;
/// Rows one card takes.
const CARD_H: u16 = 3;
/// Columns from the card's left edge to its title (`●` at 1, gap at 2, text from 3).
const CARD_TEXT_X: u16 = 3;
/// Rows kept free at the bottom (`LAST−4..=LAST`).
const RESERVED_ROWS: u16 = 5;
/// The separator inside a title (`<主> › worker-1 › 子 fleet M0`).
const SEP: &str = " › ";
/// What replaces the squeezed head of a title.
const ELLIPSIS_SEP: &str = "… › ";
/// The state dot.
const DOT: char = '●';
/// The toolbar glyphs; the hidden rail shows [`TOGGLE`] again (plan §11.1, one constant per glyph so a fallback
/// glyph is a one-line change).
const TOGGLE: char = '◫';
const NEW: char = '+';
const REOPEN: char = '↺';
const PREV: char = '←';
const NEXT: char = '→';
/// The topbar's divider between its two segments (plan §20.2: right above the groups' left border).
const DIVIDER: char = '│';
/// Between the topbar's fleet title and its summary line (plan §20.2).
const TITLE_SEP: &str = " · ";
/// The close button on a card.
const CLOSE: char = '×';
/// The restore button a closed card shows in the `×`'s place (plan §13.3: Menlo has `↺`, width 1; `↻` is the
/// fallback).
const RESTORE: char = '↺';
/// A bash card's title head (plan §14.2): `bash-<n>`, numbered from 1 in [`Side::bash`] order. `server/view.rs`
/// titles the pane it spawns with it, so the card and the pane record agree.
pub(crate) const BASH_TITLE: &str = "bash-";
/// The pane type of a sidebar bash (plan §14.3): the server spawns it as a tab of the root page's `workers` group (user
/// 2026-11-09: mapped into the main fleet's worker area; the root fleet registers it but it is no worker), so
/// `pane.list` rows of this type are [`Side::bash`] and nothing else (`server/view.rs`).
pub(crate) const SHELL_TYPE: &str = "shell";

/// The topbar's `(title, line1)`: the selected bash card's own (`bash-<n>`, `终端 · 运行中`, numbered like its card)
/// while one is selected, else the current fleet card's; `None` when there is neither.
pub(crate) fn current_title(side: &Side) -> Option<(String, String)> {
    if let Some(sel) = side.bash_sel.as_ref()
        && let Some(b) = side.bash.iter().position(|p| p == sel)
    {
        return Some((format!("{BASH_TITLE}{}", b + 1), BASH_LINE1.to_string()));
    }
    let data = side.data.as_ref()?;
    data.fleets
        .iter()
        .find(|e| e.id == data.current)
        .map(|e| (e.title.clone(), e.line1.clone()))
}
/// A bash card's first line (plan §14.2): the shell pane runs for as long as its card exists.
const BASH_LINE1: &str = "终端 · 运行中";
/// A bash card's second line (plan §14.2): the shell starts in `$HOME`, drawn as `~`.
const BASH_LINE2: &str = "~";
/// The closed fleet's placeholder (plan §13.3 / §13.6-3): the title is `fleet「<current card title>」已关闭` in white
/// bold, then the restore hint (grey) and the exit hint (dark grey).
pub const PLACEHOLDER_TITLE_HEAD: &str = "fleet「";
pub const PLACEHOLDER_TITLE_TAIL: &str = "」已关闭";
pub const PLACEHOLDER_RESTORE: &str = "运行记录保留。点侧栏卡片上的 ↺ 或按 ^b r 恢复，接着原来的活。";
pub const PLACEHOLDER_EXIT: &str = "关掉这个窗口（或 ^b d）后 10 秒服务退出；之后仍可在终端 start --resume。";
/// The history list's placeholder (plan §18.4-5 / §18.7-6): no fleet is running, the grey cards' `↺` bring one back.
/// The S2b `fleet「<name>」已关闭` lines stay for the single just-closed card ([`just_closed`]).
pub const PLACEHOLDER_NO_FLEET: &str = "没有在跑的 fleet；点侧栏卡片上的 ↺ 恢复";
/// The close confirmation box's widest form (plan §2.5: `min(60, cols − 4)`), its border glyphs and its texts.
const MODAL_MAX_W: u16 = 60;
const MODAL_TL: char = '╭';
const MODAL_TR: char = '╮';
const MODAL_BL: char = '╰';
const MODAL_BR: char = '╯';
const MODAL_H: char = '─';
const MODAL_V: char = '│';
/// The line above the buttons (plan §2.5).
const MODAL_HINT: &str = "按 y 确认，其他任意键取消";
/// The two buttons, right-aligned on the box's last inner row (plan §2.5).
const MODAL_CANCEL: &str = "[ 取消 ]";
/// The bash page's close box (plan §14.2): the same box as the fleet's, with the page (`bash-<n>`) as its subject.
const MODAL_TITLE_BASH_HEAD: &str = "关闭 bash 页「";
const MODAL_TITLE_BASH_TAIL: &str = "」？";
const MODAL_BODY_BASH: &str = " 里还有程序在跑，关掉会结束它。";
const MODAL_CONFIRM_BASH: &str = "[ 关闭 bash 页 ]";
const MODAL_CONFIRM: &str = "[ 关闭 fleet ]";
/// The default title (wrapped around the first card's title) and body, used when kv `sidebar` has no `close`
/// (plan §11.1).
const MODAL_TITLE_HEAD: &str = "关闭整个 fleet「";
const MODAL_TITLE_TAIL: &str = "」？";
const MODAL_BODY: &str = "会停掉监工和全部 worker（含它的子 fleet）。正在做的活会中断。运行记录保留，之后可以在侧栏点 ↺ 恢复，接着原来的活。";

/// kv `sidebar` → [`Data`]. Rules in the module doc; any deviation is `Err` with one English sentence.
pub fn parse(json: &str) -> Result<Data, String> {
    let value: serde_json::Value =
        serde_json::from_str(json).map_err(|e| format!("sidebar kv is not JSON: {e}"))?;
    let obj = value
        .as_object()
        .ok_or("sidebar kv is not a JSON object")?;
    match obj.get("v") {
        Some(serde_json::Value::Number(n)) if n.as_u64() == Some(1) => {}
        _ => return Err("sidebar kv `v` must be the number 1".to_string()),
    }
    let current = obj
        .get("current")
        .and_then(|v| v.as_str())
        .ok_or("sidebar kv `current` must be a string")?
        .to_string();
    let items = obj
        .get("fleets")
        .and_then(|v| v.as_array())
        .ok_or("sidebar kv `fleets` must be an array")?;
    if items.is_empty() || items.len() > MAX_FLEETS {
        return Err(format!(
            "sidebar kv `fleets` must hold 1..={MAX_FLEETS} entries, got {}",
            items.len()
        ));
    }
    let mut fleets = Vec::with_capacity(items.len());
    for (i, item) in items.iter().enumerate() {
        let o = item
            .as_object()
            .ok_or_else(|| format!("sidebar kv `fleets[{i}]` must be an object"))?;
        let field = |key: &str| -> Result<String, String> {
            let s = o
                .get(key)
                .and_then(|v| v.as_str())
                .ok_or_else(|| format!("sidebar kv `fleets[{i}].{key}` must be a string"))?;
            if s.chars().count() > 200 {
                return Err(format!(
                    "sidebar kv `fleets[{i}].{key}` is longer than 200 characters"
                ));
            }
            Ok(s.to_string())
        };
        // M4: `page` is optional; when present it is a string like the other keys (`""` names the root page).
        let page = match o.get("page") {
            None => None,
            Some(_) => Some(field("page")?),
        };
        fleets.push(Entry {
            id: field("id")?,
            title: field("title")?,
            state: field("state")?,
            line1: field("line1")?,
            line2: field("line2")?,
            page,
        });
    }
    if !fleets.iter().any(|e| e.id == current) {
        return Err(format!(
            "sidebar kv `current` {current:?} is not one of the fleets"
        ));
    }
    // `close` is optional; when it is there it must be an object of two short strings. A missing string stays empty
    // (the box then shows the default text), anything else rejects the whole value.
    let close = match obj.get("close") {
        None => None,
        Some(value) => {
            let o = value
                .as_object()
                .ok_or("sidebar kv `close` must be an object")?;
            let field = |key: &str| -> Result<String, String> {
                match o.get(key) {
                    None => Ok(String::new()),
                    Some(serde_json::Value::String(s)) if s.chars().count() <= 400 => Ok(s.clone()),
                    Some(serde_json::Value::String(_)) => Err(format!(
                        "sidebar kv `close.{key}` is longer than 400 characters"
                    )),
                    Some(_) => Err(format!("sidebar kv `close.{key}` must be a string")),
                }
            };
            Some(Close {
                title: field("title")?,
                body: field("body")?,
            })
        }
    };
    Ok(Data {
        current,
        fleets,
        close,
    })
}

/// kv `restorable` → [`Restorable`] (plan §13.6-2 / §18.3). Rules in the module doc: `v` is 1, `runs` is an array
/// of at most [`MAX_RESTORABLE`] objects, each with string `id` / `title` (at most 200 characters), numeric
/// `closed_at`, a non-empty `argv` of strings, a string `cwd` and an `env` object of string values. Any deviation is
/// `Err` with one English sentence (the server logs it and keeps the restorable list it had); unknown keys are
/// ignored.
pub fn parse_restorable(json: &str) -> Result<Restorable, String> {
    let value: serde_json::Value =
        serde_json::from_str(json).map_err(|e| format!("restorable kv is not JSON: {e}"))?;
    let obj = value
        .as_object()
        .ok_or("restorable kv is not a JSON object")?;
    match obj.get("v") {
        Some(serde_json::Value::Number(n)) if n.as_u64() == Some(1) => {}
        _ => return Err("restorable kv `v` must be the number 1".to_string()),
    }
    let items = obj
        .get("runs")
        .and_then(|v| v.as_array())
        .ok_or("restorable kv `runs` must be an array")?;
    if items.len() > MAX_RESTORABLE {
        return Err(format!(
            "restorable kv `runs` must hold at most {MAX_RESTORABLE} entries, got {}",
            items.len()
        ));
    }
    let mut runs = Vec::with_capacity(items.len());
    for (i, item) in items.iter().enumerate() {
        let o = item
            .as_object()
            .ok_or_else(|| format!("restorable kv `runs[{i}]` must be an object"))?;
        let field = |key: &str| -> Result<String, String> {
            let s = o
                .get(key)
                .and_then(|v| v.as_str())
                .ok_or_else(|| format!("restorable kv `runs[{i}].{key}` must be a string"))?;
            if s.chars().count() > 1000 {
                return Err(format!(
                    "restorable kv `runs[{i}].{key}` is longer than 1000 characters"
                ));
            }
            Ok(s.to_string())
        };
        let id = field("id")?;
        if id.chars().count() > 200 {
            return Err(format!(
                "restorable kv `runs[{i}].id` is longer than 200 characters"
            ));
        }
        let title = field("title")?;
        if title.chars().count() > 200 {
            return Err(format!(
                "restorable kv `runs[{i}].title` is longer than 200 characters"
            ));
        }
        let closed_at = o
            .get("closed_at")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| format!("restorable kv `runs[{i}].closed_at` must be a number"))?;
        let argv: Vec<String> = o
            .get("argv")
            .and_then(|v| v.as_array())
            .ok_or_else(|| format!("restorable kv `runs[{i}].argv` must be an array"))?
            .iter()
            .map(|v| {
                v.as_str()
                    .map(str::to_string)
                    .ok_or_else(|| format!("restorable kv `runs[{i}].argv` must hold strings"))
            })
            .collect::<Result<_, _>>()?;
        if argv.is_empty() {
            return Err(format!(
                "restorable kv `runs[{i}].argv` must name the command to run"
            ));
        }
        let cwd = field("cwd")?;
        let env_obj = o
            .get("env")
            .and_then(|v| v.as_object())
            .ok_or_else(|| format!("restorable kv `runs[{i}].env` must be an object"))?;
        let mut env = BTreeMap::new();
        for (key, val) in env_obj {
            let s = val.as_str().ok_or_else(|| {
                format!("restorable kv `runs[{i}].env.{key}` must be a string")
            })?;
            env.insert(key.clone(), s.to_string());
        }
        runs.push(RestoreRun {
            id,
            title,
            closed_at,
            argv,
            cwd,
            env,
        });
    }
    Ok(Restorable { runs })
}

/// The narrowest width a shown sidebar may have (plan §17.2 / §20.2: `max(SIDEBAR_MIN_W, inset + `[`TOOLBAR_W`])`).
/// Hidden, [`width`] is 0, so a legal shown width is never mistaken for the collapsed one; [`is_hidden`] keeps the
/// width test for [`hit`]'s unit tests.
fn shown_min(side: &Side) -> u16 {
    fleetmux_layout::SIDEBAR_MIN_W.max(side.inset.saturating_add(TOOLBAR_W))
}

/// The width to ask the layout for (`Layout::set_sidebar`): none without kv `sidebar`; 0 while hidden (plan §20.2:
/// collapsing takes the sidebar away, the panes fill the screen from column 0, and [`topbar_w`] keeps row 0's
/// buttons where they were); while shown the dragged width ([`Side::width`], [`SIDEBAR_W`] until they drag it), but
/// never below [`shown_min`] or the window's own header floor ([`Side::sidebar_min`], A42-LINUX-SIDEBAR-ALIGN-03).
///
/// The drag range ([`SIDEBAR_MIN_W`]..=[`fleetmux_layout::SIDEBAR_MAX_W`]) bounds the **request**, not the floor: a
/// floor the controls need above the widest drag width (a very small font makes the toolbar's intrinsic px width
/// more than 60 columns) is asked for as it is, because capping it would leave the header boundary and the grid's
/// divider on two different columns. If even that does not fit the content, the layout's own
/// [`fleetmux_layout::SIDEBAR_MIN_CONTENT`] rule hides the sidebar ([`fleetmux_layout::Layout::sidebar_cols`]) and
/// the window falls back to its single header boundary — never two.
pub fn width(side: &Side) -> u16 {
    if side.data.is_none() || side.hidden {
        0
    } else {
        let widest = fleetmux_layout::SIDEBAR_MAX_W.max(side.sidebar_min);
        side.width
            .max(shown_min(side))
            .max(side.sidebar_min)
            .min(widest)
    }
}

/// The width of row 0's left part, handed to `Layout::set_topbar` (plan §20.2): 0 without kv `sidebar` (no topbar at
/// all), otherwise `inset + `[`TOOLBAR_W`] — the toolbar's own columns, shown or hidden. While shown the layout grows
/// it to the sidebar's width, so the divider sits on the sidebar's drag line.
pub fn topbar_w(side: &Side) -> u16 {
    if side.data.is_none() {
        0
    } else {
        side.inset.saturating_add(TOOLBAR_W)
    }
}

/// A26 rev5 (CURRENT_REQUIREMENTS rev 6 `utm_topbar`): the topbar width the *grid* keeps, handed to
/// `Layout::set_topbar` — the whole point of the Linux headerbar is that the window draws one native header row
/// instead of a titlebar plus a second in-grid toolbar row, so a mirrored layout (Linux) keeps **no** topbar row
/// (0): row 0 is then content, the native headerbar carries `◫ + ↺ ← →` and the fleet title/status
/// (`crates/web/src/gui.rs` sends their text with `0x85`, `server::overlay::topbar_for`), and no grid row can
/// duplicate it. macOS keeps the in-grid topbar (grid row 0 on the native traffic lights).
pub fn topbar_cols(side: &Side, native_header: bool) -> u16 {
    if native_header {
        0
    } else {
        topbar_w(side)
    }
}

/// Whether `sb` (the width the layout reports) is the hidden sidebar: `Side::hidden`, or a width below [`shown_min`]
/// that no shown sidebar can have (plan §20.2: hidden, [`width`] is 0). The width alone serves [`hit`]'s unit tests;
/// the server passes `Side::hidden` as well, and a legal shown width is never read as hidden.
fn is_hidden(side: &Side, sb: u16) -> bool {
    side.hidden || sb < shown_min(side)
}

/// The title as drawn in `cols` columns (plan §2.2): as is when it fits; otherwise the head is squeezed to `…` and
/// the last ` › ` segment kept whole; a title without a separator is cut at the tail and ends with `…`.
pub fn fit_title(title: &str, cols: u16) -> String {
    if text_cols(title) <= cols {
        return title.to_string();
    }
    if let Some(idx) = title.rfind(SEP) {
        let tail = &title[idx + SEP.len()..];
        let head_budget = cols.saturating_sub(text_cols(ELLIPSIS_SEP) + text_cols(tail));
        if head_budget >= 1 {
            let mut head = prefix_cols(&title[..idx], head_budget);
            // Cut away the separator's own tail (a space, then `SEP`'s last character), never into a word.
            let sep_tail = SEP.trim();
            while head.ends_with(' ') || head.ends_with(sep_tail) {
                head.pop();
            }
            if text_cols(&head) >= 1 {
                return format!("{head}{ELLIPSIS_SEP}{tail}");
            }
        }
    }
    ellipsize(title, cols)
}

/// What a click in the sidebar means (plan §2.4 / §11.1): a toolbar button, the hidden rail's toggle, a card, a
/// card's `×`, or nothing at all.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SideHit {
    /// The `◫` toggle on row 0, columns 0–2: hide the sidebar.
    Hide,
    /// The hidden rail's `◫`: show the sidebar again.
    Show,
    /// The `+` on row 0, columns 3–5: new bash (S1c opens the shell; S1b only shows a notice).
    NewBash,
    /// The `↺` on row 0, columns 6–8: restore the last closed fleet (plan §20.2: S2b wires the restore, this
    /// milestone only shows a notice). Named apart from the card's `↺`, which §13 (S2b) reserves as `Restore(i)`.
    Reopen,
    /// The `←` on row 0, columns 9–11: the fleet before `current` (S3 switches; S1b only shows a notice).
    Prev,
    /// The `→` on row 0, columns 12–14: the fleet after `current` (S3 switches; S1b only shows a notice).
    Next,
    /// The body of card `i` of the merged list (plan §14.5-3: fleet cards first, then the bash cards; `i ≥
    /// fleets.len()` is a bash card. S1b does nothing; S3 switches fleets).
    Pick(usize),
    /// The `×` on card `i`'s first row (plan §11.1): close that fleet. `i ≥ fleets.len()` closes that bash page
    /// (plan §14.2).
    Close(usize),
    /// The `↺` on closed card `i`'s first row, columns `sb − 4 ..= sb − 2` (plan §13.6-1): restore that fleet with
    /// the command kv `restorable` registered for its `id`.
    Restore(usize),
    /// No button, no card (the blank row 1, the gutter column, the empty space below the cards).
    Nothing,
}

/// One card of the merged list (plan §14.3 / §14.5-3): the fleet cards kv `sidebar` lists come first, then one card
/// per bash pane ([`Side::bash`]) in creation order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Card<'a> {
    Fleet(&'a Entry),
    /// A bash card: its pane id and its 0-based place in [`Side::bash`] (its title's number is that plus one).
    Bash(&'a PaneId, usize),
}

/// How many cards the merged list has (plan §14.3: fleet cards + bash cards).
pub(crate) fn card_count(side: &Side) -> usize {
    side.data.as_ref().map_or(0, |d| d.fleets.len()) + side.bash.len()
}

/// Card `j` of the merged list, `None` past its end.
fn card_at(side: &Side, j: usize) -> Option<Card<'_>> {
    let data = side.data.as_ref()?;
    if j < data.fleets.len() {
        return Some(Card::Fleet(&data.fleets[j]));
    }
    let b = j - data.fleets.len();
    side.bash.get(b).map(|pane| Card::Bash(pane, b))
}

/// The bash pane of merged card `j` (plan §14.5-3: `j ≥ fleets.len()` is a bash card), `None` for a fleet card or
/// past the end. `server/view.rs` maps `Pick(j)` / `Close(j)` with it (`pub`, like [`blank_at`], because that call
/// lands in a later S1c segment).
pub fn bash_at(side: &Side, j: usize) -> Option<&PaneId> {
    match card_at(side, j)? {
        Card::Bash(pane, _) => Some(pane),
        Card::Fleet(_) => None,
    }
}

/// Index of the selected card in the merged list (plan §14.2: the current view follows the zoom — the zoomed bash
/// card while [`Side::bash_sel`] names one of [`Side::bash`], otherwise the card `current` names).
fn selected_index(side: &Side) -> Option<usize> {
    let data = side.data.as_ref()?;
    if let Some(sel) = side.bash_sel.as_ref()
        && let Some(b) = side.bash.iter().position(|pane| pane == sel)
    {
        return Some(data.fleets.len() + b);
    }
    data.fleets.iter().position(|e| e.id == data.current)
}

/// Whether the merged list has a card before / after the selected one (plan §14.2 / §14.5-3: `←` / `→` walk the
/// fleet cards and the bash cards as one list).
pub(crate) fn merged_neighbours(side: &Side) -> (bool, bool) {
    let total = card_count(side);
    match selected_index(side) {
        Some(i) => (i > 0, i + 1 < total),
        None => (false, false),
    }
}

/// Index of the selected card in the merged list (what [`merged_neighbours`] and the drawn blue bar use), `None`
/// when there is no card at all. `server/view.rs` steps `←` / `→` / `^b (` / `^b )` with it.
pub fn selected_card(side: &Side) -> Option<usize> {
    selected_index(side)
}

/// Hit test inside the sidebar: `sb` = `Layout::sidebar_cols` (0 = no sidebar), `content_rows` = the layout rows
/// without the status line. Everything is placed from `sb` itself (plan §16.2): the card body ends at `sb − 2`,
/// the gap column `sb − 1` hits nothing, the card's `×` sits at `sb − 3`. Row 0 is the topbar's left part, shifted
/// right by [`Side::inset`] (plan §20.2), and it is hit even when the sidebar is not drawn (`sb == 0`, hidden); every
/// other row hits nothing while hidden (see [`is_hidden`]). `top` is the layout's header band height
/// (`Layout::header_rows`), so the `k`-th card drawn starts at row `top + 4k` and hits as card [`first_card`]` + k`
/// (the list scrolls, like cmux) — the first card's first row is the workspace's
/// topmost tab row on both platforms (A26 rev7). The cards are the merged list (plan §14.5-3): the fleet cards kv
/// `sidebar` lists, then the bash cards [`Side::bash`] lists.
pub fn hit(side: &Side, sb: u16, content_rows: u16, top: u16, x: u16, y: u16) -> SideHit {
    // Plan §20.2: row 0 belongs to the in-grid topbar, whose buttons keep their columns while the sidebar
    // collapses. With a shorter header band the window's native headerbar carries the toolbar (Linux, A26 rev7),
    // so row 0 is blank there and hits nothing.
    if top >= TOOLBAR_BAND && y == TOOL_ROW {
        return if side.data.is_some() {
            toolbar_hit(side, x, is_hidden(side, sb))
        } else {
            SideHit::Nothing
        };
    }
    if sb == 0 || is_hidden(side, sb) {
        return SideHit::Nothing;
    }
    if side.data.is_none() {
        return SideHit::Nothing;
    }
    let first = first_card(side, content_rows, top);
    let drawn = card_count(side).saturating_sub(first).min(fit_count(content_rows, top));
    for j in first..first + drawn {
        let y0 = top + CARD_STEP * (j - first) as u16;
        if y >= y0 && y < y0 + CARD_H && x <= sb.saturating_sub(2) {
            let cx = close_x(sb);
            let close_area = cx.saturating_sub(1)..=cx.saturating_add(1);
            if y == y0 && close_area.contains(&x) {
                match card_at(side, j) {
                    Some(Card::Fleet(e)) => {
                        if closeable(side, e) {
                            return SideHit::Close(j);
                        }
                        // Plan §13.6-1: only `state == "closed"` turns that cell into `↺`; `Side.stop` has no say here.
                        if is_closed(e) {
                            return SideHit::Restore(j);
                        }
                    }
                    // Plan §14.2: a bash card always carries its `×` — the shell is the server's own pane.
                    Some(Card::Bash(..)) => return SideHit::Close(j),
                    None => {}
                }
            }
            return SideHit::Pick(j);
        }
    }
    SideHit::Nothing
}

/// Whether (`x`, `y`) is the sidebar's empty card area (plan §14.5-2): the sidebar is drawn (not hidden), the row is
/// `top .. content_rows` (`top` is the first card's row, `content_rows − 1` the last row above the status line), the
/// column is left of the gap column (`x ≤ sb − 2`), and no card or `×` sits there ([`hit`] says
/// [`SideHit::Nothing`]). The header band above `top` (the topbar row and its blank separation row) never counts;
/// the hidden sidebar has no card area. A double click
/// here opens a bash page (plan §14.2 / §14.5-2); `server/view.rs` wires that.
pub fn blank_at(side: &Side, sb: u16, content_rows: u16, top: u16, x: u16, y: u16) -> bool {
    if side.data.is_none() || sb == 0 || is_hidden(side, sb) {
        return false;
    }
    if y < top || y >= content_rows || x > sb.saturating_sub(2) {
        return false;
    }
    hit(side, sb, content_rows, top, x, y) == SideHit::Nothing
}

/// Row 0's buttons as click areas (plan §20.2), all shifted right by [`Side::inset`]: `◫` `k..=k+2`, `+`
/// `k+3..=k+5`, `↺` `k+6..=k+8`, `←` `k+9..=k+11`, `→` `k+12..=k+14`. While hidden the `◫` means "show the sidebar
/// again"; `+ ↺ ← →` keep their meaning (the hidden topbar draws the same toolbar).
fn toolbar_hit(side: &Side, x: u16, hidden: bool) -> SideHit {
    let k = side.inset;
    let at = |lo: u16| lo.saturating_add(k)..=lo.saturating_add(k).saturating_add(2);
    if at(0).contains(&x) {
        if hidden {
            SideHit::Show
        } else {
            SideHit::Hide
        }
    } else if at(3).contains(&x) {
        SideHit::NewBash
    } else if at(6).contains(&x) {
        SideHit::Reopen
    } else if at(9).contains(&x) {
        SideHit::Prev
    } else if at(12).contains(&x) {
        SideHit::Next
    } else {
        SideHit::Nothing
    }
}

/// Column of a card's `×` (plan §11.1 / §16.2: `sb − 3`); its click area is the three columns around it.
fn close_x(sb: u16) -> u16 {
    sb.saturating_sub(3)
}

/// Columns a card's subtitle lines may use (plan §16.2: `sb − 5`, 19 columns at the minimum width 24).
fn card_text_w(sb: u16) -> u16 {
    sb.saturating_sub(5)
}

/// Columns a card's title may use: the two columns left of the `×` stay free (plan §11.1 / §16.2: `sb − 7`).
fn card_title_w(sb: u16) -> u16 {
    sb.saturating_sub(7)
}

/// Whether a card gets the `×` (plan §11.1): only a fleet that is still `running` / `paused`, and only while no
/// `stop_request` is in flight. A reclaimed, stopping or closed one has nothing left to close, and after a request
/// the button goes away so it cannot be pressed twice.
///
/// M4 (design m4 R10): a child fleet's card that carries a `page` never gets one — a child fleet is started and
/// stopped through `subfleet.up` / `down` and its roster, not here. The root card (`page` `""` or none) keeps it.
pub(crate) fn closeable(side: &Side, e: &Entry) -> bool {
    side.stop == Stop::None
        && matches!(e.state.as_str(), "running" | "paused")
        && e.page.as_deref().is_none_or(str::is_empty)
}

/// Whether a card is a fleet the sidebar closed (plan §13.3 / §13.6-1): its first row shows `↺` instead of `×`.
fn is_closed(e: &Entry) -> bool {
    e.state == "closed"
}

/// Whether a card is a fleet that is still up (plan §18.2 / §18.7-4): while one is, a second restore only says
/// `先关闭正在运行的 fleet 再恢复`, the placeholder is not drawn and the reaper keeps the server alive (§18.4-5).
pub(crate) fn live_card(e: &Entry) -> bool {
    matches!(e.state.as_str(), "running" | "paused" | "stopping")
}

/// Whether any card of `data` is still up (plan §18.7-4 / §18.4-5).
pub(crate) fn has_live_card(data: &Data) -> bool {
    data.fleets.iter().any(live_card)
}

/// Whether `data` is the just-closed fleet alone: exactly one card, closed, and `current` names it. Plan §13.6-3
/// keeps the S2b `fleet「X」已关闭` placeholder for that case; a history list gets [`PLACEHOLDER_NO_FLEET`]
/// (§18.4-5 / §18.7-6).
pub(crate) fn just_closed(data: &Data) -> bool {
    match data.fleets.as_slice() {
        [only] => is_closed(only) && only.id == data.current,
        _ => false,
    }
}

/// Draw the sidebar into `buf` (plan §11.1 / §2.3 / §17.2): first every cell left of `sb` and above the status
/// line is reset, then the toolbar, then — while shown — the cards that fit from the scroll offset. Hidden, only the toolbar
/// is drawn (same columns as when shown, no cards, no background below row 0). `top` is the layout's header band
/// height, i.e. the first card's row ([`draw_cards`]). `sb == 0` draws nothing at all.
pub fn draw(buf: &mut Buffer, side: &Side, sb: u16, content_rows: u16, top: u16) {
    if sb == 0 {
        return;
    }
    draw_band(buf, side, sb, content_rows, top);
    if side.data.is_none() {
        return;
    }
    draw_toolbar(buf, side, is_hidden(side, sb));
}

/// The sidebar band without its toolbar: the same cells [`draw`] clears, then — while shown — the cards that fit
/// from the scroll offset. A26 rev3: the mirrored (Linux) sidebar is the right band and its row 0 belongs to the header, so
/// the band is drawn into its own `sb`-wide buffer and copied to those columns, while the toolbar stays on row 0's
/// left ([`draw_topbar`] draws it there).
pub fn draw_band(buf: &mut Buffer, side: &Side, sb: u16, content_rows: u16, top: u16) {
    if sb == 0 {
        return;
    }
    clear(buf, sb, content_rows);
    if side.data.is_none() || is_hidden(side, sb) {
        return;
    }
    draw_cards(buf, side, sb, content_rows, top);
}

/// How many cards fit above the reserved rows (plan §2.2: a card's third row may not pass `LAST − 5`).
fn fit_count(content_rows: u16, top: u16) -> usize {
    let limit = content_rows.saturating_sub(1 + RESERVED_ROWS);
    let last_card_row = top + CARD_H - 1;
    if limit < last_card_row {
        return 0;
    }
    usize::from((limit - last_card_row) / CARD_STEP) + 1
}

/// The merged-list index of the first card drawn: [`Side::scroll`] clamped to `0..=total − fit`, so a taller window
/// or a closed card never leaves a card slot blank. [`draw_cards`], [`hit`], the wheel and the scrollbar all read the
/// offset through this.
pub fn first_card(side: &Side, content_rows: u16, top: u16) -> usize {
    let fit = fit_count(content_rows, top);
    side.scroll.min(card_count(side).saturating_sub(fit))
}

/// One wheel step over the card area: move the first card drawn by `delta` cards (+ = later cards), stopping at
/// either end (cmux: one card per wheel step). Returns whether the cards moved.
pub fn scroll_cards(side: &mut Side, delta: i32, content_rows: u16, top: u16) -> bool {
    let first = first_card(side, content_rows, top);
    let last = card_count(side).saturating_sub(fit_count(content_rows, top));
    let step = delta.unsigned_abs() as usize;
    let next = if delta < 0 {
        first.saturating_sub(step)
    } else {
        first.saturating_add(step).min(last)
    };
    side.scroll = next;
    next != first
}

/// Scroll the selected card into view when the selection or the card count changed since the last call (a new bash
/// page, a step, a picked or closed card), just far enough; otherwise keep the user's scroll. Either way
/// [`Side::scroll`] ends clamped to the cards there are. Returns whether the first card drawn moved.
pub fn follow_selection(side: &mut Side, content_rows: u16, top: u16) -> bool {
    let before = first_card(side, content_rows, top);
    let fit = fit_count(content_rows, top);
    let seen = (selected_index(side), card_count(side));
    if side.followed != Some(seen) {
        side.followed = Some(seen);
        if let (Some(sel), true) = (seen.0, fit > 0) {
            if sel < before {
                side.scroll = sel;
            } else if sel >= before + fit {
                side.scroll = sel + 1 - fit;
            }
        }
    }
    side.scroll = first_card(side, content_rows, top);
    side.scroll != before
}

/// The scrollbar's thumb while the cards overflow (cmux): its first row and its height in rows. The track is the
/// card area's rows from the first card's first row to the last drawn card's last row; the thumb is the drawn
/// share of the cards (at least one row) and sits at the offset's share of the rest, so it touches the track's top
/// at offset 0 and its bottom at the last offset. `None` without kv `sidebar`, while hidden, or when every card
/// fits. The renderer (`render/draw.rs`) draws it in the band's column next to the content.
pub fn scrollbar(side: &Side, content_rows: u16, top: u16) -> Option<(u16, u16)> {
    if side.data.is_none() || side.hidden {
        return None;
    }
    let total = card_count(side);
    let fit = fit_count(content_rows, top);
    if fit == 0 || total <= fit {
        return None;
    }
    let track = CARD_STEP as usize * fit - 1;
    let len = (track * fit / total).max(1);
    let pos = (track - len) * first_card(side, content_rows, top) / (total - fit);
    Some((top + pos as u16, len as u16))
}

/// Reset every sidebar cell above the status line, so no pane character or older card survives.
fn clear(buf: &mut Buffer, sb: u16, content_rows: u16) {
    for y in 0..content_rows.min(buf.area.height) {
        for x in 0..sb.min(buf.area.width) {
            if let Some(c) = buf.cell_mut((x, y)) {
                c.reset();
            }
        }
    }
}

/// Row 0's toolbar (plan §20.2): `◫ + ↺ ← →` at `inset + 1 / 4 / 7 / 10 / 13`, the `◫` white and bold, `+` and `↺`
/// white, the arrows white only when a neighbour exists in the merged list (plan §14.5-3: fleet cards and bash cards
/// together). The hidden topbar draws this very function, so the buttons keep their columns when the sidebar
/// collapses; it then keeps the dark-grey cell behind the `◫` it had as the S1b rail.
fn draw_toolbar(buf: &mut Buffer, side: &Side, hidden: bool) {
    let (has_prev, has_next) = merged_neighbours(side);
    let arrow = |on: bool| if on { Color::White } else { Color::DarkGray };
    let k = side.inset;
    put(buf, k + TOGGLE_X, TOOL_ROW, 1, &TOGGLE.to_string(), Color::White, true);
    put(buf, k + NEW_X, TOOL_ROW, 1, &NEW.to_string(), Color::White, false);
    put(buf, k + REOPEN_X, TOOL_ROW, 1, &REOPEN.to_string(), Color::White, false);
    put(buf, k + PREV_X, TOOL_ROW, 1, &PREV.to_string(), arrow(has_prev), false);
    put(buf, k + NEXT_X, TOOL_ROW, 1, &NEXT.to_string(), arrow(has_next), false);
    if hidden
        && let Some(c) = buf.cell_mut((k + TOGGLE_X, TOOL_ROW))
    {
        c.set_bg(Color::DarkGray);
    }
}

/// Row 0 while kv `sidebar` is set (plan §20.2): the topbar's two segments. `sb` is `Layout::sidebar_cols`,
/// `left` = `Layout::topbar_left(cols)` (the left part's width, the divider's column) and `mirror` the content
/// mirror (A26 rev3, the Linux layout).
///
/// While the sidebar is drawn (`sb > 0`) the left part (`0..left`, the toolbar and the `◫`'s inset) belongs to
/// [`draw`]; this function then puts `│` on the sidebar's drag line and the current fleet's `title` (white, bold)
/// ` · ` `line1` (grey) from two columns after it to `cols − 2`. The divider is `left` — the sidebar's right edge —
/// unmirrored, and the content's right border (`cols − sb − 1`, the sidebar's left edge) mirrored; the title then
/// sits over the sidebar's row 0, which is header on both platforms. Hidden (`sb == 0`, also a window too narrow
/// for the sidebar) it draws the toolbar itself — same columns, no divider — and the title still from
/// `left + 2`. No background is painted: the topbar uses the terminal's own (plan §20.1).
/// Plain terminals on a native-header server retain tools in the existing header gap.
pub fn draw_terminal_header(buf: &mut Buffer, side: &Side, sb: u16, cols: u16) {
    draw_toolbar(buf, side, sb == 0);
    draw_topbar(buf, side, sb, sb.max(topbar_w(side)).min(cols), cols, false);
}

pub fn draw_topbar(buf: &mut Buffer, side: &Side, sb: u16, left: u16, cols: u16, mirror: bool) {
    if side.data.is_none() {
        return;
    }
    // Row 0's toolbar: drawn here while the sidebar is collapsed (it has no band to draw it into) and while the
    // content is mirrored (the band is the right one, the toolbar stays on the left).
    if sb == 0 {
        draw_toolbar(buf, side, true);
    } else if mirror {
        draw_toolbar(buf, side, false);
    }
    let divider = match (sb, mirror) {
        (0, _) => None,
        (_, true) => Some(cols.saturating_sub(sb).saturating_sub(1)),
        (_, false) => Some(left),
    };
    if let Some(d) = divider.filter(|d| *d < cols) {
        put(buf, d, TOOL_ROW, 1, &DIVIDER.to_string(), Color::Gray, false);
    }
    let x0 = divider.unwrap_or(left).saturating_add(2);
    let w = cols.saturating_sub(x0).saturating_sub(1);
    let Some((title, line1)) = current_title(side) else {
        return;
    };
    if w == 0 {
        return;
    }
    let title = fit_title(&title, w);
    let tw = text_cols(&title).min(w);
    put(buf, x0, TOOL_ROW, w, &title, Color::White, true);
    let rest = w.saturating_sub(tw);
    if rest < text_cols(TITLE_SEP) {
        return;
    }
    let sx = x0.saturating_add(tw);
    put(buf, sx, TOOL_ROW, rest, TITLE_SEP, Color::Gray, false);
    let lx = sx.saturating_add(text_cols(TITLE_SEP));
    let lw = rest.saturating_sub(text_cols(TITLE_SEP));
    if lw == 0 {
        return;
    }
    put(buf, lx, TOOL_ROW, lw, &ellipsize(&line1, lw), Color::Gray, false);
}

/// The shown sidebar's cards (plan §14.3 / §14.5-3): the merged list — the fleet cards kv `sidebar` lists, then one
/// bash card per [`Side::bash`] pane — as many whole cards as fit, from [`first_card`] on (like cmux the list scrolls
/// instead of counting what does not fit; the scrollbar is drawn by `render/draw.rs`). Every card position comes from
/// `sb` (plan §16.2: blue bar `0..=sb − 2`, gap `sb − 1`, `×` at `sb − 3`, title `sb − 7`, subtitle `sb − 5`); the
/// `k`-th card drawn starts at row `top + 4k`.
fn draw_cards(buf: &mut Buffer, side: &Side, sb: u16, content_rows: u16, top: u16) {
    let first = first_card(side, content_rows, top);
    let drawn = card_count(side).saturating_sub(first).min(fit_count(content_rows, top));
    let sel = selected_index(side);
    for j in first..first + drawn {
        let y0 = top + CARD_STEP * (j - first) as u16;
        let selected = sel == Some(j);
        match card_at(side, j) {
            Some(Card::Fleet(e)) => draw_fleet_card(buf, side, e, sb, y0, selected),
            Some(Card::Bash(_, b)) => draw_bash_card(buf, b, sb, y0, selected),
            None => {}
        }
        // After the texts, which only set foregrounds: the selected card's bar covers the wide characters' second
        // cells too and leaves the `×` / `↺` glyph in place.
        if selected {
            paint_bg(buf, y0, CARD_H, sb.saturating_sub(1), Color::Blue);
        }
    }
}

/// One fleet card (plan §2.2 / §11.1): the state dot, the title, the two grey lines and the `×` — or `↺` for a fleet
/// the sidebar closed (plan §13.6-1).
fn draw_fleet_card(buf: &mut Buffer, side: &Side, e: &Entry, sb: u16, y0: u16, selected: bool) {
    let line = if selected { Color::White } else { Color::Gray };
    let title_w = card_title_w(sb);
    let text_w = card_text_w(sb);
    put(buf, CARD_TEXT_X, y0, title_w, &fit_title(&e.title, title_w), Color::White, true);
    put(buf, CARD_TEXT_X, y0 + 1, text_w, &ellipsize(&e.line1, text_w), line, false);
    put(buf, CARD_TEXT_X, y0 + 2, text_w, &ellipsize(&e.line2, text_w), line, false);
    put(buf, 1, y0, 1, &DOT.to_string(), dot_color(&e.state), false);
    if closeable(side, e) {
        let fg = if selected { Color::White } else { Color::Gray };
        // Before `paint_bg`, which only sets the background: the `×` keeps its glyph and foreground.
        put(buf, close_x(sb), y0, 1, &CLOSE.to_string(), fg, false);
    } else if is_closed(e) {
        // Plan §13.3 / §13.6-1: a closed fleet's first row carries `↺` in the `×`'s place, white while selected.
        let fg = if selected { Color::White } else { Color::Gray };
        put(buf, close_x(sb), y0, 1, &RESTORE.to_string(), fg, false);
    }
}

/// One bash card (plan §14.2 / §14.5-3): the same shape as a fleet card — green dot, `bash-<n>`, `终端 · 运行中`,
/// `~` and the `×` at `sb − 3`. `b` is the card's place in [`Side::bash`], so its title is `bash-<b + 1>`.
fn draw_bash_card(buf: &mut Buffer, b: usize, sb: u16, y0: u16, selected: bool) {
    let line = if selected { Color::White } else { Color::Gray };
    let title_w = card_title_w(sb);
    let text_w = card_text_w(sb);
    let title = format!("{BASH_TITLE}{}", b + 1);
    put(buf, CARD_TEXT_X, y0, title_w, &fit_title(&title, title_w), Color::White, true);
    put(buf, CARD_TEXT_X, y0 + 1, text_w, &ellipsize(BASH_LINE1, text_w), line, false);
    put(buf, CARD_TEXT_X, y0 + 2, text_w, &ellipsize(BASH_LINE2, text_w), line, false);
    put(buf, 1, y0, 1, &DOT.to_string(), dot_color("running"), false);
    // Plan §14.2: a bash card always carries its `×`; the shell is the server's own pane, not a fleet.
    let fg = if selected { Color::White } else { Color::Gray };
    put(buf, close_x(sb), y0, 1, &CLOSE.to_string(), fg, false);
}

/// The title of the bash page a [`Side::close_bash`] box is about (`bash-<n>` from its place in [`Side::bash`]),
/// `None` while no bash box is open.
fn bash_close_name(side: &Side) -> Option<String> {
    let pane = side.close_bash.as_ref()?;
    let b = side.bash.iter().position(|p| p == pane)?;
    Some(format!("{BASH_TITLE}{}", b + 1))
}

/// The confirm button's text: the fleet's while the box closes a fleet, the bash page's while it closes a page
/// (plan §14.2: the same box, the page as its subject).
fn modal_confirm_label(side: &Side) -> &'static str {
    if bash_close_name(side).is_some() {
        MODAL_CONFIRM_BASH
    } else {
        MODAL_CONFIRM
    }
}

/// What the close confirmation box says (plan §11.1): kv `sidebar`'s `close.{title, body}` when the daemon sent
/// them, otherwise the default texts naming the first card. A missing or empty string falls back on its own.
pub fn modal_text(side: &Side) -> (String, String) {
    // Plan §14.2: a bash page's box names the page and says what closing it costs.
    if let Some(name) = bash_close_name(side) {
        return (
            format!("{MODAL_TITLE_BASH_HEAD}{name}{MODAL_TITLE_BASH_TAIL}"),
            format!("{name}{MODAL_BODY_BASH}"),
        );
    }
    let data = side.data.as_ref();
    let close = data.and_then(|d| d.close.as_ref());
    let name = data
        .and_then(|d| d.fleets.first())
        .map(|e| e.title.as_str())
        .unwrap_or_default();
    let default_title = format!("{MODAL_TITLE_HEAD}{name}{MODAL_TITLE_TAIL}");
    let pick = |given: Option<&str>, fallback: &str| match given {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => fallback.to_string(),
    };
    (
        pick(close.map(|c| c.title.as_str()), &default_title),
        pick(close.map(|c| c.body.as_str()), MODAL_BODY),
    )
}

/// `text` broken into lines of at most `cols` display columns (plan §2.5): an ASCII word is never split (unless it
/// alone is wider than a line), a wide character may start a new line, and a break at spaces drops those spaces.
pub fn wrap(text: &str, cols: u16) -> Vec<String> {
    let cols = cols.max(1);
    let mut lines: Vec<String> = Vec::new();
    let mut line = String::new();
    let mut used = 0u16;
    let mut rest = text.chars().peekable();
    while let Some(ch) = rest.next() {
        if ch == '\n' {
            lines.push(std::mem::take(&mut line));
            used = 0;
            continue;
        }
        // One unit: a run of ASCII non-spaces (a word), a run of spaces, or a single other character.
        let mut unit = String::from(ch);
        let same = |n: char| match ch {
            ' ' => n == ' ',
            c if c.is_ascii() => n.is_ascii() && !n.is_whitespace(),
            _ => false,
        };
        while let Some(&n) = rest.peek() {
            if !same(n) {
                break;
            }
            unit.push(n);
            rest.next();
        }
        let w = text_cols(&unit);
        if used.saturating_add(w) <= cols {
            line.push_str(&unit);
            used = used.saturating_add(w);
            continue;
        }
        // A run of spaces where the line ends is dropped, not carried over.
        if ch == ' ' {
            lines.push(std::mem::take(&mut line));
            used = 0;
            continue;
        }
        if !line.is_empty() {
            while line.ends_with(' ') {
                line.pop();
            }
            lines.push(std::mem::take(&mut line));
            used = 0;
            if w <= cols {
                line.push_str(&unit);
                used = w;
                continue;
            }
        }
        // Wider than a whole line: split it character by character.
        for c in unit.chars() {
            let cw = text_cols(&c.to_string());
            if used.saturating_add(cw) > cols && !line.is_empty() {
                lines.push(std::mem::take(&mut line));
                used = 0;
            }
            line.push(c);
            used = used.saturating_add(cw);
        }
    }
    lines.push(line);
    while lines.len() > 1 && lines.last().is_some_and(|l| l.is_empty()) {
        lines.pop();
    }
    lines
}

/// The box's width (plan §2.5): `min(60, cols − 4)`, so the box never touches the screen's edges.
fn modal_w(cols: u16) -> u16 {
    MODAL_MAX_W.min(cols.saturating_sub(4))
}

/// The box's rectangle (plan §2.5): `body_lines` body rows plus the title, two blank rows, the hint and the button
/// row, two borders — centered in the screen above the status row, which it never covers.
pub fn modal_rect(cols: u16, rows: u16, body_lines: u16) -> LRect {
    let avail = rows.saturating_sub(1);
    let w = modal_w(cols);
    let h = body_lines.saturating_add(7).min(avail);
    LRect {
        x: cols.saturating_sub(w) / 2,
        y: avail.saturating_sub(h) / 2,
        w,
        h,
    }
}

/// The box and its body lines as drawn: the body wrapped to the box's inner width and cut to what still fits above
/// the status row.
fn modal_body(side: &Side, cols: u16, rows: u16) -> (LRect, Vec<String>) {
    let (_, body) = modal_text(side);
    let w = modal_w(cols);
    let mut lines = if w > 6 {
        wrap(&body, w - 4)
    } else {
        Vec::new()
    };
    let avail = rows.saturating_sub(1);
    lines.truncate(usize::from(avail.saturating_sub(7)));
    (modal_rect(cols, rows, lines.len() as u16), lines)
}

/// Where a press lands while the close confirmation box is open (plan §2.5): the `[ 关闭 fleet ]` button confirms,
/// every other place (the `[ 取消 ]` button, the title, the body, outside the box, the status row) cancels.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModalHit {
    Confirm,
    Cancel,
}

/// The two buttons on the box's last inner row, right-aligned: `[ 取消 ]` then `[ 关闭 fleet ]`; `None` when the box
/// is too narrow to hold both (a press then always cancels).
fn modal_buttons(side: &Side, r: LRect) -> Option<(LRect, LRect)> {
    if r.w < 8 || r.h < 8 {
        return None;
    }
    let cw = text_cols(modal_confirm_label(side));
    let kw = text_cols(MODAL_CANCEL);
    let by = r.y + r.h - 2;
    let cx = r.x + r.w - 2 - cw;
    let kx = cx.checked_sub(1 + kw)?;
    if kx < r.x + 2 {
        return None;
    }
    Some((
        LRect {
            x: kx,
            y: by,
            w: kw,
            h: 1,
        },
        LRect {
            x: cx,
            y: by,
            w: cw,
            h: 1,
        },
    ))
}

/// The close confirmation box (plan §2.5): a `min(60, cols − 4)` grey box centered above the status row, its inside
/// cleared first, the title in bold white, the wrapped body in white, the hint in grey and the two right-aligned
/// buttons (`[ 取消 ]` white on dark grey, `[ 关闭 fleet ]` bold white on red). Too small a screen draws nothing.
pub fn draw_modal(buf: &mut Buffer, side: &Side, cols: u16, rows: u16) {
    let (r, body) = modal_body(side, cols, rows);
    if r.w < 8 || r.h < 8 {
        return;
    }
    let (title, _) = modal_text(side);
    let iw = r.w - 4;
    clear_rect(buf, r);
    let bar = MODAL_H.to_string().repeat(usize::from(r.w - 2));
    let edge = Color::Gray;
    put(buf, r.x, r.y, 1, &MODAL_TL.to_string(), edge, false);
    put(buf, r.x + r.w - 1, r.y, 1, &MODAL_TR.to_string(), edge, false);
    put(buf, r.x, r.y + r.h - 1, 1, &MODAL_BL.to_string(), edge, false);
    put(buf, r.x + r.w - 1, r.y + r.h - 1, 1, &MODAL_BR.to_string(), edge, false);
    put(buf, r.x + 1, r.y, r.w - 2, &bar, edge, false);
    put(buf, r.x + 1, r.y + r.h - 1, r.w - 2, &bar, edge, false);
    for y in r.y + 1..r.y + r.h - 1 {
        put(buf, r.x, y, 1, &MODAL_V.to_string(), edge, false);
        put(buf, r.x + r.w - 1, y, 1, &MODAL_V.to_string(), edge, false);
    }
    let text_x = r.x + 2;
    put(buf, text_x, r.y + 1, iw, &ellipsize(&title, iw), Color::White, true);
    for (i, line) in body.iter().enumerate() {
        put(buf, text_x, r.y + 3 + i as u16, iw, line, Color::White, false);
    }
    let hint_y = r.y + 4 + body.len() as u16;
    put(buf, text_x, hint_y, iw, &ellipsize(MODAL_HINT, iw), Color::Gray, false);
    if let Some((cancel, confirm)) = modal_buttons(side, r) {
        put(buf, cancel.x, cancel.y, cancel.w, MODAL_CANCEL, Color::White, false);
        fill_bg(buf, cancel.x, cancel.y, cancel.w, Color::DarkGray);
        let label = modal_confirm_label(side);
        put(buf, confirm.x, confirm.y, confirm.w, label, Color::White, true);
        fill_bg(buf, confirm.x, confirm.y, confirm.w, Color::Red);
    }
}

/// [`modal_hit`]: the `[ 关闭 fleet ]` button confirms, everything else cancels.
pub fn modal_hit(side: &Side, cols: u16, rows: u16, x: u16, y: u16) -> ModalHit {
    let (r, _) = modal_body(side, cols, rows);
    match modal_buttons(side, r) {
        Some((_, confirm)) if y == confirm.y && x >= confirm.x && x < confirm.x + confirm.w => {
            ModalHit::Confirm
        }
        _ => ModalHit::Cancel,
    }
}

/// Reset every cell of `r` (the box's own cells and its inside), so no pane character shows through it.
fn clear_rect(buf: &mut Buffer, r: LRect) {
    for y in r.y..r.y.saturating_add(r.h) {
        for x in r.x..r.x.saturating_add(r.w) {
            if let Some(c) = buf.cell_mut((x, y)) {
                c.reset();
            }
        }
    }
}

/// Set the background of `w` cells from (`x`, `y`) without touching their symbols or foregrounds.
fn fill_bg(buf: &mut Buffer, x: u16, y: u16, w: u16, bg: Color) {
    for cx in x..x.saturating_add(w) {
        if let Some(c) = buf.cell_mut((cx, y)) {
            c.set_bg(bg);
        }
    }
}

/// The state dot's colour (plan §2.2): running light green, paused / stopping yellow, everything else (reclaimed,
/// unknown) grey.
fn dot_color(state: &str) -> Color {
    match state {
        "running" => Color::LightGreen,
        "paused" | "stopping" => Color::Yellow,
        // Plan §13.3: a fleet the sidebar closed is grey, like a reclaimed one.
        "closed" => Color::Gray,
        _ => Color::Gray,
    }
}

/// `text` cut to `cols` columns with a trailing `…` (the subtitle lines and a title without a separator).
fn ellipsize(text: &str, cols: u16) -> String {
    if text_cols(text) <= cols {
        return text.to_string();
    }
    format!("{}…", prefix_cols(text, cols.saturating_sub(1)))
}

/// The longest prefix of `text` that is at most `budget` display columns wide (never splits a wide character).
fn prefix_cols(text: &str, budget: u16) -> String {
    let mut out = String::new();
    let mut used = 0u16;
    for ch in text.chars() {
        let w = u16::from(cell_width(ch));
        if used + w > budget {
            break;
        }
        out.push(ch);
        used += w;
    }
    out
}

/// Write `text` at (`x`, `y`), at most `w` columns, one cell per character (a wide character's second cell is
/// reset, so nothing stale shows through). Foreground and bold only: the background is painted afterwards, so a
/// selected card's bar covers the wide characters' second cells too.
fn put(buf: &mut Buffer, x: u16, y: u16, w: u16, text: &str, fg: Color, bold: bool) {
    if w == 0 || y >= buf.area.height {
        return;
    }
    let end = x.saturating_add(w);
    let mut cx = x;
    let style = Style::new().fg(fg).add_modifier(if bold {
        Modifier::BOLD
    } else {
        Modifier::empty()
    });
    for ch in text.chars() {
        let cw = u16::from(cell_width(ch));
        if cw == 0 {
            continue;
        }
        if cx + cw > end {
            break;
        }
        if let Some(c) = buf.cell_mut((cx, y)) {
            c.reset();
            c.set_symbol(&ch.to_string());
            c.set_style(style);
        }
        for k in 1..cw {
            if let Some(c) = buf.cell_mut((cx + k, y)) {
                c.reset();
            }
        }
        cx += cw;
    }
}

/// Set the background of `rows` × `w` cells from (`0`, `y0`) without touching their symbols or foregrounds.
fn paint_bg(buf: &mut Buffer, y0: u16, rows: u16, w: u16, bg: Color) {
    for y in y0..y0 + rows {
        for x in 0..w.min(buf.area.width) {
            if let Some(c) = buf.cell_mut((x, y)) {
                c.set_bg(bg);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::render::draw::text_cols;
    use ratatui::buffer::Buffer;
    use ratatui::layout::Rect;
    use ratatui::style::{Color, Modifier, Style};

    /// The kv `sidebar` value of plan §4.5: one fleet, the root.
    const ONE: &str = r#"{"v":1,"current":"fleet-20261029-231820-ab12cd34","fleets":[{"id":"fleet-20261029-231820-ab12cd34","title":"fleetmux-demo","state":"running","line1":"运行中 · 监工+2 worker（1忙）","line2":"~/.fleetmux/fleets/fmx-demo"}]}"#;

    fn fleet(id: &str, title: &str, state: &str) -> String {
        format!(
            r#"{{"id":"{id}","title":"{title}","state":"{state}","line1":"运行中 · 监工+1 worker（0忙）","line2":"~/.fleetmux/fleets/fmx-demo"}}"#
        )
    }

    fn doc(current: &str, fleets: &[String]) -> String {
        format!(
            r#"{{"v":1,"current":"{current}","fleets":[{}]}}"#,
            fleets.join(",")
        )
    }

    fn shown(json: &str) -> Side {
        Side {
            data: Some(parse(json).expect(json)),
            hidden: false,
            ..Side::default()
        }
    }

    fn buf() -> Buffer {
        Buffer::empty(Rect::new(0, 0, 240, 60))
    }

    fn row_text(buf: &Buffer, y: u16, upto: u16) -> String {
        (0..upto).map(|x| buf[(x, y)].symbol().to_string()).collect()
    }

    #[test]
    fn parse_accepts_one_fleet() {
        let d = parse(ONE).expect("ONE must parse");
        assert_eq!(d.current, "fleet-20261029-231820-ab12cd34", "{ONE}");
        assert_eq!(d.fleets.len(), 1, "{ONE}");
        let e = &d.fleets[0];
        assert_eq!(e.id, "fleet-20261029-231820-ab12cd34", "{ONE}");
        assert_eq!(e.title, "fleetmux-demo", "{ONE}");
        assert_eq!(e.state, "running", "{ONE}");
        assert_eq!(e.line1, "运行中 · 监工+2 worker（1忙）", "{ONE}");
        assert_eq!(e.line2, "~/.fleetmux/fleets/fmx-demo", "{ONE}");
        // Unknown keys are ignored (S2 adds `close` to the same value).
        let with_close = ONE.replace(r#""fleets":"#, r#""close":{"title":"x"},"fleets":"#);
        assert!(parse(&with_close).is_ok(), "{with_close}");
    }

    #[test]
    fn parse_rejects_bad_version_missing_keys_unknown_current_and_17_fleets() {
        let good = doc("f1", &[fleet("f1", "demo", "running")]);
        assert!(parse(&good).is_ok(), "{good}");
        let bad_version = good.replace(r#""v":1"#, r#""v":2"#);
        assert!(parse(&bad_version).is_err(), "{bad_version}");
        let no_v = r#"{"current":"f1","fleets":[{"id":"f1","title":"demo","state":"running","line1":"x","line2":"y"}]}"#;
        assert!(parse(no_v).is_err(), "{no_v}");
        let missing = r#"{"v":1,"current":"f1","fleets":[{"id":"f1","title":"demo","state":"running","line1":"x"}]}"#;
        assert!(parse(missing).is_err(), "{missing}");
        let not_a_string = r#"{"v":1,"current":"f1","fleets":[{"id":"f1","title":"demo","state":"running","line1":"x","line2":7}]}"#;
        assert!(parse(not_a_string).is_err(), "{not_a_string}");
        let too_long = doc("f1", &[fleet("f1", &"x".repeat(201), "running")]);
        assert!(parse(&too_long).is_err(), "a 201-character value must be rejected");
        let unknown_current = doc("f2", &[fleet("f1", "demo", "running")]);
        assert!(parse(&unknown_current).is_err(), "{unknown_current}");
        let many: Vec<String> = (0..17)
            .map(|i| fleet(&format!("f{i}"), "demo", "running"))
            .collect();
        let seventeen = doc("f0", &many);
        assert!(
            parse(&seventeen).is_err(),
            "17 fleets must be rejected ({})",
            seventeen.len()
        );
        assert!(
            parse(&doc("f0", &many[..16])).is_ok(),
            "16 fleets must be accepted"
        );
        assert!(parse(r#"{"v":1,"current":"f1","fleets":[]}"#).is_err());
        assert!(parse("[]").is_err());
        assert!(parse("not json").is_err());
    }

    #[test]
    fn width_is_0_rail_or_full() {
        assert_eq!(SIDEBAR_W, 34);
        assert_eq!(RAIL_W, 1);
        assert_eq!(width(&Side::default()), 0, "no kv `sidebar` means no sidebar");
        let full = shown(ONE);
        assert_eq!(width(&full), SIDEBAR_W);
        let rail = Side {
            data: Some(parse(ONE).expect(ONE)),
            hidden: true,
            ..Side::default()
        };
        assert_eq!(width(&rail), 0, "the hidden sidebar is 0 columns wide");
    }

    #[test]
    fn fit_title_keeps_short_titles() {
        assert_eq!(fit_title("fleetmux-demo", 29), "fleetmux-demo");
        let exact = "a".repeat(29);
        assert_eq!(fit_title(&exact, 29), exact);
        assert_eq!(fit_title("", 29), "");
        let short_wide = "子 fleet M0";
        assert_eq!(fit_title(short_wide, 29), short_wide);
    }

    #[test]
    fn fit_title_keeps_the_last_segment_and_squeezes_the_head() {
        let a = fit_title("fleetmux-demo › worker-1 › 子 fleet M0", 29);
        assert_eq!(a, "fleetmux-demo… › 子 fleet M0", "got {a:?}");
        assert!(
            text_cols(&a) <= 29,
            "{a:?} is {} columns, not ≤ 29",
            text_cols(&a)
        );
        let b = fit_title("fleetmux-demo › … › M0 › 子 fleet M0.1", 29);
        assert_eq!(b, "fleetmux-dem… › 子 fleet M0.1", "got {b:?}");
        assert!(
            text_cols(&b) <= 29,
            "{b:?} is {} columns, not ≤ 29",
            text_cols(&b)
        );
        // No room left for a head: fall back to tail truncation.
        let c = fit_title("abcdefghij › 子 fleet M0.1", 10);
        assert!(
            text_cols(&c) <= 10,
            "{c:?} is {} columns, not ≤ 10",
            text_cols(&c)
        );
        assert!(c.ends_with('…'), "got {c:?}");
    }

    #[test]
    fn fit_title_cuts_the_tail_without_separator() {
        let f = fit_title("a-very-long-fleet-name-with-no-separator-at-all", 29);
        assert_eq!(f, "a-very-long-fleet-name-with-…", "got {f:?}");
        assert_eq!(text_cols(&f), 29, "got {f:?}");
        // Wide characters are never split.
        let wide = "侧".repeat(20);
        let w = fit_title(&wide, 29);
        assert!(
            text_cols(&w) <= 29,
            "{w:?} is {} columns, not ≤ 29",
            text_cols(&w)
        );
        assert_eq!(
            w.chars().filter(|c| *c == '侧').count(),
            14,
            "got {w:?}"
        );
    }

    #[test]
    fn hit_maps_hide_cards_rail_and_gutter() {
        let s = shown(ONE);
        for x in 0..=2 {
            assert_eq!(hit(&s, SIDEBAR_W, 20, 2, x, 0), SideHit::Hide, "column {x}");
        }
        assert_eq!(hit(&s, SIDEBAR_W, 20, 2, 29, 0), SideHit::Nothing, "row 0 right of the arrows");
        assert_eq!(hit(&s, SIDEBAR_W, 20, 2, 5, 2), SideHit::Pick(0));
        assert_eq!(hit(&s, SIDEBAR_W, 20, 2, 0, 4), SideHit::Pick(0));
        assert_eq!(
            hit(&s, SIDEBAR_W, 20, 2, 33, 2),
            SideHit::Nothing,
            "the gutter column never hits"
        );
        assert_eq!(
            hit(&s, SIDEBAR_W, 20, 2, 5, 5),
            SideHit::Nothing,
            "the blank row between cards"
        );
        assert_eq!(
            hit(&s, SIDEBAR_W, 20, 2, 5, 6),
            SideHit::Nothing,
            "no second card is drawn"
        );
        let two = shown(&doc(
            "f1",
            &[fleet("f1", "a", "running"), fleet("f2", "b", "reclaimed")],
        ));
        assert_eq!(hit(&two, SIDEBAR_W, 20, 2, 5, 6), SideHit::Pick(1));
        assert_eq!(hit(&two, SIDEBAR_W, 20, 2, 5, 8), SideHit::Pick(1));
        assert_eq!(hit(&two, SIDEBAR_W, 20, 2, 5, 9), SideHit::Nothing);
        assert_eq!(hit(&s, TOOLBAR_W, 20, 2, 1, 0), SideHit::Show);
        assert_eq!(hit(&s, TOOLBAR_W, 20, 2, 0, 10), SideHit::Nothing);
        assert_eq!(hit(&s, TOOLBAR_W, 20, 2, 1, 10), SideHit::Nothing);
        assert_eq!(
            hit(&s, TOOLBAR_W, 20, 2, 0, 20),
            SideHit::Nothing,
            "below the content rows"
        );
        assert_eq!(hit(&s, 0, 20, 2, 0, 10), SideHit::Nothing);
    }

    #[test]
    fn draw_selected_card_is_a_blue_bar_with_green_dot() {
        let mut b = buf();
        draw(&mut b, &shown(ONE), SIDEBAR_W, 59, 2);
        for y in 2..=4 {
            for x in 0..=32 {
                assert_eq!(
                    b[(x, y)].bg,
                    Color::Blue,
                    "card cell ({x},{y}) must be the blue bar"
                );
            }
            assert_eq!(
                b[(33, y)].bg,
                Color::Reset,
                "the gutter column (33,{y}) stays blank"
            );
        }
        assert_eq!(b[(1, 0)].symbol(), "◫", "the toolbar toggle sits at column 1");
        assert_eq!(b[(1, 0)].fg, Color::White, "the toolbar toggle is white");
        assert_eq!(b[(1, 2)].symbol(), "●", "the state dot sits at column 1");
        assert_eq!(b[(1, 2)].fg, Color::LightGreen, "running is light green");
        assert_eq!(b[(3, 2)].symbol(), "f", "the title starts at column 3");
        assert_eq!(b[(3, 2)].fg, Color::White, "the title is white");
        assert!(
            b[(3, 2)].modifier.contains(Modifier::BOLD),
            "the title is bold"
        );
        assert_eq!(b[(3, 3)].fg, Color::White, "a selected card's lines are white");
        assert!(
            row_text(&b, 3, 32).contains('运'),
            "line1 is on the card's second row: {:?}",
            row_text(&b, 3, 32)
        );
    }

    #[test]
    fn draw_unselected_card_has_grey_lines_and_no_background() {
        let json = doc(
            "f1",
            &[fleet("f1", "first", "running"), fleet("f2", "second", "reclaimed")],
        );
        let mut b = buf();
        draw(&mut b, &shown(&json), SIDEBAR_W, 59, 2);
        for y in 6..=8 {
            for x in 0..=32 {
                assert_eq!(
                    b[(x, y)].bg,
                    Color::Reset,
                    "unselected card cell ({x},{y}) has no background"
                );
            }
        }
        assert_eq!(b[(1, 6)].symbol(), "●");
        assert_eq!(b[(1, 6)].fg, Color::Gray, "a reclaimed fleet's dot is grey");
        assert_eq!(b[(3, 6)].fg, Color::White, "an unselected title is still white");
        assert_eq!(b[(3, 7)].fg, Color::Gray, "an unselected card's lines are grey");
        assert_eq!(b[(1, 2)].fg, Color::LightGreen, "the selected card is the first one");
        assert_eq!(b[(3, 2)].symbol(), "f", "the first card's title");
    }

    #[test]
    fn draw_rail_has_the_chevron() {
        let side = Side {
            data: Some(parse(ONE).expect(ONE)),
            hidden: true,
            ..Side::default()
        };
        let mut b = buf();
        draw(&mut b, &side, TOOLBAR_W, 59, 2);
        let row = row_text(&b, 0, TOOLBAR_W);
        assert_eq!(
            b[(1, 0)].symbol(),
            "◫",
            "the hidden toolbar keeps the toggle at column 1, row 0 is {row:?}"
        );
        assert_eq!(b[(1, 0)].fg, Color::White, "the hidden toggle is white");
        assert!(
            b[(1, 0)].modifier.contains(Modifier::BOLD),
            "the hidden toggle is bold"
        );
        assert_eq!(b[(1, 0)].bg, Color::DarkGray, "the toggle sits on dark grey");
        assert_eq!(b[(4, 0)].symbol(), "+", "the new-bash + is at column 4, row 0 is {row:?}");
        assert_eq!(b[(7, 0)].symbol(), "↺", "the reopen ↺ is at column 7, row 0 is {row:?}");
        assert_eq!(b[(10, 0)].symbol(), "←", "the prev arrow is at column 10, row 0 is {row:?}");
        assert_eq!(b[(13, 0)].symbol(), "→", "the next arrow is at column 13, row 0 is {row:?}");
        for y in 1..=4 {
            for x in 0..TOOLBAR_W {
                let cell = &b[(x, y)];
                assert_eq!(
                    cell.symbol(),
                    " ",
                    "hidden row {y} column {x} is blank, row is {:?}",
                    row_text(&b, y, TOOLBAR_W)
                );
                assert_eq!(
                    cell.bg,
                    Color::Reset,
                    "hidden row {y} column {x} has no background"
                );
            }
        }
        assert_eq!(b[(15, 0)].symbol(), " ", "the hidden toolbar is 15 columns wide");
        assert_eq!(b[(15, 0)].bg, Color::Reset, "column 15 is not the hidden toolbar");
    }

    /// Like cmux: cards that do not fit are scrolled to, never counted — no `… 还有 N 个` line, only whole cards.
    #[test]
    fn draw_overflow_has_no_more_line() {
        let fleets: Vec<String> = (0..5)
            .map(|i| fleet(&format!("f{i}"), &format!("fleet {i}"), "running"))
            .collect();
        let json = doc("f0", &fleets);
        let mut b = buf();
        draw(&mut b, &shown(&json), SIDEBAR_W, 20, 2);
        assert_eq!(b[(3, 10)].symbol(), "f", "the third card starts on row 10");
        for y in 0..20 {
            let t = row_text(&b, y, SIDEBAR_W);
            assert!(!t.contains("还有") && !t.contains('…'), "row {y} has an overflow line: {t:?}");
        }
        assert_eq!(b[(3, 14)].symbol(), " ", "the fourth card is not drawn");
    }

    /// Five fleet cards, `current` = `f{sel}`; 20 content rows from row 2 fit three of them.
    fn five(sel: usize) -> Side {
        let fleets: Vec<String> = (0..5)
            .map(|i| fleet(&format!("f{i}"), &format!("fleet {i}"), "running"))
            .collect();
        shown(&doc(&format!("f{sel}"), &fleets))
    }

    /// The wheel moves the card list one card per step and stops at both ends (cmux): five cards, three fit, so
    /// the first card drawn is 0..=2.
    #[test]
    fn wheel_scrolls_one_card_per_step_and_stops_at_the_ends() {
        let mut s = five(0);
        assert_eq!(first_card(&s, 20, 2), 0);
        assert!(scroll_cards(&mut s, 1, 20, 2), "one step down moves");
        assert_eq!(first_card(&s, 20, 2), 1);
        assert!(scroll_cards(&mut s, 1, 20, 2));
        assert_eq!(first_card(&s, 20, 2), 2, "the last three cards");
        assert!(!scroll_cards(&mut s, 1, 20, 2), "past the end does not move");
        assert_eq!(first_card(&s, 20, 2), 2);
        assert!(!scroll_cards(&mut s, 5, 20, 2), "a big step stops at the end too");
        assert_eq!(first_card(&s, 20, 2), 2);
        assert!(scroll_cards(&mut s, -1, 20, 2));
        assert_eq!(first_card(&s, 20, 2), 1);
        assert!(scroll_cards(&mut s, -5, 20, 2), "a big step up stops at the top");
        assert_eq!(first_card(&s, 20, 2), 0);
        assert!(!scroll_cards(&mut s, -1, 20, 2), "past the top does not move");

        let mut fits = shown(ONE);
        assert!(!scroll_cards(&mut fits, 1, 20, 2), "nothing to scroll when every card fits");
        assert_eq!(first_card(&fits, 20, 2), 0);
    }

    /// A scrolled list draws from the first card it shows, at row `top`, and the hit test returns the merged
    /// index of the card drawn there (cmux: a click picks what is under the pointer).
    #[test]
    fn draw_and_hit_follow_the_scroll_offset() {
        let mut s = five(0);
        scroll_cards(&mut s, 2, 20, 2);
        let mut b = buf();
        draw(&mut b, &s, SIDEBAR_W, 20, 2);
        assert!(span_text(&b, 2, 3, 20).starts_with("fleet 2"), "row 2: {:?}", row_text(&b, 2, SIDEBAR_W));
        assert!(span_text(&b, 6, 3, 20).starts_with("fleet 3"), "row 6: {:?}", row_text(&b, 6, SIDEBAR_W));
        assert!(span_text(&b, 10, 3, 20).starts_with("fleet 4"), "row 10: {:?}", row_text(&b, 10, SIDEBAR_W));
        assert_eq!(b[(3, 14)].symbol(), " ", "nothing after the last card");
        assert_eq!(hit(&s, SIDEBAR_W, 20, 2, 5, 2), SideHit::Pick(2));
        assert_eq!(hit(&s, SIDEBAR_W, 20, 2, 5, 11), SideHit::Pick(4));
        assert_eq!(hit(&s, SIDEBAR_W, 20, 2, 31, 6), SideHit::Close(3), "the `×` of the card drawn there");
        assert_eq!(hit(&s, SIDEBAR_W, 20, 2, 5, 14), SideHit::Nothing, "below the last card");
        assert!(blank_at(&s, SIDEBAR_W, 20, 2, 5, 15), "the blank area under the cards stays blank");
    }

    /// The selected card is scrolled into view when the selection or the card count changes; a call that sees the
    /// same pair keeps the user's scroll (a kv push, a focus click) and only clamps it.
    #[test]
    fn the_selected_card_scrolls_into_view() {
        let mut s = five(4);
        assert!(follow_selection(&mut s, 20, 2), "the last card is selected: scroll to it");
        assert_eq!(first_card(&s, 20, 2), 2);
        scroll_cards(&mut s, -2, 20, 2);
        assert!(!follow_selection(&mut s, 20, 2), "same selection, same cards: the user's scroll stays");
        assert_eq!(first_card(&s, 20, 2), 0);

        let mut s2 = five(3);
        s2.scroll = s.scroll;
        s2.followed = s.followed;
        assert!(follow_selection(&mut s2, 20, 2), "a new selection below the view scrolls down to it");
        assert_eq!(first_card(&s2, 20, 2), 1, "just far enough: card 3 is the last one drawn");

        let mut s3 = five(0);
        s3.scroll = 2;
        s3.followed = s2.followed;
        assert!(follow_selection(&mut s3, 20, 2), "a new selection above the view scrolls up to it");
        assert_eq!(first_card(&s3, 20, 2), 0);

        let mut b = bash_side(&["a-1", "a-2", "a-3", "a-4"]);
        b.bash_sel = Some("a-4".to_string());
        follow_selection(&mut b, 20, 2);
        assert_eq!(first_card(&b, 20, 2), 2, "a new bash card (the last one, selected) is scrolled into view");
        b.bash = vec!["a-1".into(), "a-4".into()];
        follow_selection(&mut b, 20, 2);
        assert_eq!(first_card(&b, 20, 2), 0, "two cards closed: the list is clamped, no blank slot");
        assert_eq!(b.scroll, 0, "and the stored offset with it");
    }

    /// A taller window shows more cards: the offset is clamped so no card slot is left blank.
    #[test]
    fn a_taller_window_clamps_the_offset() {
        let mut s = five(0);
        scroll_cards(&mut s, 2, 20, 2);
        assert_eq!(first_card(&s, 20, 2), 2);
        assert_eq!(first_card(&s, 24, 2), 1, "four fit: cards 1..=4");
        assert_eq!(first_card(&s, 40, 2), 0, "all five fit");
        let mut b = buf();
        draw(&mut b, &s, SIDEBAR_W, 24, 2);
        assert!(span_text(&b, 14, 3, 20).starts_with("fleet 4"), "row 14: {:?}", row_text(&b, 14, SIDEBAR_W));
        assert_eq!(hit(&s, SIDEBAR_W, 24, 2, 5, 2), SideHit::Pick(1));
    }

    /// The thumb exists only while the cards overflow; its height is the visible share of the cards (at least one
    /// row) of the track — the rows from the first card's first row to the last drawn card's last row — and it
    /// touches the track's top at offset 0 and its bottom at the last offset.
    #[test]
    fn scrollbar_thumb_only_on_overflow_sized_and_placed_by_the_offset() {
        let three: Vec<String> = (0..3)
            .map(|i| fleet(&format!("f{i}"), &format!("fleet {i}"), "running"))
            .collect();
        assert_eq!(scrollbar(&shown(&doc("f0", &three)), 20, 2), None, "three cards fit");
        assert_eq!(scrollbar(&Side::default(), 20, 2), None, "no kv sidebar");

        let mut s = five(0);
        // Track: rows 2..=12 (three cards of three rows and two gaps = 11 rows); thumb 11 × 3 / 5 = 6 rows.
        assert_eq!(scrollbar(&s, 20, 2), Some((2, 6)), "offset 0: the thumb starts at the track's top");
        scroll_cards(&mut s, 1, 20, 2);
        let (y, h) = scrollbar(&s, 20, 2).unwrap();
        assert_eq!(h, 6);
        assert!(y > 2 && y + h - 1 < 12, "offset 1: the thumb is between the ends, rows {y}..{}", y + h);
        scroll_cards(&mut s, 1, 20, 2);
        assert_eq!(scrollbar(&s, 20, 2), Some((7, 6)), "last offset: the thumb ends on the track's last row 12");

        s.hidden = true;
        assert_eq!(scrollbar(&s, 20, 2), None, "hidden: no scrollbar");

        let sixteen: Vec<String> = (0..16)
            .map(|i| fleet(&format!("f{i}"), &format!("fleet {i}"), "running"))
            .collect();
        let many = shown(&doc("f0", &sixteen));
        // 14 content rows from row 2 fit two cards: track 7 rows, 7 × 2 / 16 rounds to 0 — the thumb keeps one row.
        assert_eq!(scrollbar(&many, 14, 2), Some((2, 1)));
    }

    #[test]
    fn toolbar_draws_toggle_new_and_grey_arrows() {
        let mut b = buf();
        draw(&mut b, &shown(ONE), SIDEBAR_W, 59, 2);
        let row = row_text(&b, 0, 14);
        assert_eq!(
            b[(1, 0)].symbol(),
            "◫",
            "the sidebar toggle sits at column 1, row 0 is {row:?}"
        );
        assert_eq!(b[(1, 0)].fg, Color::White, "the toggle is white, row 0 is {row:?}");
        assert_eq!(
            b[(4, 0)].symbol(),
            "+",
            "the new-bash button sits at column 4, row 0 is {row:?}"
        );
        assert_eq!(b[(4, 0)].fg, Color::White, "`+` is white, row 0 is {row:?}");
        assert_eq!(
            b[(7, 0)].symbol(),
            "↺",
            "the reopen ↺ sits at column 7, row 0 is {row:?}"
        );
        assert_eq!(
            b[(7, 0)].fg,
            Color::White,
            "`↺` is white, row 0 is {row:?}"
        );
        assert_eq!(
            b[(10, 0)].symbol(),
            "←",
            "the previous arrow sits at column 10, row 0 is {row:?}"
        );
        assert_eq!(
            b[(10, 0)].fg,
            Color::DarkGray,
            "one fleet has no previous item, row 0 is {row:?}"
        );
        assert_eq!(
            b[(13, 0)].symbol(),
            "→",
            "the next arrow sits at column 13, row 0 is {row:?}"
        );
        assert_eq!(
            b[(13, 0)].fg,
            Color::DarkGray,
            "one fleet has no next item, row 0 is {row:?}"
        );
        assert_eq!(row, " ◫  +  ↺  ←  →", "row 0 holds the toolbar only");
    }

    #[test]
    fn arrows_turn_white_with_neighbours() {
        let json = doc(
            "f1",
            &[
                fleet("f0", "first", "running"),
                fleet("f1", "second", "running"),
                fleet("f2", "third", "running"),
            ],
        );
        let mut b = buf();
        draw(&mut b, &shown(&json), SIDEBAR_W, 59, 2);
        let row = row_text(&b, 0, 14);
        assert_eq!(b[(10, 0)].symbol(), "←", "row 0 is {row:?}");
        assert_eq!(
            b[(10, 0)].fg,
            Color::White,
            "a previous fleet exists, so `←` is white; row 0 is {row:?}"
        );
        assert_eq!(b[(13, 0)].symbol(), "→", "row 0 is {row:?}");
        assert_eq!(
            b[(13, 0)].fg,
            Color::White,
            "a next fleet exists, so `→` is white; row 0 is {row:?}"
        );
    }

    #[test]
    fn hit_maps_the_toolbar_buttons() {
        let s = shown(ONE);
        for x in 0..=2 {
            assert_eq!(
                hit(&s, SIDEBAR_W, 20, 2, x, 0),
                SideHit::Hide,
                "column {x} on row 0 is the toggle"
            );
        }
        for x in 3..=5 {
            assert_eq!(
                hit(&s, SIDEBAR_W, 20, 2, x, 0),
                SideHit::NewBash,
                "column {x} on row 0 is `+`"
            );
        }
        for x in 6..=8 {
            assert_eq!(
                hit(&s, SIDEBAR_W, 20, 2, x, 0),
                SideHit::Reopen,
                "column {x} on row 0 is `↺`"
            );
        }
        for x in 9..=11 {
            assert_eq!(
                hit(&s, SIDEBAR_W, 20, 2, x, 0),
                SideHit::Prev,
                "column {x} on row 0 is `←`"
            );
        }
        for x in 12..=14 {
            assert_eq!(
                hit(&s, SIDEBAR_W, 20, 2, x, 0),
                SideHit::Next,
                "column {x} on row 0 is `→`"
            );
        }
        for x in 15..=33 {
            assert_eq!(
                hit(&s, SIDEBAR_W, 20, 2, x, 0),
                SideHit::Nothing,
                "column {x} on row 0 is neither button nor card"
            );
        }
    }

    #[test]
    fn card_close_button_is_drawn_and_hit() {
        let s = shown(ONE);
        let mut b = buf();
        draw(&mut b, &s, SIDEBAR_W, 59, 2);
        assert_eq!(
            b[(31, 2)].symbol(),
            "×",
            "the card's close button sits at column 31 on the card's first row"
        );
        assert_eq!(
            b[(31, 2)].fg,
            Color::White,
            "a selected card's `×` is white (row 2 is {:?})",
            row_text(&b, 2, 33)
        );
        assert_eq!(
            b[(31, 2)].bg,
            Color::Blue,
            "the `×` sits on the blue bar (row 2 is {:?})",
            row_text(&b, 2, 33)
        );
        for x in 30..=32 {
            assert_eq!(
                hit(&s, SIDEBAR_W, 20, 2, x, 2),
                SideHit::Close(0),
                "column {x} on the card's first row closes card 0"
            );
        }
        assert_eq!(
            hit(&s, SIDEBAR_W, 20, 2, 29, 2),
            SideHit::Pick(0),
            "column 29 on the card's first row is still the card"
        );
        assert_eq!(
            hit(&s, SIDEBAR_W, 20, 2, 31, 3),
            SideHit::Pick(0),
            "the `×` area only covers the card's first row"
        );
    }

    #[test]
    fn card_title_leaves_room_for_the_close_button() {
        let json = doc("f1", &[fleet("f1", "fleetmux-demo-with-a-very-long-name", "running")]);
        let mut b = buf();
        draw(&mut b, &shown(&json), SIDEBAR_W, 59, 2);
        let title: String = (3..=29).map(|x| b[(x, 2)].symbol().to_string()).collect();
        assert!(
            text_cols(&title) <= 27,
            "the title must leave room for `×` at column 31, got {title:?} ({} columns)",
            text_cols(&title)
        );
        assert!(title.ends_with('…'), "a squeezed title ends with `…`, got {title:?}");
        assert_eq!(
            b[(30, 2)].symbol(),
            " ",
            "column 30 stays blank between the title and `×`, row 2 is {:?}",
            row_text(&b, 2, 33)
        );
        assert_eq!(
            b[(31, 2)].symbol(),
            "×",
            "`×` is still at column 31, row 2 is {:?}",
            row_text(&b, 2, 33)
        );
    }

    #[test]
    fn draw_clears_stale_cells() {
        let mut b = buf();
        for (x, y) in [(5, 10), (33, 2), (0, 50), (20, 0)] {
            b.set_string(x, y, "X", Style::new());
        }
        draw(&mut b, &shown(ONE), SIDEBAR_W, 59, 2);
        for (x, y) in [(5, 10), (33, 2), (0, 50), (20, 0)] {
            assert_eq!(
                b[(x, y)].symbol(),
                " ",
                "stale cell ({x},{y}) must be cleared"
            );
        }
    }

    /// `ONE` with a `close` object inserted before `fleets` (the shape plan §12 has the daemon push).
    fn with_close(title: &str, body: &str) -> String {
        ONE.replace(
            r#""fleets":"#,
            &format!(r#""close":{{"title":"{title}","body":"{body}"}},"fleets":"#),
        )
    }

    /// The column of the `[` that opens a button whose first character is `first`, on the row it was drawn (a wide
    /// character's second cell is a blank, so only the character after the space is checked).
    fn button_at(b: &Buffer, first: char) -> (u16, u16) {
        for y in 0..60u16 {
            for x in 0..239u16 {
                let sym = |x: u16| b[(x, y)].symbol().to_string();
                if sym(x) == "[" && sym(x + 1) == " " && sym(x + 2) == first.to_string() {
                    return (x, y);
                }
            }
        }
        panic!("no `[ {first}…` button anywhere on the 240×60 screen");
    }

    #[test]
    fn parse_accepts_an_optional_close_text_and_rejects_a_bad_one() {
        let plain = parse(ONE).expect(ONE);
        assert!(
            plain.close.is_none(),
            "without a `close` key the modal text is the built-in default, got {:?}",
            plain.close
        );
        let good = with_close("关掉 demo？", "会停掉监工和 2 个 worker。");
        let d = parse(&good).expect(&good);
        let c = d
            .close
            .as_ref()
            .unwrap_or_else(|| panic!("`close` must be kept: {good}"));
        assert_eq!(c.title, "关掉 demo？", "{good}");
        assert_eq!(c.body, "会停掉监工和 2 个 worker。", "{good}");
        // A title-only `close` keeps the default body: the frozen `parse_accepts_one_fleet` relies on it.
        let title_only = ONE.replace(r#""fleets":"#, r#""close":{"title":"x"},"fleets":"#);
        let d = parse(&title_only).expect(&title_only);
        assert_eq!(
            d.close.expect("a title-only close is still a close").body,
            "",
            "{title_only}"
        );
        // 400 characters is the limit and is still accepted.
        let long_ok = ONE.replace(
            r#""fleets":"#,
            &format!(r#""close":{{"title":"{}"}},"fleets":"#, "x".repeat(400)),
        );
        assert!(parse(&long_ok).is_ok(), "a 400-character title must be accepted");
        for (what, bad) in [
            (
                "`close` is not an object",
                ONE.replace(r#""fleets":"#, r#""close":"x","fleets":"#),
            ),
            (
                "`close.title` is not a string",
                ONE.replace(r#""fleets":"#, r#""close":{"title":7},"fleets":"#),
            ),
            (
                "`close.body` is not a string",
                ONE.replace(r#""fleets":"#, r#""close":{"body":[]},"fleets":"#),
            ),
            (
                "`close.title` is 401 characters",
                ONE.replace(
                    r#""fleets":"#,
                    &format!(r#""close":{{"title":"{}"}},"fleets":"#, "x".repeat(401)),
                ),
            ),
            (
                "`close.body` is 401 characters",
                ONE.replace(
                    r#""fleets":"#,
                    &format!(r#""close":{{"body":"{}"}},"fleets":"#, "x".repeat(401)),
                ),
            ),
        ] {
            assert!(parse(&bad).is_err(), "{what} must be rejected: {bad}");
        }
    }

    #[test]
    fn wrap_keeps_ascii_words_whole() {
        let text = "the quick brown fox jumps over the lazy dog";
        let lines = wrap(text, 12);
        for l in &lines {
            assert!(
                text_cols(l) <= 12,
                "{l:?} is {} columns, not ≤ 12 (lines {lines:?})",
                text_cols(l)
            );
        }
        assert_eq!(
            lines.join(" "),
            text,
            "an ASCII text only breaks at spaces, got {lines:?}"
        );
        let wide = "侧".repeat(9);
        let lines = wrap(&wide, 5);
        for l in &lines {
            assert!(
                text_cols(l) <= 5,
                "{l:?} is {} columns, not ≤ 5 (lines {lines:?})",
                text_cols(l)
            );
        }
        assert_eq!(
            lines.first().map(String::as_str),
            Some("侧侧"),
            "5 columns hold two wide characters, got {lines:?}"
        );
        assert_eq!(
            lines.join(""),
            wide,
            "a CJK text may break between characters, got {lines:?}"
        );
        let word = "x".repeat(20);
        let lines = wrap(&word, 8);
        for l in &lines {
            assert!(
                text_cols(l) <= 8,
                "{l:?} is {} columns, not ≤ 8 (lines {lines:?})",
                text_cols(l)
            );
        }
        assert_eq!(
            lines.join(""),
            word,
            "a word wider than the box is split character by character, got {lines:?}"
        );
    }

    #[test]
    fn modal_uses_close_text_or_the_default() {
        let (title, body) = modal_text(&shown(ONE));
        assert_eq!(
            title, "关闭整个 fleet「fleetmux-demo」？",
            "the default title names the first card"
        );
        assert_eq!(
            body,
            "会停掉监工和全部 worker（含它的子 fleet）。正在做的活会中断。运行记录保留，之后可以在侧栏点 ↺ 恢复，接着原来的活。",
            "the default body is the §11.1 wording"
        );
        let json = with_close("关掉 demo？", "会停掉监工和 2 个 worker。");
        let (title, body) = modal_text(&shown(&json));
        assert_eq!(title, "关掉 demo？", "the daemon's `close.title` wins, json {json}");
        assert_eq!(
            body, "会停掉监工和 2 个 worker。",
            "the daemon's `close.body` wins, json {json}"
        );
    }

    #[test]
    fn modal_hit_confirm_button_and_cancel_elsewhere() {
        let side = shown(ONE);
        let (_, body) = modal_text(&side);
        let n = wrap(&body, 56).len() as u16;
        let r = modal_rect(240, 60, n);
        assert_eq!(r.w, 60, "the box is min(60, cols − 4) wide, got {r:?}");
        assert_eq!(r.x, 90, "a 240-column screen centers the box at column 90, got {r:?}");
        assert_eq!(r.h, n + 7, "{n} body lines + title/blank/hint/buttons + 2 borders, got {r:?}");
        assert_eq!(r.y, (59 - r.h) / 2, "the box centers above the status row, got {r:?}");
        assert!(
            r.y + r.h <= 59,
            "the box never covers the status row (0..=58), got {r:?}"
        );
        let mut b = buf();
        draw_modal(&mut b, &side, 240, 60);
        assert_eq!(b[(r.x, r.y)].symbol(), "╭", "the top-left corner, got {r:?}");
        assert_eq!(
            b[(r.x + r.w - 1, r.y + r.h - 1)].symbol(),
            "╯",
            "the bottom-right corner, got {r:?}"
        );
        assert_eq!(b[(r.x, r.y)].fg, Color::Gray, "the border is grey");
        assert_eq!(
            b[(0, 59)].symbol(),
            " ",
            "the status row stays empty (the modal is not drawn there)"
        );
        let (cx, cy) = button_at(&b, '关');
        assert_eq!(cy, r.y + r.h - 2, "the buttons sit on the box's last inner row, got {r:?}");
        assert_eq!(
            modal_hit(&side, 240, 60, cx, cy),
            ModalHit::Confirm,
            "`[ 关闭 fleet ]` at ({cx},{cy}) confirms"
        );
        assert_eq!(
            modal_hit(&side, 240, 60, cx + 6, cy),
            ModalHit::Confirm,
            "the whole `[ 关闭 fleet ]` button confirms, ({},{cy})",
            cx + 6
        );
        let (kx, ky) = button_at(&b, '取');
        assert_eq!(
            modal_hit(&side, 240, 60, kx, ky),
            ModalHit::Cancel,
            "`[ 取消 ]` at ({kx},{ky}) cancels"
        );
        assert_eq!(
            modal_hit(&side, 240, 60, r.x + 2, r.y + 1),
            ModalHit::Cancel,
            "a press on the title row cancels"
        );
        assert_eq!(
            modal_hit(&side, 240, 60, 0, 0),
            ModalHit::Cancel,
            "a press outside the box cancels"
        );
        assert_eq!(
            modal_hit(&side, 240, 60, 239, 59),
            ModalHit::Cancel,
            "a press on the status row cancels"
        );
    }

    /// `×` is only drawn on a card that can still be closed (plan §11.1): a reclaimed one has nothing left to
    /// close, and after a `stop_request` the button is gone too (the stop is already on its way).
    #[test]
    fn no_close_button_on_reclaimed_or_after_a_stop_request() {
        let json = doc("f1", &[fleet("f1", "fleetmux-demo", "reclaimed")]);
        let s = shown(&json);
        let mut b = buf();
        draw(&mut b, &s, SIDEBAR_W, 59, 2);
        assert_eq!(
            b[(31, 2)].symbol(),
            " ",
            "a reclaimed card draws no `×` at (31,2); row 2 is {:?}",
            row_text(&b, 2, 33)
        );
        assert_eq!(
            hit(&s, SIDEBAR_W, 20, 2, 31, 2),
            SideHit::Pick(0),
            "the `×` columns of a reclaimed card are the card itself"
        );

        for stop in [
            Stop::Requested,
            Stop::Accepted,
            Stop::Refused("no lifecycle".into()),
        ] {
            let mut s = shown(ONE);
            s.stop = stop.clone();
            let mut b = buf();
            draw(&mut b, &s, SIDEBAR_W, 59, 2);
            assert_eq!(
                b[(31, 2)].symbol(),
                " ",
                "a running card draws no `×` once the stop is {stop:?}; row 2 is {:?}",
                row_text(&b, 2, 33)
            );
            assert_eq!(
                hit(&s, SIDEBAR_W, 20, 2, 31, 2),
                SideHit::Pick(0),
                "the `×` columns of a card with a {stop:?} stop are the card itself"
            );
        }
    }

    /// §16.2: every position follows the actual width `sb` — the blue bar reaches `sb − 2`, column `sb − 1` stays
    /// blank as the gap to the panes, `×` sits at `sb − 3`, the title may use `sb − 7` columns and the subtitle
    /// `sb − 5` (24 columns: title 17, subtitle 19).
    #[test]
    fn draw_follows_the_actual_width_24_and_60() {
        // Both texts are longer than the widest card, so the title and the subtitle are squeezed at 24 and at 60.
        let title = "fleetmux-demo-with-a-very-long-name-that-never-fits-sixty-columns";
        let line = "运行中 · 监工+1 worker（0忙）".repeat(3);
        let json = format!(
            r#"{{"v":1,"current":"f1","fleets":[{{"id":"f1","title":"{title}","state":"running","line1":"{line}","line2":"{line}"}}]}}"#
        );
        for sb in [24u16, 60] {
            let mut b = buf();
            draw(&mut b, &shown(&json), sb, 59, 2);
            for y in 2..=4 {
                for x in 0..=sb - 2 {
                    assert_eq!(
                        b[(x, y)].bg,
                        Color::Blue,
                        "sb={sb}: the selected card's blue bar must reach column {} , but ({x},{y}) is not blue; row {y} is {:?}",
                        sb - 2,
                        row_text(&b, y, sb + 1)
                    );
                }
                assert_eq!(
                    b[(sb - 1, y)].bg,
                    Color::Reset,
                    "sb={sb}: the gap column {} must stay blank, but ({},{y}) is blue; row {y} is {:?}",
                    sb - 1,
                    sb - 1,
                    row_text(&b, y, sb + 1)
                );
            }
            assert_eq!(
                b[(sb - 3, 2)].symbol(),
                "×",
                "sb={sb}: `×` must sit at column {}, row 2 is {:?}",
                sb - 3,
                row_text(&b, 2, sb + 1)
            );
            assert_eq!(
                b[(sb - 2, 2)].symbol(),
                " ",
                "sb={sb}: column {} must stay blank between the title and `×`, row 2 is {:?}",
                sb - 2,
                row_text(&b, 2, sb + 1)
            );
            let title: String = (3..=sb - 5).map(|x| b[(x, 2)].symbol().to_string()).collect();
            assert_eq!(
                text_cols(&title),
                sb - 7,
                "sb={sb}: the title may use {} columns (3..={}), row 2 is {:?}",
                sb - 7,
                sb - 5,
                row_text(&b, 2, sb + 1)
            );
            assert!(
                title.ends_with('…'),
                "sb={sb}: a squeezed title ends with `…`, got {title:?}"
            );
            assert_eq!(
                b[(sb - 3, 3)].symbol(),
                "…",
                "sb={sb}: the subtitle fills {} columns up to column {}, row 3 is {:?}",
                sb - 5,
                sb - 3,
                row_text(&b, 3, sb + 1)
            );
            assert_eq!(
                b[(sb, 2)].symbol(),
                " ",
                "sb={sb}: nothing is drawn at column {sb} or right of it, row 2 is {:?}",
                row_text(&b, 2, sb + 2)
            );
        }
    }

    /// §16.2: the hit test uses the actual width too — the card body ends at `sb − 2`, the gap column `sb − 1`
    /// hits nothing, `×` is at `sb − 3` with its three-column click area, and columns past `sb` are not the
    /// sidebar's.
    #[test]
    fn hit_follows_the_actual_width() {
        let s = shown(ONE);
        for sb in [24u16, 60] {
            assert_eq!(
                hit(&s, sb, 20, 2, sb - 2, 3),
                SideHit::Pick(0),
                "sb={sb}: column {} is still the card body",
                sb - 2
            );
            assert_eq!(
                hit(&s, sb, 20, 2, sb - 1, 3),
                SideHit::Nothing,
                "sb={sb}: the gap column {} hits nothing",
                sb - 1
            );
            assert_eq!(
                hit(&s, sb, 20, 2, sb, 3),
                SideHit::Nothing,
                "sb={sb}: column {sb} is outside the sidebar"
            );
            for x in sb - 4..=sb - 2 {
                assert_eq!(
                    hit(&s, sb, 20, 2, x, 2),
                    SideHit::Close(0),
                    "sb={sb}: column {x} on the card's first row closes card 0"
                );
            }
            assert_eq!(
                hit(&s, sb, 20, 2, sb - 5, 2),
                SideHit::Pick(0),
                "sb={sb}: column {} on the card's first row is still the card",
                sb - 5
            );
            assert_eq!(
                hit(&s, sb, 20, 2, sb - 4, 3),
                SideHit::Pick(0),
                "sb={sb}: the `×` area only covers the card's first row, not ({} ,3)",
                sb - 4
            );
        }
        assert_eq!(
            hit(&s, 60, 20, 2, 33, 3),
            SideHit::Pick(0),
            "a 60-column sidebar's card still covers column 33"
        );
        assert_eq!(
            hit(&s, 24, 20, 2, 22, 2),
            SideHit::Close(0),
            "a 24-column sidebar puts `×` at column 21, so 22 closes card 0"
        );
        assert_eq!(
            hit(&s, TOOLBAR_W, 20, 2, 1, 0),
            SideHit::Show,
            "the hidden toolbar's toggle is at column 1"
        );
        assert_eq!(
            hit(&s, TOOLBAR_W, 20, 2, 0, 10),
            SideHit::Nothing,
            "below the hidden toolbar's row 0 nothing hits"
        );
        assert_eq!(
            hit(&s, TOOLBAR_W, 20, 2, 1, 10),
            SideHit::Nothing,
            "column 1 below row 0 is not the toggle"
        );
    }

    /// §16.3-2: `Side` keeps the dragged width and `width()` asks the layout for it while the sidebar is shown;
    /// hidden stays the one-column rail and no kv `sidebar` stays 0.
    #[test]
    fn width_uses_the_dragged_width_when_shown() {
        assert_eq!(
            Side::default().width,
            SIDEBAR_W,
            "a fresh sidebar starts at the default width {SIDEBAR_W}"
        );
        let mut s = shown(ONE);
        assert_eq!(
            width(&s),
            SIDEBAR_W,
            "a fresh shown sidebar asks the layout for {SIDEBAR_W}"
        );
        for w in [24u16, 44, 60] {
            s.width = w;
            assert_eq!(
                width(&s),
                w,
                "a shown sidebar asks the layout for its dragged width {w}"
            );
        }
        // A42-LINUX-SIDEBAR-ALIGN-03: a window whose header controls need a wider band raises the effective width
        // without losing the user's requested one; the floor never shrinks a wider request.
        s.width = 24;
        s.sidebar_min = 34;
        assert_eq!(
            width(&s),
            34,
            "the window's header floor did not raise a 24-column request"
        );
        s.width = 44;
        assert_eq!(width(&s), 44, "the floor shrank a 44-column request");
        // A floor above the widest *drag* width is still asked for as it is: capping it would split the header
        // boundary from the grid's divider. Whether it fits the content is the layout's own rule
        // (`sidebar_cols`), and a request can never be dragged past `SIDEBAR_MAX_W` (`width` stays the request).
        s.sidebar_min = 66;
        assert_eq!(
            width(&s),
            66,
            "a floor the controls need above the widest drag width was capped: the two boundaries would split"
        );
        s.width = 60;
        s.sidebar_min = 0;
        assert_eq!(
            width(&s),
            fleetmux_layout::SIDEBAR_MAX_W,
            "a drag above the widest sidebar is still capped"
        );
        // M81 RB-01/RB-02: the floor is a `u16` on the wire (`proto::view::SIDEBAR_MIN_MAX`), so 65535 columns is a
        // legal value. `width()` keeps it as it is (the drag range bounds the request, not the floor); the layout's
        // own content-minimum rule then decides it — hidden here, and the `sidebar + SIDEBAR_MIN_CONTENT` comparison
        // must not overflow (`65535 + 60` does not fit a `u16`).
        s.width = 24;
        s.sidebar_min = u16::MAX;
        assert_eq!(
            width(&s),
            u16::MAX,
            "the wire's largest floor was truncated instead of passed to the layout"
        );
        let mut wire = fleetmux_layout::Layout::new();
        wire.set_sidebar(width(&s));
        assert_eq!(
            wire.sidebar_cols(240),
            0,
            "a 65535-column band cannot fit a 240-column viewer and must be hidden"
        );
        s.sidebar_min = 0;
        s.hidden = true;
        assert_eq!(width(&s), 0, "a hidden sidebar stays 0 even with a floor");
        let mut hidden = shown(ONE);
        hidden.width = 44;
        hidden.hidden = true;
        assert_eq!(
            width(&hidden),
            0,
            "the hidden sidebar is 0 columns wide, whatever width was dragged"
        );
        assert_eq!(
            width(&Side::default()),
            0,
            "no kv `sidebar` still means no sidebar"
        );
    }

    /// §17.6-1: the toolbar moves right by `Side::inset` columns, the gui's traffic lights' place (plan §17.2:
    /// `◫ + ← →` at `k+1 / k+4 / k+7 / k+10`).
    #[test]
    fn toolbar_shifts_by_the_inset() {
        let mut side = shown(ONE);
        side.inset = 10;
        let sb = width(&side);
        let mut b = buf();
        draw(&mut b, &side, sb, 59, 2);
        let row = row_text(&b, 0, 24);
        assert_eq!(
            b[(11, 0)].symbol(),
            "◫",
            "with inset 10 the toggle sits at column 11, row 0 is {row:?}"
        );
        assert_eq!(
            b[(14, 0)].symbol(),
            "+",
            "with inset 10 the new-bash + sits at column 14, row 0 is {row:?}"
        );
        assert_eq!(
            b[(17, 0)].symbol(),
            "↺",
            "with inset 10 the reopen ↺ sits at column 17, row 0 is {row:?}"
        );
        assert_eq!(
            b[(20, 0)].symbol(),
            "←",
            "with inset 10 the prev arrow sits at column 20, row 0 is {row:?}"
        );
        assert_eq!(
            b[(0, 0)].symbol(),
            " ",
            "the inset columns stay blank (the traffic lights' place), row 0 is {row:?}"
        );
        assert_eq!(
            hit(&side, sb, 20, 2, 11, 0),
            SideHit::Hide,
            "with inset 10 the toggle's click area starts at column 11"
        );
        assert_eq!(
            hit(&side, sb, 20, 2, 20, 0),
            SideHit::Prev,
            "with inset 10 the prev arrow's click area is at column 20"
        );
    }

    /// §17.6-2: hiding the sidebar keeps the toolbar where it was — same columns, same glyphs, same colours
    /// (plan §17.2: the collapsed sidebar is the toolbar plus blank rows, no cards).
    #[test]
    fn hidden_keeps_the_toolbar_where_it_was() {
        let mut side = shown(ONE);
        side.inset = 10;
        let mut shown_buf = buf();
        draw(&mut shown_buf, &side, width(&side), 59, 2);
        let mut hidden = side.clone();
        hidden.hidden = true;
        let hidden_w = topbar_w(&hidden);
        assert_eq!(
            hidden_w,
            25,
            "the hidden topbar is inset 10 + TOOLBAR_W 15 = 25 columns wide"
        );
        let mut hidden_buf = buf();
        draw_topbar(&mut hidden_buf, &hidden, 0, topbar_w(&hidden), 240, false);
        for x in [11u16, 14, 17, 20, 23] {
            assert_eq!(
                hidden_buf[(x, 0)].symbol(),
                shown_buf[(x, 0)].symbol(),
                "column {x} on row 0 moves when the sidebar is hidden: shown {:?}, hidden {:?}",
                row_text(&shown_buf, 0, hidden_w),
                row_text(&hidden_buf, 0, hidden_w)
            );
            assert_eq!(
                hidden_buf[(x, 0)].fg,
                shown_buf[(x, 0)].fg,
                "column {x} on row 0 changes colour when the sidebar is hidden"
            );
        }
        for y in 1..=8 {
            for x in 0..hidden_w {
                assert_eq!(
                    hidden_buf[(x, y)].symbol(),
                    " ",
                    "hidden row {y} column {x} is not blank: {:?}",
                    row_text(&hidden_buf, y, hidden_w)
                );
                assert_eq!(
                    hidden_buf[(x, y)].bg,
                    Color::Reset,
                    "hidden row {y} column {x} still has a background"
                );
            }
        }
    }

    /// §17.6-3: a shown sidebar is never narrower than the toolbar it has to draw (plan §17.2:
    /// `max(SIDEBAR_MIN_W, inset + TOOLBAR_W)`).
    #[test]
    fn expanded_width_never_below_the_toolbar() {
        let mut side = shown(ONE);
        side.width = 24;
        assert_eq!(
            width(&side),
            24,
            "with inset 0 the 24-column minimum stays 24"
        );
        side.inset = 10;
        side.width = 34;
        assert_eq!(
            width(&side),
            34,
            "inset 10 + TOOLBAR_W 15 = 25 stays below the dragged 34"
        );
        side.width = 24;
        side.inset = 20;
        assert_eq!(
            width(&side),
            35,
            "inset 20 + TOOLBAR_W 15 = 35 beats the 24-column minimum"
        );
        side.hidden = true;
        assert_eq!(
            width(&side),
            0,
            "hidden with inset 20 the sidebar is 0 columns wide"
        );
    }

    /// A shown sidebar whose width is exactly `max(SIDEBAR_MIN_W, inset + TOOLBAR_W)` is still the shown one: it
    /// draws its cards and its `◫` hides (plan §17.2: every `inset` 0..=40 is legal, and a hidden sidebar of that
    /// width exists too — `Side::hidden` decides, the width alone may never turn a legal shown sidebar into the
    /// collapsed toolbar).
    #[test]
    fn a_shown_sidebar_at_the_minimum_width_is_never_read_as_hidden() {
        for inset in [12u16, 40] {
            let mut side = shown(ONE);
            side.inset = inset;
            side.width = 24;
            let sb = width(&side);
            let min = (inset + TOOLBAR_W).max(24);
            assert_eq!(
                sb, min,
                "inset {inset}: the shown sidebar is {min} columns wide, not {sb}"
            );
            let mut b = buf();
            draw(&mut b, &side, sb, 59, 2);
            let row2 = row_text(&b, 2, sb);
            assert_eq!(
                b[(1, 2)].symbol(),
                "●",
                "inset {inset}, width {sb}: the card's state dot is missing at (1,2), row 2 is {row2:?}"
            );
            assert_eq!(
                b[(3, 2)].symbol(),
                "f",
                "inset {inset}, width {sb}: the card title is missing at (3,2), row 2 is {row2:?}"
            );
            assert_eq!(
                b[(1 + inset, 0)].symbol(),
                "◫",
                "inset {inset}: the toggle is not at column {}, row 0 is {:?}",
                1 + inset,
                row_text(&b, 0, sb)
            );
            assert_eq!(
                hit(&side, sb, 20, 2, 1 + inset, 0),
                SideHit::Hide,
                "inset {inset}, width {sb}: the shown sidebar's ◫ must hide, not show"
            );
            assert_eq!(
                hit(&side, sb, 20, 2, 3, 2),
                SideHit::Pick(0),
                "inset {inset}, width {sb}: the card body is not a pick"
            );

            let mut hidden = side.clone();
            hidden.hidden = true;
            assert_eq!(
                width(&hidden),
                0,
                "inset {inset}: the hidden sidebar is 0 columns wide"
            );
            let mut hb = buf();
            draw(&mut hb, &hidden, width(&hidden), 59, 2);
            assert_eq!(
                hb[(1, 2)].symbol(),
                " ",
                "inset {inset}: the hidden sidebar still draws a card at (1,2), row 2 is {:?}",
                row_text(&hb, 2, sb)
            );
            assert_eq!(
                hit(&hidden, width(&hidden), 20, 2, 1 + inset, 0),
                SideHit::Show,
                "inset {inset}: the hidden sidebar's ◫ must show"
            );
        }
    }

    /// §20.6 / plan §20.2: the topbar's `↺` sits between `+` and the arrows and its click area is columns 6–8.
    #[test]
    fn toolbar_has_reopen_between_plus_and_the_arrows() {
        let side = shown(ONE);
        let mut b = buf();
        draw(&mut b, &side, SIDEBAR_W, 59, 2);
        let row = row_text(&b, 0, TOOLBAR_W);
        assert_eq!(
            b[(REOPEN_X, TOOL_ROW)].symbol(),
            "↺",
            "the reopen ↺ must sit at column {REOPEN_X}, row 0 is {row:?}"
        );
        assert_eq!(
            b[(REOPEN_X, TOOL_ROW)].fg,
            Color::White,
            "`↺` must be white, row 0 is {row:?}"
        );
        for x in 6..=8 {
            assert_eq!(
                hit(&side, SIDEBAR_W, 20, 2, x, 0),
                SideHit::Reopen,
                "column {x} on row 0 must be the reopen button, row 0 is {row:?}"
            );
        }
    }

    /// §20.6 / plan §20.4-3: `topbar_w` is `inset + TOOLBAR_W` with kv `sidebar`, 0 without it (shown or hidden).
    #[test]
    fn topbar_w_is_the_toolbar_plus_inset_or_0() {
        assert_eq!(
            topbar_w(&Side::default()),
            0,
            "no kv `sidebar` means no topbar"
        );
        let mut side = shown(ONE);
        assert_eq!(
            topbar_w(&side),
            TOOLBAR_W,
            "inset 0: the topbar left part is {TOOLBAR_W} columns wide"
        );
        side.inset = 10;
        assert_eq!(topbar_w(&side), 25, "inset 10 + TOOLBAR_W 15 = 25");
        side.hidden = true;
        assert_eq!(
            topbar_w(&side),
            25,
            "hidden the topbar left part is still inset 10 + 15 = 25"
        );
    }

    /// A26 rev5 (`CURRENT_REQUIREMENTS` rev 6 `utm_topbar`): a mirrored layout (Linux) keeps **no** in-grid topbar
    /// row — the native GTK headerbar is the one header row — while macOS keeps row 0 as the topbar.
    #[test]
    fn topbar_cols_is_0_with_a_native_header() {
        let side = shown(ONE);
        assert_eq!(
            topbar_cols(&side, false),
            topbar_w(&side),
            "macOS: the grid keeps row 0 as the topbar"
        );
        assert_eq!(topbar_cols(&side, false), TOOLBAR_W);
        assert_eq!(
            topbar_cols(&side, true),
            0,
            "Linux: the native headerbar is the topbar, so no grid row is reserved for it"
        );
        let mut hidden = shown(ONE);
        hidden.hidden = true;
        assert_eq!(
            topbar_cols(&hidden, true),
            0,
            "Linux: no topbar row with the sidebar collapsed either"
        );
        assert_eq!(
            topbar_cols(&Side::default(), true),
            0,
            "no kv `sidebar`: nothing to draw on either platform"
        );
    }

    /// §20.6 / plan §20.2: shown, the topbar puts `│` at `left` and the current fleet's title + line1 from
    /// `left + 2`; the left part belongs to [`draw`].
    #[test]
    fn topbar_draws_divider_and_the_current_fleet_title() {
        let side = shown(ONE);
        let mut b = buf();
        b[(20, 0)].set_symbol("X");
        draw_topbar(&mut b, &side, SIDEBAR_W, SIDEBAR_W, 240, false);
        let row = row_text(&b, 0, 80);
        assert_eq!(
            b[(34, 0)].symbol(),
            "│",
            "the divider must sit at column 34, row 0 is {row:?}"
        );
        assert_eq!(
            b[(34, 0)].fg,
            Color::Gray,
            "the divider must be gray, row 0 is {row:?}"
        );
        assert_eq!(
            b[(35, 0)].symbol(),
            " ",
            "column 35 is the gap after the divider, row 0 is {row:?}"
        );
        assert_eq!(
            b[(36, 0)].symbol(),
            "f",
            "the current fleet's title must start at column 36, row 0 is {row:?}"
        );
        assert_eq!(
            b[(36, 0)].fg,
            Color::White,
            "the title must be white, row 0 is {row:?}"
        );
        assert!(
            b[(36, 0)].modifier.contains(Modifier::BOLD),
            "the title must be bold, row 0 is {row:?}"
        );
        let title: String = (36..49).map(|x| b[(x, 0)].symbol().to_string()).collect();
        assert_eq!(
            title, "fleetmux-demo",
            "the right segment must start with the title, row 0 is {row:?}"
        );
        assert_eq!(
            b[(50, 0)].symbol(),
            "·",
            "the ` · ` separator must sit at column 50, row 0 is {row:?}"
        );
        assert_eq!(
            b[(52, 0)].symbol(),
            "运",
            "line1 must follow the separator at column 52, row 0 is {row:?}"
        );
        assert_eq!(
            b[(52, 0)].fg,
            Color::Gray,
            "line1 must be gray after `· `, row 0 is {row:?}"
        );
        assert_eq!(
            b[(20, 0)].symbol(),
            "X",
            "the shown topbar must leave the left part to draw(), row 0 is {row:?}"
        );
        for x in 36..80 {
            assert_eq!(
                b[(x, 0)].bg,
                Color::Reset,
                "the topbar must paint no background, ({x},0) is {row:?}"
            );
        }
    }

    /// §20.6 / plan §20.2: hidden, [`draw_topbar`] draws the same toolbar columns, no divider, the title from
    /// `left + 2`, and nothing below row 0.
    #[test]
    fn hidden_topbar_draws_the_toolbar_at_the_same_columns() {
        let mut side = shown(ONE);
        side.inset = 10;
        side.hidden = true;
        let left = topbar_w(&side);
        let mut b = buf();
        draw_topbar(&mut b, &side, 0, left, 240, false);
        let row = row_text(&b, 0, 40);
        assert_eq!(
            b[(11, 0)].symbol(),
            "◫",
            "the toggle must stay at column 11, row 0 is {row:?}"
        );
        assert_eq!(
            b[(11, 0)].bg,
            Color::DarkGray,
            "the hidden toggle must keep its dark grey background, row 0 is {row:?}"
        );
        assert_eq!(b[(14, 0)].symbol(), "+", "`+` must stay at column 14, row 0 is {row:?}");
        assert_eq!(b[(17, 0)].symbol(), "↺", "`↺` must stay at column 17, row 0 is {row:?}");
        assert_eq!(b[(20, 0)].symbol(), "←", "`←` must stay at column 20, row 0 is {row:?}");
        assert_eq!(b[(23, 0)].symbol(), "→", "`→` must stay at column 23, row 0 is {row:?}");
        assert_eq!(
            b[(25, 0)].symbol(),
            " ",
            "hidden draws no divider at column 25, row 0 is {row:?}"
        );
        assert_eq!(
            b[(27, 0)].symbol(),
            "f",
            "the title must start at left + 2 = 27, row 0 is {row:?}"
        );
        for y in 1..=8 {
            for x in 0..240 {
                assert_eq!(
                    b[(x, y)].symbol(),
                    " ",
                    "hidden row {y} column {x} must stay blank, row is {:?}",
                    row_text(&b, y, 40)
                );
                assert_eq!(
                    b[(x, y)].bg,
                    Color::Reset,
                    "hidden row {y} column {x} must have no background"
                );
            }
        }
    }

    /// §20.6 / plan §20.2: the right segment names `current` (not the first card) and stops at `cols − 2`.
    #[test]
    fn topbar_title_uses_the_current_fleet_and_is_cut_to_the_window() {
        let json = doc(
            "f1",
            &[
                fleet("f0", "first-fleet", "running"),
                fleet("f1", "fleetmux-demo", "running"),
                fleet("f2", "third-fleet", "running"),
            ],
        );
        let side = shown(&json);
        let mut b = buf();
        draw_topbar(&mut b, &side, SIDEBAR_W, SIDEBAR_W, 60, false);
        let row = row_text(&b, 0, 60);
        let title: String = (36..49).map(|x| b[(x, 0)].symbol().to_string()).collect();
        assert_eq!(
            title, "fleetmux-demo",
            "the middle fleet (`current`) must be shown, row 0 is {row:?}"
        );
        let mut last = None;
        for x in 0..60 {
            if b[(x, 0)].symbol() != " " {
                last = Some(x);
            }
        }
        assert!(
            last.is_some_and(|x| x <= 58),
            "the right segment must stop at column 58, last non-blank is {last:?}, row 0 is {row:?}"
        );
        let tail: String = (36..59).map(|x| b[(x, 0)].symbol().to_string()).collect();
        assert!(
            tail.trim_end().ends_with('…'),
            "the cut line1 must end with `…`, row 0 is {row:?}"
        );
        assert_eq!(
            b[(59, 0)].symbol(),
            " ",
            "column 59 must stay blank, row 0 is {row:?}"
        );
    }

    /// §20.6 / plan §20.4-3: row 0 is the toolbar even when the sidebar is not drawn (hidden: `sb == 0`).
    #[test]
    fn hit_row_0_is_the_toolbar_even_at_width_0() {
        let mut h = shown(ONE);
        h.hidden = true;
        assert_eq!(
            hit(&h, 0, 20, 2, 1, 0),
            SideHit::Show,
            "hidden, column 1 on row 0 must show the sidebar"
        );
        assert_eq!(
            hit(&h, 0, 20, 2, 7, 0),
            SideHit::Reopen,
            "hidden, column 7 on row 0 must be the reopen button"
        );
        assert_eq!(
            hit(&h, 0, 20, 2, 16, 0),
            SideHit::Nothing,
            "column 16 on row 0 is past the toolbar"
        );
        assert_eq!(
            hit(&h, 0, 20, 2, 1, 5),
            SideHit::Nothing,
            "hidden, rows below row 0 must hit nothing"
        );
    }
    /// One card of a fleet the sidebar closed (plan §13.3 / §13.6-1): grey dot, `已关闭 · 可恢复` and the card's `↺`.
    fn closed_fleet(id: &str, title: &str) -> String {
        format!(
            r#"{{"id":"{id}","title":"{title}","state":"closed","line1":"已关闭 · 可恢复","line2":"关闭于 10-29 23:18"}}"#
        )
    }

    /// The kv `restorable` value of plan §18.3: the resume command of one fleet the sidebar closed.
    const RESTORABLE: &str = r#"{"v":1,"runs":[{"id":"fleet-20261029-231820-ab12cd34","title":"fleetmux-demo","closed_at":1761812345,"argv":["/usr/bin/python3","/x/fleetmux/fleet.py","start","--resume","/h/runs/fleet-20261029-231820-ab12cd34"],"cwd":"/h/runs/fleet-20261029-231820-ab12cd34","env":{"FLEETMUX_HOME":"/h","PATH":"/usr/bin:/bin"}}]}"#;

    /// Plan §13.6-4: the parsed kv `restorable` lives in `Side`, so `Reopen` / `Restore(i)` can find the command by
    /// the card's `id`. `runs: []` is legal (the daemon clears the key by writing the empty string, not this).
    #[test]
    fn parse_restorable_reads_the_resume_command() {
        let r = parse_restorable(RESTORABLE).expect("RESTORABLE must parse");
        assert_eq!(r.runs.len(), 1, "{RESTORABLE}");
        let run = &r.runs[0];
        assert_eq!(run.id, "fleet-20261029-231820-ab12cd34", "{RESTORABLE}");
        assert_eq!(run.title, "fleetmux-demo", "{RESTORABLE}");
        assert_eq!(run.closed_at, 1761812345, "{RESTORABLE}");
        assert_eq!(
            run.argv,
            vec![
                "/usr/bin/python3".to_string(),
                "/x/fleetmux/fleet.py".to_string(),
                "start".to_string(),
                "--resume".to_string(),
                "/h/runs/fleet-20261029-231820-ab12cd34".to_string()
            ],
            "the argv must be the run's `start --resume` command: {RESTORABLE}"
        );
        assert_eq!(run.cwd, "/h/runs/fleet-20261029-231820-ab12cd34", "{RESTORABLE}");
        assert_eq!(
            run.env.get("FLEETMUX_HOME").map(String::as_str),
            Some("/h"),
            "the env map must keep FLEETMUX_HOME: {RESTORABLE}"
        );
        assert_eq!(run.env.len(), 2, "two env entries in {RESTORABLE}");
        // No runs is legal, so `Reopen` can say there is nothing to restore.
        let none = parse_restorable(r#"{"v":1,"runs":[]}"#).expect("empty runs must parse");
        assert!(none.runs.is_empty(), "empty runs must stay empty");
        // Unknown keys are ignored (S2c adds history fields).
        let extra = RESTORABLE.replace(r#""runs":"#, r#""history":true,"runs":"#);
        assert!(parse_restorable(&extra).is_ok(), "{extra}");
    }

    /// A bad kv `restorable` value is one English sentence, like [`parse`]: nothing half-parsed reaches `Side`.
    #[test]
    fn parse_restorable_rejects_a_bad_value() {
        let bad_version = RESTORABLE.replace(r#""v":1"#, r#""v":2"#);
        assert!(parse_restorable(&bad_version).is_err(), "{bad_version}");
        let no_runs = r#"{"v":1}"#;
        assert!(parse_restorable(no_runs).is_err(), "{no_runs}");
        let runs_not_an_array = r#"{"v":1,"runs":{}}"#;
        assert!(parse_restorable(runs_not_an_array).is_err(), "{runs_not_an_array}");
        let no_argv = r#"{"v":1,"runs":[{"id":"a","title":"t","closed_at":1,"cwd":"/r","env":{}}]}"#;
        assert!(parse_restorable(no_argv).is_err(), "{no_argv}");
        let empty_argv = r#"{"v":1,"runs":[{"id":"a","title":"t","closed_at":1,"argv":[],"cwd":"/r","env":{}}]}"#;
        assert!(parse_restorable(empty_argv).is_err(), "{empty_argv}");
        let argv_not_strings =
            r#"{"v":1,"runs":[{"id":"a","title":"t","closed_at":1,"argv":[1],"cwd":"/r","env":{}}]}"#;
        assert!(parse_restorable(argv_not_strings).is_err(), "{argv_not_strings}");
        let env_not_strings = r#"{"v":1,"runs":[{"id":"a","title":"t","closed_at":1,"argv":["x"],"cwd":"/r","env":{"A":1}}]}"#;
        assert!(parse_restorable(env_not_strings).is_err(), "{env_not_strings}");
        let mut many = Vec::new();
        for i in 0..17 {
            many.push(format!(
                r#"{{"id":"f{i}","title":"t","closed_at":1,"argv":["x"],"cwd":"/r","env":{{}}}}"#
            ));
        }
        let seventeen = format!(r#"{{"v":1,"runs":[{}]}}"#, many.join(","));
        assert!(
            parse_restorable(&seventeen).is_err(),
            "17 runs must be rejected ({} bytes)",
            seventeen.len()
        );
        assert!(parse_restorable("").is_err(), "the empty string is not a value");
        assert!(parse_restorable("not json").is_err(), "not JSON must be rejected");
    }

    /// Plan §13.6-5: the new state lives in `Side` only, with the hand-written `Default` (no kv, not resuming).
    #[test]
    fn side_defaults_have_no_restorable_and_are_not_resuming() {
        let d = Side::default();
        assert!(d.restorable.is_none(), "no kv `restorable` means nothing to restore");
        assert!(!d.resuming, "a fresh sidebar is not restoring anything");
        let mut s = shown(ONE);
        s.restorable = Some(parse_restorable(RESTORABLE).expect(RESTORABLE));
        s.resuming = true;
        assert_eq!(
            s.restorable.as_ref().map(|r| r.runs.len()),
            Some(1),
            "`Side` keeps the parsed restorable list"
        );
        assert!(s.resuming, "`Side` keeps the resuming flag");
    }

    /// Plan §13.3 / §13.6-1: a card whose `state` is `closed` draws a grey dot and `↺` at `sb − 3` on its first row;
    /// the selected card's `↺` is `White`, the other one `Gray` (same as the `×`).
    #[test]
    fn closed_card_draws_a_grey_dot_and_the_restore_button() {
        let json = doc("f1", &[closed_fleet("f1", "demo"), closed_fleet("f2", "other")]);
        let s = shown(&json);
        let mut b = buf();
        draw(&mut b, &s, SIDEBAR_W, 59, 2);
        assert_eq!(
            b[(1, 2)].symbol(),
            "●",
            "the selected closed card keeps its dot, row 2 is {:?}",
            row_text(&b, 2, 33)
        );
        assert_eq!(
            b[(1, 2)].fg,
            Color::Gray,
            "a closed fleet's dot is grey, row 2 is {:?}",
            row_text(&b, 2, 33)
        );
        assert_eq!(
            b[(31, 2)].symbol(),
            "↺",
            "the selected closed card shows `↺` at column 31 on row 2, row 2 is {:?}",
            row_text(&b, 2, 33)
        );
        assert_eq!(
            b[(31, 2)].fg,
            Color::White,
            "a selected card's `↺` is white, row 2 is {:?}",
            row_text(&b, 2, 33)
        );
        assert_eq!(
            b[(31, 6)].symbol(),
            "↺",
            "the unselected closed card shows `↺` at (31,6), row 6 is {:?}",
            row_text(&b, 6, 33)
        );
        assert_eq!(
            b[(31, 6)].fg,
            Color::Gray,
            "an unselected card's `↺` is grey, row 6 is {:?}",
            row_text(&b, 6, 33)
        );
        assert_eq!(
            b[(1, 6)].fg,
            Color::Gray,
            "an unselected closed fleet's dot is grey, row 6 is {:?}",
            row_text(&b, 6, 33)
        );
        assert_eq!(
            b[(31, 3)].symbol(),
            " ",
            "`↺` sits on the card's first row only, row 3 is {:?}",
            row_text(&b, 3, 33)
        );
        // The lines the closer wrote are drawn as they came (plan §13.5); a wide character leaves its second cell
        // as a blank, so skip it while reading the row back.
        let mut line1 = String::new();
        let mut skip = false;
        for x in 3..=20 {
            if skip {
                skip = false;
                continue;
            }
            let sym = b[(x, 3)].symbol();
            if sym.is_empty() {
                continue;
            }
            line1.push_str(sym);
            if text_cols(sym) >= 2 {
                skip = true;
            }
        }
        assert!(
            line1.starts_with("已关闭 · 可恢复"),
            "row 3 must show the closed line, got {line1:?}"
        );
    }

    /// Plan §13.6-1: `↺` is hit on the card's first row in columns `sb − 4 ..= sb − 2` (30–32 at width 34), the rest
    /// of the card is still `Pick(i)`; a running card keeps its `×` there instead.
    #[test]
    fn restore_button_is_hit_on_the_card_first_row_only() {
        let s = shown(&doc("f1", &[closed_fleet("f1", "demo")]));
        for x in 30..=32 {
            assert_eq!(
                hit(&s, SIDEBAR_W, 20, 2, x, 2),
                SideHit::Restore(0),
                "column {x} on the closed card's first row restores card 0"
            );
        }
        assert_eq!(
            hit(&s, SIDEBAR_W, 20, 2, 29, 2),
            SideHit::Pick(0),
            "column 29 on the closed card's first row is still the card"
        );
        assert_eq!(
            hit(&s, SIDEBAR_W, 20, 2, 31, 3),
            SideHit::Pick(0),
            "`↺` only covers the card's first row"
        );
        assert_eq!(
            hit(&s, SIDEBAR_W, 20, 2, 31, 5),
            SideHit::Nothing,
            "the blank row between cards hits nothing"
        );
        let running = shown(ONE);
        assert_eq!(
            hit(&running, SIDEBAR_W, 20, 2, 31, 2),
            SideHit::Close(0),
            "a running card's column 31 stays its `×`"
        );
    }

    /// Plan §13.6-1: whether the card draws `↺` depends on `state == "closed"` alone — not on `Side.stop` (that only
    /// takes the running card's `×` away) and not on the sidebar being shown.
    #[test]
    fn restore_ignores_the_stop_state_and_the_hidden_sidebar() {
        let mut s = shown(&doc("f1", &[closed_fleet("f1", "demo")]));
        s.stop = Stop::Accepted;
        assert_eq!(
            hit(&s, SIDEBAR_W, 20, 2, 31, 2),
            SideHit::Restore(0),
            "a closed card keeps `↺` after a stop request"
        );
        let mut b = buf();
        draw(&mut b, &s, SIDEBAR_W, 59, 2);
        assert_eq!(
            b[(31, 2)].symbol(),
            "↺",
            "a closed card still draws `↺` after a stop request, row 2 is {:?}",
            row_text(&b, 2, 33)
        );
        let mut running = shown(ONE);
        running.stop = Stop::Accepted;
        assert_eq!(
            hit(&running, SIDEBAR_W, 20, 2, 31, 2),
            SideHit::Pick(0),
            "a running card's `×` goes away after a stop request"
        );
        s.hidden = true;
        assert_eq!(
            hit(&s, 0, 20, 2, 31, 2),
            SideHit::Nothing,
            "hidden (width 0) the closed card is not drawn, so `↺` hits nothing"
        );
    }

    /// The text of row `y` from column `x0` up to `x1`, with the padding cell of a wide character dropped (so the
    /// card texts compare as they read on screen).
    fn span_text(buf: &Buffer, y: u16, x0: u16, x1: u16) -> String {
        let mut out = String::new();
        let mut skip = false;
        for x in x0..x1 {
            if skip {
                skip = false;
                continue;
            }
            let cell = buf[(x, y)].symbol().to_string();
            skip = cell
                .chars()
                .next()
                .is_some_and(|ch| fleetmux_term::cell_width(ch) == 2);
            out.push_str(&cell);
        }
        out
    }

    /// A sidebar showing `ONE` plus one bash card per pane id, in creation order (plan §14.3: `Side.bash`).
    fn bash_side(panes: &[&str]) -> Side {
        let mut s = shown(ONE);
        s.bash = panes.iter().map(|p| p.to_string()).collect();
        s
    }

    /// Plan §14.5-2: the blank card area is row 2 up to the status line, left of the gap column, and only where
    /// [`hit`] says nothing. The topbar (row 0), the blank row 1 and the hidden sidebar are not part of it.
    #[test]
    fn blank_at_is_the_empty_card_area_only() {
        let s = shown(ONE);
        let rows = 59; // a 60-row layout: the status line is row 59, so `content_rows − 1` = 58
        assert!(
            blank_at(&s, SIDEBAR_W, rows, 2, 2, 5),
            "row 5 is under the one fleet card (rows 2–4), so it is blank"
        );
        assert!(
            blank_at(&s, SIDEBAR_W, rows, 2, 32, 58),
            "column sb − 2 = 32 and the last row above the status line are still blank"
        );
        for (x, y, why) in [
            (2, 2, "row 2 is the first card's title"),
            (31, 2, "column 31 is the card's `×`"),
            (33, 5, "column 33 is the gap column sb − 1"),
            (0, 0, "row 0 is the topbar"),
            (0, 1, "row 1 is the blank line under the topbar"),
            (2, 59, "row 59 is the status line"),
        ] {
            assert!(
                !blank_at(&s, SIDEBAR_W, rows, 2, x, y),
                "({x}, {y}) must not be blank: {why}"
            );
        }
        let mut hidden = shown(ONE);
        hidden.hidden = true;
        assert!(
            !blank_at(&hidden, 0, rows, 2, 2, 5),
            "the hidden sidebar has no card area"
        );
        assert!(
            !blank_at(&hidden, SIDEBAR_W, rows, 2, 2, 5),
            "a hidden sidebar is not blank at any width"
        );
        assert!(
            !blank_at(&Side::default(), SIDEBAR_W, rows, 2, 2, 5),
            "no kv `sidebar` means no card area"
        );
        let with_bash = bash_side(&["a-1", "a-2"]);
        assert!(
            !blank_at(&with_bash, SIDEBAR_W, rows, 2, 2, 6),
            "row 6 is the first bash card's title"
        );
        assert!(
            blank_at(&with_bash, SIDEBAR_W, rows, 2, 2, 9),
            "row 9 is between the two bash cards"
        );
        assert!(
            !blank_at(&with_bash, SIDEBAR_W, rows, 2, 2, 10),
            "row 10 is the second bash card"
        );
    }

    /// Plan §14.2 / §14.5-3: one card per bash pane after the fleet cards, at row `2 + 4j`: green dot, `bash-<n>`,
    /// `终端 · 运行中`, `~`, and the same `×` at `sb − 3` as a fleet card.
    #[test]
    fn bash_cards_follow_the_fleet_cards() {
        let s = bash_side(&["a-1", "a-2"]);
        let mut b = buf();
        draw(&mut b, &s, SIDEBAR_W, 59, 2);
        assert_eq!(
            span_text(&b, 6, 3, 9),
            "bash-1",
            "the first bash card's title sits at (3, 6); row 6 is {:?}",
            row_text(&b, 6, SIDEBAR_W)
        );
        assert_eq!(
            b[(1, 6)].symbol(),
            "●",
            "the bash card has the state dot at column 1; row 6 is {:?}",
            row_text(&b, 6, SIDEBAR_W)
        );
        assert_eq!(
            b[(1, 6)].fg,
            Color::LightGreen,
            "a bash page is running, so its dot is green; row 6 is {:?}",
            row_text(&b, 6, SIDEBAR_W)
        );
        assert_eq!(
            span_text(&b, 7, 3, 16),
            "终端 · 运行中",
            "the bash card's first line is the terminal's state; row 7 is {:?}",
            row_text(&b, 7, SIDEBAR_W)
        );
        assert_eq!(
            span_text(&b, 8, 3, 4),
            "~",
            "the shell starts in $HOME, drawn as ~; row 8 is {:?}",
            row_text(&b, 8, SIDEBAR_W)
        );
        assert_eq!(
            b[(31, 6)].symbol(),
            "×",
            "the bash card's `×` is at sb − 3 = 31; row 6 is {:?}",
            row_text(&b, 6, SIDEBAR_W)
        );
        assert_eq!(
            span_text(&b, 10, 3, 9),
            "bash-2",
            "the second bash card is at row 2 + 4 = 10; row 10 is {:?}",
            row_text(&b, 10, SIDEBAR_W)
        );
        assert_eq!(
            b[(31, 10)].symbol(),
            "×",
            "the second bash card has its own `×` at (31, 10); row 10 is {:?}",
            row_text(&b, 10, SIDEBAR_W)
        );
        assert_eq!(
            b[(31, 2)].symbol(),
            "×",
            "the fleet card keeps its `×` at (31, 2); row 2 is {:?}",
            row_text(&b, 2, SIDEBAR_W)
        );
        assert_eq!(
            span_text(&b, 2, 3, 16),
            "fleetmux-demo",
            "the fleet card stays first; row 2 is {:?}",
            row_text(&b, 2, SIDEBAR_W)
        );
        assert_eq!(
            row_text(&b, 14, SIDEBAR_W),
            " ".repeat(usize::from(SIDEBAR_W)),
            "a third card would be at row 14, but there are only three cards"
        );
    }

    /// Plan §14.5-3: the merged list's index decides the hit — a bash card is `Pick(j)` / `Close(j)` with
    /// `j ≥ fleets.len()`, exactly where its rows and its `×` sit.
    #[test]
    fn hit_maps_the_bash_cards_and_their_close() {
        let s = bash_side(&["a-1", "a-2"]);
        for (x, y, want, why) in [
            (
                2,
                6,
                SideHit::Pick(1),
                "the first bash card's body is card 1 of the merged list",
            ),
            (
                29,
                6,
                SideHit::Pick(1),
                "column 29 is left of the `×`'s click area",
            ),
            (
                30,
                6,
                SideHit::Close(1),
                "column sb − 4 = 30 clicks the first bash card's `×`",
            ),
            (
                31,
                6,
                SideHit::Close(1),
                "column sb − 3 = 31 clicks the first bash card's `×`",
            ),
            (
                32,
                6,
                SideHit::Close(1),
                "column sb − 2 = 32 clicks the first bash card's `×`",
            ),
            (
                2,
                10,
                SideHit::Pick(2),
                "the second bash card is card 2 of the merged list",
            ),
            (
                31,
                10,
                SideHit::Close(2),
                "the second bash card's `×` at (31, 10)",
            ),
            (
                2,
                5,
                SideHit::Nothing,
                "row 5 is between the fleet card and the first bash card",
            ),
            (
                2,
                9,
                SideHit::Nothing,
                "row 9 is between the two bash cards",
            ),
            (2, 14, SideHit::Nothing, "row 14 is below the last card"),
            (
                33,
                6,
                SideHit::Nothing,
                "column 33 is the gap column sb − 1",
            ),
        ] {
            assert_eq!(hit(&s, SIDEBAR_W, 59, 2, x, y), want, "({x}, {y}): {why}");
        }
        assert_eq!(
            bash_at(&s, 1),
            Some(&"a-1".to_string()),
            "merged index 1 is the first bash pane"
        );
        assert_eq!(bash_at(&s, 0), None, "merged index 0 is the fleet card");
        assert_eq!(bash_at(&s, 3), None, "merged index 3 is past the last card");
    }

    /// Plan §14.2: the current view follows the zoom, so the zoomed bash card is the selected one — and the fleet card
    /// is not selected at the same time.
    #[test]
    fn the_zoomed_bash_card_is_the_selected_one() {
        let mut s = bash_side(&["a-1", "a-2"]);
        s.bash_sel = Some("a-2".to_string());
        let mut b = buf();
        draw(&mut b, &s, SIDEBAR_W, 59, 2);
        assert_eq!(
            b[(0, 10)].bg,
            Color::Blue,
            "the zoomed bash card's rows 10–12 are blue; row 10 is {:?}",
            row_text(&b, 10, SIDEBAR_W)
        );
        assert_eq!(
            b[(32, 12)].bg,
            Color::Blue,
            "the blue bar reaches column sb − 2 = 32 on the card's last row"
        );
        assert_eq!(
            b[(33, 10)].bg,
            Color::Reset,
            "the gap column sb − 1 keeps no background"
        );
        assert_eq!(
            b[(3, 11)].fg,
            Color::White,
            "a selected card's lines are white; row 11 is {:?}",
            row_text(&b, 11, SIDEBAR_W)
        );
        assert_eq!(
            b[(31, 10)].fg,
            Color::White,
            "a selected card's `×` is white; row 10 is {:?}",
            row_text(&b, 10, SIDEBAR_W)
        );
        assert_eq!(
            b[(0, 6)].bg,
            Color::Reset,
            "the other bash card is not selected; row 6 is {:?}",
            row_text(&b, 6, SIDEBAR_W)
        );
        assert_eq!(
            b[(3, 7)].fg,
            Color::Gray,
            "an unselected bash card's lines are grey; row 7 is {:?}",
            row_text(&b, 7, SIDEBAR_W)
        );
        assert_eq!(
            b[(0, 2)].bg,
            Color::Reset,
            "the fleet card is not selected while the bash card is zoomed; row 2 is {:?}",
            row_text(&b, 2, SIDEBAR_W)
        );
        assert_eq!(
            b[(3, 3)].fg,
            Color::Gray,
            "the fleet card's lines are grey while a bash card is selected; row 3 is {:?}",
            row_text(&b, 3, SIDEBAR_W)
        );
    }

    /// Plan §14.2 / §14.5-3: `←` / `→` walk the merged list, so a bash card after the fleet card turns `→` white.
    #[test]
    fn arrows_follow_the_bash_cards() {
        let mut s = bash_side(&["a-1"]);
        assert_eq!(
            merged_neighbours(&s),
            (false, true),
            "the fleet card has the bash card after it"
        );
        let mut b = buf();
        draw(&mut b, &s, SIDEBAR_W, 59, 2);
        assert_eq!(
            b[(13, 0)].fg,
            Color::White,
            "a next card exists, so `→` is white; row 0 is {:?}",
            row_text(&b, 0, 14)
        );
        assert_eq!(
            b[(10, 0)].fg,
            Color::DarkGray,
            "nothing precedes the fleet card, so `←` stays grey; row 0 is {:?}",
            row_text(&b, 0, 14)
        );
        s.bash_sel = Some("a-1".to_string());
        assert_eq!(
            merged_neighbours(&s),
            (true, false),
            "the bash card has the fleet card before it"
        );
        let mut b = buf();
        draw(&mut b, &s, SIDEBAR_W, 59, 2);
        assert_eq!(
            b[(10, 0)].fg,
            Color::White,
            "a previous card exists, so `←` is white; row 0 is {:?}",
            row_text(&b, 0, 14)
        );
        assert_eq!(
            b[(13, 0)].fg,
            Color::DarkGray,
            "nothing follows the bash card, so `→` stays grey; row 0 is {:?}",
            row_text(&b, 0, 14)
        );
        s.bash_sel = Some("gone".to_string());
        assert_eq!(
            merged_neighbours(&s),
            (false, true),
            "a zoomed pane that is not a bash card leaves the fleet selected"
        );
    }

    /// Plan §14.5-3 (cmux): the bash cards that do not fit are scrolled to, never counted on an overflow line.
    #[test]
    fn no_overflow_line_counts_the_bash_cards() {
        let s = bash_side(&["a-1", "a-2"]);
        let mut b = buf();
        draw(&mut b, &s, SIDEBAR_W, 14, 2); // room for two cards
        let line = span_text(&b, 9, 1, SIDEBAR_W);
        assert!(!line.contains("还有"), "no overflow line; row 9 is {line:?}");
        assert_eq!(
            b[(3, 10)].symbol(),
            " ",
            "the third card is not drawn; row 10 is {:?}",
            row_text(&b, 10, SIDEBAR_W)
        );
        assert_eq!(
            b[(3, 6)].symbol(),
            "b",
            "the first bash card still fits at row 6; row 6 is {:?}",
            row_text(&b, 6, SIDEBAR_W)
        );
    }

    /// Plan §14.2: the bash close box is the fleet box with the page as its subject — its title and body name
    /// `bash-<n>`, its confirm button closes a bash page, and the fleet box keeps its own text and button.
    #[test]
    fn the_bash_close_box_names_the_page_and_its_button() {
        let mut s = bash_side(&["a-1", "a-2"]);
        s.confirm = true;
        s.close_bash = Some("a-2".to_string());
        let (title, body) = modal_text(&s);
        assert_eq!(
            title, "关闭 bash 页「bash-2」？",
            "the bash close box title, page a-2 is the second card"
        );
        assert_eq!(
            body, "bash-2 里还有程序在跑，关掉会结束它。",
            "the bash close box body, page a-2 is the second card"
        );
        let mut b = buf();
        draw_modal(&mut b, &s, 240, 60);
        let screen: Vec<String> = (0..60).map(|y| span_text(&b, y, 0, 240)).collect();
        let joined = screen.join("\n");
        assert!(
            joined.contains("[ 关闭 bash 页 ]"),
            "the bash box's confirm button is missing:\n{joined}"
        );
        assert!(
            !joined.contains("[ 关闭 fleet ]"),
            "the bash box still offers the fleet button:\n{joined}"
        );

        // A fleet box keeps its own texts and button after a bash box was shown.
        s.close_bash = None;
        let (title, body) = modal_text(&s);
        assert_eq!(
            title, "关闭整个 fleet「fleetmux-demo」？",
            "the fleet box title after a bash box"
        );
        assert!(
            body.contains("会停掉监工"),
            "the fleet box body after a bash box: {body}"
        );
        let mut b = buf();
        draw_modal(&mut b, &s, 240, 60);
        let joined: Vec<String> = (0..60).map(|y| span_text(&b, y, 0, 240)).collect();
        assert!(
            joined.join("\n").contains("[ 关闭 fleet ]"),
            "the fleet box's own confirm button is missing:\n{}",
            joined.join("\n")
        );
    }

    fn paged(id: &str, title: &str, page: &str) -> String {
        format!(
            r#"{{"id":"{id}","title":"{title}","state":"running","line1":"运行中","line2":"~/x","page":"{page}"}}"#
        )
    }

    #[test]
    fn m4_card_page_key_is_optional_and_must_be_a_string() {
        let d = parse(&doc("r", &[paged("r", "root", ""), paged("c", "child", "M0"), fleet("o", "old", "running")]))
            .expect("cards with page parse");
        let pages: Vec<Option<&str>> = d.fleets.iter().map(|e| e.page.as_deref()).collect();
        assert_eq!(pages, [Some(""), Some("M0"), None]);
        let bad = doc("r", &[fleet("r", "root", "running").replace("}", r#","page":7}"#)]);
        assert!(parse(&bad).is_err(), "{bad}");
    }

    /// M4 R10: a child card that carries `page` has no `×` (drawn or hit) and its close columns pick the card; the
    /// root card (`page` `""`) keeps its `×`; a child card without `page` keeps the old one.
    #[test]
    fn m4_child_cards_with_a_page_have_no_close_button() {
        let s = shown(&doc(
            "r",
            &[paged("r", "root", ""), paged("c", "child", "M0"), fleet("o", "old", "running")],
        ));
        let mut b = buf();
        draw(&mut b, &s, SIDEBAR_W, 59, 2);
        assert_eq!(b[(31, 2)].symbol(), "×", "root row {:?}", row_text(&b, 2, 33));
        assert_eq!(hit(&s, SIDEBAR_W, 59, 2, 31, 2), SideHit::Close(0));
        assert_eq!(b[(31, 6)].symbol(), " ", "child row {:?}", row_text(&b, 6, 33));
        assert_eq!(hit(&s, SIDEBAR_W, 59, 2, 31, 6), SideHit::Pick(1));
        assert_eq!(b[(31, 10)].symbol(), "×", "old child row {:?}", row_text(&b, 10, 33));
        assert_eq!(hit(&s, SIDEBAR_W, 59, 2, 31, 10), SideHit::Close(2));
    }
}
