//! M2.5 `web.*` relay (PROTOCOL.md §4.18–§4.22, §6.3 `0x84` / `0x04`; design.md §3.5.3–§3.5.4): the four control
//! commands, the per-viewer window bookkeeping and the page push. Signatures from W0 (ctl-1, brief-m25 §3.1);
//! W2 (ctl-1) implements them. `web.eval` is not here: `handlers` answers it `not_implemented` (ruling M87).
//!
//! Ownership and hook points:
//! - `handlers::handle` calls [`open`] / [`goto`] / [`url`] / [`reload`] **outside** the state lock: each one
//!   relays under the lock, then waits up to [`REPLY_TIMEOUT`] for the replies without holding it.
//! - `view::handle_frame` (attach-1's file) passes every `Frame::WebReply` payload to [`on_client_line`] and sends
//!   the frames it returns to that viewer.
//! - The end of a view connection (`view.rs`, next to `st.viewers.remove(id)`) calls [`viewer_gone`].
//! - The bookkeeping lives in `State::web` ([`WebHub`]).

use std::collections::{BTreeMap, BTreeSet};
use std::io;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use fleetmux_proto::view::{self, Frame};
use fleetmux_proto::{ErrorCode, b64};
use serde_json::{Map, Value, json};
use tokio::sync::{Mutex, oneshot};
use tokio::time::{Instant, timeout_at};

use super::State;
use super::boot::log;
use super::view::ViewerId;

/// How long a relayed command waits for the clients' `0x04` replies before `web_timeout` (PROTOCOL.md §4.18).
pub const REPLY_TIMEOUT: Duration = Duration::from_secs(3);

/// The result of one `web.*` command: the reply body, or a PROTOCOL.md §5 code + English sentence.
pub type WebResult = Result<Value, (ErrorCode, String)>;

/// Which viewers have a web window, which one is primary, and the relays waiting for replies. One per server,
/// in `State::web`.
#[derive(Debug, Default)]
pub struct WebHub {
    /// Viewers whose client reported `{"event":"window","state":"open"}` and neither `closed` nor left since.
    windows: BTreeSet<ViewerId>,
    /// The viewer that most recently reported `open` (PROTOCOL.md §4.18 "primary window").
    primary: Option<ViewerId>,
    /// `seq` of the next relayed `0x84`.
    next_seq: u64,
    /// Relays waiting for `0x04` replies, by `seq` (one `seq` per command; every target answers it once).
    pending: BTreeMap<u64, Relay>,
    /// Viewers with a window, oldest `open` first: the primary falls back to the last one when it goes.
    opened: Vec<ViewerId>,
}

/// One relayed command waiting for its replies.
#[derive(Debug)]
struct Relay {
    /// Who must answer: the viewers it was sent to, or `None` = every viewer (`web.open`, sent with
    /// `ViewerHub::broadcast`), of which `expected` were connected when it was sent.
    targets: Option<BTreeSet<ViewerId>>,
    expected: usize,
    replies: BTreeMap<ViewerId, Value>,
    /// Fired once every target has answered (or left).
    done: Option<oneshot::Sender<()>>,
}

impl Relay {
    fn complete(&self) -> bool {
        match &self.targets {
            Some(t) => t.iter().all(|id| self.replies.contains_key(id)),
            None => self.replies.len() >= self.expected,
        }
    }

    fn check(&mut self) {
        if self.complete()
            && let Some(done) = self.done.take()
        {
            let _ = done.send(());
        }
    }
}

impl WebHub {
    /// Does viewer `id` have a window?
    pub fn has_window(&self, id: ViewerId) -> bool {
        self.windows.contains(&id)
    }

    /// How many viewers have a window.
    pub fn windows(&self) -> usize {
        self.windows.len()
    }

    /// The primary window's viewer, if any viewer has a window.
    pub fn primary(&self) -> Option<ViewerId> {
        self.primary
    }

