//! fleetmux-proto: wire types and codecs for the fleetmux control protocol, the view connection and the
//! recording format. The normative text is `rust/PROTOCOL.md`; `rust/protocol/schema.json` is its
//! machine-readable twin. Signatures from phase 0 (plan.md §3.1) and stage A; bodies from B0 (proto-impl).

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Protocol version (PROTOCOL.md §2).
pub const PROTOCOL: u32 = 1;
/// Longest request or reply line, `\n` not counted (PROTOCOL.md §1.2).
pub const MAX_LINE: usize = 4 * 1024 * 1024;
/// `pane.kill` default and maximum `timeout_ms` (PROTOCOL.md §4.13).
pub const DEFAULT_KILL_TIMEOUT_MS: u32 = 5000;
pub const MAX_KILL_TIMEOUT_MS: u32 = 600_000;
/// `pane.kill` escalation (PROTOCOL.md §4.13, ruling R4): SIGHUP / SIGTERM / SIGKILL at these offsets, each sent only
/// when its offset is <= `timeout_ms`; then at most `KILL_REAP_GRACE_MS` more to confirm the reap, with no new signal.
pub const KILL_SCHEDULE_MS: [u32; 3] = [0, 2000, 5000];
pub const KILL_REAP_GRACE_MS: u32 = 500;

/// A pane id such as `k7q2-12` (PROTOCOL.md §1.4). Kept as the wire string; `fleetmux-layout` has its own type.
pub type PaneId = String;

/// One of the three tab groups (PROTOCOL.md §1.5).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Group {
    Supervisor,
    Lower,
    Workers,
}

impl Group {
    /// Wire name: `supervisor` / `lower` / `workers`.
    pub fn as_str(&self) -> &'static str {
        match self {
            Group::Supervisor => "supervisor",
            Group::Lower => "lower",
            Group::Workers => "workers",
        }
    }
    /// `None` for anything that is not one of the three names (→ `unknown_type`).
    pub fn parse(s: &str) -> Option<Group> {
        match s {
            "supervisor" => Some(Group::Supervisor),
            "lower" => Some(Group::Lower),
            "workers" => Some(Group::Workers),
            _ => None,
        }
    }
}

/// `pane.write` mode (PROTOCOL.md §4.7).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WriteMode {
    Raw,
    Bracketed,
    Paste,
}

impl WriteMode {
    pub fn as_str(&self) -> &'static str {
        match self {
            WriteMode::Raw => "raw",
            WriteMode::Bracketed => "bracketed",
            WriteMode::Paste => "paste",
        }
    }
    pub fn parse(s: &str) -> Option<WriteMode> {
        match s {
            "raw" => Some(WriteMode::Raw),
            "bracketed" => Some(WriteMode::Bracketed),
            "paste" => Some(WriteMode::Paste),
            _ => None,
        }
    }
}

/// Target position of `pane.move`: `before` / `after` a pane, or the end of the group (PROTOCOL.md §4.12).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Pos {
    Before(PaneId),
    After(PaneId),
    End,
}

/// Arguments of `pane.spawn` (PROTOCOL.md §4.4).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Spawn {
    pub argv: Vec<String>,
    pub cwd: Option<String>,
    pub env: BTreeMap<String, String>,
    pub title: String,
    /// The `type` argument, verbatim (`""` when absent).
    pub ty: String,
    pub group: Option<Group>,
    pub anchor: Option<PaneId>,
    pub before: bool,
}

/// The 22 commands of PROTOCOL.md §4, one variant each, arguments decoded and defaults applied.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Hello {
        protocol: Option<u32>,
    },
    KvSet {
        key: String,
        value: String,
    },
    KvGet {
        key: String,
    },
    PaneSpawn(Spawn),
    PaneList,
    PaneGet {
        pane: PaneId,
    },
    /// `data` is already base64-decoded.
    PaneWrite {
        pane: PaneId,
        data: Vec<u8>,
        mode: WriteMode,
    },
    PaneKey {
        pane: PaneId,
        key: String,
    },
    PaneRead {
        pane: PaneId,
        lines: u32,
    },
    PaneRename {
        pane: PaneId,
        title: String,
    },
    PaneFocus {
        pane: PaneId,
    },
    PaneMove {
        pane: PaneId,
        group: Group,
        pos: Pos,
    },
    PaneKill {
        pane: PaneId,
        timeout_ms: u32,
    },
    LayoutGet,
    /// Ratios as sent (the server clamps to 10–90). `zoom`: `None` = leave alone, `Some(None)` = unzoom.
    LayoutSet {
        h_ratio: Option<i64>,
        v_ratio: Option<i64>,
        zoom: Option<Option<PaneId>>,
    },
    ServerShutdown {
        force: bool,
    },
    /// Stage A (A-R2): no arguments; the reply body is a [`Stats`] (PROTOCOL.md §4.17).
    ServerStats,
    /// M2.5 (brief-m25 W0): open the web window on every viewer; `url` absent = kv `history_url` (§4.18).
    WebOpen {
        url: Option<String>,
    },
    /// M2.5: navigate every viewer that has a web window (§4.19).
    WebGoto {
        url: String,
    },
    /// M2.5: the primary window's current URL (§4.20).
    WebUrl,
    /// M2.5: reload every window, pushing the page first when it changed (§4.21).
    WebReload,
    /// M2.5: reserved; the server always answers `not_implemented` (§4.22, supervisor ruling M87).
    WebEval {
        js: String,
    },
    /// M4 (§4.23, capability `pages:1`): create, reopen or rename a child fleet page. `page` is a valid non-root id
    /// ([`valid_page_id`]); `title` absent = the id on create, unchanged otherwise; `reclaimed` makes / keeps it a
    /// read-only page whose kv `history_url` is set to `history_url`.
    PageOpen {
        page: String,
        title: Option<String>,
        reclaimed: bool,
        history_url: Option<String>,
    },
    /// M4 (§4.24): `keep` (default true) → the page becomes `reclaimed` and stays; `false` removes it.
    PageClose {
        page: String,
        keep: bool,
    },
    /// M4 (§4.25): make `page` the current page of every viewer (`""` = the root page).
    PageSelect {
        page: String,
    },
    /// M4 (§4.26): every page, root first.
    PageList,
}

impl Command {
    /// Wire name, e.g. `pane.spawn`.
    pub fn name(&self) -> &'static str {
        match self {
            Command::Hello { .. } => "hello",
            Command::KvSet { .. } => "kv.set",
            Command::KvGet { .. } => "kv.get",
            Command::PaneSpawn(_) => "pane.spawn",
            Command::PaneList => "pane.list",
            Command::PaneGet { .. } => "pane.get",
            Command::PaneWrite { .. } => "pane.write",
            Command::PaneKey { .. } => "pane.key",
            Command::PaneRead { .. } => "pane.read",
            Command::PaneRename { .. } => "pane.rename",
            Command::PaneFocus { .. } => "pane.focus",
            Command::PaneMove { .. } => "pane.move",
            Command::PaneKill { .. } => "pane.kill",
            Command::LayoutGet => "layout.get",
            Command::LayoutSet { .. } => "layout.set",
            Command::ServerShutdown { .. } => "server.shutdown",
            Command::ServerStats => "server.stats",
            Command::WebOpen { .. } => "web.open",
            Command::WebGoto { .. } => "web.goto",
            Command::WebUrl => "web.url",
            Command::WebReload => "web.reload",
            Command::WebEval { .. } => "web.eval",
            Command::PageOpen { .. } => "page.open",
            Command::PageClose { .. } => "page.close",
            Command::PageSelect { .. } => "page.select",
            Command::PageList => "page.list",
        }
    }
}

const KNOWN_COMMANDS: [&str; 22] = [
    "hello",
    "kv.set",
    "kv.get",
    "pane.spawn",
    "pane.list",
    "pane.get",
    "pane.write",
    "pane.key",
    "pane.read",
    "pane.rename",
    "pane.focus",
    "pane.move",
    "pane.kill",
    "layout.get",
    "layout.set",
    "server.shutdown",
    "server.stats",
    "web.open",
    "web.goto",
    "web.url",
    "web.reload",
    "web.eval",
];

/// Every `cmd` name of PROTOCOL.md §4.1–§4.22, in §4 order (checked against schema.json). The M4 page commands
/// are listed apart in [`page_commands`]: they exist only on a server that announces `pages:1` (§2 addendum).
pub fn known_commands() -> &'static [&'static str] {
    &KNOWN_COMMANDS
}

const PAGE_COMMANDS: [&str; 4] = ["page.open", "page.close", "page.select", "page.list"];

/// M4: the page commands of PROTOCOL.md §4.23–§4.26 (capability `pages:1`), in §4 order.
pub fn page_commands() -> &'static [&'static str] {
    &PAGE_COMMANDS
}

/// M4: at most this many child pages (root not counted, reclaimed ones counted); the next `page.open` that would
/// create one is `bad_request` (design m4 R14).
pub const MAX_PAGES: usize = 64;

/// M4: a child page id is 1–64 characters of `A-Z a-z 0-9 . _ -` (PROTOCOL.md §4.23). The root page is `""`.
pub fn valid_page_id(id: &str) -> bool {
    (1..=64).contains(&id.len())
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}

