//! One control request → one reply (PROTOCOL.md §4). Signature from stage A (brief-m03 §3.3); B6 (server-ctl)
//! implements the 16 phase-0 commands, B21 (stats) fills `stats::snapshot` behind the `server.stats` branch.
//!
//! Locking: every branch holds the `State` lock only for in-memory work. `pane.kill` and the forced
//! `server.shutdown` take it once per escalation step (`kill_begin` / `kill_step`) and never across a sleep, so
//! a kill that waits seconds for a process group does not stall other connections, PTY output or viewers.
//!
//! Dirty marking: only commands that change what viewers see (or may see) mark the viewer hub dirty; the
//! read-only `hello`, `pane.list`, `pane.get`, `pane.read`, `layout.get`, `kv.get` and `server.stats` never do.

use std::sync::Arc;
use std::time::Instant;

use fleetmux_layout::{Group as LGroup, Layout, PaneType, Pos as LPos};
use fleetmux_proto::{
    Command, DEFAULT_KILL_TIMEOUT_MS, ErrorCode, Group, MAX_LINE, MAX_PAGES, PROTOCOL, PaneInfo, Pos,
    Reply, Request, Spawn,
};
use serde_json::{Value, json};
use tokio::sync::Mutex;
use tokio::task::JoinSet;

use super::boot;
use super::pane::{KeyError, KillError, KillPoll, WriteError};
use super::pty::SpawnError;
use super::{FleetPage, State, stats, view, views, web};

/// Execute `req` against the server state and build its reply (`Reply::Ok` with the command's result keys, or
/// `Reply::Err` with a PROTOCOL.md §5 code). Never panics on a well-formed `Request`.
///
/// `conn` is this control connection's page (M4, PROTOCOL.md §2 addendum): `None` = the root page, set once by the
/// first `hello {page}` on the connection and never changed after.
pub async fn handle(state: &Arc<Mutex<State>>, req: Request, conn: &mut Option<String>) -> Reply {
    let id = req.id;
    let Request {
        cmd,
        page: arg_page,
        all,
        ..
    } = req;
    if let Command::Hello { protocol } = &cmd
        && protocol.is_none_or(|p| p == PROTOCOL)
        && let Some(p) = &arg_page
    {
        match conn {
            None => *conn = Some(p.clone()),
            Some(c) if c == p => {}
            Some(c) => {
                return Reply::Err {
                    id: Some(id),
                    code: ErrorCode::BadRequest,
                    error: format!(
                        "hello: this connection is bound to page {c:?}; a connection serves one page"
                    ),
                };
            }
        }
    }
    let ctx = Ctx {
        page: conn.clone().unwrap_or_default(),
        bound: conn.is_some(),
        arg_page,
        all,
    };
    let result = match cmd {
        Command::PaneKill { pane, timeout_ms } => {
            kill(state, &pane, timeout_ms).await.map(|()| json!({}))
        }
        // M4 (design m4 §0 item 5): a child fleet never shuts the shared server down.
        Command::ServerShutdown { .. } if ctx.bound => Err(bad_request(format!(
            "server.shutdown: the connection of page {:?} cannot shut the tree's server down",
            ctx.page
        ))),
        Command::ServerShutdown { force } => shutdown(state, force).await.map(|()| json!({})),
        // M2.5 (PROTOCOL.md §4.18–§4.22): relayed to the viewers' windows and awaited outside the lock. M4: only
        // the current page's connection reaches the windows (§4.2 `web.*` rows).
        Command::WebOpen { url } => web::open(state, url, &ctx.page).await.map_err(web_fail),
        Command::WebGoto { url } => web::goto(state, url, &ctx.page).await.map_err(web_fail),
        Command::WebUrl => web::url(state, &ctx.page).await.map_err(web_fail),
        Command::WebReload => web::reload(state, &ctx.page).await.map_err(web_fail),
        Command::WebEval { .. } => Err(Fail(
            ErrorCode::NotImplemented,
            "web.eval is reserved and not implemented".into(),
        )),
        // M4 (§4.25): switch every viewer, then push the new page's history page to the web windows without
        // holding up the reply (the windows answer the relay on their own time).
        Command::PageSelect { page } => {
            let push = {
                let mut st = state.lock().await;
                page_select(&mut st, &page)
            };
            push.map(|url| {
                if let Some(url) = url {
                    let state = state.clone();
                    tokio::spawn(async move { web::push_goto(&state, url).await });
                }
                json!({})
            })
        }
        cmd => {
            let mut st = state.lock().await;
            immediate(&mut st, cmd, &ctx)
        }
    };
    match result {
        Ok(body) => Reply::Ok { id, body },
        Err(Fail(code, error)) => Reply::Err {
            id: Some(id),
            code,
            error,
        },
    }
}

/// An error reply: code + English sentence. `pub(super)`: `server::view` calls [`spawn`] for the sidebar's
/// restore pane and only needs to log the failure.
pub(super) struct Fail(pub(super) ErrorCode, pub(super) String);

type Res = Result<Value, Fail>;

fn web_fail((code, error): (ErrorCode, String)) -> Fail {
    Fail(code, error)
}

fn no_such_pane(pane: &str) -> Fail {
    Fail(ErrorCode::NoSuchPane, format!("no pane {pane}"))
}

fn bad_request(msg: impl Into<String>) -> Fail {
    Fail(ErrorCode::BadRequest, msg.into())
}