    fn window_open(&mut self, id: ViewerId) {
        self.windows.insert(id);
        self.opened.retain(|v| *v != id);
        self.opened.push(id);
        self.primary = Some(id);
    }

    /// `id` has no window any more (closed, or the viewer left): relays aimed at it stop waiting for it.
    fn window_gone(&mut self, id: ViewerId) {
        self.windows.remove(&id);
        self.opened.retain(|v| *v != id);
        if self.primary == Some(id) {
            self.primary = self.opened.last().copied();
        }
        for relay in self.pending.values_mut() {
            if let Some(t) = &mut relay.targets {
                t.remove(&id);
            }
            relay.check();
        }
    }

    fn reply(&mut self, id: ViewerId, seq: u64, v: Value) {
        let Some(relay) = self.pending.get_mut(&seq) else {
            return; // late (after its timeout) or unknown: nothing waits for it
        };
        if relay.targets.as_ref().is_some_and(|t| !t.contains(&id)) {
            return; // not sent to this viewer
        }
        relay.replies.entry(id).or_insert(v);
        relay.check();
    }
}

/// Who a relay goes to.
enum To {
    Every,
    Viewers(Vec<ViewerId>),
}

/// What came back for one relay after its 3 s deadline (or once everyone answered).
struct Outcome {
    /// Who it was sent to (`None` = every viewer).
    targets: Option<BTreeSet<ViewerId>>,
    replies: BTreeMap<ViewerId, Value>,
}

/// Send one `0x84` (`fields` + `seq`) under the lock, then wait without it until every target answered or
/// [`REPLY_TIMEOUT`] passed.
async fn relay(state: &Arc<Mutex<State>>, to: impl FnOnce(&State) -> To, fields: Value) -> Outcome {
    let deadline = Instant::now() + REPLY_TIMEOUT;
    let (seq, done) = {
        let mut st = state.lock().await;
        st.web.next_seq += 1;
        let seq = st.web.next_seq;
        let mut line = fields;
        line["seq"] = json!(seq);
        let frame = Frame::Web(line.to_string());
        let (targets, expected) = match to(&st) {
            To::Every => {
                st.viewers.broadcast(frame);
                (None, st.viewers.len())
            }
            To::Viewers(ids) => {
                for id in &ids {
                    st.viewers.send_to(*id, frame.clone());
                }
                let n = ids.len();
                (Some(ids.into_iter().collect()), n)
            }
        };
        let (tx, rx) = oneshot::channel();
        let mut relay = Relay {
            targets,
            expected,
            replies: BTreeMap::new(),
            done: Some(tx),
        };
        relay.check();
        st.web.pending.insert(seq, relay);
        (seq, rx)
    };
    let _ = timeout_at(deadline, done).await;
    let mut st = state.lock().await;
    let relay = st
        .web
        .pending
        .remove(&seq)
        .expect("relay is removed only here");
    Outcome {
        targets: relay.targets,
        replies: relay.replies,
    }
}

impl Outcome {
    /// `web_timeout` when a target that has a window (now) did not answer.
    fn check_timeouts(&self, st: &State, cmd: &str) -> Result<(), (ErrorCode, String)> {
        let mute = match &self.targets {
            Some(t) => t
                .iter()
                .filter(|id| !self.replies.contains_key(id) && st.web.has_window(**id))
                .count(),
            None => st
                .web
                .windows
                .iter()
                .filter(|id| !self.replies.contains_key(id))
                .count(),
        };
        if mute > 0 {
            return Err((
                ErrorCode::WebTimeout,
                format!("{cmd}: {mute} web window(s) did not answer within 3 s"),
            ));
        }
        Ok(())
    }

    fn ok_count(&self) -> usize {
        self.replies.values().filter(|v| is_ok(v)).count()
    }
}

fn is_ok(v: &Value) -> bool {
    v.get("ok") == Some(&Value::Bool(true))
}