/// A decoded control request line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    pub id: u64,
    pub cmd: Command,
    /// M4 (§2 addendum): the optional `page` of `hello` (bind this connection) and `pane.spawn` (spawn into that
    /// page). Absent, `null` or `""` = `None` (the connection's own page / the root page). Other commands: `None`.
    pub page: Option<String>,
    /// M4: `pane.list {all: true}` lists every page. Other commands: `false`.
    pub all: bool,
}

/// The fixed error-code set (PROTOCOL.md §5).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ErrorCode {
    BadRequest,
    UnknownCmd,
    NoSuchPane,
    UnknownKey,
    UnknownType,
    NotEmpty,
    SpawnFailed,
    KillTimeout,
    WriteFailed,
    ProtocolVersion,
    Internal,
    /// M2.5: no viewer has a web window (`web.goto` / `web.url`).
    NoWindow,
    /// M2.5: the window's client did not answer within 3 s.
    WebTimeout,
    /// M2.5: the command is reserved but not implemented (`web.eval`).
    NotImplemented,
}

impl ErrorCode {
    pub const ALL: [ErrorCode; 14] = [
        ErrorCode::BadRequest,
        ErrorCode::UnknownCmd,
        ErrorCode::NoSuchPane,
        ErrorCode::UnknownKey,
        ErrorCode::UnknownType,
        ErrorCode::NotEmpty,
        ErrorCode::SpawnFailed,
        ErrorCode::KillTimeout,
        ErrorCode::WriteFailed,
        ErrorCode::ProtocolVersion,
        ErrorCode::Internal,
        ErrorCode::NoWindow,
        ErrorCode::WebTimeout,
        ErrorCode::NotImplemented,
    ];
    /// Wire form, snake_case (`no_such_pane`).
    pub fn as_str(&self) -> &'static str {
        match self {
            ErrorCode::BadRequest => "bad_request",
            ErrorCode::UnknownCmd => "unknown_cmd",
            ErrorCode::NoSuchPane => "no_such_pane",
            ErrorCode::UnknownKey => "unknown_key",
            ErrorCode::UnknownType => "unknown_type",
            ErrorCode::NotEmpty => "not_empty",
            ErrorCode::SpawnFailed => "spawn_failed",
            ErrorCode::KillTimeout => "kill_timeout",
            ErrorCode::WriteFailed => "write_failed",
            ErrorCode::ProtocolVersion => "protocol_version",
            ErrorCode::Internal => "internal",
            ErrorCode::NoWindow => "no_window",
            ErrorCode::WebTimeout => "web_timeout",
            ErrorCode::NotImplemented => "not_implemented",
        }
    }
    pub fn parse(s: &str) -> Option<ErrorCode> {
        ErrorCode::ALL.into_iter().find(|c| c.as_str() == s)
    }
}

/// A reply line. `Ok.body` is a JSON object whose keys are spliced next to `id` / `ok`; it never contains
/// `id`, `ok`, `code` or `error`. `Err.id` is `None` when the request's id could not be recovered.
#[derive(Debug, Clone, PartialEq)]
pub enum Reply {
    Ok {
        id: u64,
        body: Value,
    },
    Err {
        id: Option<u64>,
        code: ErrorCode,
        error: String,
    },
}

/// Why a request line could not be decoded. `code` is `BadRequest` or `UnknownCmd`, or `UnknownType` for a `group` /
/// `mode` string outside its enumeration (R5); `id` is the request id when it was recoverable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodeError {
    pub id: Option<u64>,
    pub code: ErrorCode,
    pub message: String,
}

impl DecodeError {
    /// The error reply to send back for this line.
    pub fn into_reply(self) -> Reply {
        Reply::Err {
            id: self.id,
            code: self.code,
            error: self.message,
        }
    }
}

/// Largest request id (PROTOCOL.md §1.3: 0 ≤ id ≤ 2^53−1).
const MAX_ID: u64 = (1 << 53) - 1;

/// `line` without one trailing `\n` (a `\r` before it is JSON whitespace).
fn strip_newline(line: &[u8]) -> &[u8] {
    line.strip_suffix(b"\n").unwrap_or(line)
}

fn bad_request(id: Option<u64>, message: impl Into<String>) -> DecodeError {
    DecodeError {
        id,
        code: ErrorCode::BadRequest,
        message: message.into(),
    }
}

/// Parse one line as a JSON object (UTF-8, JSON whitespace allowed, at most `MAX_LINE` bytes).
fn parse_object(line: &[u8]) -> Result<Map<String, Value>, String> {
    let line = strip_newline(line);
    if line.len() > MAX_LINE {
        return Err(format!(
            "line of {} bytes is over the {MAX_LINE}-byte limit",
            line.len()
        ));
    }
    match serde_json::from_slice::<Value>(line) {
        Ok(Value::Object(obj)) => Ok(obj),
        Ok(_) => Err("a request line is a JSON object".into()),
        Err(e) => Err(format!("not a JSON line: {e}")),
    }
}

/// The arguments of one request, read with PROTOCOL.md §1.3 rules: a known key with the wrong JSON type is
/// `bad_request`; an optional key set to `null` counts as absent.
struct Args<'a> {
    obj: &'a Map<String, Value>,
    id: u64,
    cmd: &'a str,
}