/// M4: the request's connection context.
struct Ctx {
    /// The connection's page (`""` = root).
    page: String,
    /// The connection is bound by `hello {page}` (a "page connection").
    bound: bool,
    /// An explicit `page` argument (`pane.spawn {page}`).
    arg_page: Option<String>,
    /// `pane.list {all: true}`.
    all: bool,
}

fn no_such_page(page: &str) -> Fail {
    bad_request(format!("no such page: {page}"))
}

/// kv keys that only the root page has (design m4 §4.2 `kv` row).
const ROOT_ONLY_KEYS: [&str; 4] = ["sidebar", "stop_request", "restorable", "sidebar_width"];

/// Every command that completes without waiting, under one lock.
fn immediate(st: &mut State, cmd: Command, ctx: &Ctx) -> Res {
    match cmd {
        Command::Hello { protocol } => {
            if let Some(p) = protocol
                && p != PROTOCOL
            {
                return Err(Fail(
                    ErrorCode::ProtocolVersion,
                    format!("server speaks protocol {PROTOCOL}, client asked for {p}"),
                ));
            }
            // SAFETY: geteuid has no preconditions and cannot fail.
            let uid = unsafe { libc::geteuid() };
            Ok(json!({
                "uid": uid,
                "pid": std::process::id(),
                "epoch": st.epoch,
                "version": env!("CARGO_PKG_VERSION"),
                "protocol": PROTOCOL,
                "run": page_kv(st, &ctx.page, "run"),
                "viewers": st.viewers.len(),
                "prefix": boot::prefix_name(),
                // M2.6 theme (PROTOCOL.md §4.1): the palette this server answers colour queries with.
                "theme": boot::theme_json(),
                // M4 (PROTOCOL.md §2 addendum): the capability key, this connection's page and the current page.
                "pages": 1,
                "page": ctx.page,
                "current": st.fleet_pages.current,
            }))
        }
        Command::KvSet { key, value } if ctx.bound => {
            if ROOT_ONLY_KEYS.contains(&key.as_str()) {
                return Err(bad_request(format!(
                    "kv.set: {key:?} is a root-page key; page {:?} cannot set it",
                    ctx.page
                )));
            }
            ensure_page(st, &ctx.page)?;
            if let Some(p) = st.fleet_pages.pages.get_mut(&ctx.page) {
                p.kv.insert(key, value);
            }
            st.viewers.mark_dirty();
            Ok(json!({}))
        }
        Command::KvGet { key } if ctx.bound => {
            if ROOT_ONLY_KEYS.contains(&key.as_str()) {
                return Err(bad_request(format!(
                    "kv.get: {key:?} is a root-page key; page {:?} cannot read it",
                    ctx.page
                )));
            }
            Ok(json!({ "value": page_kv(st, &ctx.page, &key) }))
        }
        Command::KvSet { key, value } => {
            let sidebar = key == "sidebar";
            // S1d (plan §16.3-5): a pushed `sidebar_width` re-syncs the layout like the cards do.
            let width = key == "sidebar_width";
            let stop = key == "stop_request";
            // S2b (plan §13.6-2): the closer registers the closed fleets' `start --resume` commands here.
            let restorable = key == "restorable";
            st.kv.insert(key, value);
            st.viewers.mark_dirty();
            if sidebar || width {
                // S1 (plan §4.3): the fleet daemon pushes the sidebar cards through kv; re-parse them now so the
                // next frame shows them and the panes get their new width.
                view::sync_sidebar(st);
            }
            if stop {
                // S1b (plan §11.3): the daemon answers a close request with `accepted` / `refused`; the status line
                // follows.
                view::sync_stop(st);
            }
            if restorable {
                // S2b (plan §13.6-2): the cards' `↺` and the topbar's `↺` restore what this list names; an empty
                // value (the daemon is back) drops it and the `正在恢复…` status.
                view::sync_restorable(st);
            }
            Ok(json!({}))
        }
        Command::KvGet { key } => Ok(json!({ "value": st.kv.get(&key) })),
        Command::PaneSpawn(s) => {
            let target = match &ctx.arg_page {
                Some(p) if page_exists(st, p) => p.clone(),
                Some(p) => return Err(no_such_page(p)),
                None => {
                    ensure_page(st, &ctx.page)?;
                    ctx.page.clone()
                }
            };
            spawn_on(st, &target, &s)
        }
        Command::PaneList => {
            let mut panes: Vec<(String, PaneInfo)> = st
                .panes
                .list()
                .into_iter()
                .map(|p| (st.panes.page_of(&p.pane).unwrap_or("").to_string(), p))
                .filter(|(page, _)| ctx.all || *page == ctx.page)
                .collect();
            panes.sort_by(|(pa, a), (pb, b)| {
                (pa, group_rank(a.group), a.index).cmp(&(pb, group_rank(b.group), b.index))
            });
            let with_page = ctx.all || ctx.bound;
            let panes: Vec<Value> = panes
                .iter()
                .map(|(page, info)| paged_record(st, info, page, with_page))
                .collect();
            Ok(json!({ "panes": panes }))
        }
        Command::PaneGet { pane } => {
            let info = st.panes.info(&pane).ok_or_else(|| no_such_pane(&pane))?;
            let page = st.panes.page_of(&pane).unwrap_or("").to_string();
            Ok(paged_record(st, &info, &page, ctx.bound))
        }
        Command::PaneWrite { pane, data, mode } => match st.panes.write(&pane, &data, mode) {
            Ok(written) => {
                st.viewers.mark_dirty();
                Ok(json!({ "written": written }))
            }
            Err(WriteError::NoSuchPane) => Err(no_such_pane(&pane)),
            Err(e) => Err(write_failed(&pane, &e)),
        },
        Command::PaneKey { pane, key } => match st.panes.key(&pane, &key) {
            Ok(()) => {
                st.viewers.mark_dirty();
                Ok(json!({}))
            }
            Err(KeyError::NoSuchPane) => Err(no_such_pane(&pane)),
            Err(KeyError::UnknownKey) => Err(Fail(
                ErrorCode::UnknownKey,
                format!("unknown key name {key:?}"),
            )),
            Err(KeyError::Write(WriteError::NoSuchPane)) => Err(no_such_pane(&pane)),
            Err(KeyError::Write(e)) => Err(write_failed(&pane, &e)),
        },
        Command::PaneRead { pane, lines } => {
            let text = st
                .panes
                .read_text(&pane, lines as usize)
                .ok_or_else(|| no_such_pane(&pane))?;
            Ok(json!({ "text": fit_line(text) }))
        }
        Command::PaneRename { pane, title } => {
            if !st.panes.rename(&pane, &title) {
                return Err(no_such_pane(&pane));
            }
            persist_panes(st);
            st.viewers.mark_dirty();
            Ok(json!({}))
        }
        Command::PaneFocus { pane } => {
            // M4 (§4.2 `pane.focus` row): in the pane's own page; the current page never changes.
            let page = st
                .panes
                .page_of(&pane)
                .ok_or_else(|| no_such_pane(&pane))?
                .to_string();
            on_page(st, &page, |st| {
                let group = live_group(st, &pane)?;
                st.layout.select(pane);
                st.layout.focus_group(group);
                after_layout_change(st, false);
                Ok(json!({}))
            })
        }
        Command::PaneMove { pane, group, pos } => pane_move_paged(st, &ctx.page, &pane, group, pos),
        Command::LayoutGet => Ok(match layout_of(st, &ctx.page) {
            Some(l) => l.to_object(),
            None => new_page_layout(st).to_object(),
        }),
        Command::LayoutSet {
            h_ratio,
            v_ratio,
            zoom,
        } => {
            ensure_page(st, &ctx.page)?;
            on_page(st, &ctx.page, |st| layout_set(st, h_ratio, v_ratio, zoom))
        }
        Command::ServerStats => {
            let mut v = serde_json::to_value(stats::snapshot(st))
                .map_err(|e| Fail(ErrorCode::Internal, e.to_string()))?;
            // M4: the page count, root included (design m4 §4.2 `server.stats` row).
            v["pages"] = json!(1 + st.fleet_pages.pages.len());
            Ok(v)
        }
        Command::PageOpen {
            page,
            title,
            reclaimed,
            history_url,
        } => page_open(st, &page, title, reclaimed, history_url),
        Command::PageClose { page, keep } => page_close(st, &page, keep),
        Command::PageList => Ok(json!({ "pages": page_list(st) })),
        Command::PaneKill { .. }
        | Command::ServerShutdown { .. }
        | Command::WebOpen { .. }
        | Command::WebGoto { .. }
        | Command::WebUrl
        | Command::WebReload
        | Command::WebEval { .. }
        | Command::PageSelect { .. } => {
            unreachable!("handled by `handle` outside the lock")
        }
    }
}