fn no_window(cmd: &str) -> (ErrorCode, String) {
    (
        ErrorCode::NoWindow,
        format!("{cmd}: no viewer has a web window"),
    )
}

/// Connected viewers that have a window.
fn window_viewers(st: &State) -> Vec<ViewerId> {
    st.web
        .windows
        .iter()
        .copied()
        .filter(|id| st.viewers.contains(*id))
        .collect()
}

/// M4 (design m4 §4.2 `web.*` rows): only the connection of the current page reaches the windows, so a daemon
/// whose page is in the background never drives the one shared window.
fn is_current(st: &State, page: &str) -> bool {
    st.fleet_pages.current == page
}

/// The path part of a URL (no `?query`, no `#fragment`).
fn url_path(u: &str) -> &str {
    u.split(['?', '#']).next().unwrap_or(u)
}

/// `web.open {url?}` (§4.18): `0x84 open` to every viewer; `url` absent = kv `history_url` of the connection's page.
/// Result `{"windows"}`. M4: from a background page → `{"windows": 0}`, nothing relayed.
pub async fn open(state: &Arc<Mutex<State>>, url: Option<String>, page: &str) -> WebResult {
    if !is_current(&*state.lock().await, page) {
        return Ok(json!({ "windows": 0 }));
    }
    let url = match url {
        Some(u) => Some(u),
        None => super::handlers::page_kv(&*state.lock().await, page, "history_url"),
    };
    let mut fields = json!({"op": "open"});
    if let Some(u) = url {
        fields["url"] = json!(u);
    }
    let out = relay(state, |_| To::Every, fields).await;
    let st = state.lock().await;
    out.check_timeouts(&st, "web.open")?;
    Ok(json!({ "windows": st.web.windows() }))
}

/// `web.goto {url}` (§4.19): `0x84 goto` to every viewer with a window. Result `{"windows"}`; none → `no_window`.
/// M4: from a background page → `{"windows": 0}`, nothing relayed.
pub async fn goto(state: &Arc<Mutex<State>>, url: String, page: &str) -> WebResult {
    if !is_current(&*state.lock().await, page) {
        return Ok(json!({ "windows": 0 }));
    }
    if window_viewers(&*state.lock().await).is_empty() {
        return Err(no_window("web.goto"));
    }
    let out = relay(
        state,
        |st| To::Viewers(window_viewers(st)),
        json!({"op": "goto", "url": url}),
    )
    .await;
    out.check_timeouts(&*state.lock().await, "web.goto")?;
    Ok(json!({ "windows": out.ok_count() }))
}

/// `web.url` (§4.20): `0x84 url` to the primary window. Result `{"url"}`; none → `no_window`. M4: `no_window` too
/// from a background page, and — once the tree has child pages — when the window shows another page than this
/// page's `history_url` (path compared, query and fragment ignored).
pub async fn url(state: &Arc<Mutex<State>>, page: &str) -> WebResult {
    if !is_current(&*state.lock().await, page) {
        return Err(no_window("web.url"));
    }
    let answer = url_of_primary(state).await?;
    let st = state.lock().await;
    if !st.fleet_pages.pages.is_empty()
        && let Some(own) = super::handlers::page_kv(&st, page, "history_url")
        && let Some(Value::String(u)) = answer.get("url")
        && url_path(u) != url_path(&own)
    {
        return Err(no_window("web.url"));
    }
    Ok(answer)
}

/// M4 `page.select` (§4.25): `0x84 goto <url>` to every viewer with a window; the outcome is only logged.
pub async fn push_goto(state: &Arc<Mutex<State>>, url: String) {
    if window_viewers(&*state.lock().await).is_empty() {
        return;
    }
    let out = relay(
        state,
        |st| To::Viewers(window_viewers(st)),
        json!({"op": "goto", "url": url}),
    )
    .await;
    if let Err((_, e)) = out.check_timeouts(&*state.lock().await, "page.select goto") {
        log("INFO", &e);
    }
}