impl Args<'_> {
    fn bad(&self, message: impl std::fmt::Display) -> DecodeError {
        bad_request(Some(self.id), format!("{}: {message}", self.cmd))
    }

    fn get(&self, key: &str) -> Option<&Value> {
        self.obj.get(key).filter(|v| !v.is_null())
    }

    fn required<T>(&self, key: &str, v: Result<Option<T>, DecodeError>) -> Result<T, DecodeError> {
        v?.ok_or_else(|| self.bad(format!("missing {key:?}")))
    }

    fn str_opt(&self, key: &str) -> Result<Option<String>, DecodeError> {
        match self.get(key) {
            None => Ok(None),
            Some(Value::String(s)) => Ok(Some(s.clone())),
            Some(_) => Err(self.bad(format!("{key:?} must be a string"))),
        }
    }

    fn str_req(&self, key: &str) -> Result<String, DecodeError> {
        self.required(key, self.str_opt(key))
    }

    fn bool_opt(&self, key: &str) -> Result<Option<bool>, DecodeError> {
        match self.get(key) {
            None => Ok(None),
            Some(Value::Bool(b)) => Ok(Some(*b)),
            Some(_) => Err(self.bad(format!("{key:?} must be a boolean"))),
        }
    }

    /// An integer argument; values beyond `i64` saturate (every range check below is far inside it).
    fn int_opt(&self, key: &str) -> Result<Option<i64>, DecodeError> {
        match self.get(key) {
            None => Ok(None),
            Some(Value::Number(n)) if n.is_i64() || n.is_u64() => {
                Ok(Some(n.as_i64().unwrap_or(i64::MAX)))
            }
            Some(_) => Err(self.bad(format!("{key:?} must be an integer"))),
        }
    }

    fn int_req(&self, key: &str) -> Result<i64, DecodeError> {
        self.required(key, self.int_opt(key))
    }

    /// A closed-enum string: wrong JSON type → `bad_request`, unknown name → `unknown_type` (R5).
    fn enum_opt<T>(
        &self,
        key: &str,
        parse: impl Fn(&str) -> Option<T>,
    ) -> Result<Option<T>, DecodeError> {
        match self.str_opt(key)? {
            None => Ok(None),
            Some(s) => parse(&s).map(Some).ok_or_else(|| DecodeError {
                id: Some(self.id),
                code: ErrorCode::UnknownType,
                message: format!("{}: unknown {key} {s:?}", self.cmd),
            }),
        }
    }

    fn group_opt(&self) -> Result<Option<Group>, DecodeError> {
        self.enum_opt("group", Group::parse)
    }

    fn group_req(&self) -> Result<Group, DecodeError> {
        self.required("group", self.group_opt())
    }

    fn pane(&self) -> Result<PaneId, DecodeError> {
        self.str_req("pane")
    }

    fn spawn(&self) -> Result<Spawn, DecodeError> {
        let argv = match self.get("argv") {
            Some(Value::Array(items)) if !items.is_empty() => items
                .iter()
                .map(|a| a.as_str().map(str::to_owned))
                .collect::<Option<Vec<_>>>()
                .ok_or_else(|| self.bad("\"argv\" must hold only strings"))?,
            Some(Value::Array(_)) => return Err(self.bad("\"argv\" must not be empty")),
            Some(_) => return Err(self.bad("\"argv\" must be an array of strings")),
            None => return Err(self.bad("missing \"argv\"")),
        };
        let cwd = self.str_opt("cwd")?;
        if cwd.as_deref().is_some_and(|c| !c.starts_with('/')) {
            return Err(self.bad("\"cwd\" must be an absolute path"));
        }
        let env = match self.get("env") {
            None => BTreeMap::new(),
            Some(Value::Object(m)) => m
                .iter()
                .map(|(k, v)| v.as_str().map(|v| (k.clone(), v.to_owned())))
                .collect::<Option<BTreeMap<_, _>>>()
                .ok_or_else(|| self.bad("\"env\" must map strings to strings"))?,
            Some(_) => return Err(self.bad("\"env\" must be an object")),
        };
        let title = self.str_opt("title")?.unwrap_or_default();
        let ty = self.str_opt("type")?.unwrap_or_default();
        let group = self.group_opt()?;
        let anchor = self.str_opt("anchor")?;
        let before = self.bool_opt("before")?;
        if before.is_some() && anchor.is_none() {
            return Err(self.bad("\"before\" given without \"anchor\""));
        }
        Ok(Spawn {
            argv,
            cwd,
            env,
            title,
            ty,
            group,
            anchor,
            before: before.unwrap_or(false),
        })
    }

    /// M4: an optional page id argument; `""` counts as absent (the root page).
    fn page_opt(&self, key: &str) -> Result<Option<String>, DecodeError> {
        match self.str_opt(key)? {
            None => Ok(None),
            Some(p) if p.is_empty() => Ok(None),
            Some(p) if valid_page_id(&p) => Ok(Some(p)),
            Some(p) => Err(self.bad(format!(
                "{key:?} {p:?} is not a page id (1-64 of A-Z a-z 0-9 . _ -)"
            ))),
        }
    }

    /// M4: a required child page id (the root `""` is refused).
    fn child_page(&self) -> Result<String, DecodeError> {
        match self.page_opt("page")? {
            Some(p) => Ok(p),
            None if self.get("page").is_some() => {
                Err(self.bad("the root page \"\" cannot be opened or closed"))
            }
            None => Err(self.bad("missing \"page\"")),
        }
    }

    fn command(&self) -> Result<Command, DecodeError> {
        Ok(match self.cmd {
            "hello" => Command::Hello {
                protocol: match self.int_opt("protocol")? {
                    None => None,
                    Some(p) => {
                        Some(u32::try_from(p).map_err(|_| self.bad("\"protocol\" out of range"))?)
                    }
                },
            },
            "kv.set" => {
                let key = self.str_req("key")?;
                if key.is_empty() || key.len() > 256 {
                    return Err(self.bad("\"key\" must be 1-256 bytes"));
                }
                let value = self.str_req("value")?;
                if value.len() > 64 * 1024 {
                    return Err(self.bad("\"value\" is over 64 KiB"));
                }
                Command::KvSet { key, value }
            }
            "kv.get" => Command::KvGet {
                key: self.str_req("key")?,
            },
            "pane.spawn" => Command::PaneSpawn(self.spawn()?),
            "pane.list" => Command::PaneList,
            "pane.get" => Command::PaneGet { pane: self.pane()? },
            "pane.write" => {
                let pane = self.pane()?;
                let data = b64::decode(&self.str_req("data")?)
                    .map_err(|e| self.bad(format!("\"data\" is not base64 (offset {})", e.at)))?;
                let mode = self.required("mode", self.enum_opt("mode", WriteMode::parse))?;
                Command::PaneWrite { pane, data, mode }
            }
            "pane.key" => Command::PaneKey {
                pane: self.pane()?,
                key: self.str_req("key")?,
            },
            "pane.read" => {
                let pane = self.pane()?;
                let lines = self.int_req("lines")?;
                if lines < 1 {
                    return Err(self.bad("\"lines\" must be >= 1"));
                }
                Command::PaneRead {
                    pane,
                    lines: u32::try_from(lines).unwrap_or(u32::MAX),
                }
            }
            "pane.rename" => Command::PaneRename {
                pane: self.pane()?,
                title: self.str_req("title")?,
            },
            "pane.focus" => Command::PaneFocus { pane: self.pane()? },
            "pane.move" => {
                let pane = self.pane()?;
                let group = self.group_req()?;
                let pos = match (self.str_opt("before")?, self.str_opt("after")?) {
                    (Some(_), Some(_)) => {
                        return Err(self.bad("\"before\" and \"after\" are exclusive"));
                    }
                    (Some(b), None) => Pos::Before(b),
                    (None, Some(a)) => Pos::After(a),
                    (None, None) => Pos::End,
                };
                Command::PaneMove { pane, group, pos }
            }
            "pane.kill" => {
                let pane = self.pane()?;
                let timeout_ms = self
                    .int_opt("timeout_ms")?
                    .unwrap_or(i64::from(DEFAULT_KILL_TIMEOUT_MS));
                if !(0..=i64::from(MAX_KILL_TIMEOUT_MS)).contains(&timeout_ms) {
                    return Err(self.bad(format!("\"timeout_ms\" must be 0-{MAX_KILL_TIMEOUT_MS}")));
                }
                Command::PaneKill {
                    pane,
                    timeout_ms: timeout_ms as u32,
                }
            }
            "layout.get" => Command::LayoutGet,
            "layout.set" => Command::LayoutSet {
                h_ratio: self.int_opt("h_ratio")?,
                v_ratio: self.int_opt("v_ratio")?,
                zoom: match self.obj.get("zoom") {
                    None => None,
                    Some(Value::Null) => Some(None),
                    Some(Value::String(p)) => Some(Some(p.clone())),
                    Some(_) => return Err(self.bad("\"zoom\" must be a pane id or null")),
                },
            },
            "server.shutdown" => Command::ServerShutdown {
                force: self.bool_opt("force")?.unwrap_or(false),
            },
            "server.stats" => Command::ServerStats,
            "web.open" => Command::WebOpen {
                url: self.str_opt("url")?,
            },
            "web.goto" => Command::WebGoto {
                url: self.str_req("url")?,
            },
            "web.url" => Command::WebUrl,
            "web.reload" => Command::WebReload,
            "web.eval" => Command::WebEval {
                js: self.str_req("js")?,
            },
            "page.open" => Command::PageOpen {
                page: self.child_page()?,
                title: self.str_opt("title")?,
                reclaimed: self.bool_opt("reclaimed")?.unwrap_or(false),
                history_url: self.str_opt("history_url")?,
            },
            "page.close" => Command::PageClose {
                page: self.child_page()?,
                keep: self.bool_opt("keep")?.unwrap_or(true),
            },
            "page.select" => {
                // `""` (the root page) is a valid target here; anything else must be a page id.
                self.str_req("page")?;
                Command::PageSelect {
                    page: self.page_opt("page")?.unwrap_or_default(),
                }
            }
            "page.list" => Command::PageList,
            other => {
                return Err(DecodeError {
                    id: Some(self.id),
                    code: ErrorCode::UnknownCmd,
                    message: format!("unknown command {other:?}"),
                });
            }
        })
    }
}

/// Decode one request line (with or without its trailing `\n`). Never panics on any input.
pub fn decode_line(line: &[u8]) -> Result<Request, DecodeError> {
    let obj = parse_object(line).map_err(|m| bad_request(None, m))?;
    let id = obj
        .get("id")
        .and_then(Value::as_u64)
        .filter(|&id| id <= MAX_ID)
        .ok_or_else(|| bad_request(None, "a request needs an integer \"id\" in 0..2^53-1"))?;
    let cmd = match obj.get("cmd") {
        Some(Value::String(c)) => c.as_str(),
        _ => return Err(bad_request(Some(id), "a request needs a string \"cmd\"")),
    };
    let args = Args { obj: &obj, id, cmd };
    let cmd = args.command()?;
    let (page, all) = match &cmd {
        Command::Hello { .. } | Command::PaneSpawn(_) => (args.page_opt("page")?, false),
        Command::PaneList => (None, args.bool_opt("all")?.unwrap_or(false)),
        _ => (None, false),
    };
    Ok(Request { id, cmd, page, all })
}

/// Encode a reply as one compact JSON line ending in exactly one `\n`, with no other raw `\n` in it.
pub fn encode_reply(reply: &Reply) -> Vec<u8> {
    let mut out = Map::new();
    match reply {
        Reply::Ok { id, body } => {
            out.insert("id".into(), Value::from(*id));
            out.insert("ok".into(), Value::Bool(true));
            if let Value::Object(body) = body {
                for (k, v) in body {
                    if !matches!(k.as_str(), "id" | "ok" | "code" | "error") {
                        out.insert(k.clone(), v.clone());
                    }
                }
            }
        }
        Reply::Err { id, code, error } => {
            out.insert("id".into(), id.map_or(Value::Null, Value::from));
            out.insert("ok".into(), Value::Bool(false));
            out.insert("code".into(), Value::from(code.as_str()));
            out.insert("error".into(), Value::from(error.as_str()));
        }
    }
    // serde_json escapes every control character inside strings, so the only raw `\n` is the terminator.
    let mut line = serde_json::to_vec(&Value::Object(out)).expect("a JSON value always serialises");
    line.push(b'\n');
    line
}

/// Peer check (PROTOCOL.md §3): a connection is served only when the peer uid equals the server's.
pub fn peer_allowed(server_uid: u32, peer_uid: u32) -> bool {
    server_uid == peer_uid
}

/// Result of `server.stats` (PROTOCOL.md §4.17): counters are cumulative since the server started.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct Stats {
    pub panes: u32,
    pub viewers: u32,
    /// Screen frames drawn, all viewers together.
    pub frames: u64,
    /// PTY output bytes fed to the parsers.
    pub parse_bytes: u64,
    /// Event-loop wake-ups; does not move while the server is idle (answering `server.stats` is not counted).
    pub wakeups: u64,
    pub rss_bytes: u64,
    /// Sum of every pane's scrollback rows.
    pub scrollback_rows: u64,
}