fn write_failed(pane: &str, e: &WriteError) -> Fail {
    let why = match e {
        WriteError::QueueFull => "its input queue is over 16 MiB",
        WriteError::Closed => "its PTY is closed",
        WriteError::NoSuchPane => "it is gone",
    };
    Fail(
        ErrorCode::WriteFailed,
        format!("cannot write to pane {pane}: {why}"),
    )
}

fn record(info: &PaneInfo) -> Value {
    serde_json::to_value(info).unwrap_or_else(|_| json!({}))
}

/// The pane record plus M4's `page` key: on a page connection, with `pane.list {all}`, for a pane outside the
/// root page, and — once the tree has a child page — for root panes on a root connection too (`"page":""`). A root
/// connection on a tree without child pages gets the M3 record unchanged. Every record carries `fg_pgid`: the PTY's
/// foreground process group read now (`tcgetpgrp`), `null` when the PTY reports none (PROTOCOL.md §4.0).
fn paged_record(st: &State, info: &PaneInfo, page: &str, with_page: bool) -> Value {
    let mut v = record(info);
    v["fg_pgid"] = json!(st.panes.foreground_pgid(&info.pane));
    if with_page || !page.is_empty() || !st.fleet_pages.pages.is_empty() {
        v["page"] = json!(page);
    }
    v
}

fn group_rank(g: Group) -> u8 {
    match g {
        Group::Supervisor => 0,
        Group::Lower => 1,
        Group::Workers => 2,
    }
}

fn to_layout_group(g: Group) -> LGroup {
    match g {
        Group::Supervisor => LGroup::Supervisor,
        Group::Lower => LGroup::Lower,
        Group::Workers => LGroup::Workers,
    }
}

/// The layout group holding `pane`, or `no_such_pane` when it is not a live pane.
fn live_group(st: &State, pane: &str) -> Result<LGroup, Fail> {
    if st.panes.info(pane).is_none() {
        return Err(no_such_pane(pane));
    }
    group_of(&st.layout, pane).ok_or_else(|| no_such_pane(pane))
}

fn group_of(layout: &Layout, pane: &str) -> Option<LGroup> {
    LGroup::ALL
        .into_iter()
        .find(|&g| layout.group(g).iter().any(|p| p == pane))
}