async fn url_of_primary(state: &Arc<Mutex<State>>) -> WebResult {
    let primary = |st: &State| st.web.primary().filter(|id| st.viewers.contains(*id));
    let Some(id) = primary(&*state.lock().await) else {
        return Err(no_window("web.url"));
    };
    let out = relay(state, |_| To::Viewers(vec![id]), json!({"op": "url"})).await;
    match out.replies.get(&id) {
        Some(v) if is_ok(v) => match v.get("url") {
            Some(Value::String(u)) => Ok(json!({ "url": u })),
            _ => Err((
                ErrorCode::Internal,
                "web.url: the window's reply has no url".into(),
            )),
        },
        Some(v) => Err((
            ErrorCode::Internal,
            format!("web.url: the window failed: {}", reply_error(v)),
        )),
        None if state.lock().await.web.has_window(id) => Err((
            ErrorCode::WebTimeout,
            "web.url: the primary web window did not answer within 3 s".into(),
        )),
        None => Err(no_window("web.url")),
    }
}

/// `web.reload` (§4.21): `0x84 reload` to every viewer with a window, page pushed first where it changed.
/// Result `{"windows"}` (0 is not an error). M4: from a background page → `{"windows": 0}`, nothing relayed.
pub async fn reload(state: &Arc<Mutex<State>>, page: &str) -> WebResult {
    if !is_current(&*state.lock().await, page) || window_viewers(&*state.lock().await).is_empty() {
        return Ok(json!({ "windows": 0 }));
    }
    let out = relay(
        state,
        |st| To::Viewers(window_viewers(st)),
        json!({"op": "reload"}),
    )
    .await;
    out.check_timeouts(&*state.lock().await, "web.reload")?;
    Ok(json!({ "windows": out.ok_count() }))
}

fn reply_error(v: &Value) -> String {
    v.get("error")
        .and_then(Value::as_str)
        .unwrap_or("no reason given")
        .to_owned()
}

/// One `0x04` payload from viewer `id` (PROTOCOL.md §6.3): a reply `{"seq","ok",…}` completes its pending relay;
/// `{"event":"window","state":"open"|"closed"}` updates [`WebHub`]; `{"op":"page","path","have"}` is answered.
/// Returns the frames to send back to that viewer (the `0x84` carrying `page`). A line that is not one of
/// these is ignored (logged), never fatal for the connection.
pub fn on_client_line(st: &mut State, id: ViewerId, line: &str) -> Vec<Frame> {
    let obj = match serde_json::from_str::<Value>(line) {
        Ok(Value::Object(o)) => o,
        _ => {
            log(
                "WARN",
                &format!("viewer {id}: 0x04 is not a JSON object, ignored"),
            );
            return Vec::new();
        }
    };
    if obj.get("event").and_then(Value::as_str) == Some("window") {
        match obj.get("state").and_then(Value::as_str) {
            Some("open") => st.web.window_open(id),
            Some("closed") => st.web.window_gone(id),
            _ => log(
                "WARN",
                &format!("viewer {id}: 0x04 window event without open / closed, ignored"),
            ),
        }
        return Vec::new();
    }
    if obj.get("op").and_then(Value::as_str) == Some("page") {
        return vec![Frame::Web(page_answer(id, &obj))];
    }
    if let (Some(seq), Some(_)) = (obj.get("seq").and_then(Value::as_u64), obj.get("ok")) {
        st.web.reply(id, seq, Value::Object(obj));
        return Vec::new();
    }
    log("WARN", &format!("viewer {id}: unknown 0x04 line, ignored"));
    Vec::new()
}