/// The pane record of `pane.list` / `pane.get` (PROTOCOL.md §4.0).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaneInfo {
    pub pane: PaneId,
    pub title: String,
    #[serde(rename = "type")]
    pub ty: String,
    pub group: Group,
    pub index: u32,
    pub pid: u32,
    pub pgid: u32,
    pub tty: String,
    pub cols: u16,
    pub rows: u16,
    pub focused: bool,
    /// Optional key (stage A P-2): output arrived while the pane was not displayed; absent = `false`.
    #[serde(default)]
    pub unseen: bool,
    /// Optional key (stage A P-2): current scrollback rows; absent = `0`.
    #[serde(default)]
    pub scrollback: u32,
}

/// Standard base64 (RFC 4648 §4, `+/`, `=` padding) used by `pane.write.data`. Ruling R12: M0 implements these two
/// functions on top of the `base64` 0.22 crate (added to the workspace by the integrator); no hand-written codec.
pub mod b64 {
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct B64Error {
        /// Byte offset of the first offending character.
        pub at: usize,
    }

    use base64::Engine;
    use base64::engine::general_purpose::STANDARD;

    pub fn encode(data: &[u8]) -> String {
        STANDARD.encode(data)
    }

    /// Rejects characters outside the alphabet, missing / misplaced padding and lengths not a multiple of 4.
    pub fn decode(s: &str) -> Result<Vec<u8>, B64Error> {
        // STANDARD requires canonical padding and rejects non-zero trailing bits.
        STANDARD.decode(s).map_err(|e| B64Error {
            at: match e {
                base64::DecodeError::InvalidByte(at, _)
                | base64::DecodeError::InvalidLastSymbol(at, _) => at,
                base64::DecodeError::InvalidLength(len) => len,
                base64::DecodeError::InvalidPadding => s.find('=').unwrap_or(s.len()),
            },
        })
    }
}

/// The view connection used by `fleetmux attach` (PROTOCOL.md §6).
pub mod view {
    use std::io::{self, Read, Write};

    use serde_json::{Map, Value, json};

    use crate::{DecodeError, ErrorCode, bad_request, parse_object};

    /// View-framing version carried in the handshake's `view` key.
    pub const VIEW_VERSION: u32 = 1;
    /// Largest frame payload, every type except `0x84` (PROTOCOL.md §6.2).
    pub const MAX_FRAME: usize = 1024 * 1024;
    /// M2.5: largest `0x84` (web command) payload; it can carry a whole `history.html` (PROTOCOL.md §6.2).
    pub const WEB_FRAME_MAX: usize = 32 * 1024 * 1024;

    pub const T_INPUT: u8 = 0x01;
    pub const T_RESIZE: u8 = 0x02;
    pub const T_DETACH: u8 = 0x03;
    /// gui-scrollbar: client → server, one JSON line: scroll a pane to an offset (PROTOCOL.md §6.3).
    pub const T_SCROLL_TO: u8 = 0x05;
    /// S1e: client → server, one JSON line `{"inset":N}`: the titlebar inset of that viewer (PROTOCOL.md §6.3).
    pub const T_INSET: u8 = 0x06;
    pub const T_SCREEN: u8 = 0x81;
    pub const T_EXIT: u8 = 0x82;
    /// Stage A (A-R1): server → client, open a local path (PROTOCOL.md §6.3).
    pub const T_OPEN: u8 = 0x83;
    /// M2.5 (brief-m25 W0): server → client, one JSON line for the client's web window (PROTOCOL.md §6.3).
    pub const T_WEB: u8 = 0x84;
    /// M2.5: client → server, one JSON line: a reply to a `0x84`, or a window / page event (PROTOCOL.md §6.3).
    pub const T_WEB_REPLY: u8 = 0x04;
    /// M2.6 (brief-m26 W0): server → client, one JSON line: where the history overlay goes (PROTOCOL.md §6.3).
    /// Sent only on a connection whose handshake said `"overlay":1`.
    pub const T_OVERLAY: u8 = 0x85;
    /// gui-scrollbar: server → client, one JSON line: every visible cell's scrollbar state (PROTOCOL.md §6.3).
    /// Sent only on a connection whose handshake said `"scroll":1`.
    pub const T_SCROLL: u8 = 0x86;