/// `pane.read` keeps its reply under the 4 MiB line limit by dropping the oldest rows (PROTOCOL.md §4.9).
fn fit_line(mut text: String) -> String {
    // Room for the envelope (`{"id":…,"ok":true,"text":…}`) next to the escaped text.
    const BUDGET: usize = MAX_LINE - 64;
    loop {
        let len = serde_json::to_string(&text).map_or(0, |s| s.len());
        if len <= BUDGET {
            return text;
        }
        // Drop whole rows covering at least the excess, then measure again (escaping can make the encoded
        // text longer than the raw bytes removed). A `\n` byte is always a char boundary.
        let from = (len - BUDGET).min(text.len());
        let cut = text.as_bytes()[from..]
            .iter()
            .position(|&b| b == b'\n')
            .map_or(text.len(), |i| from + i + 1);
        text.drain(..cut);
    }
}

// ---------------------------------------------------------------- layout changes

/// After any layout change: mirror group / index / focus into the pane records, resize PTYs whose rectangle
/// changed, rewrite `layout.json` (and `panes.json` when the pane set or order changed) and mark viewers dirty.
pub(super) fn after_layout_change(st: &mut State, panes_changed: bool) {
    // M4: while another page's layout is swapped in (`on_page`), wait for the swap to end.
    if st.fleet_pages.in_state != st.fleet_pages.current {
        let before = st.fleet_pages.pending.unwrap_or(false);
        st.fleet_pages.pending = Some(before || panes_changed);
        return;
    }
    st.fleet_pages.pending = None;
    st.panes.sync_layout(&st.layout);
    // M4 (F8): every other page's panes mirror their own page's layout, none of them displayed.
    if let Some(l) = &st.fleet_pages.root_layout {
        st.panes.sync_layout_hidden(l);
    }
    for p in st.fleet_pages.pages.values() {
        if let Some(l) = &p.layout {
            st.panes.sync_layout_hidden(l);
        }
    }
    // S1c (plan §14.3): the bash cards are the `type "shell"` panes; spawn, kill and a shell that exits all come
    // through here, so the card list follows the pane set.
    view::sync_bash(st);
    view::apply_pty_sizes(st);
    if panes_changed {
        persist_panes(st);
    }
    persist_layout(st);
    st.viewers.mark_dirty();
}

/// Rewrite `panes.json` (PROTOCOL.md §8.2); a failure is logged, never a failed command.
fn persist_panes(st: &State) {
    if let Err(e) = st.panes.write_panes_json(&st.runtime.join("panes.json")) {
        boot::log("WARN", &format!("panes.json: {e}"));
    }
}

/// Rewrite `layout.json` atomically through the shared `views::save_layout` (PROTOCOL.md §8.3:
/// `Layout::to_json`, mode 0600); a failure is a `WARN`, never a failed command.
fn persist_layout(st: &State) {
    let path = st.runtime.join("layout.json");
    // M4 (R11): `layout.json` holds the root page only, whichever page is current.
    if let Err(e) = views::save_layout(&path, root_layout(st)) {
        boot::log("WARN", &format!("{}: {e}", path.display()));
    }
}

fn pane_type(ty: &str) -> PaneType {
    match ty {
        "supervisor" => PaneType::Supervisor,
        "advisor" => PaneType::Advisor,
        "worker" => PaneType::Worker,
        "daemon" => PaneType::Daemon,
        "tab" => PaneType::Tab,
        "history" => PaneType::History,
        "placeholder" => PaneType::Placeholder,
        _ => PaneType::Unknown,
    }
}

/// `pane.spawn` (PROTOCOL.md §4.4). The new pane's place is previewed in the layout under the id the store
/// will hand out next, so the PTY starts at its group's size; if the spawn fails the layout is restored as it
/// was and nothing else changed. A pane that lands in an empty group becomes that group's selected tab (B6 owns
/// this; `Layout::insert` stays a pure placement). `pub(super)`: `server::view` spawns the sidebar's restore pane
/// through this same path (plan §13.6-2).
pub(super) fn spawn(st: &mut State, s: &Spawn) -> Res {
    // M4 (design m4 R8): the server's own panes (the restore pane; the sidebar's bash tabs, user 2026-11-09: in the
    // main fleet's worker area) always land in the root page.
    spawn_on(st, "", s)
}

/// M4: `pane.spawn` into `page` (which exists): its layout is swapped in for the placement, the record gets the
/// page, and a reclaimed page turns live again unless the new pane is a `history` pane (§4.2 `pane.spawn` row).
fn spawn_on(st: &mut State, page: &str, s: &Spawn) -> Res {
    if let Some(a) = &s.anchor
        && let Some(ap) = st.panes.page_of(a)
        && ap != page
    {
        return Err(bad_request(format!(
            "pane.spawn: the anchor {a} is in page {ap:?}, not in page {page:?}"
        )));
    }
    let r = on_page(st, page, |st| spawn_here(st, page, s))?;
    if s.ty != "history"
        && let Some(p) = st.fleet_pages.pages.get_mut(page)
        && p.reclaimed
    {
        p.reclaimed = false;
        st.viewers.mark_dirty();
    }
    Ok(r)
}