/// The `0x84` answering `{"seq"?,"op":"page","path","have"}`: `{"seq"?,"op":"page","page":{…}}`, or, when the
/// page cannot be read or would not fit in one `0x84`, `{"seq"?,"op":"page","error":…}` (no bytes sent).
fn page_answer(id: ViewerId, req: &Map<String, Value>) -> String {
    let mut out = json!({"op": "page"});
    if let Some(seq) = req.get("seq") {
        out["seq"] = seq.clone();
    }
    let have = req.get("have").and_then(|h| {
        Some(PageStat {
            size: h.get("size")?.as_u64()?,
            mtime: h.get("mtime")?.as_i64()?,
        })
    });
    let result = match req.get("path").and_then(Value::as_str) {
        Some(p) if Path::new(p).is_absolute() => page_for(Path::new(p), have),
        _ => Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "path is not an absolute path",
        )),
    };
    match result {
        Ok(msg) => out["page"] = page_json(&msg),
        Err(e) => {
            log(
                "WARN",
                &format!("viewer {id}: page request not served: {e}"),
            );
            out["error"] = json!(format!("page not sent: {e}"));
        }
    }
    out.to_string()
}

fn page_json(msg: &PageMsg) -> Value {
    let mut page = json!({"path": msg.path, "size": msg.size, "mtime": msg.mtime});
    if let Some(b) = &msg.b64 {
        page["b64"] = json!(b);
    }
    page
}

/// The view connection of viewer `id` ended: it no longer has a window; a primary moves to the most recent
/// remaining `open`; its pending relays stop waiting for it.
pub fn viewer_gone(st: &mut State, id: ViewerId) {
    st.web.window_gone(id);
}

/// What a copy of the page is: `size` in bytes, `mtime` in nanoseconds since the Unix epoch (PROTOCOL.md §4.21).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PageStat {
    pub size: u64,
    pub mtime: i64,
}

/// The server's answer to a `page` request (the `page` object of a `0x84`, design.md §3.5.3).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PageMsg {
    pub path: String,
    pub size: u64,
    pub mtime: i64,
    /// The whole file, base64; `None` when the client's `have` already equals the server's copy.
    pub b64: Option<String>,
}

/// `size` / `mtime` of the file at `path`.
pub fn stat_page(path: &Path) -> io::Result<PageStat> {
    let meta = std::fs::metadata(path)?;
    if !meta.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "not a regular file",
        ));
    }
    let modified = meta.modified()?;
    let mtime = match modified.duration_since(UNIX_EPOCH) {
        Ok(d) => i64::try_from(d.as_nanos()).unwrap_or(i64::MAX),
        Err(e) => -i64::try_from(e.duration().as_nanos()).unwrap_or(i64::MAX),
    };
    Ok(PageStat {
        size: meta.len(),
        mtime,
    })
}

/// The page answer for a client that has `have` (`None` = no copy): `b64` only when `have` differs from the
/// file at `path`. An error when the file cannot be read or the `0x84` would be over `view::WEB_FRAME_MAX`.
pub fn page_for(path: &Path, have: Option<PageStat>) -> io::Result<PageMsg> {
    let text = path
        .to_str()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "path is not UTF-8"))?
        .to_owned();
    let stat = stat_page(path)?;
    let too_big = || {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "the page does not fit in one 0x84 frame ({} bytes max)",
                view::WEB_FRAME_MAX
            ),
        )
    };
    let mut msg = PageMsg {
        path: text,
        size: stat.size,
        mtime: stat.mtime,
        b64: None,
    };
    if have == Some(stat) {
        return Ok(msg);
    }
    // base64 is 4/3 of the file: refuse before reading when it cannot fit.
    if stat.size.div_ceil(3).saturating_mul(4) > view::WEB_FRAME_MAX as u64 {
        return Err(too_big());
    }
    let bytes = std::fs::read(path)?;
    msg.size = bytes.len() as u64;
    msg.b64 = Some(b64::encode(&bytes));
    // The whole line as it goes out, with the widest `seq`.
    let line = json!({"seq": u64::MAX, "op": "page", "page": page_json(&msg)}).to_string();
    if line.len() > view::max_payload(view::T_WEB) {
        return Err(too_big());
    }
    Ok(msg)
}