    /// One binary frame: 1 type byte + u32 big-endian payload length + payload.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub enum Frame {
        /// 0x01 client → server: raw bytes from the outer terminal.
        Input(Vec<u8>),
        /// 0x02 client → server: outer terminal size.
        Resize { cols: u16, rows: u16 },
        /// 0x03 client → server: detaching.
        Detach,
        /// 0x05 client → server: one JSON line `{"pane","offset"}`: scroll that pane there (PROTOCOL.md §6.3).
        ScrollTo(String),
        /// 0x06 client → server: one JSON line `{"inset":N}`: this viewer's titlebar inset in columns
        /// (PROTOCOL.md §6.3); sent only after the handshake's `ok` line carried `"inset":1`.
        Inset(String),
        /// 0x81 server → client: bytes to write to the outer terminal as they are.
        Screen(Vec<u8>),
        /// 0x82 server → client: server exiting, UTF-8 reason (may be empty).
        Exit(String),
        /// 0x83 server → client: open this UTF-8 local path on the client's host (empty = no `history_url`).
        Open(String),
        /// 0x84 server → client: one JSON line `{"seq","op",…}` for the web window; up to `WEB_FRAME_MAX`.
        Web(String),
        /// 0x04 client → server: one JSON line, a reply `{"seq","ok",…}` or an event (`window` / `page`).
        WebReply(String),
        /// 0x85 server → client: one JSON line, the history overlay (PROTOCOL.md §6.3); only after `"overlay":1`.
        Overlay(String),
        /// 0x86 server → client: one JSON line, the scrollbar state of every visible cell (PROTOCOL.md §6.3);
        /// only after `"scroll":1`.
        Scroll(String),
    }

    impl Frame {
        pub fn type_byte(&self) -> u8 {
            match self {
                Frame::Input(_) => T_INPUT,
                Frame::Resize { .. } => T_RESIZE,
                Frame::Detach => T_DETACH,
                Frame::ScrollTo(_) => T_SCROLL_TO,
                Frame::Inset(_) => T_INSET,
                Frame::Screen(_) => T_SCREEN,
                Frame::Exit(_) => T_EXIT,
                Frame::Open(_) => T_OPEN,
                Frame::Web(_) => T_WEB,
                Frame::WebReply(_) => T_WEB_REPLY,
                Frame::Overlay(_) => T_OVERLAY,
                Frame::Scroll(_) => T_SCROLL,
            }
        }

        fn payload(&self) -> Vec<u8> {
            match self {
                Frame::Input(b) | Frame::Screen(b) => b.clone(),
                Frame::Resize { cols, rows } => [cols.to_be_bytes(), rows.to_be_bytes()].concat(),
                Frame::Detach => Vec::new(),
                Frame::Exit(s)
                | Frame::Open(s)
                | Frame::Web(s)
                | Frame::WebReply(s)
                | Frame::Overlay(s)
                | Frame::ScrollTo(s)
                | Frame::Inset(s)
                | Frame::Scroll(s) => s.as_bytes().to_vec(),
            }
        }
    }

    #[derive(Debug)]
    pub enum FrameError {
        Io(io::Error),
        /// Header announced (or a writer was given) a payload over `max_payload(type)`; the payload was not read.
        Oversize {
            len: u64,
        },
        UnknownType(u8),
        /// Payload length not allowed for the type (resize ≠ 4, detach ≠ 0, resize with a 0 dimension).
        BadLength {
            ty: u8,
            len: u32,
        },
        /// Exit reason, open path or web line is not UTF-8.
        BadUtf8,
    }

    /// M2.5: the largest payload allowed for frame type `ty`: `WEB_FRAME_MAX` for `0x84`, `MAX_FRAME` for every
    /// other type (PROTOCOL.md §6.2). `check_header` / `encode_frame` use it.
    pub fn max_payload(ty: u8) -> usize {
        if ty == T_WEB {
            WEB_FRAME_MAX
        } else {
            MAX_FRAME
        }
    }

    /// Read one frame. `Ok(None)` on clean EOF before the first header byte; EOF inside a frame is `Io`.
    pub fn read_frame<R: Read>(r: &mut R) -> Result<Option<Frame>, FrameError> {
        let mut hdr = [0u8; 5];
        loop {
            match r.read(&mut hdr[..1]) {
                Ok(0) => return Ok(None),
                Ok(_) => break,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(FrameError::Io(e)),
            }
        }
        r.read_exact(&mut hdr[1..]).map_err(FrameError::Io)?;
        let len = check_header(&hdr)?;
        let mut payload = vec![0u8; len];
        r.read_exact(&mut payload).map_err(FrameError::Io)?;
        parse_payload(hdr[0], payload).map(Some)
    }

    /// Write one frame (header + payload). Refuses payloads over `max_payload(type)` without writing anything.
    pub fn write_frame<W: Write>(w: &mut W, frame: &Frame) -> Result<(), FrameError> {
        let bytes = encode_frame(frame)?;
        w.write_all(&bytes).map_err(FrameError::Io)
    }

    /// Validate a 5-byte header; the payload length it announces.
    fn check_header(hdr: &[u8; 5]) -> Result<usize, FrameError> {
        let ty = hdr[0];
        let len = u32::from_be_bytes([hdr[1], hdr[2], hdr[3], hdr[4]]);
        let ok = match ty {
            T_INPUT | T_SCREEN | T_EXIT | T_OPEN | T_WEB | T_WEB_REPLY | T_OVERLAY | T_SCROLL
            | T_SCROLL_TO | T_INSET => true,
            T_RESIZE => len == 4,
            T_DETACH => len == 0,
            other => return Err(FrameError::UnknownType(other)),
        };
        if len as usize > max_payload(ty) {
            return Err(FrameError::Oversize {
                len: u64::from(len),
            });
        }
        if !ok {
            return Err(FrameError::BadLength { ty, len });
        }
        Ok(len as usize)
    }

    /// Build the frame of type `ty` (already checked by `check_header`) from its whole payload.
    fn parse_payload(ty: u8, payload: Vec<u8>) -> Result<Frame, FrameError> {
        let text = |p: Vec<u8>| String::from_utf8(p).map_err(|_| FrameError::BadUtf8);
        Ok(match ty {
            T_INPUT => Frame::Input(payload),
            T_SCREEN => Frame::Screen(payload),
            T_EXIT => Frame::Exit(text(payload)?),
            T_OPEN => Frame::Open(text(payload)?),
            T_WEB => Frame::Web(text(payload)?),
            T_WEB_REPLY => Frame::WebReply(text(payload)?),
            T_OVERLAY => Frame::Overlay(text(payload)?),
            T_SCROLL => Frame::Scroll(text(payload)?),
            T_SCROLL_TO => Frame::ScrollTo(text(payload)?),
            T_INSET => Frame::Inset(text(payload)?),
            T_DETACH => Frame::Detach,
            T_RESIZE => {
                let cols = u16::from_be_bytes([payload[0], payload[1]]);
                let rows = u16::from_be_bytes([payload[2], payload[3]]);
                if cols == 0 || rows == 0 {
                    return Err(FrameError::BadLength { ty, len: 4 });
                }
                Frame::Resize { cols, rows }
            }
            other => return Err(FrameError::UnknownType(other)),
        })
    }

    /// Incremental form for async readers: `Ok(Some((frame, consumed)))` when `buf` starts with a whole frame,
    /// `Ok(None)` when more bytes are needed. Header errors (oversize, unknown type, length not allowed for the
    /// type) are reported as soon as the 5 header bytes are in, before the payload arrives.
    pub fn decode_frame(buf: &[u8]) -> Result<Option<(Frame, usize)>, FrameError> {
        let Some(hdr) = buf.first_chunk::<5>() else {
            return Ok(None);
        };
        let len = check_header(hdr)?;
        let Some(payload) = buf.get(5..5 + len) else {
            return Ok(None);
        };
        parse_payload(hdr[0], payload.to_vec()).map(|f| Some((f, 5 + len)))
    }

    /// Bytes of one frame. Errors as `write_frame`.
    pub fn encode_frame(frame: &Frame) -> Result<Vec<u8>, FrameError> {
        let payload = frame.payload();
        if payload.len() > max_payload(frame.type_byte()) {
            return Err(FrameError::Oversize {
                len: payload.len() as u64,
            });
        }
        if let Frame::Resize { cols, rows } = frame
            && (*cols == 0 || *rows == 0)
        {
            return Err(FrameError::BadLength {
                ty: T_RESIZE,
                len: 4,
            });
        }
        let mut out = Vec::with_capacity(5 + payload.len());
        out.push(frame.type_byte());
        out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        out.extend_from_slice(&payload);
        Ok(out)
    }

    fn json_line(v: Value) -> Vec<u8> {
        let mut line = serde_json::to_vec(&v).expect("a JSON value always serialises");
        line.push(b'\n');
        line
    }

    fn get_str(obj: &Map<String, Value>, key: &str) -> Result<String, DecodeError> {
        obj.get(key)
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| bad_request(None, format!("{key:?} must be a string")))
    }

    fn get_uint(obj: &Map<String, Value>, key: &str, max: u64) -> Result<u64, DecodeError> {
        obj.get(key)
            .and_then(Value::as_u64)
            .filter(|&n| n <= max)
            .ok_or_else(|| bad_request(None, format!("{key:?} must be an integer in 0..={max}")))
    }

    /// The client's first line (PROTOCOL.md §6.1).
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Hello {
        pub view: u32,
        pub cols: u16,
        pub rows: u16,
        pub term: String,
        pub colorterm: String,
    }

    impl Hello {
        /// One JSON line ending in `\n`.
        pub fn encode(&self) -> Vec<u8> {
            json_line(json!({
                "view": self.view,
                "cols": self.cols,
                "rows": self.rows,
                "term": self.term,
                "colorterm": self.colorterm,
            }))
        }
        /// `BadRequest` for a malformed line; a `view` other than `VIEW_VERSION` is still decoded (the server
        /// answers `protocol_version`).
        pub fn decode(line: &[u8]) -> Result<Hello, DecodeError> {
            let obj = parse_object(line).map_err(|m| bad_request(None, m))?;
            if obj.contains_key("cmd") {
                return Err(bad_request(None, "a view handshake carries no \"cmd\""));
            }
            let size = |key: &str| {
                get_uint(&obj, key, u64::from(u16::MAX)).and_then(|n| {
                    u16::try_from(n)
                        .ok()
                        .filter(|&n| n >= 1)
                        .ok_or_else(|| bad_request(None, format!("{key:?} must be 1-65535")))
                })
            };
            Ok(Hello {
                view: get_uint(&obj, "view", u64::from(u32::MAX))? as u32,
                cols: size("cols")?,
                rows: size("rows")?,
                term: get_str(&obj, "term")?,
                colorterm: get_str(&obj, "colorterm")?,
            })
        }
    }

    /// The server's handshake reply line.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub enum HelloReply {
        Ok { epoch: String, protocol: u32 },
        Err { code: ErrorCode, error: String },
    }

    impl HelloReply {
        pub fn encode(&self) -> Vec<u8> {
            json_line(match self {
                HelloReply::Ok { epoch, protocol } => {
                    json!({"ok": true, "epoch": epoch, "protocol": protocol})
                }
                HelloReply::Err { code, error } => {
                    json!({"ok": false, "code": code.as_str(), "error": error})
                }
            })
        }
        pub fn decode(line: &[u8]) -> Result<HelloReply, DecodeError> {
            let obj = parse_object(line).map_err(|m| bad_request(None, m))?;
            match obj.get("ok") {
                Some(Value::Bool(true)) => Ok(HelloReply::Ok {
                    epoch: get_str(&obj, "epoch")?,
                    protocol: get_uint(&obj, "protocol", u64::from(u32::MAX))? as u32,
                }),
                Some(Value::Bool(false)) => Ok(HelloReply::Err {
                    code: ErrorCode::parse(&get_str(&obj, "code")?)
                        .ok_or_else(|| bad_request(None, "unknown error \"code\""))?,
                    error: get_str(&obj, "error")?,
                }),
                _ => Err(bad_request(
                    None,
                    "a handshake reply needs a boolean \"ok\"",
                )),
            }
        }

        /// S1e (PROTOCOL.md §6.1, plan §17.3): [`HelloReply::encode`] plus `"inset":1` on an `ok` line — the
        /// server's way of saying it understands the handshake's `inset` key and the `0x06` frame, so the page
        /// only sends `0x06` to servers that know it. A separate encoder (not a field on [`HelloReply::Ok`]) so
        /// the variant keeps its shape; an error line is unchanged.
        pub fn encode_with_inset(&self) -> Vec<u8> {
            match self {
                HelloReply::Ok { epoch, protocol } => json_line(json!({
                    "ok": true,
                    "epoch": epoch,
                    "protocol": protocol,
                    "inset": 1,
                })),
                other => other.encode(),
            }
        }
    }

    /// M2.6: the handshake's optional `overlay` key (PROTOCOL.md §6.1): key absent / integer `0` → `false`,
    /// integer `1` → `true`; anything else — explicit `null` included (an exception to §1.3), another number,
    /// `1.0`, a string, a boolean, … — → `bad_request`; a line that is not a JSON object → `bad_request`. Read beside [`Hello::decode`], which
    /// keeps ignoring keys it does not know (so `Hello` gets no new field). W2 implements it.
    pub fn wants_overlay(line: &[u8]) -> Result<bool, DecodeError> {
        wants_flag(line, "overlay")
    }

    /// Optional native header capability: absent/0 means the viewer needs the in-grid toolbar.
    pub fn wants_native_header(line: &[u8]) -> Result<bool, DecodeError> {
        wants_flag(line, "native_header")
    }

    /// M2.6 gui-scrollbar: the handshake's optional `scroll` key (PROTOCOL.md §6.1): exactly the `overlay` rule —
    /// key absent / integer `0` → `false`, integer `1` → `true`; anything else (`null` included) → `bad_request`.
    /// A separate key on purpose: an old `fleetmux gui` already says `"overlay":1`, and must keep receiving no
    /// `0x86` (it would close the connection as `UnknownType`).
    pub fn wants_scroll(line: &[u8]) -> Result<bool, DecodeError> {
        wants_flag(line, "scroll")
    }

    /// S1e (PROTOCOL.md §6.1, plan §17.3): the handshake's optional `inset` key, the columns the gui's traffic
    /// lights take on that viewer's row 0. Key absent → `0`; an integer `0`..=[`INSET_MAX`] → that many columns;
    /// anything else (`null`, a negative number, `41`, `1.0`, a string, a boolean, …) → `bad_request`. Read beside
    /// [`Hello::decode`], which keeps ignoring keys it does not know (so `Hello` gets no new field).
    pub fn wants_inset(line: &[u8]) -> Result<u16, DecodeError> {
        let obj = parse_object(line).map_err(|m| bad_request(None, m))?;
        match obj.get("inset") {
            None => Ok(0),
            Some(v) => match v.as_u64() {
                Some(n) if n <= u64::from(INSET_MAX) => Ok(n as u16),
                _ => Err(bad_request(
                    None,
                    format!("\"inset\" must be an integer 0-{INSET_MAX} (or absent)"),
                )),
            },
        }
    }

    /// One optional boolean handshake key, shared by [`wants_overlay`] and [`wants_scroll`].
    fn wants_flag(line: &[u8], key: &str) -> Result<bool, DecodeError> {
        let obj = parse_object(line).map_err(|m| bad_request(None, m))?;
        // `as_u64` is `None` for negative and floating-point numbers (`1.0` included), so only integers pass.
        match obj.get(key) {
            None => Ok(false),
            Some(v) => match v.as_u64() {
                Some(0) => Ok(false),
                Some(1) => Ok(true),
                _ => Err(bad_request(
                    None,
                    format!("{key:?} must be the integer 0 or 1 (or absent)"),
                )),
            },
        }
    }

    /// M2.6: a rectangle in a viewer's own canvas, in cells, 0-based (PROTOCOL.md §6.3 `0x85` `rect`).
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub struct CellRect {
        pub x: u16,
        pub y: u16,
        pub w: u16,
        pub h: u16,
    }

    /// A26 rev5/rev7 (CURRENT_REQUIREMENTS rev 6 `utm_topbar`, rev 10 `top_container_alignment`): the topbar's
    /// content for a window that draws its own header row (the Linux GTK headerbar, `crates/web/src/gui.rs`).
    /// Unclipped: the headerbar ellipsizes the two texts itself.
    #[derive(Debug, Clone, PartialEq, Eq, Default)]
    pub struct Topbar {
        /// The current fleet's `title` (the headerbar draws it bold).
        pub title: String,
        /// The current fleet's `line1` (the headerbar draws it grey).
        pub status: String,
        /// A26 rev7: the sidebar band drawn on this viewer, in columns ([`fleetmux_layout::Layout::sidebar_cols`]);
        /// 0 = no sidebar. The headerbar puts the workspace/sidebar container boundary on that band's drag line.
        pub sidebar: u16,
        /// A26 rev7: the viewer's canvas width in columns, the denominator of that boundary. 0 = not sent.
        pub cols: u16,
    }

    /// M2.6: the `0x85` payload (PROTOCOL.md §6.3): the history pane (`null` = none), the rectangle where it is
    /// visible on this viewer (`null` = not visible), kv `history_url` (`null` = not set), and A26 rev5 the
    /// topbar's title/status text for a native headerbar (`null` = no fleet to show).
    #[derive(Debug, Clone, PartialEq, Eq, Default)]
    pub struct Overlay {
        pub pane: Option<String>,
        pub rect: Option<CellRect>,
        pub url: Option<String>,
        /// A26 rev5: the fleet title/status of the native headerbar; `None` while kv `sidebar` names no fleet.
        pub topbar: Option<Topbar>,
    }

    impl Overlay {
        /// One JSON object `{"pane","rect","url","topbar"}`, no newline; a `None` is written as `null`. W2
        /// implements it. `topbar` is an A26 rev5 addition: a client older than it ignores the key.
        pub fn encode(&self) -> String {
            let rect = self
                .rect
                .map(|r| json!({"x": r.x, "y": r.y, "w": r.w, "h": r.h}));
            let topbar = self.topbar.as_ref().map(|t| {
                json!({"title": t.title, "status": t.status, "sidebar": t.sidebar, "cols": t.cols})
            });
            json!({"pane": self.pane, "rect": rect, "url": self.url, "topbar": topbar}).to_string()
        }
        /// The inverse of [`Overlay::encode`]: the three M2.6 keys required (each may be `null`); `rect` has the
        /// four integer keys `x` / `y` / `w` / `h` in 0..=65535; A26 rev5 `topbar` is optional (absent or `null`
        /// = none) and, when present, an object with the two string keys `title` / `status`; anything else →
        /// `bad_request`. W2 implements it.
        pub fn decode(s: &str) -> Result<Overlay, DecodeError> {
            let obj = parse_object(s.as_bytes()).map_err(|m| bad_request(None, m))?;
            let opt_str = |key: &str| match obj.get(key) {
                Some(Value::Null) => Ok(None),
                Some(Value::String(v)) => Ok(Some(v.clone())),
                _ => Err(bad_request(
                    None,
                    format!("{key:?} must be a string or null"),
                )),
            };
            let rect = match obj.get("rect") {
                Some(Value::Null) => None,
                Some(Value::Object(r)) => {
                    let cell = |key: &str| get_uint(r, key, u64::from(u16::MAX)).map(|n| n as u16);
                    Some(CellRect {
                        x: cell("x")?,
                        y: cell("y")?,
                        w: cell("w")?,
                        h: cell("h")?,
                    })
                }
                _ => {
                    return Err(bad_request(
                        None,
                        "\"rect\" must be an object {x, y, w, h} or null",
                    ));
                }
            };
            let topbar = match obj.get("topbar") {
                None | Some(Value::Null) => None,
                Some(Value::Object(t)) => {
                    let text = |key: &str| match t.get(key) {
                        Some(Value::String(v)) => Ok(v.clone()),
                        _ => Err(bad_request(
                            None,
                            format!("\"topbar.{key}\" must be a string"),
                        )),
                    };
                    // A26 rev7: the two geometry keys are optional, so a client of rev 6 (or an older server's
                    // payload) still decodes; absent = 0 = the headerbar keeps its toolbar-sized fallback.
                    let num = |key: &str| match t.get(key) {
                        None => Ok(0),
                        Some(_) => get_uint(t, key, u64::from(u16::MAX)).map(|n| n as u16),
                    };
                    Some(Topbar {
                        title: text("title")?,
                        status: text("status")?,
                        sidebar: num("sidebar")?,
                        cols: num("cols")?,
                    })
                }
                _ => {
                    return Err(bad_request(
                        None,
                        "\"topbar\" must be an object {title, status} or null",
                    ));
                }
            };
            Ok(Overlay {
                pane: opt_str("pane")?,
                rect,
                url: opt_str("url")?,
                topbar,
            })
        }
    }

    /// gui-scrollbar: one cell's scrollbar state in the `0x86` payload (PROTOCOL.md §6.3): the pane id, its inner
    /// rectangle on this viewer's canvas (clipped), the current `display_offset` and the `scrollback_len`.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct PaneScroll {
        pub pane: String,
        pub rect: CellRect,
        pub offset: u64,
        pub total: u64,
    }

    /// gui-scrollbar: the `0x86` payload (PROTOCOL.md §6.3): every visible, non-history cell, in
    /// supervisor / lower / workers order. Empty when nothing is visible.
    #[derive(Debug, Clone, PartialEq, Eq, Default)]
    pub struct ScrollState {
        pub panes: Vec<PaneScroll>,
    }

    impl ScrollState {
        /// One JSON object `{"panes":[…]}` (may be `[]`), no newline.
        pub fn encode(&self) -> String {
            let panes: Vec<Value> = self
                .panes
                .iter()
                .map(|p| {
                    let r = p.rect;
                    json!({
                        "pane": p.pane,
                        "rect": {"x": r.x, "y": r.y, "w": r.w, "h": r.h},
                        "offset": p.offset,
                        "total": p.total,
                    })
                })
                .collect();
            json!({"panes": panes}).to_string()
        }
        /// The inverse of [`ScrollState::encode`]: `panes` is required and must be an array; every entry needs a
        /// string `pane`, a `rect` with the four integer keys `x` / `y` / `w` / `h` in 0..=65535, and integer
        /// `offset` / `total`; anything else → `bad_request`.
        pub fn decode(s: &str) -> Result<ScrollState, DecodeError> {
            let obj = parse_object(s.as_bytes()).map_err(|m| bad_request(None, m))?;
            let entries = match obj.get("panes") {
                Some(Value::Array(v)) => v,
                _ => return Err(bad_request(None, "\"panes\" must be an array")),
            };
            let mut panes = Vec::with_capacity(entries.len());
            for entry in entries {
                let Value::Object(p) = entry else {
                    return Err(bad_request(None, "every \"panes\" entry must be an object"));
                };
                let rect = match p.get("rect") {
                    Some(Value::Object(r)) => {
                        let cell =
                            |key: &str| get_uint(r, key, u64::from(u16::MAX)).map(|n| n as u16);
                        CellRect {
                            x: cell("x")?,
                            y: cell("y")?,
                            w: cell("w")?,
                            h: cell("h")?,
                        }
                    }
                    _ => {
                        return Err(bad_request(None, "\"rect\" must be an object {x, y, w, h}"));
                    }
                };
                panes.push(PaneScroll {
                    pane: get_str(p, "pane")?,
                    rect,
                    offset: get_uint(p, "offset", u64::MAX)?,
                    total: get_uint(p, "total", u64::MAX)?,
                });
            }
            Ok(ScrollState { panes })
        }
    }

    /// S1e: the `0x06` payload (PROTOCOL.md §6.3): this viewer's titlebar inset, in columns. The handshake's
    /// `inset` key takes the same value, an integer 0-40 (§17.3).
    ///
    /// A42-LINUX-SIDEBAR-ALIGN-03: `sidebar_min` is the narrowest sidebar band (columns) this viewer's window needs
    /// for its header controls (the Linux GTK headerbar's five tools beside GTK's own window buttons). The window
    /// derives it from the row's real allocation, the toolbar's measured intrinsic width and the page's cell width,
    /// so it is a geometry result, not a fixed column count. 0 = the viewer does not ask for a floor (macOS, an
    /// older window, or no sidebar). The server keeps the maximum over all viewers, like `inset`.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub struct Inset {
        pub cols: u16,
        pub sidebar_min: u16,
    }

    /// Largest `inset` (columns) the handshake and the `0x06` line accept.
    pub const INSET_MAX: u16 = 40;

    /// Largest `sidebar_min` (columns) the `0x06` line accepts: the wire type only bounds it to a `u16`; the
    /// server caps the effective width at the layout's own widest sidebar (`fleetmux_layout::SIDEBAR_MAX_W`), so a
    /// window whose controls need more than that cannot widen the band past the layout's rule.
    pub const SIDEBAR_MIN_MAX: u16 = u16::MAX;

    impl Inset {
        /// One JSON object `{"inset":N}`, no newline; `sidebar_min` rides along only when it is non-zero, so the
        /// macOS and older-window messages keep their exact old bytes.
        pub fn encode(&self) -> String {
            if self.sidebar_min == 0 {
                json!({"inset": self.cols}).to_string()
            } else {
                json!({"inset": self.cols, "sidebar_min": self.sidebar_min}).to_string()
            }
        }
        /// The inverse of [`Inset::encode`]: a non-negative integer `inset` of at most [`INSET_MAX`], plus an
        /// optional `sidebar_min` of at most [`SIDEBAR_MIN_MAX`]; anything else → `bad_request`.
        pub fn decode(s: &str) -> Result<Inset, DecodeError> {
            let obj = parse_object(s.as_bytes()).map_err(|m| bad_request(None, m))?;
            let sidebar_min = match obj.get("sidebar_min") {
                None => 0,
                Some(_) => get_uint(&obj, "sidebar_min", u64::from(SIDEBAR_MIN_MAX))? as u16,
            };
            Ok(Inset {
                cols: get_uint(&obj, "inset", u64::from(INSET_MAX))? as u16,
                sidebar_min,
            })
        }
    }

    /// gui-scrollbar: the `0x05` payload (PROTOCOL.md §6.3): which pane to scroll and where to.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct ScrollTo {
        pub pane: String,
        pub offset: u64,
    }

    impl ScrollTo {
        /// One JSON object `{"pane","offset"}`, no newline.
        pub fn encode(&self) -> String {
            json!({"pane": self.pane, "offset": self.offset}).to_string()
        }
        /// The inverse of [`ScrollTo::encode`]: a string `pane` and a non-negative integer `offset`; anything else
        /// → `bad_request`.
        pub fn decode(s: &str) -> Result<ScrollTo, DecodeError> {
            let obj = parse_object(s.as_bytes()).map_err(|m| bad_request(None, m))?;
            Ok(ScrollTo {
                pane: get_str(&obj, "pane")?,
                offset: get_uint(&obj, "offset", u64::MAX)?,
            })
        }
    }
}