/// `pane.spawn` into the layout in `State::layout` (the original M3 body).
fn spawn_here(st: &mut State, page: &str, s: &Spawn) -> Res {
    let anchor = match &s.anchor {
        Some(a) => {
            let g = live_group(st, a)?;
            if let Some(want) = s.group
                && to_layout_group(want) != g
            {
                return Err(bad_request(format!(
                    "pane.spawn: group {} differs from the anchor's group {}",
                    want.as_str(),
                    g.name()
                )));
            }
            Some((a.clone(), g))
        }
        None => None,
    };
    let id = st.panes.next_pane_id();
    let saved = st.layout.clone();
    let mut group = st.layout.insert(id.clone(), pane_type(&s.ty));
    let placed = match (&anchor, s.group) {
        (Some((a, g)), _) => {
            group = *g;
            let pos = if s.before {
                LPos::Before(a.clone())
            } else {
                LPos::After(a.clone())
            };
            st.layout.move_to(id.clone(), *g, pos)
        }
        (None, Some(g)) => {
            group = to_layout_group(g);
            st.layout.move_to(id.clone(), group, LPos::End)
        }
        (None, None) => Ok(()),
    };
    if let Err(e) = placed {
        st.layout = saved;
        return Err(Fail(
            ErrorCode::Internal,
            format!("pane.spawn: placing {id}: {e}"),
        ));
    }
    let group_was_empty = saved.group(group).is_empty();
    let (cols, rows) = view::pty_size_for(st, group);
    match st.panes.spawn(s, cols, rows) {
        Ok(info) => {
            debug_assert_eq!(info.pane, id, "next_pane_id and spawn disagree");
            st.panes.set_page(&info.pane, page);
            if group_was_empty {
                st.layout.select(id.clone());
            }
            after_layout_change(st, true);
            boot::log(
                "INFO",
                &format!(
                    "spawned {} pid {} tty {} type {:?}: {:?}",
                    info.pane, info.pid, info.tty, s.ty, s.argv
                ),
            );
            Ok(json!({"pane": info.pane, "pid": info.pid, "pgid": info.pgid, "tty": info.tty}))
        }
        Err(e) => {
            st.layout = saved;
            let why = match e {
                SpawnError::EmptyArgv => "argv is empty".to_string(),
                SpawnError::BadCwd(c) => format!("cwd {c} is not a directory"),
                SpawnError::Io(e) => e.to_string(),
                SpawnError::StartTime => "the child's start time could not be read".to_string(),
            };
            Err(Fail(
                ErrorCode::SpawnFailed,
                format!("pane.spawn {:?}: {why}", s.argv),
            ))
        }
    }
}

/// `pane.move` (PROTOCOL.md §4.12).
fn pane_move(st: &mut State, pane: &str, group: Group, pos: Pos) -> Res {
    live_group(st, pane)?;
    let target = to_layout_group(group);
    let pos = match pos {
        Pos::End => LPos::End,
        Pos::Before(r) | Pos::After(r) if r == pane => {
            return Err(bad_request(format!(
                "pane.move: {pane} cannot be placed next to itself"
            )));
        }
        Pos::Before(r) => {
            check_ref(st, &r, target)?;
            LPos::Before(r)
        }
        Pos::After(r) => {
            check_ref(st, &r, target)?;
            LPos::After(r)
        }
    };
    st.layout
        .move_to(pane.to_string(), target, pos)
        .map_err(|e| bad_request(format!("pane.move: {e}")))?;
    after_layout_change(st, true);
    Ok(json!({}))
}

/// A `before` / `after` reference must be live and in the target group.
fn check_ref(st: &State, r: &str, target: LGroup) -> Result<(), Fail> {
    if live_group(st, r)? != target {
        return Err(bad_request(format!(
            "pane.move: {r} is not in group {}",
            target.name()
        )));
    }
    Ok(())
}

/// `layout.set` (PROTOCOL.md §4.15): clamp ratios, (un)zoom, and persist only when something changed.
fn layout_set(st: &mut State, h: Option<i64>, v: Option<i64>, zoom: Option<Option<String>>) -> Res {
    if let Some(Some(z)) = &zoom
        && st.panes.info(z).is_none()
    {
        return Err(no_such_pane(z));
    }
    let before = st.layout.to_object();
    if h.is_some() || v.is_some() {
        let (ch, cv) = st.layout.ratios();
        let ratio = |r: Option<i64>, cur: u8| r.map_or(cur, |r| r.clamp(0, 255) as u8);
        st.layout.set_ratios(ratio(h, ch), ratio(v, cv));
    }
    match zoom {
        None => {}
        Some(None) => {
            if let Some(z) = st.layout.zoom().cloned() {
                st.layout.toggle_zoom(z);
            }
        }
        Some(Some(z)) if st.layout.zoom() != Some(&z) => st.layout.toggle_zoom(z),
        Some(Some(_)) => {}
    }
    let after = st.layout.to_object();
    if after != before {
        after_layout_change(st, false);
    }
    Ok(after)
}

// ---------------------------------------------------------------- kill and shutdown

/// `pane.kill` (PROTOCOL.md §4.13): the R4 escalation runs in `PaneStore::kill_step`; the lock is held for one
/// step at a time and released for every wait. `pub(super)`: `server::view` closes the sidebar's bash pages through
/// this same path (plan §14.2 / §22.2).
pub(super) async fn kill(state: &Arc<Mutex<State>>, pane: &str, timeout_ms: u32) -> Result<(), Fail> {
    let started = Instant::now();
    let mut ticket = {
        let mut st = state.lock().await;
        st.panes
            .kill_begin(pane, timeout_ms)
            .map_err(|e| kill_error(pane, e))?
    };
    loop {
        let poll = {
            let mut st = state.lock().await;
            let poll = st
                .panes
                .kill_step(&mut ticket)
                .map_err(|e| kill_error(pane, e))?;
            if matches!(poll, KillPoll::Done) {
                // The pane may already have left the layout through the exit path; removing again is a no-op.
                // M4: from whichever page holds it.
                remove_everywhere(&mut st, pane);
                // A42-LINUX-RESTORE-FAILURE-04: killing the restore pane itself is a cancel, not a failure.
                if st.resume.pane() == Some(pane) {
                    view::restore_cancelled(&mut st);
                }
                after_layout_change(&mut st, true);
                boot::log(
                    "INFO",
                    &format!("pane {pane} killed after {:?}", started.elapsed()),
                );
            }
            poll
        };
        match poll {
            KillPoll::Done => return Ok(()),
            KillPoll::Wait(d) => tokio::time::sleep(d).await,
            KillPoll::TimedOut => {
                return Err(Fail(
                    ErrorCode::KillTimeout,
                    format!(
                        "process group of pane {pane} still alive after {timeout_ms} ms + 500 ms grace"
                    ),
                ));
            }
        }
    }
}

fn kill_error(pane: &str, e: KillError) -> Fail {
    match e {
        KillError::NoSuchPane => no_such_pane(pane),
        KillError::KillTimeout => Fail(
            ErrorCode::KillTimeout,
            format!("process group of pane {pane} still alive"),
        ),
        KillError::Signal(e) => Fail(ErrorCode::Internal, format!("signalling pane {pane}: {e}")),
    }
}

/// `server.shutdown` (PROTOCOL.md §4.16). With `force`, every pane is killed in parallel at the default
/// timeout; if any survives the reply is `kill_timeout`, the server keeps running and the survivors stay in the
/// layout and `panes.json`. On success viewers get `0x82` and the accept loop stops; the connection task sends
/// the `ok` and then lets the server exit. Also the closed-fleet reaper's exit path (`boot::closed_reaper`, plan
/// §13.5): there is no connection to send a reply, so it calls `boot::shutdown_replied()` itself.
pub(super) async fn shutdown(state: &Arc<Mutex<State>>, force: bool) -> Result<(), Fail> {
    let panes: Vec<String> = state
        .lock()
        .await
        .panes
        .list()
        .into_iter()
        .map(|p| p.pane)
        .collect();
    if !panes.is_empty() {
        if !force {
            return Err(Fail(
                ErrorCode::NotEmpty,
                format!("{} panes still running", panes.len()),
            ));
        }
        let mut kills = JoinSet::new();
        for pane in panes {
            let state = state.clone();
            kills.spawn(async move {
                match kill(&state, &pane, DEFAULT_KILL_TIMEOUT_MS).await {
                    // Gone on its own meanwhile: that is what shutdown wants.
                    Err(Fail(ErrorCode::NoSuchPane, _)) => Ok(()),
                    r => r.map_err(|Fail(_, e)| e),
                }
            });
        }
        let mut failed = Vec::new();
        while let Some(r) = kills.join_next().await {
            match r {
                Ok(Ok(())) => {}
                Ok(Err(e)) => failed.push(e),
                Err(e) => failed.push(e.to_string()),
            }
        }
        if !failed.is_empty() {
            return Err(Fail(
                ErrorCode::KillTimeout,
                format!("server keeps running: {}", failed.join("; ")),
            ));
        }
    }
    let st = state.lock().await;
    let left = st.panes.list().len();
    if left > 0 {
        // A pane was spawned while the forced kills ran.
        return Err(Fail(
            ErrorCode::NotEmpty,
            format!("{left} panes still running"),
        ));
    }
    boot::begin_shutdown();
    st.viewers
        .broadcast(fleetmux_proto::view::Frame::Exit("server shutdown".into()));
    boot::log("INFO", "server.shutdown accepted");
    Ok(())
}

// ---------------------------------------------------------------- M4 fleet pages (design m4 §4.2–§4.4)

/// Run `f` with `page`'s layout in `State::layout` (R2), then put the previous one back; the layout refresh that `f`
/// asked for through [`after_layout_change`] runs once the current page is back in place. `page` must exist. Never
/// swaps across an `.await`: callers hold the state lock for the whole call.
fn on_page<R>(st: &mut State, page: &str, f: impl FnOnce(&mut State) -> R) -> R {
    let prev = st.fleet_pages.in_state.clone();
    swap_to(st, page);
    let r = f(st);
    swap_to(st, &prev);
    if st.fleet_pages.in_state == st.fleet_pages.current
        && let Some(changed) = st.fleet_pages.pending.take()
    {
        after_layout_change(st, changed);
    }
    r
}

/// Put `page`'s layout into `State::layout` and park the one that was there. The tree-wide runtime fields
/// (ratios, sidebar / topbar / header widths, mirror, native header) travel with the swap, so they never jump.
fn swap_to(st: &mut State, page: &str) {
    if st.fleet_pages.in_state == page {
        return;
    }
    let parked = if page.is_empty() {
        st.fleet_pages.root_layout.take()
    } else {
        st.fleet_pages
            .pages
            .get_mut(page)
            .and_then(|p| p.layout.take())
    };
    let mut incoming = parked.unwrap_or_else(|| new_page_layout(st));
    share_runtime(&st.layout, &mut incoming);
    let outgoing = std::mem::replace(&mut st.layout, incoming);
    let prev = std::mem::replace(&mut st.fleet_pages.in_state, page.to_string());
    if prev.is_empty() {
        st.fleet_pages.root_layout = Some(outgoing);
    } else if let Some(p) = st.fleet_pages.pages.get_mut(&prev) {
        p.layout = Some(outgoing);
    }
}