/// PTY output recordings, `<pane id>.rec` (PROTOCOL.md §7).
pub mod record {
    use std::io::{self, Read, Write};

    /// Largest `bytes` in one entry.
    pub const MAX_ENTRY: usize = 1024 * 1024;

    /// One entry: u64 BE microseconds since spawn + u32 BE length + bytes. No file header.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Entry {
        pub offset_us: u64,
        pub bytes: Vec<u8>,
    }

    /// Append one entry. `InvalidInput` for empty or over-`MAX_ENTRY` bytes.
    pub fn write<W: Write>(w: &mut W, entry: &Entry) -> io::Result<()> {
        let len = entry.bytes.len();
        if len == 0 || len > MAX_ENTRY {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("record entry of {len} bytes (allowed 1-{MAX_ENTRY})"),
            ));
        }
        let mut buf = Vec::with_capacity(12 + len);
        buf.extend_from_slice(&entry.offset_us.to_be_bytes());
        buf.extend_from_slice(&(len as u32).to_be_bytes());
        buf.extend_from_slice(&entry.bytes);
        w.write_all(&buf)
    }

    /// Fill `buf` unless EOF comes first; the number of bytes read (< `buf.len()` only at EOF).
    fn read_full<R: Read>(r: &mut R, buf: &mut [u8]) -> io::Result<usize> {
        let mut n = 0;
        while n < buf.len() {
            match r.read(&mut buf[n..]) {
                Ok(0) => break,
                Ok(k) => n += k,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => return Err(e),
            }
        }
        Ok(n)
    }

    /// Read the next entry. `Ok(None)` at EOF on an entry boundary; `UnexpectedEof` inside an entry;
    /// `InvalidData` for a length of 0 or over `MAX_ENTRY`.
    pub fn read<R: Read>(r: &mut R) -> io::Result<Option<Entry>> {
        let mut hdr = [0u8; 12];
        match read_full(r, &mut hdr)? {
            0 => return Ok(None),
            12 => {}
            n => {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    format!("record entry header truncated after {n} bytes"),
                ));
            }
        }
        let offset_us = u64::from_be_bytes(hdr[..8].try_into().expect("8 bytes"));
        let len = u32::from_be_bytes(hdr[8..].try_into().expect("4 bytes")) as usize;
        if len == 0 || len > MAX_ENTRY {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("record entry length {len} (allowed 1-{MAX_ENTRY})"),
            ));
        }
        let mut bytes = vec![0u8; len];
        r.read_exact(&mut bytes)?;
        Ok(Some(Entry { offset_us, bytes }))
    }
}

/// A terminal colour palette: the `theme` key of `hello` (PROTOCOL.md §4.1), the `--theme FILE` JSON of
/// `fleetmux server` (§9.1) and the `--theme JSON` of `fleetmux-web --gui` (§15.4). Shapes and validation only —
/// the cmux default values live in `fleetmux-term`'s `Palette::CMUX`, never here.
pub mod theme {
    use serde::{Deserialize, Serialize};

    /// One complete palette. Every colour is a `#rrggbb` string; `ansi` holds exactly 16.
    #[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
    #[serde(deny_unknown_fields)]
    pub struct Theme {
        pub bg: String,
        pub fg: String,
        pub cursor: String,
        pub ansi: Vec<String>,
    }

    /// `#rrggbb`, either case, as its three bytes. `None` for anything else.
    pub fn hex_rgb(s: &str) -> Option<(u8, u8, u8)> {
        let hex = s.strip_prefix('#')?;
        if hex.len() != 6 || !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
            return None;
        }
        let byte = |i: usize| u8::from_str_radix(&hex[i..i + 2], 16).ok();
        Some((byte(0)?, byte(2)?, byte(4)?))
    }

    /// The inverse of [`hex_rgb`]: always lowercase.
    pub fn rgb_hex(c: (u8, u8, u8)) -> String {
        format!("#{:02x}{:02x}{:02x}", c.0, c.1, c.2)
    }

    impl Theme {
        /// `Err` naming the first不合格 field: a colour that is not `#rrggbb`, or `ansi` not 16 long.
        pub fn validate(&self) -> Result<(), String> {
            for (name, v) in [("bg", &self.bg), ("fg", &self.fg), ("cursor", &self.cursor)] {
                if hex_rgb(v).is_none() {
                    return Err(format!("{name} {v:?} is not #rrggbb"));
                }
            }
            if self.ansi.len() != 16 {
                return Err(format!("ansi has {} colours, need 16", self.ansi.len()));
            }
            for (i, v) in self.ansi.iter().enumerate() {
                if hex_rgb(v).is_none() {
                    return Err(format!("ansi[{i}] {v:?} is not #rrggbb"));
                }
            }
            Ok(())
        }

        /// Parse a whole theme object: unknown keys, a missing field, a non-object and bad values are all errors.
        /// Colours are normalised to lowercase so what leaves the server is always lowercase.
        pub fn parse_json(s: &str) -> Result<Theme, String> {
            let mut t: Theme = serde_json::from_str(s).map_err(|e| e.to_string())?;
            t.validate()?;
            t.bg.make_ascii_lowercase();
            t.fg.make_ascii_lowercase();
            t.cursor.make_ascii_lowercase();
            for v in &mut t.ansi {
                v.make_ascii_lowercase();
            }
            Ok(t)
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        const CMUX: &str = r##"{"bg":"#1e1e1e","fg":"#ffffff","cursor":"#98989d","ansi":[
            "#1a1a1a","#cc372e","#26a439","#cdac08","#0869cb","#9647bf","#479ec2","#98989d",
            "#464646","#ff453a","#32d74b","#ffd60a","#0a84ff","#bf5af2","#76d6ff","#ffffff"]}"##;

        #[test]
        fn theme_hex_rgb_takes_both_cases_and_rejects_bad() {
            assert_eq!(hex_rgb("#1E1E1E"), Some((0x1e, 0x1e, 0x1e)));
            assert_eq!(hex_rgb("#cc372e"), Some((0xcc, 0x37, 0x2e)));
            assert_eq!(hex_rgb("#000000"), Some((0, 0, 0)));
            for bad in [
                "1e1e1e", "#12345", "#1234567", "#gggggg", "#12345g", "", "#", "# 12345",
            ] {
                assert_eq!(hex_rgb(bad), None, "{bad:?}");
            }
        }

        #[test]
        fn theme_rgb_hex_is_lowercase() {
            assert_eq!(rgb_hex((0x1e, 0x1e, 0x1e)), "#1e1e1e");
            assert_eq!(rgb_hex((0xCC, 0x37, 0x2E)), "#cc372e");
            assert_eq!(rgb_hex((0, 0, 0)), "#000000");
            // Round trip.
            for v in ["#1e1e1e", "#ffffff", "#98989d", "#0a84ff", "#000000"] {
                assert_eq!(rgb_hex(hex_rgb(v).unwrap()), v);
            }
        }

        #[test]
        fn theme_parse_json_accepts_a_whole_palette_and_lowercases_it() {
            let t = Theme::parse_json(CMUX).unwrap();
            assert_eq!(t.bg, "#1e1e1e");
            assert_eq!(t.fg, "#ffffff");
            assert_eq!(t.cursor, "#98989d");
            assert_eq!(t.ansi.len(), 16);
            assert_eq!(t.ansi[1], "#cc372e");
            let upper: String = CMUX
                .chars()
                .map(|c| c.to_ascii_uppercase())
                .collect::<String>()
                .replace("{\"BG\"", "{\"bg\"")
                .replace(",\"FG\"", ",\"fg\"")
                .replace(",\"CURSOR\"", ",\"cursor\"")
                .replace(",\"ANSI\"", ",\"ansi\"");
            let upper = Theme::parse_json(&upper).unwrap();
            assert_eq!(upper, t, "uppercase input must normalise to lowercase");
        }

        #[test]
        fn theme_parse_json_rejects_unknown_keys_null_and_bad_types() {
            for bad in [
                r##"{"bg":"#123456"}"##, // missing fg/cursor/ansi
                r##"{"bg":"#123456","fg":"#ffffff","cursor":"#98989d","ansi":["#000000"],"foo":1}"##,
                r##"{"bg":"blue","fg":"#ffffff","cursor":"#98989d","ansi":["#000000"]}"##,
                r##"{"bg":null,"fg":"#ffffff","cursor":"#98989d","ansi":["#000000"]}"##,
                r##"{"bg":1,"fg":"#ffffff","cursor":"#98989d","ansi":["#000000"]}"##,
                r#"[]"#,
                r#"null"#,
                r#"not json"#,
                r##"{"bg":"#123456","fg":"#ffffff","cursor":"#98989d","ansi":["#000000"]}"##, // ansi not 16
                r##"{"bg":"#123456","fg":"#ffffff","cursor":"#98989d","ansi":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16]}"##,
            ] {
                assert!(Theme::parse_json(bad).is_err(), "{bad}");
            }
        }

        #[test]
        fn frame_0x06_inset_round_trips() {
            use crate::view::{self, Frame, FrameError};

            // 0x06 client → server: one JSON line `{"inset":N}` (PROTOCOL.md §6.3), the same shape rules as 0x05.
            let line = r#"{"inset":10}"#;
            let f = Frame::Inset(line.to_string());
            assert_eq!(f.type_byte(), 0x06, "the inset frame is type 0x06: {f:?}");
            assert_eq!(view::T_INSET, 0x06, "T_INSET must be 0x06");
            let bytes = view::encode_frame(&f).expect("an inset frame encodes");
            assert_eq!(bytes[0], 0x06, "encoded type byte of {line}: {bytes:?}");
            assert_eq!(
                &bytes[1..5],
                &(line.len() as u32).to_be_bytes(),
                "payload length of {line}"
            );
            assert_eq!(&bytes[5..], line.as_bytes(), "payload of {line}");
            assert_eq!(
                view::decode_frame(&bytes).unwrap(),
                Some((f.clone(), bytes.len())),
                "decode_frame of {line}"
            );
            assert_eq!(
                view::read_frame(&mut std::io::Cursor::new(bytes.clone())).unwrap(),
                Some(f.clone()),
                "read_frame of {line}"
            );
            assert_eq!(
                view::max_payload(view::T_INSET),
                view::MAX_FRAME,
                "the inset frame has the 1 MiB cap"
            );
            let over = Frame::Inset("x".repeat(view::MAX_FRAME + 1));
            assert!(
                matches!(view::encode_frame(&over), Err(FrameError::Oversize { .. })),
                "an inset line over MAX_FRAME is refused"
            );
            let mut bad = vec![0x06];
            bad.extend_from_slice(&2u32.to_be_bytes());
            bad.extend_from_slice(&[0xff, 0xfe]);
            assert!(
                matches!(
                    view::read_frame(&mut std::io::Cursor::new(bad)),
                    Err(FrameError::BadUtf8)
                ),
                "a non-UTF-8 inset payload is refused"
            );

            // Typed payload: the handshake's `inset` rule (§17.3), an integer 0-40.
            let ins = view::Inset {
                cols: 10,
                sidebar_min: 0,
            };
            assert_eq!(
                ins.encode(),
                r#"{"inset":10}"#,
                "Inset::encode of 10 columns"
            );
            assert_eq!(
                view::Inset::decode(&ins.encode()).unwrap(),
                ins,
                "Inset round trip of 10 columns"
            );
            let floor = view::Inset {
                cols: 0,
                sidebar_min: 34,
            };
            assert_eq!(
                floor.encode(),
                r#"{"inset":0,"sidebar_min":34}"#,
                "A42-LINUX-SIDEBAR-ALIGN-03: the window's sidebar floor rides the inset line"
            );
            assert_eq!(
                view::Inset::decode(&floor.encode()).unwrap(),
                floor,
                "sidebar_min round trip"
            );
            assert_eq!(
                view::Inset::decode(r#"{"inset":0}"#).unwrap().sidebar_min,
                0,
                "an older window's line has no sidebar floor"
            );
            assert_eq!(
                view::Inset::decode(r#"{"inset":0}"#).unwrap().cols,
                0,
                "0 columns is allowed"
            );
            assert_eq!(
                view::Inset::decode(r#"{"inset":40}"#).unwrap().cols,
                40,
                "40 columns is allowed"
            );
            assert_eq!(
                view::Inset::decode(r#"{"inset":10,"x":1}"#).unwrap().cols,
                10,
                "an unknown key is ignored, as everywhere else"
            );
            // A42-LINUX-SIDEBAR-ALIGN-03: the wire only bounds `sidebar_min` to a `u16` (SIDEBAR_MIN_MAX); a large
            // but representable floor is a real window geometry, not malformed input — whether it fits the viewer is
            // the layout's content-minimum rule, not the parser's. Only a value the wire cannot represent is refused.
            // `u16::MAX` itself is accepted: the layout decides it (`sidebar + SIDEBAR_MIN_CONTENT` must not
            // overflow), the parser does not reject a legal bound.
            assert_eq!(view::SIDEBAR_MIN_MAX, u16::MAX);
            for big in [66u16, 200, 60_000, u16::MAX] {
                let line = format!(r#"{{"inset":0,"sidebar_min":{big}}}"#);
                assert_eq!(
                    view::Inset::decode(&line).unwrap().sidebar_min,
                    big,
                    "a representable sidebar floor of {big} columns was refused as malformed"
                );
            }
            for bad in [
                "",
                "[]",
                r#"{}"#,
                r#"{"inset":null}"#,
                r#"{"inset":-1}"#,
                r#"{"inset":41}"#,
                r#"{"inset":"10"}"#,
                r#"{"inset":0,"sidebar_min":65536}"#,
                r#"{"inset":0,"sidebar_min":-1}"#,
                r#"{"inset":0,"sidebar_min":"34"}"#,
            ] {
                assert!(view::Inset::decode(bad).is_err(), "{bad} must be refused");
            }
        }
    }
}