/// Copy the layout fields every page shares (R2: not part of `layout.json`'s groups) from `from` into `to`.
fn share_runtime(from: &Layout, to: &mut Layout) {
    let (h, v) = from.ratios();
    to.set_ratios(h, v);
    to.set_sidebar(from.sidebar());
    to.set_topbar(from.topbar());
    to.set_header(from.header());
    to.set_mirror(from.mirror());
    if from.mirror() {
        to.set_sidebar_left(!from.sidebar_on_right());
    }
    to.set_native_header(from.native_header());
}

/// Bring every parked layout's shared fields up to `State::layout` (before their rectangles are used).
pub(crate) fn spread_shared(st: &mut State) {
    let from = &st.layout;
    if let Some(l) = st.fleet_pages.root_layout.as_mut() {
        share_runtime(from, l);
    }
    for p in st.fleet_pages.pages.values_mut() {
        if let Some(l) = p.layout.as_mut() {
            share_runtime(from, l);
        }
    }
}

/// The parked layouts (every page but the one in `State::layout`).
pub(crate) fn parked_layouts(st: &State) -> impl Iterator<Item = &Layout> {
    st.fleet_pages
        .root_layout
        .iter()
        .chain(st.fleet_pages.pages.values().filter_map(|p| p.layout.as_ref()))
}

/// The root page's layout, wherever it is (`layout.json` saves this one, R11). B2's `layout.json` writers in
/// `views.rs` / `view.rs` should save this instead of `State::layout`.
pub(crate) fn root_layout(st: &State) -> &Layout {
    if st.fleet_pages.in_state.is_empty() {
        &st.layout
    } else {
        st.fleet_pages.root_layout.as_ref().unwrap_or(&st.layout)
    }
}

fn layout_of<'a>(st: &'a State, page: &str) -> Option<&'a Layout> {
    if st.fleet_pages.in_state == page {
        Some(&st.layout)
    } else if page.is_empty() {
        st.fleet_pages.root_layout.as_ref()
    } else {
        st.fleet_pages.pages.get(page)?.layout.as_ref()
    }
}

fn layout_mut_of<'a>(st: &'a mut State, page: &str) -> Option<&'a mut Layout> {
    if st.fleet_pages.in_state == page {
        Some(&mut st.layout)
    } else if page.is_empty() {
        st.fleet_pages.root_layout.as_mut()
    } else {
        st.fleet_pages.pages.get_mut(page)?.layout.as_mut()
    }
}

/// Take `pane` out of every page's layout (pane exit and `pane.kill`; the ledger record is already gone or going).
pub(crate) fn remove_everywhere(st: &mut State, pane: &str) {
    st.layout.remove(pane.to_string());
    if let Some(l) = st.fleet_pages.root_layout.as_mut() {
        l.remove(pane.to_string());
    }
    for p in st.fleet_pages.pages.values_mut() {
        if let Some(l) = p.layout.as_mut() {
            l.remove(pane.to_string());
        }
    }
}

fn page_exists(st: &State, page: &str) -> bool {
    page.is_empty() || st.fleet_pages.pages.contains_key(page)
}

/// A fresh page layout: the platform's empty fixed layout with the tree's shared fields (R1).
fn new_page_layout(st: &State) -> Layout {
    let mut l = Layout::for_platform();
    share_runtime(&st.layout, &mut l);
    l
}

fn new_page(st: &mut State, page: &str, title: String, reclaimed: bool) -> Result<(), Fail> {
    if st.fleet_pages.pages.len() >= MAX_PAGES {
        return Err(bad_request(format!(
            "page.open: a tree holds at most {MAX_PAGES} child pages"
        )));
    }
    let layout = new_page_layout(st);
    st.fleet_pages.pages.insert(
        page.to_string(),
        FleetPage {
            title,
            reclaimed,
            layout: Some(layout),
            kv: Default::default(),
        },
    );
    Ok(())
}

/// The page a bound connection names exists from its first `pane.spawn` / `pane.move` / `kv.set` / `layout.set`
/// on: created `live`, titled with its id (§4.2 `hello` row).
fn ensure_page(st: &mut State, page: &str) -> Result<(), Fail> {
    if page_exists(st, page) {
        return Ok(());
    }
    new_page(st, page, page.to_string(), false)?;
    st.viewers.mark_dirty();
    Ok(())
}

/// kv `key` of `page` (`""` = the root kv); `None` when unset or the page does not exist yet.
pub(crate) fn page_kv(st: &State, page: &str, key: &str) -> Option<String> {
    if page.is_empty() {
        st.kv.get(key).cloned()
    } else {
        st.fleet_pages.pages.get(page)?.kv.get(key).cloned()
    }
}

/// kv `key` of the current page: what the server itself reads (e.g. `history_url` for `0x85` / prefix `o`, B2).
pub(crate) fn current_page_kv(st: &State, key: &str) -> Option<String> {
    page_kv(st, &st.fleet_pages.current, key)
}

fn panes_in(st: &State, page: &str) -> usize {
    st.panes
        .list()
        .iter()
        .filter(|p| st.panes.page_of(&p.pane) == Some(page))
        .count()
}

/// `page.open` (§4.23): create, reopen or rename (idempotent).
fn page_open(
    st: &mut State,
    page: &str,
    title: Option<String>,
    reclaimed: bool,
    history_url: Option<String>,
) -> Res {
    match st.fleet_pages.pages.get_mut(page) {
        Some(p) => {
            if let Some(t) = title {
                p.title = t;
            }
            p.reclaimed = reclaimed;
        }
        None => new_page(st, page, title.unwrap_or_else(|| page.to_string()), reclaimed)?,
    }
    if let Some(u) = history_url
        && let Some(p) = st.fleet_pages.pages.get_mut(page)
    {
        p.kv.insert("history_url".into(), u);
    }
    st.viewers.mark_dirty();
    Ok(json!({}))
}

/// `page.close` (§4.24): only an empty page; `keep` → reclaimed, else removed (the current page goes back to the
/// root first).
pub(super) fn page_close(st: &mut State, page: &str, keep: bool) -> Res {
    if !st.fleet_pages.pages.contains_key(page) {
        return Err(no_such_page(page));
    }
    let n = panes_in(st, page);
    if n > 0 {
        return Err(Fail(
            ErrorCode::NotEmpty,
            format!("page.close: page {page:?} still has {n} panes"),
        ));
    }
    if keep {
        if let Some(p) = st.fleet_pages.pages.get_mut(page) {
            p.reclaimed = true;
        }
        st.viewers.mark_dirty();
        return Ok(json!({}));
    }
    if st.fleet_pages.current == page {
        st.fleet_pages.current = String::new();
        swap_to(st, "");
        st.page = None;
        st.viewers.forget_drawn();
    }
    st.fleet_pages.pages.remove(page);
    after_layout_change(st, false);
    view::sync_sidebar(st);
    Ok(json!({}))
}

fn page_list(st: &State) -> Vec<Value> {
    let entry = |page: &str, title: &str, reclaimed: bool| {
        json!({
            "page": page,
            "title": title,
            "state": if reclaimed { "reclaimed" } else { "live" },
            "panes": panes_in(st, page),
            "current": st.fleet_pages.current == page,
        })
    };
    let root_title = st.kv.get("run").cloned().unwrap_or_default();
    std::iter::once(entry("", &root_title, false))
        .chain(
            st.fleet_pages
                .pages
                .iter()
                .map(|(id, p)| entry(id, &p.title, p.reclaimed)),
        )
        .collect()
}

/// `page.select` (§4.25) under the lock: switch the current page and redraw every viewer. Returns the new page's
/// `history_url` for the `0x84 goto` to the web windows.
/// `pub(super)`: the sidebar (R8) and the jump event (R13) in `server::view` select pages through this too.
pub(super) fn page_select(st: &mut State, page: &str) -> Result<Option<String>, Fail> {
    if !page_exists(st, page) {
        return Err(no_such_page(page));
    }
    if st.fleet_pages.current != page {
        st.fleet_pages.current = page.to_string();
        swap_to(st, page);
        // Page mode scrolls a pane of the page that just went away.
        st.page = None;
        st.viewers.forget_drawn();
        after_layout_change(st, false);
        // R9: the sidebar's highlight and the topbar follow the current page.
        view::sync_sidebar(st);
        boot::log("INFO", &format!("page.select {page:?}"));
    }
    Ok(page_kv(st, page, "history_url"))
}

/// `pane.move` on the connection of `page` (§4.2 `pane.move` row): the reference pane must be in `page`; the moved
/// pane may be in any page — it is taken out of its page's layout and put into this one (same PTY, same process).
fn pane_move_paged(st: &mut State, page: &str, pane: &str, group: Group, pos: Pos) -> Res {
    let src = st
        .panes
        .page_of(pane)
        .ok_or_else(|| no_such_pane(pane))?
        .to_string();
    if let Pos::Before(r) | Pos::After(r) = &pos
        && r != pane
    {
        match st.panes.page_of(r) {
            None => return Err(no_such_pane(r)),
            Some(rp) if rp != page => {
                return Err(bad_request(format!(
                    "pane.move: {r} is in page {rp:?}, not in this connection's page {page:?}"
                )));
            }
            Some(_) => {}
        }
    }
    if src == page {
        return on_page(st, page, |st| pane_move(st, pane, group, pos));
    }
    ensure_page(st, page)?;
    let ty_name = st.panes.info(pane).map(|i| i.ty).unwrap_or_default();
    on_page(st, page, |st| {
        let target = to_layout_group(group);
        let lpos = match pos {
            Pos::End => LPos::End,
            Pos::Before(r) | Pos::After(r) if r == pane => {
                return Err(bad_request(format!(
                    "pane.move: {pane} cannot be placed next to itself"
                )));
            }
            Pos::Before(r) => {
                check_ref(st, &r, target)?;
                LPos::Before(r)
            }
            Pos::After(r) => {
                check_ref(st, &r, target)?;
                LPos::After(r)
            }
        };
        let ty = layout_of(st, &src)
            .and_then(|l| l.pane_type(pane))
            .unwrap_or_else(|| pane_type(&ty_name));
        if let Some(l) = layout_mut_of(st, &src) {
            l.remove(pane.to_string());
        }
        let was_empty = st.layout.group(target).is_empty();
        st.layout.insert(pane.to_string(), ty);
        st.layout
            .move_to(pane.to_string(), target, lpos)
            .map_err(|e| bad_request(format!("pane.move: {e}")))?;
        if was_empty {
            st.layout.select(pane.to_string());
        }
        st.panes.set_page(pane, page);
        if ty_name != "history"
            && let Some(p) = st.fleet_pages.pages.get_mut(page)
        {
            p.reclaimed = false;
        }
        boot::log(
            "INFO",
            &format!("pane {pane} moved from page {src:?} to page {page:?}"),
        );
        after_layout_change(st, true);
        Ok(json!({}))
    })
}
