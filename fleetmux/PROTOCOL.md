# fleetmux protocol (version 1)

Status: phase 0, with the stage A additions appended: `server.stats` (§4.17), the optional pane-record keys `unseen` /
`scrollback` (§4.0), the optional `hello` keys `viewers` / `prefix` (§4.1), view frame `0x83` (§6.3), recording
fixtures (§7.1), the logind warning (§8.7) and §9–§14 (command line, keys, mouse, copy, page mode, viewers and colour
depth). Normative for `crates/proto`,
`crates/app`, `tests/fake_fleetmux.py` and `cmux_fleet/backends/fleetmux.py`. Every rule below is a ruled decision;
any further change goes through the supervisor. The machine-readable twin of §4–§5 is `protocol/schema.json` (one `request` /
`response` definition plus examples per command, and the error-code enum); when the two disagree, fix whichever
is wrong in the same commit — they must never ship out of step.

Conventions in this document: "MUST" / "MUST NOT" are hard requirements tested by `crates/proto/tests/codec.rs`,
`crates/app/tests/protocol.rs` or `tests/test_fleetmux_backend.py`. A *pane id* is a string such as `k7q2-12`
(§1.4). A *group* is one of `supervisor`, `lower`, `workers` (§1.5).

---

## 1. Transport

### 1.1 Socket

- One unix stream socket per server: `<runtime>/ctl.sock`, where `<runtime>` = `<state_dir>/fleetmux/<token>/`
  (design.md §2.2). Directory mode 0700, socket mode 0600.
- The socket path is never shortened or moved to `/tmp`: if `sockaddr_un` cannot hold it (103 bytes usable on
  macOS, 107 on Linux) the server refuses to start and the client refuses to connect.
- Two connection kinds share the socket and are told apart by the first line. A first line that is a JSON object
  with a top-level `"view"` key is a **view handshake** and opens a **view connection** (§6). Every other first
  line — a control request, but also bad JSON, a non-object, an object with neither `view` nor `cmd` — makes the
  connection a **control connection** and is answered like any control line (§1.3): the error reply is sent and
  the connection **stays open**. Only a recognisable but invalid view handshake (a `view` key with a bad version,
  missing / ill-typed `cols` / `rows` / `term` / `colorterm`, or a `cmd` key next to `view`) is answered with an
  error line and then closed (§6.1). Once a connection is a control connection it stays one: a later line with a
  `view` key is just a control line without `cmd` (`bad_request`).

### 1.2 Control connection framing

- One request per line, one reply per line: UTF-8 JSON object, terminated by a single `\n` (0x0A). JSON
  whitespace (including a `\r` before the `\n`) is allowed around the object.
- A line is at most **4 MiB** (4,194,304 bytes, the `\n` not counted), in both directions. A request line over the
  limit gets one `bad_request` reply with `"id": null` and the server then closes the connection (it does not try
  to resynchronise inside an oversize line). A reply that would exceed the limit is avoided by the command itself
  (`pane.read` drops its oldest lines, §4.9); no other reply can get near it.
- Replies are written as compact JSON. A reply line MUST NOT contain a raw `\n`: newlines inside strings are
  escaped (`\n`) by the encoder (codec test `encode_reply_is_one_line_no_embedded_newline`).
- The client chooses `id`; the server copies it into the reply unchanged. On one connection replies come back
  **in request order** (the server handles a connection's requests one at a time). Different connections are
  independent: a slow `pane.kill` on one connection does not delay another connection.
- Connections may be long-lived or one-shot. Either side may close at any time; closing mid-request abandons the
  reply but not the action (a `pane.kill` already started keeps running to its deadline).

### 1.3 Request and reply envelope

Request:

```json
{"id": 7, "cmd": "pane.read", "pane": "k7q2-12", "lines": 40}
```

- `id`: integer, 0 ≤ id ≤ 2^53−1. Required. The server does not require ids to be unique or increasing.
- `cmd`: string, one of the 22 names in §4 — 26 on a server that announces `pages:1` (the M4 page commands
  §4.23–§4.26, §2 addendum). Required.
- All other keys are the command's arguments, at the top level next to `id` and `cmd`. **No argument is named
  `id`, `cmd`, `ok`, `code` or `error`** — the pane being addressed is always `pane` (§1.4).
- Unknown extra keys are ignored (forward compatibility); a known key with the wrong JSON type is `bad_request`.

Success reply:

```json
{"id": 7, "ok": true, "text": "line one\nline two"}
```

Error reply:

```json
{"id": 7, "ok": false, "code": "no_such_pane", "error": "no pane k7q2-12"}
```

- `ok` is always present. On success the command's result keys (§4) sit at the top level next to `id` and `ok`;
  a result key is never named `id`, `ok`, `code` or `error`. On failure there are exactly the four keys
  `id`, `ok`, `code`, `error`: `code` is one of the fixed set in §5, `error` is a human-readable English sentence
  (not parsed by clients; Python copies it into `last_error`).
- If the request's `id` cannot be recovered (the line is not JSON, not an object, `id` missing or not a valid
  integer, invalid UTF-8, oversize line) the reply is `bad_request` with `"id": null`. The connection stays open
  in all of these cases except the oversize line.
- A JSON object that parses and has a valid `id` but an unknown `cmd` gets `unknown_cmd` with that `id`, and the
  connection stays open. Its `error` names the unknown `cmd`. Names are case-sensitive. Decoding never panics on any input.

### 1.4 Identifiers

- **epoch**: 4 characters `[0-9a-z]` (base 36), chosen at random when a server starts, reported by `hello` and
  written to `server.json`. A new server process always gets a new epoch.
- **pane id**: `<epoch>-<seq>`, `seq` a decimal integer starting at 1, incremented per spawn and never reused
  within an epoch. Pattern `^[0-9a-z]{4}-[1-9][0-9]*$`. Because the epoch differs, an id recorded against an
  earlier server can never name a pane of a later one (design.md §2.4).
- **The top-level `id` is always the request sequence number; a pane is always named `pane`**. An earlier draft
  wrote the pane argument of `pane.write` and the result of `pane.spawn` as `id`, which would put two `id` keys in
  one JSON object; the pane field is therefore `pane` everywhere — in arguments, in results, in the pane record
  (§4.0) and in the runtime files (§8). No field was dropped.

### 1.5 Enumerations

| Name | Values |
|---|---|
| group | `supervisor` (top left), `lower` (bottom left: history / control / tabs), `workers` (right) |
| pane type | `supervisor`, `advisor`, `worker`, `daemon`, `tab`, `history`, `placeholder`, `shell`; `shell` is the sidebar's bash (S1c): the **server** spawns it as the last tab of the root page's `workers` group (§11; every new bash opened from the fleetmux sidebar is mapped into the main fleet's worker area and managed by the main fleet), the sidebar draws it as a `bash-<n>` card, and the root fleet takes it into its registry (`<session>.fleetmux.json`, `type: shell`) — the daemon keeps it in the worker area behind the workers and lists it on the history page; a bash opened while no fleet runs is taken in by the next root fleet. It is **no worker**: no slot, no work, not counted by `--max-busy`, not in the roster, and a sub-fleet never takes it in; the root fleet's close kills it only while its shell is idle at its prompt (`fg_pgid` = `pgid`, §4.0) and keeps it — and the server — while a program runs in its foreground (a sub-fleet's close never touches it). Any other string (or none) is accepted and placed as *unknown* (design.md §3.4) |
| write mode | `raw`, `bracketed`, `paste` |

---

## 2. Protocol version

- One integer, `protocol`. This document is version **1**.
- The server reports it in `hello` (§4.1), in the view handshake reply (§6.1) and in `server.json` (§8.1).
- A client speaks exactly one version. `cmux_fleet/backends/fleetmux.py` has `PROTOCOL = 1` and treats any
  other value as "fleetmux version mismatch: need X, installed Y" (`available()` / `unavailable_reason`).
- A client MAY send its version in `hello` (`"protocol": 1`); a server that does not speak it replies
  `protocol_version` and keeps the connection open, so the client can still read the server's version with a
  bare `hello`.
- Any change to a command's arguments, results or error codes, to the view frames or to the recording format
  bumps the version. Adding an optional result key does not, and neither does adding a command.
- Stage A additions stay at version **1**: no version has been released yet, so the new command
  `server.stats` (§4.17), the new view frame `0x83` (§6.3) and the new optional keys (§4.0, §4.1) are part of
  version 1 itself; `VIEW_VERSION` is also still 1.
- **M4 addendum: fleet pages, still version 1**. One server serves a whole fleet
  tree: the root fleet's page (`""`) and one *page* per child fleet. The additions are the four page commands
  (§4.23–§4.26), new optional result keys (`hello.pages` / `page` / `current`, the pane record's `page`,
  `server.stats.pages`, `server.json` `"pages": 1`) and new optional arguments (`hello.page`, `pane.spawn.page`,
  `pane.list.all`, `page.open.reclaimed` / `history_url`). By the letter of the rule above new arguments would bump
  the version; they do not, under this explicit rule:
  - **Without the new arguments and commands, every command's arguments, results and error codes are byte for
    byte those of M3.** A root connection (no `hello.page`) on a server with no child page sees exactly M3. New
    behaviour appears only after a new argument or a new command was used.
  - **The capability key is `pages: 1`**, in every `hello` reply and in `server.json`. A client MUST check it before
    it uses any page argument or command: an M3 server silently ignores unknown keys (§1.3), so `hello {page}` there
    would leave the connection on the root page. A page-bound client (the Python manager of a child fleet) checks on
    every connection that the reply has `pages == 1` and `page` equal to its own page, and fails the call otherwise.
  - No new error code: an unknown page is `bad_request` with `error` `no such page: <id>`; `page.close` on a page
    that still has panes is `not_empty` (§5).
  - **Page connection.** A control connection whose first `hello` carries `page` (a page id, §4.23) is *bound* to
    that page for its lifetime; a later `hello` with another `page` on it is `bad_request` (the same `page` again,
    or no `page`, is fine). A connection without `hello {page}` is a root connection — old Python, the root manager
    and the `fleetmux` CLI are. The bound page may not exist yet: the connection's first `pane.spawn`, `pane.move`,
    `kv.set` or `layout.set` creates it `live` with its id as title; until then `pane.list` is `[]`, `layout.get`
    returns empty groups and every kv read is `null`.
  - On a page connection: `server.shutdown` is `bad_request` (a child fleet never stops the shared server); the
    root-only kv keys `sidebar`, `stop_request`, `restorable`, `sidebar_width` are `bad_request` for `kv.set` and
    `kv.get`; every other kv key is the page's own; `web.*` reach the window only while the page is the current
    page (§4.18–§4.21 M4 notes).
  - The *current page* is one server-side pointer shared by every viewer; it starts at the root page and only
    `page.select` changes it (§4.25). Neither the page table nor the pointer is persistent: `layout.json` keeps the
    root page only (§8.3) and a new server starts with the root page alone.
- **Addendum, still version 1**: the pane record's `fg_pgid` (§4.0) is a new optional result key (the
  rule above: no bump). It is sent in every record on every connection, so the M4 addendum's "byte for byte M3"
  sentence holds for everything except this key. `pane.kill` also signals the pane's foreground job (§4.13): no
  argument, result or error code changed.
- `fleetmux --version` prints exactly one line `fleetmux <semver> (protocol <n>)`, e.g. `fleetmux 0.1.0 (protocol 1)`
  so `available()` can check the protocol without starting a server.

---

## 3. Peer check

- On every accepted connection, before reading anything, the server takes the peer's uid from the socket
  credentials (`SO_PEERCRED` on Linux, `getpeereid` on macOS; tokio `UnixStream::peer_cred`).
- If the uid differs from the server's own effective uid, the server closes the connection **without writing a
  single byte** and logs one `WARN` line to `server.log` naming the uid. No error reply is sent.
- The check applies to control and view connections alike. It is in addition to (not instead of) the 0700
  directory and 0600 socket.
- The comparison is a pure function `peer_allowed(server_uid, peer_uid) -> bool` so it is testable without a
  second user.
- **Client side (`fleetmux attach`).** After connecting, the attach client takes the kernel peer
  credentials of the unix socket it actually connected to (`getpeereid` on macOS, `SO_PEERCRED` on Linux) and goes
  on only when that uid equals its own effective uid; when the credentials cannot be read it refuses (exit 3, §9.2).
  It does **not** compare `hello.uid` (§4.1) with its own uid: through an SSH forward (§9.2) the socket's peer is the
  user's own `ssh`, and `hello.uid` is a uid of the server's host, which another host numbers differently. Nothing
  on the wire changes. Same host: the peer is the server itself, so this is as strict as the server-side check.
  Across hosts: the client trusts whatever the user's own SSH forward reaches; on that host `sshd` connects as the
  login user, which the server-side check above still requires to be the server's uid (design.md §3.5.3).

---

## 4. Commands

17 commands. (design.md §4.1 says "15"; phase 0 specified 16 — `kv.set` and `kv.get` are
written as one row in design.md — and stage A added the 17th, `server.stats`. All 17 are specified;
`proto::Command` has 17 variants and `proto::known_commands()` lists 17 names.) M2.5 appended
18–22, the `web.*` commands of §4.18–§4.22 (design.md §3.5.4 ①): from M2.5 there are **22 commands**, `proto::Command`
has 22 variants and `proto::known_commands()` lists 22 names, in this table's order. M4 appended
23–26, the page commands of §4.23–§4.26: `proto::Command` has 26 variants; `proto::known_commands()` still lists
the 22 names above (and `protocol/schema.json`'s command list the same 22), the four page commands are listed apart by
`proto::page_commands()`, because they exist only on a server that announces `pages:1` (§2 addendum);
`protocol/schema.json` lists the page commands apart in the same way (a separate page-command list, not among the 22).

| # | `cmd` | `proto::Command` variant |
|---|---|---|
| 1 | `hello` | `Hello` |
| 2 | `kv.set` | `KvSet` |
| 3 | `kv.get` | `KvGet` |
| 4 | `pane.spawn` | `PaneSpawn` |
| 5 | `pane.list` | `PaneList` |
| 6 | `pane.get` | `PaneGet` |
| 7 | `pane.write` | `PaneWrite` |
| 8 | `pane.key` | `PaneKey` |
| 9 | `pane.read` | `PaneRead` |
| 10 | `pane.rename` | `PaneRename` |
| 11 | `pane.focus` | `PaneFocus` |
| 12 | `pane.move` | `PaneMove` |
| 13 | `pane.kill` | `PaneKill` |
| 14 | `layout.get` | `LayoutGet` |
| 15 | `layout.set` | `LayoutSet` |
| 16 | `server.shutdown` | `ServerShutdown` |
| 17 | `server.stats` | `ServerStats` |
| 18 | `web.open` | `WebOpen` |
| 19 | `web.goto` | `WebGoto` |
| 20 | `web.url` | `WebUrl` |
| 21 | `web.reload` | `WebReload` |
| 22 | `web.eval` | `WebEval` (reserved: always `not_implemented`) |
| 23 | `page.open` | `PageOpen` (M4) |
| 24 | `page.close` | `PageClose` (M4) |
| 25 | `page.select` | `PageSelect` (M4) |
| 26 | `page.list` | `PageList` (M4) |

Errors every command can return, not repeated per row: `bad_request` (missing / ill-typed argument, value out
of its stated range), `internal` (server-side bug or OS failure not covered by a specific code). Every command
that takes `pane` can also return `no_such_pane` (no live pane with that id on this server, including ids from
another epoch).

### 4.0 The pane record

`pane.list` and `pane.get` return the same record:

| Key | Type | Meaning |
|---|---|---|
| `pane` | string | pane id (§1.4) |
| `title` | string | tab title as last set by `pane.spawn` / `pane.rename`; never rewritten by the server |
| `type` | string | the `type` given at spawn, verbatim (`""` if none) |
| `group` | string | group the pane is in (§1.5) |
| `index` | integer | 0-based position inside its group (tab order, left to right) |
| `pid` | integer | pid of the process the server spawned (the PTY child) |
| `pgid` | integer | its process group id (= `pid`: the child is a session and group leader) |
| `tty` | string | full device path of the PTY slave, e.g. `/dev/ttys012` (macOS), `/dev/pts/3` (Linux) |
| `cols` | integer | current PTY width |
| `rows` | integer | current PTY height |
| `focused` | bool | this pane is the selected tab of the group that has focus |
| `unseen` | bool, **optional** (absent = `false`) | the pane received output while it was not displayed (not the selected tab of its group, or hidden by zoom); cleared when it is displayed. The tab title gets a `•` while it is set (§14) |
| `scrollback` | int, **optional** (absent = `0`) | current number of scrollback rows held for the pane (`TermParser::scrollback_len`) |
| `fg_pgid` | int or `null`, **optional** | the PTY's foreground process group, read when the reply is built (`tcgetpgrp` on the master): equal to `pgid` while the pane's own program holds the terminal (a shell idle at its prompt), another group while a job runs in front of it, `null` when the PTY reports none. Clients MUST read an absent key or `null` as "not known to be idle" (`cmux_fleet` keeps such a bash page) |

`unseen` and `scrollback` are optional result keys (§2: no version bump). The server always sends them; clients
MUST accept a record without them and read it as `false` / `0` (`proto::PaneInfo` fields with `#[serde(default)]`,
codec test `pane_info_optional_keys_default_when_absent`).

M4 (§2 addendum): the record gets the optional key `page` (string, the pane's page, `""` = root) whenever the
reply is to a page connection, to `pane.list {all: true}`, or the pane is outside the root page, and — on a root
connection too — whenever the tree has at least one child page (so a root pane then reads `"page":""`). A root
connection on a tree without child pages therefore gets the M3 record unchanged. `group` / `index` / `focused` follow the
layout of the pane's **own** page; a pane of a page that is not the current page is never displayed (it collects
`unseen` like a background tab). Every pane id is unique across the whole server, so every command that takes
`pane` addresses it whichever page holds it.

A pane exists from a successful `pane.spawn` until its PTY child exits or `pane.kill` confirms it reaped. When
the child exits on its own the pane is removed from the layout at once (no "dead pane" state); remaining members
of its process group get the kernel's SIGHUP when the PTY master closes.

### 4.1 `hello`

| | |
|---|---|
| Arguments | `protocol`: integer, optional — the client's protocol version |
| Result | `uid` int (server euid), `pid` int (server pid), `epoch` string, `version` string (program semver, e.g. `0.1.0`), `protocol` int, `run` string or `null` (value of kv key `run`, §4.2); optional: `viewers` int (number of view connections open now), `prefix` string (the server's prefix key name, `--prefix`, default `C-b`, §9.1 / §10), `theme` object (the server's palette, §9.1 `--theme`: `{"bg","fg","cursor","ansi"}`, colours lowercase `#rrggbb`, `ansi` = the 16 colours of indices 0–15) |
| Errors | `protocol_version` — `protocol` was given and differs from the server's |

```json
{"id":1,"cmd":"hello","protocol":1}
{"id":1,"ok":true,"uid":501,"pid":4242,"epoch":"k7q2","version":"0.1.0","protocol":1,"run":null}
```

`viewers` and `prefix` are optional result keys (§2): the server always sends them, clients MUST accept a reply
without them (the phase 0 example above is still a valid reply). With them:

```json
{"id":1,"cmd":"hello"}
{"id":1,"ok":true,"uid":501,"pid":4242,"epoch":"k7q2","version":"0.1.0","protocol":1,"run":"fleet-20261028-095326-d346b75c","viewers":1,"prefix":"C-b"}
```

`theme` (M2.6) is the third optional result key: the palette the server answers OSC 10 / 11 / 12 / 4 with, and the
one `fleetmux gui` hands to the window (§9.5, §15.4). The server always sends it; clients MUST accept a reply
without it (§2) and then keep their own colours. Colours are lowercase `#rrggbb`; `ansi` has exactly 16 entries in
index order. The default values are the cmux palette, listed once in §9.1. A program inside a pane always gets
these answers, whichever terminal the viewer attached from — the known `attach` limitation is in §9.1.

Clients call `hello` first on every connection they open and verify `protocol`. A client on the server's own
host (fleet's Python backend) also verifies `uid` (= own uid). `fleetmux attach` does not compare `uid`: it checks
the kernel peer credentials of the socket it connected to instead (§3, client side), because through an SSH forward
`uid` belongs to the remote host. Clients compare `epoch` / `pid` against `server.json` when they need to know it is
the same server.

M4 (§2 addendum): optional argument `page` — a page id (§4.23) binds this connection to that page; `""` or
`null` = no binding. The reply always carries `pages: 1` (the capability key), `page` (this connection's page, `""`
when unbound) and `current` (the current page, §4.25); `run` is the kv `run` of this connection's page. A `hello`
answered `protocol_version` binds nothing.

```json
{"id":1,"cmd":"hello","protocol":1,"page":"M0"}
{"id":1,"ok":true,"uid":501,"pid":4242,"epoch":"k7q2","version":"0.1.0","protocol":1,"run":null,"viewers":1,"prefix":"C-b","theme":{},"pages":1,"page":"M0","current":""}
```

### 4.2 `kv.set`

| | |
|---|---|
| Arguments | `key`: string, 1–256 bytes, required; `value`: string, ≤ 64 KiB, required |
| Result | none (`{"id":n,"ok":true}`) |
| Errors | — |

A small in-memory string map, used for the run marker (`claim_run` sets key `run`). It lives as long as the
server process: a new server (new epoch) starts empty. Setting an existing key overwrites it.

```json
{"id":2,"cmd":"kv.set","key":"run","value":"fleet-20261028-095326-d346b75c"}
{"id":2,"ok":true}
```

**Reserved keys.** The fleet daemon writes these; the server only reads them — except `stop_request` and
`sidebar_width`, which the server writes (the daemon reads `stop_request`; nothing else reads `sidebar_width`).

- `sidebar` — the fleet sidebar the server draws in a band of columns (34 by default; its inner edge can be dragged
  to 24 up to `min(60, columns − 60)` columns, see `sidebar_width` and §11). The band is on the **left** on macOS
  and on the **right** on Linux (the content mirror of §11); everything else about it is the same. Value: one JSON object
  `{"v":1,"current":"<id>","fleets":[{"id":"…","title":"…","state":"…","line1":"…","line2":"…"}, …],"close":{"title":"…","body":"…"}}`
  — `v` is `1`, `current` names one of the `id`s, `fleets` holds 1–16 cards, every value is a string of at most 200
  characters; unknown keys are ignored. The optional `close` object carries the two texts of the close
  confirmation box (two strings of at most 400 characters each; the daemon names the workers and the child fleets
  it will stop); a missing or empty string falls back to the server's default text. A value that does not parse is
  ignored: the server draws no sidebar and logs one `WARN` line `kv sidebar ignored: <reason>`. The sidebar schema
  is not part of this protocol's version: the server never writes the key, and a new server (new epoch) starts
  without it. Whether the sidebar is shown or hidden is shared by every viewer and is **not** saved in
  `layout.json` (a restarted server starts with it shown; the daemon re-pushes the value within a second).
- `stop_request` — the server's answer to the close confirmation box: `y`
  (or `C-b X`, or a click on the card's `×` followed by `y`) sets `{"v":1,"state":"requested","at":<unix
  seconds>}`. The fleet daemon reads it and overwrites it with `{"v":1,"state":"accepted"}` once it has taken the
  request, or with `{"v":1,"state":"refused","reason":"<one line>"}` when it cannot. The server shows all three
  on the status line: `已请求关闭，等 daemon…` ("close requested, waiting for the daemon…"), `正在关闭…` ("closing…"), `关闭被拒：<reason>` ("close refused: <reason>"). The sidebar milestone that introduces
  this key only writes `requested`; the fleet daemon starts acting on it in the next milestone, so a request made
  now stops nothing.
- `restorable` — the fleet daemon's record of the runs this HOME can still restore: the fleets it closed from the
  sidebar (card line `已关闭 · 可恢复` ("closed · restorable")) and the runs a lifecycle timeout stopped with `resumable` (card line
  `到点停 · 可恢复` ("stopped on timeout · restorable")); one
  JSON object `{"v":1,"runs":[{"id":"…","title":"…","closed_at":<unix seconds>,"argv":["…",…],"cwd":"…","env":{"K":"V"}}]}`
  — `v` is `1`, `runs` holds 1–16 runs, newest closed first (a `timeout` run is listed by its `closed_at` like any
  other, and a run whose `result.json` is gone — a resumed one — drops out), `id` / `title` are at most 200
  characters, `closed_at`
  is a unix timestamp, `argv` is the non-empty command a restore spawns (§11), `cwd` the directory it runs in and
  `env` the extra environment (string to string); unknown keys are ignored. The server reads it together with
  `sidebar`, and an empty string clears it: the daemon writes one once its own cards are back, and the server also
  drops it when a restore is impossible. When no daemon runs, the launcher's `gui` entry point (`./start.sh gui`) scans this HOME's history once and writes this key together with `sidebar` before it execs
  `fleetmux gui`, so a window opened with no fleet still lists the grey cards; with nothing to list it writes
  neither key, prints `这个 HOME 没有可恢复的 fleet；用 start 新起，或 start --resume <run>` ("this HOME has no restorable fleet; use start for a new one, or start --resume <run>") and exits 1 without
  starting a server. A value that does not parse is ignored and logged once as
  `WARN kv restorable ignored: <reason>`. Like `sidebar` this key is not part of the protocol's version: the daemon
  writes it, the server never does, and a new server (new epoch) starts without it.
- `sidebar_width` — the width the user dragged the sidebar's inner edge to, written by the server when the drag is
  released: a decimal string of the width in columns. The server reads it back together with `sidebar`; a value
  that is not a number or is outside `24..=60` is ignored and the sidebar keeps its width. Like the shown/hidden
  flag this lives only in the server's memory and is **not** saved in `layout.json`: hiding and showing the sidebar
  again keeps the last width, while a restarted server starts at the default 34 columns (the daemon never writes
  this key).

M4 (§2 addendum): `kv.set` / `kv.get` use the namespace of the connection's page. The root-only keys `sidebar`,
`stop_request`, `restorable`, `sidebar_width` are `bad_request` on a page connection (set and get). `run` and
`history_url` are per page; when the server reads `history_url` itself it takes the current page's.

### 4.3 `kv.get`

| | |
|---|---|
| Arguments | `key`: string, required |
| Result | `value`: string, or `null` if the key was never set |
| Errors | — |

```json
{"id":3,"cmd":"kv.get","key":"run"}
{"id":3,"ok":true,"value":"fleet-20261028-095326-d346b75c"}
```

### 4.4 `pane.spawn`

| | |
|---|---|
| Arguments | `argv`: array of strings, non-empty, required — exec'd directly, no shell is typed into; `cwd`: string, optional — absolute path, must be an existing directory (absent: the child inherits the server's cwd); `env`: object string→string, optional — set on top of the server's inherited environment; `title`: string, optional (default `""`); `type`: string, optional (§1.5); `group`: group, optional; `anchor`: pane id, optional; `before`: bool, optional (default `false`) |
| Result | `pane` string, `pid` int, `pgid` int, `tty` string |
| Errors | `spawn_failed` (exec failed, `cwd` missing, PTY allocation failed); `unknown_type` (`group` is not one of the three names); `no_such_pane` (`anchor` given but not live); `bad_request` (a `before` key present without `anchor` — including `"before": false`; `group` given and different from `anchor`'s group) |

Placement of the new pane:
1. `anchor` given → into `anchor`'s group, immediately before it if `before` is `true`, else immediately after it.
2. else `group` given → at the end of that group.
3. else by `type` (design.md §3.4): `supervisor` → first in `supervisor`; `advisor` → last in `supervisor`;
   `worker` → last in `workers`; `daemon` → in `lower` right after the `history` pane (first if there is none);
   `tab` → last in `lower`; `history` → first in `lower`; `placeholder`, anything else, or no type → last in
   `workers`.

The new pane is not selected and does not take focus, except that it becomes the selected tab of a group that
was empty. The child gets a new session and process group with the PTY as controlling terminal, its size is the
current rectangle of its group (design.md §2.5), and the server sets `TERM=xterm-256color` and
`COLORTERM=truecolor` unless `env` sets them.

```json
{"id":4,"cmd":"pane.spawn","argv":["bash","-lc","cd /w && exec claude"],"cwd":"/w","env":{"FLEET_SLOT":"worker-1"},"title":"fx-worker-1","type":"worker"}
{"id":4,"ok":true,"pane":"k7q2-12","pid":5151,"pgid":5151,"tty":"/dev/ttys012"}
```

M4: optional argument `page` (a page id). Absent: the pane lands in the connection's page (created `live` if the
bound page does not exist yet). Given: the pane lands in that page, from any connection (the parent fleet spawns a
read-only record's history pane this way); the page must exist, else `bad_request` `no such page: <id>`. An
`anchor` must be in the same page (else `bad_request`). A pane spawned into a `reclaimed` page turns it `live`
again unless its `type` is `history`. The PTY size is the rectangle of its group in that page's layout. The
server's own panes — the restore pane and each sidebar bash (a tab of the `workers` group, §11) — land in the root
page.

### 4.5 `pane.list`

| | |
|---|---|
| Arguments | none |
| Result | `panes`: array of pane records (§4.0) for every live pane, ordered by group (`supervisor`, `lower`, `workers`) then `index` |
| Errors | — |

```json
{"id":5,"cmd":"pane.list"}
{"id":5,"ok":true,"panes":[{"pane":"k7q2-1","title":"fx-supervisor-1","type":"supervisor","group":"supervisor","index":0,"pid":5001,"pgid":5001,"tty":"/dev/ttys004","cols":118,"rows":34,"focused":true}]}
```

M4: optional argument `all`: bool, default `false`. By default only the panes of the connection's page are
listed; `all: true` lists every page's panes (ordered by page id, root first, then group and `index`), each record
with its `page` (§4.0).

### 4.6 `pane.get`

| | |
|---|---|
| Arguments | `pane`: pane id, required |
| Result | the pane record (§4.0), its keys at the top level of the reply |
| Errors | `no_such_pane` |

```json
{"id":6,"cmd":"pane.get","pane":"k7q2-12"}
{"id":6,"ok":true,"pane":"k7q2-12","title":"fx-worker-1","type":"worker","group":"workers","index":0,"pid":5151,"pgid":5151,"tty":"/dev/ttys012","cols":119,"rows":57,"focused":false}
```

### 4.7 `pane.write`

| | |
|---|---|
| Arguments | `pane`: pane id, required; `data`: string, required — standard base64 (RFC 4648 §4, `+/`, with `=` padding) of the bytes to write; `mode`: write mode, required |
| Result | `written`: int — number of decoded `data` bytes accepted (paste markers not counted) |
| Errors | `no_such_pane`; `bad_request` (invalid base64); `unknown_type` (`mode` not one of the three); `write_failed` (the pane's input queue is over its limit, or its PTY is closed) |

- `raw`: the bytes are written as they are, never wrapped.
- `bracketed`: always wrapped as `ESC [200~` + bytes + `ESC [201~`, whether or not the inner program has mode
  2004 on (fleet decides per CLI, design.md §2.6). If 2004 is off the server writes one `WARN` line to
  `server.log` ("bracketed paste sent but pane … has 2004 off"); the reply is still `ok`.
- `paste`: wrapped only if the inner program has 2004 on (what a real terminal does; for humans).
- The write is queued, not performed inline: the reply comes back once the bytes are in the pane's own
  queue, so one pane that does not read its input never blocks another pane or another connection. Queue limit
  per pane: 16 MiB pending.
- The data being base64 means a payload starting with `-` (or containing NUL, ESC, newlines) is plain data
  (codec test `base64_data_roundtrip_including_leading_dash`).

```json
{"id":7,"cmd":"pane.write","pane":"k7q2-12","data":"LS1oZWxwCg==","mode":"bracketed"}
{"id":7,"ok":true,"written":7}
```

### 4.8 `pane.key`

| | |
|---|---|
| Arguments | `pane`: pane id, required; `key`: string, required — a name from `protocol/keys.json` |
| Result | none |
| Errors | `no_such_pane`; `unknown_key` (name not in the table — **nothing is written**); `write_failed` |

The server encodes the key for the inner program's current modes: `Up` / `Down` / `Right` / `Left` / `Home` /
`End` are `ESC O x` in application-cursor mode and `ESC [ x` otherwise; every other name writes the bytes
listed in `keys.json`. Queued like `pane.write`.

```json
{"id":8,"cmd":"pane.key","pane":"k7q2-12","key":"Enter"}
{"id":8,"ok":true}
```

### 4.9 `pane.read`

| | |
|---|---|
| Arguments | `pane`: pane id, required; `lines`: int ≥ 1, required |
| Result | `text`: string |
| Errors | `no_such_pane` |

The text is built from scrollback followed by the current screen, one entry per **physical** row (a soft-wrapped
long line counts as several rows). The only change made to a row is that its trailing blanks are stripped; no row
is dropped, so blank rows at the bottom of the screen come back as empty lines. The last `lines` rows are kept and
joined with `\n` (no `\n` after the last row).
Asking for more rows than exist returns all of them (`capture_output(100000)`). Scrollback holds 10,000 rows by
default. If the reply would exceed the 4 MiB line limit, the oldest rows are dropped until it fits. Wide
characters appear once (their placeholder cell is not repeated).

```json
{"id":9,"cmd":"pane.read","pane":"k7q2-12","lines":4}
{"id":9,"ok":true,"text":"$ echo hi\nhi\n$\n"}
```

### 4.10 `pane.rename`

| | |
|---|---|
| Arguments | `pane`: pane id, required; `title`: string, required |
| Result | none |
| Errors | `no_such_pane` |

```json
{"id":10,"cmd":"pane.rename","pane":"k7q2-12","title":"fx-worker-1.1"}
{"id":10,"ok":true}
```

### 4.11 `pane.focus`

| | |
|---|---|
| Arguments | `pane`: pane id, required |
| Result | none |
| Errors | `no_such_pane` |

Makes the pane the selected tab of its group and gives that group the focus. The zoom state is unchanged.

```json
{"id":11,"cmd":"pane.focus","pane":"k7q2-1"}
{"id":11,"ok":true}
```

M4: selects and focuses the pane inside **its own page's** layout; it never changes the current page (a child
fleet that starts never steals the screen).

### 4.12 `pane.move`

| | |
|---|---|
| Arguments | `pane`: pane id, required; `group`: group, required; `before`: pane id, optional; `after`: pane id, optional |
| Result | none |
| Errors | `no_such_pane` (`pane`, `before` or `after` not live); `unknown_type` (bad `group`); `bad_request` (both `before` and `after`; the reference pane is not in `group`; the reference pane is `pane` itself) |

Position: `before` → immediately before that pane; `after` → immediately after it; neither → end of `group`
(`proto` maps these onto `layout::Pos::{Before, After, End}`). Moving within the same group only reorders it;
no selection changes. Moving to another group: the target group's current selection is kept (the moved pane does
not take it over), except that a target group that was empty selects the moved pane; in the old group, if the moved
pane was its selected tab, the selection moves by the removal rule of §4.13 / design.md §3.2 (to the pane inserted
immediately before it, else its left neighbour). Focus does not change. Note the two
position shapes: `pane.spawn` takes `anchor` + `before: bool`, `pane.move` takes `before` / `after` as pane
id strings.

```json
{"id":12,"cmd":"pane.move","pane":"k7q2-13","group":"workers","before":"k7q2-12"}
{"id":12,"ok":true}
```

M4: the moved pane may be in any page; the reference pane of `before` / `after` must be in the connection's page
(else `bad_request`), and without a reference the pane goes to the group of the connection's page. A pane from
another page is removed from that page's layout (selection moves as for any removal) and placed into this page's —
same PTY, same process, same id; it is moved even when the group name is the same. Inside one page the existing
rules and refusals are unchanged. The PTY is resized to its rectangle in the new page.

### 4.13 `pane.kill`

| | |
|---|---|
| Arguments | `pane`: pane id, required; `timeout_ms`: int, optional, 0–600000, default **5000** |
| Result | none — sent only after every process of the pane's process group has exited and the child is reaped (and its foreground job's group is gone or past the deadline, below) |
| Errors | `no_such_pane`; `kill_timeout` — the group was not gone by the deadline; **the pane stays** (still listed, still in the layout and in `panes.json`) so the caller can retry |

Escalation (design.md §2.4), times measured from the request: SIGHUP to the process group at t = 0, SIGTERM
at t = 2000 ms, SIGKILL at t = 5000 ms. A step is sent only if the group is still alive **and the step's time is
≤ `timeout_ms`**. After the last allowed step the server waits for the reap until t = `timeout_ms` + 500 ms (the
reap-confirmation grace); **no signal is ever sent during the grace**. If the group is still there at the end of
the grace the reply is `kill_timeout`. Consequences:
- `timeout_ms` = 5000 (default): SIGHUP, SIGTERM at 2 s, SIGKILL at 5 s, then up to 500 ms to confirm the reap.
- `timeout_ms` < 5000: **SIGKILL is never sent**; e.g. 2000 → SIGHUP and SIGTERM, 0 → SIGHUP only. A process that
  ignores the signals it gets yields `kill_timeout` (how `pane_kill_timeout_is_error_and_pane_stays` provokes it).
- `timeout_ms` > 5000: the full schedule, then waiting continues until `timeout_ms` + 500 ms.

After a `kill_timeout` the server sends no further signals; a later `pane.kill` restarts at SIGHUP. Only the pane's own
process group and its foreground job's group (below) are signalled — never `pkill`, never by command-line pattern.

**Foreground job.** An interactive shell (job control) runs its foreground job in a process group of
its own, which the pane's group signal does not reach; a job that ignores SIGHUP used to outlive the kill as an
orphan. At the request, before the first signal, the server reads the PTY's foreground group (`fg_pgid`, §4.0); if
it is not the pane's own and its leader is in the pane's session (`getsid` = the pane's `pid`), every step is sent to
that group too, each only while its leader is still in the pane's session — a pgid reused outside the session is
never hit. The pane is removed once its own group is gone and the job's group is gone or no longer verifiable; a
job group still alive at `timeout_ms` + 500 ms does not hold the pane (it is removed, `server.log` gets a `WARN`
naming the group) and is never a `kill_timeout`, which stays the pane's own group's verdict. Limits: the group is
read once, at the request; background jobs (`cmd &`), a job whose group leader already exited, panes whose shell
exits on its own (§4.0) and the crash-time `fleetmux reap` (§9.3, `panes.json`) still reach only the pane's own
group.

On success the pane is removed from its group, and if it was the selected tab the selection moves per
design.md §3.2 (to the pane inserted immediately before it, else its left neighbour).

```json
{"id":13,"cmd":"pane.kill","pane":"k7q2-12","timeout_ms":5000}
{"id":13,"ok":true}
```

### 4.14 `layout.get`

| | |
|---|---|
| Arguments | none |
| Result | the layout object: `h_ratio` int (left column width, % of content width), `v_ratio` int (supervisor group height, % of left column), `zoom` pane id or `null`, `focus` group, `groups` object group → array of pane ids in tab order, `selected` object group → pane id or `null` |
| Errors | — |

The same object is what `layout.json` holds (§8.3). Defaults: `h_ratio` 50, `v_ratio` 60, `zoom` null, `focus`
`supervisor`.

```json
{"id":14,"cmd":"layout.get"}
{"id":14,"ok":true,"h_ratio":50,"v_ratio":60,"zoom":null,"focus":"supervisor","groups":{"supervisor":["k7q2-1"],"lower":["k7q2-2","k7q2-3"],"workers":["k7q2-12"]},"selected":{"supervisor":"k7q2-1","lower":"k7q2-2","workers":"k7q2-12"}}
```

### 4.15 `layout.set`

| | |
|---|---|
| Arguments | `h_ratio`: int, optional; `v_ratio`: int, optional; `zoom`: pane id or `null`, optional (`null` = unzoom) |
| Result | the full layout object after the change (same keys as `layout.get`) |
| Errors | `no_such_pane` (`zoom` names a pane that is not live) |

Ratios are clamped to 10–90 (as `Layout::set_ratios` does); a non-integer is `bad_request`. Absent keys are left
unchanged; `{}` is a no-op that returns the layout. A successful change rewrites `layout.json` atomically and
resizes the affected PTYs.

```json
{"id":15,"cmd":"layout.set","h_ratio":65,"zoom":null}
{"id":15,"ok":true,"h_ratio":65,"v_ratio":60,"zoom":null,"focus":"supervisor","groups":{"supervisor":["k7q2-1"],"lower":[],"workers":[]},"selected":{"supervisor":"k7q2-1","lower":null,"workers":null}}
```

M4 (`layout.get` / `layout.set`): `groups`, `selected`, `zoom` and `focus` are those of the connection's page
(a bound page that does not exist yet reads as empty groups; `layout.set` creates it). `h_ratio` / `v_ratio` are
shared by the whole tree.

### 4.16 `server.shutdown`

| | |
|---|---|
| Arguments | `force`: bool, optional, default `false` |
| Result | none |
| Errors | `not_empty` — panes still exist and `force` is not `true` (nothing is changed); `kill_timeout` — with `force: true`, at least one pane's process group could not be reaped (see below) |

With `force: true` and panes present, the server first kills every pane with the `pane.kill` escalation at the
default timeout (its foreground job included, §4.13). If any pane's process group is still alive at its deadline, the reply is `kill_timeout`, the server
**keeps running** (still accepting connections), and every unreaped pane stays listed, in the layout and in
`panes.json`; panes that were reaped are removed as usual. The caller may retry.

Once there are no panes (immediately when there were none), the server stops accepting connections, sends `0x82`
to every view connection (§6.3), replies `ok`, then removes `ctl.sock`, `server.json` and `panes.json` and exits 0.
`server.log`, `layout.json` and `start.lock` are kept. This is the meaning of "only logs remain after `stop`":
nothing that describes a live server or live processes survives, but two non-log files do on
purpose — `layout.json` is a persistent preference (the ratios a later server reuses, §8.3) and `start.lock` is
the lock target that must not be deleted (§8.4). The client confirms the exit by waiting
for the server pid to disappear (design.md §2.3).

```json
{"id":16,"cmd":"server.shutdown","force":false}
{"id":16,"ok":false,"code":"not_empty","error":"3 panes still running"}
```

M4: from a page connection → `bad_request` (nothing changes). From a root connection: unchanged; `not_empty`
counts the panes of every page and `force` kills every page's panes.

### 4.17 `server.stats`

| | |
|---|---|
| Arguments | none |
| Result | `panes` int (live panes), `viewers` int (open view connections), `frames` int (screen frames drawn since the server started, all viewers together), `parse_bytes` int (PTY output bytes fed to the parsers since start), `wakeups` int (times the event loop woke up since start), `rss_bytes` int (the server's resident set size in bytes), `scrollback_rows` int (sum of the scrollback rows of every pane) |
| Errors | — (only the common `bad_request` / `internal`) |

All counters are cumulative and never reset while the server runs (a new server starts at 0). `wakeups` is the
decidable form of "no polling" (design.md §2.7): with no PTY output, no viewer input and no control request,
two `server.stats` calls 1 s apart MUST report the same `wakeups` (answering `server.stats` itself is not counted
as a wake-up). The result maps onto `proto::Stats` (`Command::ServerStats`); used by `tests/stats.rs` and
`tests/perf.rs` (M3).

```json
{"id":17,"cmd":"server.stats"}
{"id":17,"ok":true,"panes":2,"viewers":1,"frames":148,"parse_bytes":52344,"wakeups":391,"rss_bytes":18874368,"scrollback_rows":1200}
```

M4: optional result key `pages` int — the number of pages, root included (`1` on a tree without child pages).

### 4.18 `web.open` (M2.5)

The `web.*` commands drive the native web window of design.md §3.5. The server never talks to a window itself: it
relays each command over the view connections (§6.3 `0x84` / `0x04`) to the attach clients that have a window,
and those clients talk to their own `fleetmux-web` over `web.sock` (§15). A viewer "has a window" from the moment
its client reports `{"event":"window","state":"open"}` until it reports `closed` or the view connection ends. The
**primary window** is the viewer that most recently reported `open` (design.md §3.5.4). The server waits at most
**3 s** for the replies of one relayed command; past that the command fails with `web_timeout`. The server does
nothing else for `web.*` (no window state of its own beyond who has one), and the `web.*` commands never mark the
viewers dirty.

| | |
|---|---|
| Arguments | `url` string, optional (absent / `null` = the kv value `history_url`, §4.2) |
| Result | `windows` int: viewers that have a window after the command |
| Errors | `web_timeout` (a viewer with a window did not answer within 3 s) |

Sends `{"op":"open","url":…}` to **every** viewer (with or without a window): a client without a window starts one
if it can (§15, design.md §3.5.6), a client with one brings it to the front and loads `url`. `windows` = 0 is not
an error (nobody can show a window, e.g. every viewer is on a headless host).

```json
{"id":18,"cmd":"web.open"}
{"id":18,"ok":true,"windows":1}
```

### 4.19 `web.goto` (M2.5)

| | |
|---|---|
| Arguments | `url` string, required |
| Result | `windows` int: viewers whose window loaded `url` |
| Errors | `no_window` (no viewer has a window), `web_timeout` |

Broadcast `{"op":"goto","url":…}` to every viewer that has a window.

```json
{"id":19,"cmd":"web.goto","url":"file:///r/history.html?x=1"}
{"id":19,"ok":true,"windows":2}
```

### 4.20 `web.url` (M2.5)

| | |
|---|---|
| Arguments | none |
| Result | `url` string: the primary window's current URL (`WebView::url()`) |
| Errors | `no_window`, `web_timeout` |

Asks the primary window only.

```json
{"id":20,"cmd":"web.url"}
{"id":20,"ok":true,"url":"file:///r/history.html?x=1"}
```

### 4.21 `web.reload` (M2.5)

| | |
|---|---|
| Arguments | none |
| Result | `windows` int: viewers whose window reloaded (0 when none has a window; not an error) |
| Errors | `web_timeout` |

Broadcast `{"op":"reload"}` to every viewer that has a window. Each client first reports what copy of the page it
has (`{"op":"page",…}`, §6.3); the server pushes the page bytes only to a client whose copy differs from the
server's `history.html` (design.md §3.5.3), so a page that did not change never crosses the connection again.
`refresh_history` calls this every round. In `have` and `page`, `size` is in bytes and `mtime` is the file's modification time in integer
nanoseconds since the Unix epoch.

```json
{"id":21,"cmd":"web.reload"}
{"id":21,"ok":true,"windows":1}
```

M4 (`web.open` / `web.goto` / `web.url` / `web.reload`, §2 addendum): only the connection of the **current**
page reaches the windows, so the daemon of a page in the background never drives the one shared window. From a
connection whose page is not the current page, `web.open`, `web.goto` and `web.reload` relay nothing and answer
`{"windows": 0}`, and `web.url` answers `no_window`. `web.url` also answers `no_window` once the tree has child
pages and the primary window's URL path differs from the path of this page's kv `history_url` (query and fragment
ignored). `web.open` without `url` opens this page's `history_url`.

### 4.22 `web.eval` (M2.5, reserved)

| | |
|---|---|
| Arguments | `js` string, required |
| Result | — |
| Errors | `not_implemented`, always (design.md §3.5.11) |

Reserved, not implemented: the server answers `not_implemented` and relays nothing. It is decoded (not
`unknown_cmd`) so that a later version can implement it without changing the command set.

```json
{"id":22,"cmd":"web.eval","js":"document.title"}
{"id":22,"ok":false,"code":"not_implemented","error":"web.eval is reserved and not implemented"}
```

### 4.23 `page.open` (M4)

| | |
|---|---|
| Arguments | `page`: page id, required — 1–64 characters of `A-Z a-z 0-9 . _ -` (`proto::valid_page_id`); `title`: string, optional; `reclaimed`: bool, optional (default `false`); `history_url`: string, optional |
| Result | none |
| Errors | `bad_request` — `page` missing, invalid or `""` (the root page cannot be opened); a new page beyond the limit of 64 child pages (`proto::MAX_PAGES`, reclaimed pages counted) |

Create, reopen or rename a child page; idempotent. A new page starts with the fixed layout's empty groups and its
own empty kv, titled `title` (absent: its id). An existing page: `title` (when given) replaces its title, and its
state becomes `reclaimed` when `reclaimed` is `true`, else `live`. `history_url` sets that page's kv `history_url`
(the read-only record page of a reclaimed child fleet). Any connection may send it.

```json
{"id":23,"cmd":"page.open","page":"M0","title":"M0 build"}
{"id":23,"ok":true}
```

### 4.24 `page.close` (M4)

| | |
|---|---|
| Arguments | `page`: page id, required (not `""`); `keep`: bool, optional (default `true`) |
| Result | none |
| Errors | `bad_request` — `page` missing / invalid / `""`, or `no such page: <id>`; `not_empty` — the page still has panes (nothing changes) |

`keep: true` turns the page `reclaimed` and keeps it (its kv too). `keep: false` removes the page; when it was the
current page, the current page goes back to the root first. Python never removes a page (`keep: false` is for
tests and manual use).

```json
{"id":24,"cmd":"page.close","page":"M0"}
{"id":24,"ok":false,"code":"not_empty","error":"page.close: page \"M0\" still has 2 panes"}
```

### 4.25 `page.select` (M4)

| | |
|---|---|
| Arguments | `page`: string, required — `""` (the root page) or a page id |
| Result | none |
| Errors | `bad_request` — `page` missing or invalid, or `no such page: <id>` |

Make `page` the current page of every viewer; any connection may send it. Every viewer redraws the new page, page
mode (§13) ends, and every viewer with a web window gets `0x84 goto <that page's history_url>` when the page has
one (relayed after the reply; its outcome is only logged). Selecting the current page again changes nothing. A
`reclaimed` page can be the current page (the read-only record).

```json
{"id":25,"cmd":"page.select","page":"M0"}
{"id":25,"ok":true}
```

### 4.26 `page.list` (M4)

| | |
|---|---|
| Arguments | none |
| Result | `pages`: array of `{page, title, state, panes, current}` — root page first, then the child pages by id; `state` is `live` or `reclaimed` (the root is always `live`, its `title` is its kv `run` or `""`), `panes` the number of panes in that page, `current` whether it is the current page |
| Errors | — |

```json
{"id":26,"cmd":"page.list"}
{"id":26,"ok":true,"pages":[{"page":"","title":"fleet-20261104-120756-e150d901","state":"live","panes":4,"current":true},{"page":"M0","title":"M0 build","state":"reclaimed","panes":0,"current":false}]}
```

---

## 5. Error codes

The fixed set. `proto::ErrorCode` has exactly these variants; their wire form is the snake_case string.

| `code` | Meaning | Returned by |
|---|---|---|
| `bad_request` | not JSON / not an object / invalid UTF-8 / oversize line / `id` missing or invalid / `cmd` missing / an argument missing, of the wrong type, out of range or contradictory / invalid base64 | any line |
| `unknown_cmd` | `cmd` is a string but not one of the 22 names (26 with the M4 page commands) | any line |
| `no_such_pane` | the pane id (or `anchor` / `before` / `after` / `zoom` reference) is not a live pane of this server | every `pane.*` taking `pane`, `layout.set` |
| `unknown_key` | `key` is not in `keys.json`; nothing was written | `pane.key` |
| `unknown_type` | a closed-enum argument has an unknown value: `group` or `mode`. An unknown *pane type* is never an error: it is placed last in `workers` | `pane.spawn`, `pane.move`, `pane.write` |
| `not_empty` | panes still exist (M4 `page.close`: in that page) | `server.shutdown`, `page.close` |
| `spawn_failed` | PTY allocation, fork or exec failed, or `cwd` is not a directory | `pane.spawn` |
| `kill_timeout` | process group still alive at the deadline; pane kept (for `server.shutdown`: the server keeps running) | `pane.kill`, `server.shutdown` (`force: true`) |
| `write_failed` | the pane's input queue is full or its PTY is closed | `pane.write`, `pane.key` |
| `protocol_version` | the client's `protocol` / view version is not the server's | `hello`, view handshake |
| `internal` | anything else (bug, unexpected OS error); details in `error` and `server.log` | any |
| `no_window` | M2.5: no view connection has a web window | `web.goto`, `web.url` |
| `web_timeout` | M2.5: a viewer with a web window did not answer within 3 s | `web.open`, `web.goto`, `web.url`, `web.reload` |
| `not_implemented` | M2.5: the command is reserved but not implemented | `web.eval` |

M4 adds no code (§2 addendum): an unknown page is `bad_request` with `error` `no such page: <id>`, and the
page-connection refusals (`server.shutdown`, root-only kv keys, a second `hello` with another `page`, the 65th
child page) are `bad_request` too.

---

## 6. View connection (attach)

### 6.1 Handshake

The client's first line on the socket (UTF-8 JSON + `\n`, same 4 MiB limit):

```json
{"view":1,"cols":120,"rows":40,"term":"xterm-256color","colorterm":"truecolor"}
```

- `view`: int, the view-framing version, **1**. `cols`, `rows`: int 1–65535, the outer terminal's size.
  `term`, `colorterm`: strings, the outer terminal's `$TERM` / `$COLORTERM` (`""` if unset); the server uses them
  to choose colour depth (M1 downgrades RGB to 256 colours).
- There is no `id` and no `cmd`; a handshake that also carries `cmd` is invalid. The presence of the top-level
  `view` key is what makes a first line a handshake (§1.1); a first line without it is a control line.
- M2.6, optional `overlay`: int. `1` = this connection wants `0x85` overlay frames (§6.3; `fleetmux gui`, §9.5);
  key absent or `0` = it does not (every `fleetmux attach` connection). Only the integers `0` and `1` are valid: any
  other value — **including an explicit `null`** (an exception to the §1.3 null-as-absent rule), `1.0`, another
  number, a string, a boolean — makes the handshake invalid: `bad_request` line, then close, as for any invalid handshake
  below (`view::wants_overlay`, read beside `view::Hello`, which ignores keys it does not know). A server older than
  M2.6 ignores the key and never sends `0x85`.
- M2.6, optional `scroll` (gui-scrollbar): int, exactly the `overlay` rule above, and a **separate** key. `1` = this
  connection wants `0x86` scroll-state frames (§6.3; `fleetmux gui`, §9.5); key absent or `0` = it does not (every
  `fleetmux attach` connection, and a `fleetmux gui` older than this change). Only the integers `0` and `1` are
  valid: any other value — **including an explicit `null`**, `1.0`, another number, a string, a boolean — makes the
  handshake invalid: `bad_request` line, then close (`view::wants_scroll`, same helper as `wants_overlay`). A server
  older than this change ignores the key and never sends `0x86`. It is not folded into `overlay` on purpose: an old
  `fleetmux gui` already sends `"overlay":1` and must keep receiving no `0x86` (it would close on an unknown type,
  §6.2).
- S1e, optional `inset`: int 0–40, the columns this viewer's row 0 leaves to the gui window's controls (macOS
  traffic lights, §15.4), so the sidebar toolbar starts right of them. Linux draws its one header row in the
  window's own GTK headerbar (§15.4), so its grid keeps no topbar row and its page reports no inset
  (`cols` 0) — it still sends its `cell` width on the same `inset` message (§15.4), which is what puts that
  headerbar's boundary on the grid's divider column.
  Key absent or `0` = no
  room needed (every `fleetmux attach` connection, and a window too narrow for the toolbar). Only integers `0`–`40` are valid: any other
  value — **including an explicit `null`**, `41`, `-1`, `1.0`, another number, a string, a boolean — makes the
  handshake invalid: `bad_request` line, then close (`view::wants_inset`). The server keeps the value per connection;
  the sidebar's inset is the **maximum** over all connected viewers (§6.3 `0x06`). A server older than S1e ignores the
  key and never sends `0x06`.

Server reply line:

```json
{"ok":true,"epoch":"k7q2","protocol":1}
```

or, for an invalid handshake (a `view` key is present but the line is otherwise wrong), one line
`{"ok":false,"code":"bad_request"|"protocol_version","error":"…"}` followed by close (`protocol_version` when
only the `view` number is wrong). This is the only first-line case that closes the connection; bad JSON and every
other first line are handled as control lines (§1.1, §1.3).

- S1e: an `ok` line may also carry `"inset":1` (`{"ok":true,"epoch":"k7q2","protocol":1,"inset":1}`): the
  server understands the handshake's `inset` key and the `0x06` frame, so a client **may** send `0x06` (§6.3). A
  client that does not see the key must not send `0x06` (a server older than S1e closes on an unknown type, §6.2).
  Clients ignore the key; the view-framing version stays 1.
- M4: an `ok` line may also carry `"pages":1` (`{"ok":true,"epoch":"k7q2","protocol":1,"inset":1,"pages":1}`): the
  server has fleet pages (§2 M4 addendum) and handles the `0x04` event `{"event":"jump","url"}` (§6.3). Only then
  does a client with a web window (M2.5 window, `fleetmux gui`) stop `?fleet-jump=` links and report them
  (§15.3, §15.4); without the key it loads them as before. Clients that do not use it ignore the key; the
  view-framing version stays 1. A viewer that declares nothing new sees page switches as ordinary redraws: no new
  frame type reaches it.

After the `ok` line the connection carries only binary frames, in both directions. The server sends a full
screen as its first `0x81` frame(s).

### 6.2 Frame format

```text
+--------+----------------------+-----------------+
| type   | length               | payload         |
| 1 byte | 4 bytes, big-endian  | length bytes    |
+--------+----------------------+-----------------+
```

- Maximum payload: **1 MiB** (1,048,576 bytes). A frame header announcing more is a protocol error: the reader
  returns an error without reading or allocating the payload and the connection is closed (codec test
  `view_frame_rejects_oversize`). A sender splits larger screen updates into several `0x81` frames.
- An unknown type, or a payload length not allowed for the type, is a protocol error → close.
- M2.5: type `0x84` alone has its own maximum, **32 MiB** (33,554,432 bytes; `view::WEB_FRAME_MAX`), because it can
  carry a whole `history.html` (design.md §3.5.3: 0.3–5.2 MB measured). Every other type keeps the 1 MiB maximum,
  including `0x04`. `view::max_payload(ty)` gives the maximum per type.

### 6.3 Frame types

| Type | Direction | Payload | Meaning |
|---|---|---|---|
| `0x01` | client → server | 0–1 MiB raw bytes | Input read from the outer terminal (keys, mouse reports, bracketed-paste blocks), unmodified. The server intercepts the prefix key (`Ctrl-b`), SGR mouse sequences and outer paste blocks; everything else goes to the focused pane (design.md §2.6) |
| `0x02` | client → server | exactly 4 bytes: `cols` u16 BE, `rows` u16 BE (each ≥ 1) | The outer terminal was resized |
| `0x03` | client → server | 0 bytes | The client is detaching; the server closes the connection |
| `0x81` | server → client | 0–1 MiB raw bytes | Screen bytes: escape sequences the client writes to its terminal as they are |
| `0x82` | server → client | 0–1 MiB UTF-8 text (may be empty) | The server is exiting; payload is a reason for the client to print. The server closes after it |
| `0x83` | server → client | 0–1 MiB UTF-8 text (may be empty) | **Open**: the prefix key `o` was pressed (§10). Payload is a local path to open on the **client's** machine: the value of kv key `history_url` with a leading `file://` removed; empty when `history_url` was never set. The client runs `open` (macOS) / `xdg-open` (elsewhere) on it; if the path is empty it shows `no history url`, and if opening fails (e.g. the path does not exist on the client's host, design.md §3.3) it shows the path. A non-UTF-8 payload is a protocol error → close (`FrameError::BadUtf8`). M2.5 client handling order: web window (§15) → `open` / `xdg-open` → show the path |
| `0x84` | server → client | 0–32 MiB UTF-8, one JSON line | **Web** (M2.5): a command for the client's web window, `{"seq":n,"op":"open"\|"goto"\|"url"\|"reload"\|"eval"\|"quit"\|"page","url"?:…,"page"?:{"path","size","mtime","b64"?}}`. `seq` is the server's number for this relay; the client answers each `seq` with one `0x04` reply. `page` carries the server's copy of the page (§4.21): `b64` present = the client writes it to its `page/history.html` (0600, §15) and opens that; absent = the client's copy is current. A non-UTF-8 payload is a protocol error → close |
| `0x04` | client → server | 0–1 MiB UTF-8, one JSON line | **Web reply** (M2.5): either the reply to one `0x84`, `{"seq":n,"ok":true\|false,"url"?:…,"result"?:…,"error"?:…}`, or an event the client sends on its own: `{"event":"window","state":"open"\|"closed"}` (the window started / went away), `{"op":"page","path":…,"have":{"size":…,"mtime":…}\|null}` (what copy of the page at `path` the client has; the server answers with a `0x84` carrying `page`, design.md §3.5.3), or — M4, only after an `ok` line with `"pages":1` (§6.1) — `{"event":"jump","url":…}`: the window stopped a `?fleet-jump=…&to=<target history page>` link (the `url` names the server's own path: a client that showed a pushed copy from `page/` maps it back first). The server selects the page whose kv `history_url` has the path of the (percent-decoded) `to` — the root page included — exactly as `page.select` does (every viewer switches, the web windows get `0x84 goto <that page's history_url>`); no such page → an INFO line in `server.log`, nothing else. No reply either way. A non-UTF-8 payload is a protocol error → close |
| `0x05` | client → server | 0–1 MiB UTF-8, one JSON line | **Scroll to** (M2.6, gui-scrollbar): `{"pane":id,"offset":n}` — put that pane's view `n` lines above the bottom (rules below). Any view connection may send it, whether or not its handshake declared `scroll`; there is no reply. A non-UTF-8 payload is a protocol error → close |
| `0x06` | client → server | 0–1 MiB UTF-8, one JSON line | **Inset** (S1e): `{"inset":n}` or `{"inset":n,"sidebar_min":m}`, `n` int 0–40 — the columns this viewer's row 0 leaves for the gui window's native controls (macOS traffic lights; Linux draws that row in its GTK headerbar instead so it sends none, §15.4), re-sent after a font-size or window-size change. `sidebar_min` (optional, int 0–65535, absent = 0) is the narrowest sidebar band this viewer's own header controls need, derived by the window from the header row's real allocation, the toolbar's measured intrinsic width and the page's `cell`; the server keeps the shown sidebar at least that wide (a floor above the widest drag width is honoured as it is; if the band would leave the content fewer than its minimum columns the layout hides it, so there is still one boundary, never two), so a drag below it moves the grid's divider and the header boundary together (the requested width in kv is unchanged). 65535 is a legal value: the layout decides it with that content rule, the parser does not reject it. A client sends it only after an `ok` line that carried `"inset":1` (§6.1); any view connection may send it, there is no reply. A non-UTF-8 payload is a protocol error → close |
| `0x85` | server → client | 0–1 MiB UTF-8, one JSON line | **Overlay** (M2.6): where the history page goes on this viewer, `{"pane":id\|null,"rect":{"x","y","w","h"}\|null,"url":string\|null,"topbar":{"title","status"}\|null}` (rules below). Sent **only** on a connection whose handshake said `"overlay":1` (§6.1). A non-UTF-8 payload is a protocol error → close |
| `0x86` | server → client | 0–1 MiB UTF-8, one JSON line | **Scroll state** (M2.6, gui-scrollbar): where each visible cell is on this viewer and how far it is scrolled, `{"panes":[{"pane":id,"rect":{"x","y","w","h"},"offset":n,"total":m},…]}` (rules below). Sent **only** on a connection whose handshake said `"scroll":1` (§6.1). A non-UTF-8 payload is a protocol error → close |

**Page exchange (M2.5, design.md §3.5.3).** Before a client loads a page for a `0x84 open` / `reload` (or for a
`0x83`), it may ask for the server's copy: `0x04 {"seq":<that command's seq; absent for 0x83>,"op":"page","path":P,
"have":{"size","mtime"}|null}` (`have` = the client's own copy of `P`, or of its `page/history.html` when it shows a
pushed page). The server answers `0x84 {"seq":<same>,"op":"page","page":{"path":P,"size","mtime","b64"?}}` — `b64`
only when `have` differs — and the client writes `b64` (when present) to `page/history.html` before loading, then
sends the command's own reply. A client that does not ask gets no page; the server never requires the exchange. The
3 s limit of §4.18 covers the whole exchange. `size` is in bytes, `mtime` in integer nanoseconds since the Unix epoch.
When the server cannot read `P` (missing, not a regular file, not an absolute path) or the answer would not fit in
one `0x84` (32 MiB, §6.2), it answers `0x84 {"seq"?,"op":"page","error":"<English sentence>"}` without `page` / `b64`
(nothing of the file is sent); `seq` is echoed when the request had one and omitted otherwise.

**Overlay (M2.6, design.md §3.6.3).** `0x85` tells a GUI viewer (§9.5) where to lay the history page over its
screen. All three keys are always present:

| Key | Rule |
|---|---|
| `pane` | The pane whose type is `history` (§4.4); `null` when there is none |
| `rect` | Only while that pane is visible on this viewer: when it is the zoomed pane → the zoom rectangle; when nothing is zoomed and it is the lower group's selected tab → the lower group's inner rectangle (the PTY area: no border, no tab row); otherwise `null`. Cells of **this viewer's own canvas**, 0-based: computed at the layout size (the PTY size, below), then clipped to this viewer's `cols × (rows − 1)` (its bottom row is its own status line and is never covered); a clip with no width or no height → `null`. `x`, `y`, `w`, `h`: int 0–65535 |
| `url` | kv `history_url` (§4.2) as stored; `null` when unset |
| `topbar` | the content for a window that draws its own header row — `{"title":string,"status":string,"sidebar":int,"cols":int}`. `title`/`status` = the current fleet's `title` and `line1` (kv `sidebar`, §4.2), unclipped (the window ellipsizes); both empty when no current card is named but the sidebar exists. `sidebar` = the band drawn on this viewer in columns (`Layout::sidebar_cols`, 0 = none) and `cols` = the layout's columns; the window puts its workspace/sidebar container boundary on that band's divider (§15.4). The two geometry keys are optional: absent = 0 = no boundary (an older client ignores them; an older server's payload decodes). `null` when there is no kv `sidebar` data at all, and always `null` for an unmirrored layout (macOS), whose topbar is grid row 0 |
| When | Once after the handshake, after the first screen frame; after that, on every render round (also when the screen itself did not change, e.g. after `kv.set history_url`) it is computed again and sent only when it differs from the last `0x85` this viewer got. It goes into the same queue as the screen frames, after that round's `0x81` frames, so order is kept. A round skipped for this viewer because its queue is full sends nothing; the next round catches up |

**Compatibility rule: `0x85` is sent only to a connection whose handshake declared `"overlay":1`; a client that
did not declare it never receives one** (a client that predates M2.6 treats an unknown frame type as a protocol
error and disconnects, §6.2). The client never sends `0x85`; one arriving at the server is a protocol error →
close. The protocol version (1) and the view-framing version (1) are unchanged.

**Scroll state (M2.6, gui-scrollbar).** `0x86` tells a GUI viewer (§9.5) where every visible cell sits on its own
canvas and how far that cell is scrolled, so the page can draw one scrollbar per cell. `panes` is always present
and is an array, possibly empty. Entries are in `Group::ALL` order (supervisor, lower, workers); while a pane is
zoomed only the zoomed pane appears, otherwise each group's selected tab, and an empty group is skipped.

| Key | Rule |
|---|---|
| `pane` | the pane id (§1.4) |
| `rect` | that pane's inner rectangle from the layout at the PTY size (the PTY area: no border, no tab row), in cells of **this viewer's own canvas**, 0-based, clipped to this viewer's `cols × (rows − 1)` (its bottom row is its own status line and is never covered); a clip with no width or no height → the pane is left out. `x`, `y`, `w`, `h`: int 0–65535 |
| `offset` | the pane's `display_offset`: lines scrolled back from the live screen (0 = live), the same number page mode's status line reports as `[off/total]` (§13) |
| `total` | the pane's `scrollback_len()`: history lines above the live screen |
| Skipped | a pane of type `history` (§4.4) never appears: the gui covers it with the history iframe, so the embedded page gets no scrollbar of ours and keeps its own scrolling; a zoomed history pane makes `panes` empty. A pane without a parser is left out (it should not happen) |
| When | once after the handshake, after the first screen frame; after that, on every render round (also when the screen itself did not change) it is computed again and sent only when it differs from the last `0x86` this viewer got. Same queue as the screen frames, after that round's `0x81`, so order is kept. A round skipped for this viewer because its queue is full (or it is parked) sends nothing; the next round catches up |

The rectangles are per viewer, so two viewers of different sizes get different `0x86`.

**Scroll to (M2.6, gui-scrollbar).** `0x05` puts one pane's `display_offset` at `offset`; the parser clamps it to
its own `scrollback_len()` (`total` above). A line that does not decode, or a `pane` that does not exist, is a
warning in the server log and nothing else — the connection stays open. There is no reply: the next `0x86` is the
acknowledgement. A real change makes the server render again, so the screen and the next `0x86` follow it. Writing
into that pane (a `0x01` byte for it, §11) puts its offset back to 0, as before.

**Inset (S1e).** `0x06` updates one connection's `inset` — the same key as the handshake (§6.1), the same range
0–40. The sidebar's toolbar is pushed right by the **biggest** inset any connected viewer reported (`0` when none
did), so every viewer — a plain `fleetmux attach` terminal included — sees `◫ + ↺ ← →` at the same columns and
the gui's window controls never sit on them. Collapsing the sidebar takes its width to 0 — the panes fill the
screen from column 0 — and row 0 keeps the toolbar's own `inset + 15` columns, so the buttons do not move; a
shown sidebar is at least `max(24, inset + 15)` (§10, §11). A line that does not decode is a warning in the server
log and nothing else — the connection stays open; there is no reply, the next screen update carries the moved
toolbar. A viewer that disconnects gives its inset back (the maximum is recomputed), so the toolbar returns to
column 1 when the last gui leaves. The client never receives `0x06`; one arriving at the client is a protocol
error → close.

**Inset's `sidebar_min`.** The Linux window draws its controls in the native GTK
headerbar, outside the grid, so its page reports `inset` 0 but the window adds `sidebar_min`: the narrowest sidebar
band, in columns, at which the five custom tools and GTK's own window buttons still fit in that header row. The
window computes it from the row's real allocation, the toolbar's measured intrinsic width and the page's `cell`
(§15.4), never from a fixed column count. The server keeps the shown sidebar at least `max(24, inset + 15,
sidebar_min)` columns (a floor above the widest drag width is honoured; when even that cannot leave the content its
minimum columns the layout hides the band — one boundary, never two), aggregated as the maximum over connected
viewers like `inset`. A legal `sidebar_min` up to 65535 is decided by that content rule; the comparison is made
without overflowing a `u16`. That is what makes the header boundary and the grid's divider one geometry when the user drags the
sidebar below the controls' real intrinsic width: the effective band moves, the requested width in kv does not.
Absent `sidebar_min` (macOS, an older window) is 0 and changes nothing.

**Alt screen.** A pane whose program uses the alternate screen (`vim`, `less`, …) has no history:
`scrollback_len() == 0` → `total` is `0`, the thumb fills its whole track, and dragging it moves nothing. That is
the expected behaviour, not a failure; a scrollbar is drawn even then (a just-started pane with no history looks
the same).

**Compatibility rule: `0x86` is sent only to a connection whose handshake declared `"scroll":1`; a client that
did not declare it never receives one** (a client that predates this change treats an unknown frame type as a
protocol error and disconnects, §6.2). And a client sends `0x05` only after it has received at least one `0x86`:
the gui page creates no scrollbar element before the first `0x86` (§15.4), so a new `fleetmux gui` against a server
that never sends `0x86` never sends `0x05` — an old server would close on an unknown `0x05` (§6.2). That is a
structural guarantee, not a flag. The client never sends `0x86`, and the server never sends `0x05`; either one
arriving at the other side is a protocol error → close. The protocol version (1) and the view-framing version (1)
are unchanged.

Several viewers see the same picture (same selected tabs, ratios and zoom). PTY sizes follow the viewer that most
recently sent a `0x01` frame (before any input: the one that connected last); larger viewers show blank margins,
smaller ones are cropped (design.md §2.1).

---

## 7. Recording format (M3)

- Enabled by `fleetmux server … --record <dir>`. For every pane the server writes `<dir>/<pane id>.rec`
  (mode 0600), recording the bytes the pane's program wrote to its PTY (output only, never input).
- The file is a sequence of entries with no file header:

```text
+--------------------------+----------------------+-------------+
| offset_us                | length               | bytes       |
| u64 big-endian           | u32 big-endian       | length      |
+--------------------------+----------------------+-------------+
```

- `offset_us`: microseconds since the pane was spawned; non-decreasing within a file. `length`: 1–1 MiB (one entry
  per PTY read; a read larger than 1 MiB is split).
- Reading: EOF exactly at an entry boundary is the normal end; EOF inside an entry is an error (truncated file,
  e.g. the server was killed mid-write) — readers report it after returning the complete entries before it.
- Replay feeds only `bytes`, in order, into a `TermParser`; `offset_us` is for pacing and diagnosis. Byte order
  is big-endian, like the view frames.

### 7.1 Recording fixtures (P-10)

- Fixtures live in `fleetmux/fixtures/recordings/<cli>-<n>.rec`, `<cli>` ∈ `claude` / `codex` / `pi`, `<n>` = 1..3
  (9 files). Each has a sidecar `<cli>-<n>.meta.json`:
  `{"cols": <int>, "rows": <int>, "cli_version": "<string>", "recorded_at": "<RFC 3339>", "redacted": true}`.
- Snapshots: `crates/app/tests/snapshots/replay__<cli>_<n>.snap` (insta), the screen text after replaying the whole
  file into `alacritty::new(cols, rows, 10000)`.
- Only redacted recordings are committed. `scripts/redact_recording.py in.rec out.rec` rewrites the `bytes` of every
  entry, replacing e-mail addresses, absolute paths that start with the user's home directory, and token shapes
  (`sk-…`, `ghp_…`, runs of ≥ 32 hex digits) by placeholders of **the same length**, so every entry length and every
  byte offset is unchanged.

---

## 8. Runtime files

All in `<runtime>/` (design.md §2.2), owned by the server's uid, mode 0600, directory 0700. JSON files are
written atomically (write `<name>.tmp` in the same directory, `fsync`, `rename`). Readers tolerate a missing file
(treated as "no server" / "no panes") and unknown extra keys.

### 8.1 `server.json`

Written once, right after the socket is bound; removed on clean shutdown.

| Key | Type | Meaning |
|---|---|---|
| `pid` | int | server pid |
| `start_time` | int | the server process's start time as the OS records it, normalised to integer Unix seconds (§8.6) |
| `version` | string | program semver |
| `protocol` | int | §2 |
| `epoch` | string | §1.4 |
| `pages` | int | M4: `1` — this server has fleet pages (§2 M4 addendum); absent from an older server |

```json
{"pid":4242,"start_time":1793178806,"version":"0.1.0","protocol":1,"epoch":"k7q2","pages":1}
```

Signal fallback (design.md §2.3): when the socket does not answer but `pid` is alive, a client may signal it only
if the process's command line contains `--runtime <this runtime dir>` **and** its start time, read from the OS now
and normalised per §8.6, is exactly equal to `start_time`.

### 8.2 `panes.json`

Rewritten after every spawn, exit, kill, move or rename; used by `fleetmux reap` and post-mortems.

```json
{"epoch":"k7q2","panes":[{"pane":"k7q2-12","pid":5151,"pgid":5151,"start_time":1793178900,"tty":"/dev/ttys012","group":"workers","index":0,"title":"fx-worker-1","type":"worker"}]}
```

M4: a pane outside the root page also carries `"page":"<page id>"` (§4.26), and its `group` / `index` are its
place in that page's layout; a root-page pane has no `page` key, so a tree without child pages writes the M3 bytes.

`start_time` is the pane child's start time, normalised per §8.6. `reap` signals a recorded process group only if
`pid` is alive and its start time, read from the OS now, is exactly equal to `start_time` (pid-reuse guard); if it
cannot be read, that group is not signalled and `reap` reports it.

### 8.3 `layout.json`

The layout object of §4.14, plus `"version": 1`. Rewritten on every change of ratios, zoom, focus, selection or
tab order. A new server reuses only `h_ratio` / `v_ratio` from it; group lists, selection and zoom name panes of
an earlier epoch and are discarded. A file with a ratio outside 10–90 or the wrong types is ignored with a `WARN`
(defaults used), never a startup failure. The JSON shape is produced and parsed by `fleetmux-layout`
(`Layout::to_json` / `from_json`).

### 8.4 `start.lock`

An empty file used only as an `flock(LOCK_EX)` target by whoever starts a server (design.md §2.3: take lock →
`hello` again → clear a dead socket → start → wait for `hello` up to 10 s). Never deleted (deleting a lock file
races with the next locker). Its content is meaningless.

### 8.5 `server.log`

Append-only UTF-8 text, one event per line: `<RFC 3339 UTC timestamp> <LEVEL> <message>`, `LEVEL` one of `INFO`,
`WARN`, `ERROR`. The `--daemonize` path redirects the server's stdout / stderr here as well, so startup failures
(socket too long, bad permissions, socket already answering) end up in it; the Python backend puts its tail into
`last_error` when a start fails.

### 8.6 Process start time

`server.json` and `panes.json` carry `start_time` so that a pid can be told apart from a later process that
reused it. One definition, used by every writer and every checker (server, `fleetmux reap`, Python backend,
`tests/fake_fleetmux.py`):

- **Value**: the start time the OS recorded for the process, as an integer number of Unix seconds (rounded down).
  - macOS: `kinfo_proc.kp_proc.p_starttime.tv_sec` (`sysctl` `KERN_PROC_PID`); `ps -ww -o lstart= -p <pid>` shows the
    same second in local time.
  - Linux: ⌊`btime` + `starttime` ÷ `CLK_TCK`⌋, with `btime` from `/proc/stat`, `starttime` = field 22 of
    `/proc/<pid>/stat` (counted after the `)` that ends the command name) and `CLK_TCK` = `sysconf(_SC_CLK_TCK)`.
- **Written from the OS, never from a clock**: the server reads its own start time, and each pane child's start
  time after the child exists, by the rule above. The current time sampled around `fork` / `spawn` is never written
  in its place.
- **Compared exactly**: a process matches only if its start time read now equals the recorded `start_time` as
  integers. There is no tolerance.
- **Unverifiable means no**: if the start time cannot be read (process gone, `/proc` or `ps` unreadable, output
  that does not parse), the identity is unverified and no signal is sent — the signal fallback (§8.1) and `reap`
  (§8.2) both refuse and report failure instead.

### 8.7 logind `KillUserProcesses` (P-11)

- On Linux only, at startup the server reads `$FLEETMUX_LOGIND_CONF` (default `/etc/systemd/logind.conf`). If it has
  an uncommented line `KillUserProcesses=yes`, the server writes one line to `server.log`:
  ``WARN logind KillUserProcesses=yes: run `loginctl enable-linger` or the server dies with your ssh session``
  and prints the same sentence to stderr. It then starts normally.
- A missing or unreadable file, a commented line, or any other value: no warning. The server never changes any
  system setting (design.md §5).

---

## 9. Command line (P-4)

`fleetmux --version` is unchanged (§2). Every subcommand exits **2** on a usage error (unknown flag, missing
required flag, malformed value) after printing a usage line to stderr.

Any other first argument, and `gui` without `--runtime`, is a fleet command: the binary `exec`s the Python fleet CLI
of the same release with all its arguments (argv, stdio and exit code are that CLI's; exit 127 when none is found).
Lookup order and the `gui` split: docs/design.md §50. No arguments stays a usage error (exit 2); `--help` / `-h`
prints the usage and a one-line hint to stdout (exit 0).

### 9.1 `fleetmux server`

```text
fleetmux server --runtime D --session S [--daemonize] [--default-size COLSxROWS] [--record DIR]
                [--scrollback N] [--prefix KEY] [--theme FILE]
```

| Flag | Default | Meaning |
|---|---|---|
| `--runtime D` | required | runtime directory (§8) |
| `--session S` | required | session name (informational; the path is already derived from it) |
| `--daemonize` | off | fork twice, `setsid`, stdin from `/dev/null`, stdout / stderr into `server.log` (design.md §2.3) |
| `--default-size COLSxROWS` | `240x60` | virtual screen used to size PTYs while no viewer is connected (design.md §2.5) |
| `--record DIR` | off | write `<DIR>/<pane id>.rec` per pane (§7) |
| `--scrollback N` | `10000` | scrollback rows kept per pane |
| `--prefix KEY` | `C-b` | prefix key name (§10), reported by `hello.prefix` (§4.1) |
| `--theme FILE` | the cmux palette below | palette for the OSC 10 / 11 / 12 / 4 answers and `hello.theme` (§4.1); a bad file is a usage error (exit 2) |

| Exit | Meaning |
|---|---|
| 0 | normal exit (`server.shutdown`) |
| 2 | usage error |
| 3 | the socket already answers (another server is live; design.md §5) |
| 4 | the socket path is too long (§1.1) |
| 5 | runtime directory permissions or owner are wrong and cannot be fixed (design.md §5) |

Exits 3, 4 and 5 each write one `ERROR` line to `server.log` (§8.5) saying why.

**`--theme FILE` (M2.6).** A JSON object; all four keys are optional and a missing one keeps the cmux default
below. Colours are `#rrggbb` in either case (stored and reported lowercase); `ansi` is exactly 16 colours in index
order. An unknown key, a bad colour, an `ansi` of another length, a file that cannot be read or that is not a JSON
object is a usage error (exit 2, as for any malformed value) — never swallowed. The palette feeds the three places
that must agree: the OSC 10 (foreground) / 11 (background) / 12 (cursor) / `4;N` (ANSI colour N) answers a program
inside a pane receives, `hello.theme` (§4.1), and — through `fleetmux gui` — the window's xterm.js theme and page
background (§9.5, §15.4). The 256-colour cube and greyscale are computed as xterm does; a colour the program itself
set with OSC 4 / 10 / 11 still wins.

The default (cmux 0.65.0, measured with those OSC queries; one copy per language: `Palette::CMUX` in the Rust
server, `CMUX_THEME` in the Python protocol fake):

| Key | Value |
|---|---|
| `bg` | `#1e1e1e` |
| `fg` | `#ffffff` |
| `cursor` | `#98989d` |
| `ansi[0]`–`ansi[15]` | `#1a1a1a` `#cc372e` `#26a439` `#cdac08` `#0869cb` `#9647bf` `#479ec2` `#98989d` `#464646` `#ff453a` `#32d74b` `#ffd60a` `#0a84ff` `#bf5af2` `#76d6ff` `#ffffff` |

**Known limitation (`attach`).** The OSC answers come from the server's palette, never from the terminal the
viewer attached from: `fleetmux attach` leaves `Color::Default` to the outer terminal (its canvas is the outer
terminal, not a palette of its own), so in a non-cmux
terminal (or over SSH) a program inside a pane "believes" its background is `#1e1e1e` while the outer terminal
paints its own default. Accepted: cmux and `fleetmux gui` agree, which is the main use; translating `Default` to
RGB in `attach` would break ssh / non-cmux terminals and need a background for the border and status bar.

### 9.2 `fleetmux attach`

```text
fleetmux attach --runtime D [--no-mouse]
```

Opens a view connection (§6) to `<D>/ctl.sock`, puts the outer terminal in raw mode and, unless `--no-mouse`,
turns on outer mouse modes 1002 + 1006 (§11). Exit 0: the user detached (§10 `d`) or the server exited (`0x82`).
Exit 2: usage error. Exit 3: cannot connect — stderr gets the last 20 lines of `<D>/server.log` (or a line saying it
is missing).

**Identity:** the client goes on only when the kernel peer uid of `<D>/ctl.sock` equals its own
effective uid, and refuses (exit 3) when the credentials cannot be read; it does not compare `hello.uid` (§3 client
side, §4.1).

**From another host (design.md §3.5.3).** `D` only has to hold a `ctl.sock` that reaches the server, so
an SSH unix-socket forward is enough. On the machine the person sits at (`<…>` are placeholders):

```text
mkdir -m 0700 <L>                    # a new, short, private directory each time; it must not exist yet
ssh -N -o ExitOnForwardFailure=yes -o StreamLocalBindUnlink=no -L <L>/ctl.sock:<R>/ctl.sock <user>@<host>
fleetmux attach --runtime <L>        # in a second terminal, with the same <L>
```

`StreamLocalBindUnlink=no`: `ssh` never removes an existing `<L>/ctl.sock`; when the directory or the socket is
already there, `mkdir` / `ssh` fail and the person picks a new `<L>`. Nothing is overwritten or cleaned up.

`<R>` is the server's runtime directory on `<host>` (absolute, §1.1), `<L>` a local directory. Everything the client
needs goes over that one socket: the control `hello`, the view connection, `0x84` / `0x04` including the pushed page
(§6.3, §4.21). The web window, `web.sock` (§15) and the clipboard are local, so nothing else is forwarded and no TCP
port is opened. Constraints: `<L>/ctl.sock` must fit the platform limit (§1.1); the last component of `<L>` is the
token of the local web runtime directory (§15.2); `<user>` must be the uid the server runs as (the server's §3 check
sees `sshd` connecting as the login user); `<D>/server.log` (exit 3) is the local `<L>/server.log`, which does not
exist, so read the server's log on `<host>`. `ssh <host> fleetmux attach --runtime <R>` (with `-t`) also works for the
terminal, but its client runs on `<host>`, so a web window can only open there.

### 9.3 `fleetmux reap`

```text
fleetmux reap --runtime D
```

Reads `<D>/panes.json` (§8.2) and signals every recorded process group whose identity checks out (§8.6). Prints
exactly one JSON line to stdout:

```json
{"reaped":[5151,5160],"skipped":[{"pane":"k7q2-14","pid":5170,"reason":"start_time_mismatch"}]}
```

`reaped`: pgids signalled. `skipped`: one object per recorded pane not signalled, `reason` one of
`start_time_mismatch` (the pid now belongs to another process), `unverifiable` (start time could not be read),
`gone` (no such process).

| Exit | Meaning |
|---|---|
| 0 | no `unverifiable` entry (`start_time_mismatch` and `gone` are expected outcomes) |
| 1 | at least one `unverifiable` entry, or sending a signal failed |
| 2 | usage error |
| 3 | the runtime directory or `panes.json` cannot be read |

### 9.4 `fleetmux web` (M2.5)

```text
fleetmux web [ARGS…]
fleetmux web --ctl <web runtime dir> <open URL | goto URL | url | reload | quit>
fleetmux web --version
```

Finds the `fleetmux-web` binary — the directory of the running `fleetmux` first, then `PATH`, then
`$CMUX_FLEET_FLEETMUX_WEB` — and `exec`s it with `ARGS` unchanged. `--ctl D CMD` is for manual debugging: it sends
one `web.sock` command (§15) to `<D>/web.sock`, prints the reply line and exits 0 when `ok` is true, 1 otherwise.
`--version` prints `fleetmux-web`'s own version line. `fleetmux-web --version` prints
`fleetmux-web <x.y.z> (protocol 1)`, the same version as `fleetmux`; `fleetmux web` refuses to start a binary whose
version differs (exit 1, both versions on stderr).

| Exit | Meaning |
|---|---|
| 0 | `--ctl` command answered `ok` / `--version` printed (otherwise the exit is `fleetmux-web`'s own) |
| 1 | `--ctl` command failed or the window did not answer; version mismatch |
| 2 | usage error |
| 3 | `fleetmux-web` not found (stderr names the three places looked in) |

### 9.5 `fleetmux gui` (M2.6)

```text
fleetmux gui --runtime D [--devtools]
```

The graphical viewer (design.md §3.6): a native window (`fleetmux-web --gui`, §15.4) whose full-window terminal shows
the same composed screen as `fleetmux attach`, with the history page laid over the lower-left cell (`0x85`, §6.3).
Order: the control `hello` with the same identity check as §9.2 (kernel peer uid of `<D>/ctl.sock`) and
`kv.get history_url` → the window check (graphical session, `fleetmux-web` found in §9.4 order with the same
version) → the web runtime directory (§15.2) → start `fleetmux-web --gui --dir … [--run-dir …] --prefix …
[--theme JSON] [--devtools]` (never `--test-hooks`) with two pipes → wait for `web.sock` and the window's first
`0x02` (3 s each) → open the view connection with `"overlay":1` (§6.1), `term` `xterm-256color`, `colorterm`
`truecolor`, at the size of that first `0x02`. From then on it relays: `0x81` / `0x85` from the server into the
window pipe as they are; `0x01` / `0x02` from the window to the server (prefix + `d` detaches, §10); `0x84` /
`0x04` as `fleetmux attach` (§6.3; `web.*` act on the embedded page).
**When the window counts as open:** the client reports `{"event":"window","state":"open"}` (`0x04`, §6.3) on the
view connection once the first load of the `history_url` carried by the first `0x85` has finished (its page
exchange is no longer pending, whether the load succeeded or not), so a `web.goto` from fleet is never overwritten
by that initial load. It reports it at once when that `0x85` has no `file://` `url` to load, and when no `0x85`
comes after the first screen (a server older than M2.6: no overlay, the one-time notice of design.md §3.6.8).
Until it is reported, the server counts this viewer as having no window, as for any client without one
(`web.goto` / `web.url` answer `no_window` when no other viewer has a window, §4.19–§4.20). A `0x85` whose `pane`
is `null` (no history pane) shows no overlay; the terminal screen is unchanged. Prefix `o` (`0x83`):
focus the history pane (`pane.focus`) and the embedded page (`web.sock` `focus`) when there is one, else the §6.3
fallbacks. Cross-host: as §9.2, with `fleetmux gui --runtime <L>`.

**Scrollbars (M2.6, gui-scrollbar).** The view handshake also says `"scroll":1` (§6.1), so the server sends `0x86`
(§6.3) for the window's cells. The client relays `0x86` from the server into the window pipe as it is, like `0x85`,
and relays a `0x05` coming back from the window to the server unchanged (it neither parses it nor remembers any
state). The window draws one scrollbar per cell and sends `0x05` only after the first `0x86` (§15.4); a drag
changes the same `display_offset` the wheel and page mode use, so the screen and the `0x86` that follows stay in
step. A `fleetmux gui` older than this change sends `"overlay":1` without `"scroll":1` and gets no `0x86`; a server
older than it never sends one, and then the page shows no scrollbar at all.

**Theme (M2.6).** The `hello` reply's optional `theme` (§4.1) is handed to the window verbatim as `--theme JSON`
(§15.4), which is how the terminal cell's background / foreground / cursor / 16 ANSI colours and the page background
match the server's OSC answers (§9.1). A server that sends no `theme` (an older one): no `--theme`, the window keeps
xterm.js's own colours. `--theme` is not a `fleetmux gui` flag: the palette belongs to the server, so every viewer of
one server shows the same colours.

| Exit | Meaning |
|---|---|
| 0 | the user detached (prefix + `d`), the person closed the window, or the server exited (`0x82`) |
| 2 | usage error |
| 3 | cannot connect (as §9.2) |
| 4 | cannot open the window: no graphical session, `fleetmux-web` missing or of another version, WebKitGTK missing, web runtime path too long, or the window not ready within 3 s |

On exit 4 stderr gets one line with the reason (for a window that failed to start: the tail of its stderr /
`web.log`, e.g. `libwebkit2gtk-4.1.so.0: cannot open shared object file`) and the hint
`use: fleetmux attach --runtime D`. `fleetmux gui` never falls back to a terminal attach by itself. Every way out
stops the window first (`quit`, close its stdin, wait); no window outlives its client.

---

## 10. Keys (P-5)

The server interprets keys; `fleetmux attach` only forwards the outer terminal's bytes as `0x01` frames.

- **Prefix**: `C-b` (byte 0x02) by default, `--prefix` to change (§9.1). After the prefix the server waits for one
  more key; if none comes within **1 s** the prefix is cancelled and nothing is written.
- Pressing the prefix twice writes the prefix byte itself to the focused pane.

| After the prefix | Action |
|---|---|
| `1` … `9` | select the n-th tab of the focused group |
| `Tab` / `BTab` (Shift-Tab) | next / previous tab of the focused group |
| `Left` / `Right` | move focus between the left column and `workers` |
| `Up` / `Down` | move focus between `supervisor` and `lower` |
| `z` | toggle zoom of the focused pane |
| `[` | enter page mode (§13) |
| `<` / `>` | `h_ratio` −5 / +5 (clamped 10–90) |
| `+` / `-` | `v_ratio` +5 / −5 (clamped 10–90) |
| `=` | reset ratios to 50 / 60 |
| `o` | send `0x83` to this viewer (§6.3): payload = kv `history_url` without `file://`, empty if unset (M4: the current page's `history_url`) |
| `s` | toggle the fleet sidebar: only while kv `sidebar` is set (§4.2); the shown/hidden flag is shared by every viewer and not saved in `layout.json`. Hiding folds the cards away only: the sidebar's width becomes 0, the panes fill the screen from column 0, and row 0's topbar left part stays at the toolbar's own `inset + 15` columns, so `◫ + ↺ ← →` keep their columns and nothing is drawn below row 0. Without the kv key there is no topbar and row 0 is the tab row again |
| `r` | restore the newest closed fleet: the same as clicking the topbar's `↺`, it spawns the first kv `restorable` run (§4.2) as a pane titled `resume`; with nothing to restore it writes the status-line notice `没有可恢复的 fleet` ("no restorable fleet"), and while the sidebar still lists a `running` / `paused` / `stopping` card it only writes `先关闭正在运行的 fleet 再恢复` ("close the running fleet before restoring"; one session holds one fleet) |
| `(` / `)` | step the sidebar's selection back / forward — the same as clicking `←` / `→` on its toolbar. With one fleet both arrows are grey and nothing happens. M4: a step onto a card that carries `page` switches to that fleet page (§11); a card without `page` only writes `切换 fleet：S3 开放` ("switch fleet: opens in S3") as before |
| `X` | close this fleet: open the close confirmation box, the same as clicking the `×` on the card; `y` confirms (kv `stop_request`, §4.2), any other key cancels |
| `d` | detach: the client sends `0x03` and exits 0; panes keep running |
| the prefix again | write the prefix byte to the focused pane |

**Topbar (S1f).** While kv `sidebar` is set, row 0 is a topbar across the whole viewer, split in two at the
sidebar's drag line (§11). Its left part is `max(sidebar width, inset + 15)` columns while the sidebar is shown and
`inset + 15` while it is hidden; it holds the toolbar `◫  +  ↺  ←  →` at `inset + 1 / 4 / 7 / 10 / 13` (`inset`
is the gui window-control room, §6.3), each button a three-column click area. The `│` divider sits at the sidebar's
edge (column `sidebar width`, only while the sidebar is drawn); the right part starts two columns after it and
shows the current fleet's `title` (bold white) ` · ` its `line1` (grey), cut with `…` at the last column before the
right edge. With a sidebar the groups' border and tab-label row is row 1 and their panes start on row 2, where the
cards still start. The topbar is derived runtime state: no new frame, handshake key or kv key, and `layout.get` /
`layout.json` do not carry it.

**Content mirror (Linux only).** The three groups are placed by `Layout::rects()` and the *platform*
decides which side each takes:

- macOS (default): fleet sidebar **left**, `supervisor` above `lower` in the middle column, `workers` **right**.
- Linux: `workers` **left**, `supervisor` above `lower` in the middle column, fleet sidebar **right**. The
  stack keeps its `h_ratio` share of the content width, measured from the right edge, and the vertical separator
  drag is measured the same way (`ratio_from_x_mirrored`), so a drag never jumps when the mirror flips.

The mirror is a property of `Layout` (`set_mirror`), applied by `rects()`, so drawing, mouse hit testing (panes,
tabs, separators, the sidebar band and its drag line), both ratio drags, the sidebar-width drag, the zoom
rectangle, the `0x85` history overlay, the `0x86` scrollbars, the PTY sizes and the pane restoration all follow it;
row 0 (the topbar) and the status row do not move, and the toolbar stays on the left with the fleet title/status on
the right. It is derived at server boot from the platform (`FLEETMUX_LAYOUT_MIRROR=1|0` overrides it for tests) and
is **not** in `layout.json`: a resumed server sets it again. The sidebar band is drawn into its own width-wide
buffer and copied to its columns, so every card position stays relative to the sidebar's width.

Ratio, zoom, focus and selection changes are shared by every viewer (§14) and rewrite `layout.json` (§8.3).

---

## 11. Mouse (P-6)

- `fleetmux attach` turns on outer mouse modes **1002** (button-event tracking) and **1006** (SGR encoding) unless
  `--no-mouse`; the server only parses SGR mouse reports (`ESC [ < b ; x ; y M|m`). Hit testing uses
  `fleetmux_layout::Layout::hit_test`.
- Left click on a tab → select that tab and focus its group. Left click inside a group's pane → focus that group.
- Left click in the fleet sidebar (only while kv `sidebar` is set, §4.2). Row 0 is a two-segment topbar across
  the viewer, split at the sidebar's drag line (`Layout::topbar_left`). Its left part is the toolbar
  `◫  +  ↺  ←  →` at `k+1 / k+4 / k+7 / k+10 / k+13` (`k` = the viewer's `inset`, §6.3), with three-column click
  areas: `◫` `k..=k+2`, `+` `k+3..=k+5`, `↺` `k+6..=k+8`, `←` `k+9..=k+11`, `→` `k+12..=k+14`. `◫` folds the sidebar
  away — width 0, the panes fill the screen from column 0 — and a click on it again, or `C-b s`, shows it at the
  width it had; the buttons keep these columns in both states. `+` opens a bash, as `C-b c` does (below); `↺` restores the newest closed fleet — the same
  as `C-b r` (§10): it spawns the first kv `restorable` run's `argv` in its `cwd` with its `env` as a pane titled
  `resume`, zooms it and writes `正在恢复…` ("restoring…") on the status line; with nothing to restore it writes
  `没有可恢复的 fleet` ("no restorable fleet"), and while a `running` / `paused` / `stopping` card is listed it writes
  `先关闭正在运行的 fleet 再恢复` ("close the running fleet before restoring") and spawns nothing (one session holds one fleet); `←` / `→` step the card selection and, when there is a card before / after
  the current one, select it like a click on that card (below; a card without `page` only writes the notice
  `切换 fleet：S3 开放` ("switch fleet: opens in S3"); grey arrows do nothing). The right part, from two columns
  after the divider to the last column before the right edge, shows the current fleet's `title` + ` · ` + `line1`,
  cut with `…`; a press there is `Outside` — the server does nothing and, in the gui, a title cell is not a
  window-drag cell either. With a sidebar the groups' border and tab-label row is row 1 and their panes start on
  row 2, where the cards still start; without the kv key row 0 stays the tab row. A click on a card (M4, sidebar
  S3) whose kv `sidebar` entry carries the optional `page` key switches every viewer to that fleet page, exactly
  as `page.select` (§4.25; the web windows get `0x84 goto` to its `history_url`); a `page` the server does not
  have only writes `这个 fleet 没有画面可看` ("this fleet has nothing to show"). With cards that carry `page`, the highlighted card and the topbar's
  right part are the card of the **current page**, whatever kv `current` names. A card without `page` keeps the
  M3 behaviour: a click on the current fleet's card shows its normal grid, another card writes
  `切换 fleet：S3 开放` ("switch fleet: opens in S3"). Each bash card's shell is a tab of the root page's `workers` group (every new bash is
  mapped into the main fleet's worker area): `+`, the blank double click and `C-b c` add it last in
  that group, and opening one or picking its card shows the root page, cancels a zoom of another pane, selects the
  tab and focuses its group. A bash card is the selected card while the root page's zoomed pane — else its focused
  group's selected tab — is that bash; the root fleet's card then focuses the `supervisor` group and cancels a
  zoom, the bash staying a tab of the worker area. While a bash card is selected the topbar (and the Linux
  headerbar) reads `bash-<n> · 终端 · 运行中` ("bash-<n> · terminal · running"). A shell that is gone takes its tab and its card with it. A current page that is a reclaimed child fleet (`page.close {keep:true}` /
  `page.open {reclaimed:true}`) is a read-only record: its empty supervisor and worker groups show
  `子 fleet <title> 已收回 · 只读` ("sub-fleet <title> reclaimed · read-only"; White, bold), `下面是它的运行记录；点侧栏别的卡片切回` ("below is its run record; click another sidebar card to switch back"; Gray) and
  `同名再起时这一页会重新启用` ("this page is reused when a fleet of the same name starts again"; DarkGray); a child page's title also goes on the status line as `page: <title>`
  right before `focus:`. A click on a card's `×` —
  drawn on its first row, columns 30–32, only for a `running` / `paused` fleet that has no stop request yet, and
  (M4) never for a child card that carries a non-empty `page` (child fleets start and stop through their roster
  and `subfleet.up` / `down`) —
  opens the close confirmation box (§4.2 `stop_request`); while the box is open a press on `[ 关闭 fleet ]` ("close fleet") confirms
  and any other press cancels. A `closed` card's first row draws `↺` at column 31 (columns 30–32 click) instead of
  the `×` and restores that card's run by its `id` in kv `restorable`; a closed fleet's cards keep the grey dot and
  the `已关闭 · 可恢复` ("closed · restorable") line (a run a lifecycle timeout stopped says `到点停 · 可恢复` ("stopped on timeout · restorable"); a run stopped otherwise while a
  busy bash page kept its server reads `state` `stopped` / `已停止` ("stopped"): grey, neither `×` nor `↺`, not live), and while the fleet has no
  pane (bash tabs do not count) the pane area shows the three-line closed-fleet
  placeholder (White title, Gray restore hint, DarkGray exit hint), except while a bash card is selected. A sidebar that holds only grey cards — the
  history list `./start.sh gui` writes — has no closed fleet of its own: with no pane and no `running` / `paused` /
  `stopping` card its pane area shows `没有在跑的 fleet；点侧栏卡片上的 ↺ 恢复` ("no fleet running; click ↺ on a sidebar card to restore"; White, then the DarkGray exit hint)
  instead, and such a server exits 10 s after the last pane and viewer like a closed one; a
  server without kv `sidebar` never exits by itself.
- Double click on a tab (two clicks within **300 ms**) → toggle zoom of that pane.
- Drag the vertical separator → `h_ratio = ratio_from_x(cols, x)`; drag the left column's horizontal separator →
  `v_ratio = ratio_from_y(left_rows, y)`. Both clamp to 10–90; `layout.json` is written when the button is released.
- Drag the sidebar's inner edge → resize the sidebar. The edge is the gap column plus the content's border column
  next to it on any row above the status line, while the sidebar is shown and at least 2 columns wide: columns
  `{width−1, width}` on macOS (the left band) and `{columns−width−1, columns−width}` on Linux (the right
  band). On row 0 those two cells are the topbar's `│` divider and the blank column beside it, so the two
  segments split exactly where the drag happens and the divider resizes the sidebar like the border below;
  hidden (width 0) there is no edge to drag, the topbar's left part is only the toolbar. A press there starts the
  drag, every move sets the width to the dragged distance from the sidebar's own side of the window (the column
  itself on macOS, `columns − column` on Linux) clamped to `24..=min(60, columns − 60)`, and the release saves it
  to kv `sidebar_width` (§4.2). A window whose maximum is below 24 columns cannot drag at all. While the drag is held, that border column — row 1 (the groups' first row) down to the row above the status
  line, which is never painted — is `Yellow` bold; row 0's divider is not repainted during the drag, it keeps its
  `Gray` (S1f). The grid, the pane PTY sizes, the `0x85` history rectangle and the `0x86` scrollbar follow
  automatically because they all read `Layout::rects()`. The gui cannot drag it: it draws one xterm.js canvas and
  the page does not know which column is the border, so it shows no resize cursor there either.
- Wheel over a pane: if the inner program has mouse reporting on (`modes.mouse`), the event is re-encoded for it
  (`encode_mouse`, coordinates relative to the pane's origin) and written to the pane; otherwise the wheel moves
  that pane's view `display_offset` by ±3 rows (an implicit page view; any input written to that pane first resets
  the offset to 0).
- Wheel over the fleet sidebar's card rows (from the first card's row down to the row above the status line, the
  gap column included; Linux: the right band) scrolls the card list, like cmux: one card per wheel step, stopping at
  the first and the last card. Only whole cards are drawn and nothing counts the rest (there is no `… 还有 N 个` ("… N more")
  line). The scroll offset is server state shared by every viewer, like the card selection and shown/hidden — not
  per connection, not in `layout.json`. While the cards overflow, a grey `┃` thumb sits in the band's column next
  to the content (the gap column `width − 1`; Linux: the band's first column), over frameless content on the rows
  of the `│` divider it replaces; its height is visible / total cards of the rows from the first card's first row
  to the last drawn card's last row (at least one row) and its place follows the offset. A click on a card or its
  `×` acts on the card drawn under the pointer. A changed selection — a click, `←` / `→`, `C-b (` / `C-b )`, a new
  bash page — or a card that comes or goes scrolls the selected card into view; a taller window clamps the offset
  so no card slot is left blank.
- Left button inside a pane whose inner program has mouse reporting **off** selects text in that pane's view
  (whatever its `display_offset`): a press starts a selection, every move while the button is held extends it,
  and the release copies it (§12). A second press on the same cell within **300 ms** selects the word under it, a
  third press on the same row within 300 ms of the second selects the whole line; both copy at once, and the press
  after a triple click starts over. A word is the longest run of cells around the pressed one holding neither
  whitespace nor one of `'` `"` `│` `` ` `` `|` `:` `;` `,` `(` `)` `[` `]` `{` `}` `<` `>` `$` (Ghostty's default `selection-word-chars`, as in
  cmux), crossing soft-wrapped row ends; a press on such a character or a blank selects nothing. A line is the
  logical line: soft-wrapped rows (the inner program's text ran past the right edge) are followed up and down
  within the view. The selection is drawn reverse-video on the selecting viewer's canvas only and stays after the
  copy, until that viewer's next mouse press or key (typed, pasted or prefix key), until the pane's view is scrolled
  to another offset, or until the pane closes. With the inner program's mouse reporting on, all of this goes to the
  program instead (re-encoded as above).
- Any mouse event with **Shift** held is neither handled nor forwarded by the server (the outer terminal's own
  selection takes it).

---

## 12. Copy (P-7)

- When the inner program's mouse reporting is off, a left-button drag, double click or triple click inside a pane
  selects text (§11) and copies it at once: on release (drag) or on the press (double / triple click) the server
  takes `TermParser::selection_text(from, to)` and sends a `0x81` frame with
  `ESC ] 52 ; c ; <base64 of the text> BEL` to **every** view connection. Each row's trailing whitespace is
  stripped (a blank row copies as an empty line); rows are joined with `\n`, except that a soft-wrapped row runs on
  into the next one with no `\n`. A click without a drag copies nothing.
- The gui page (`fleetmux gui`) also copies a selection xterm.js makes itself (Option-click forced selection,
  including its own double-click word select) on mouseup; Cmd-C / Ctrl-Shift-C copies it as before.
- The server also copies on its own host: macOS → `pbcopy`; else with `WAYLAND_DISPLAY` set → `wl-copy`; else with
  `DISPLAY` set → `xclip -selection clipboard`; none of these → skip and log one `INFO` line. If the tool fails,
  one `WARN` line is logged; copying never fails the selection.

---

## 13. Page mode (P-8)

- Entered with prefix `[` on the focused pane. While in page mode, keys go to page mode, never to the pane:

| Key | Action |
|---|---|
| `PgUp` / `PgDn` | offset ± (rows − 1) |
| `Up` / `Down` | offset ± 1 |
| `/` | start typing a search string on the status line; `Enter` jumps to the nearest match above (`search_up`) |
| `n` / `N` | next match further up / next match further down |
| `q` / `Esc` | leave page mode; offset back to 0 |

- The status line shows `[offset/scrollback]` while in page mode. Every other key is ignored (not written to the
  pane). The offset is clamped to the pane's scrollback length.

---

## 14. Viewers and colour depth (P-9)

- Every view connection sees the same selection, ratios and zoom. PTY sizes follow the viewer that most recently
  sent `0x01` (§6.3).
- A tab whose pane has `unseen` set (§4.0) shows `•` before its title.
- Colour depth per viewer, from the handshake (§6.1): `colorterm` ∈ {`truecolor`, `24bit`} → RGB colours are sent
  as they are; anything else → RGB is downgraded with `fleetmux_term::color::to_256`.
- Each view connection has its own canvas and its own diff baseline (what was last sent to it).

---

## 15. `web.sock` and the web window (M2.5)

`fleetmux-web` (crate `crates/web`, package `fleetmux-web`) is one native window per process: tao event loop on the
main thread + wry WebView (macOS WKWebView, Linux WebKitGTK; design.md §3.5.2). The attach client that starts a
window owns it; nothing else ever connects to it (the fleet daemon never does: it may be on another machine).

### 15.1 Start, lifeline, exit

```text
fleetmux-web --dir <web runtime dir> --run-dir <run dir> [--devtools]
fleetmux-web --version
```

- **Started by** the attach client (§6.3 `0x83` / `0x84 open`) when the client's host has a graphical session
  (macOS always; Linux when `DISPLAY` or `WAYLAND_DISPLAY` is set), the binary is found (§9.4 order) and its
  `--version` equals the client's. Otherwise the client falls back (design.md §3.5.6) and says why on its own status
  line only (never in `server.log`).
- **Lifeline:** the window's **stdin** is a pipe from the client. EOF on stdin (client exit, ssh drop, `C-b d`)
  → the window exits within **1 s**. A `quit` command (§15.3) exits at once. When the person closes the window,
  `fleetmux-web` writes `{"event":"window","state":"closed"}` to every connected `web.sock` client, then exits.
  The window is never left running without its client: no launchd / systemd / cron.
- **Readiness:** the window is ready when `<dir>/web.sock` accepts a connection. A client gives up after 3 s
  (falls back, design.md §3.5.6).
- `--devtools` turns on the WebView developer tools (default off). `--version` prints
  `fleetmux-web <x.y.z> (protocol 1)` and exits 0. A usage error exits 2.

### 15.2 Directories and permissions

- **Web runtime directory** (`--dir`): `<root>/fleetmux-web/<first 24 characters of the run token>-<client pid>/`,
  `<root>` = `$XDG_RUNTIME_DIR`, or `~/.local/state` when it is unset. Mode **0700**, owned by the user; created by
  the client before it starts the window (`fleetmux-web` checks it, `sock::check_dir`, and refuses a directory with
  another owner or group / other bits set). It holds `web.sock`, `page/history.html` (a page pushed from another
  host, mode **0600**) and `web.log`.
- **`web.sock`**: mode **0600**; every accepted connection's peer uid must equal the window's own uid (§3), else it
  is closed without a reply. A socket path over the platform limit (103 bytes on macOS, 107 on Linux, §1.1) is a
  startup failure (exit 1), never a truncated path.
- **Navigation allow-list** (`policy::allow`): `about:blank`, and `file://` URLs under the run directory
  (`--run-dir`) or under `<dir>/page/`, load in the window — query string and fragment allowed (e.g.
  `?fleet-jump=…`, which is recognised and loads like any other allowed URL — except, M4, in a window whose `open`
  carried `"pages":1` (§15.3): there it is not loaded but reported as `{"event":"jump","url"}`). `http://` / `https://`
  never load in the window: they go to the system opener (`open` / `xdg-open`). Everything else is refused.
- **Data directory** (login state, design.md §3.5.7): Linux `${XDG_DATA_HOME:-~/.local/share}/fleetmux-web/<token>/`
  (0700, no pid, survives re-attach); macOS a data-store identifier kept in that directory's `datastore.uuid`
  (macOS 14+; older versions use the default store).

### 15.3 Commands (one JSON line each way)

Request lines go client → window; each gets exactly one reply line, in order.

| Request | Reply | Effect |
|---|---|---|
| `{"cmd":"open","url":U}` | `{"ok":true}` | load `U`, bring the window to the front. M4: optional `"pages":1` — the client's server said `"pages":1` on its view `ok` line (§6.1); from then on the window stops every `?fleet-jump=` navigation and sends `{"event":"jump","url":U'}` instead of loading it (absent: such a link loads as before) |
| `{"cmd":"goto","url":U}` | `{"ok":true}` | load `U` |
| `{"cmd":"url"}` | `{"ok":true,"url":U}` | the WebView's current URL |
| `{"cmd":"reload"}` | `{"ok":true}` | reload the current page |
| `{"cmd":"quit"}` | `{"ok":true}` | reply, then exit |

A URL the allow-list refuses, an unknown `cmd`, or a line that is not a JSON object gets
`{"ok":false,"error":"<English sentence>"}` and changes nothing. `eval` is reserved for a later version (design.md
§3.5.11) and is answered like an unknown `cmd`. Unsolicited lines from the window: `{"event":"window","state":"closed"}`
(§15.1) and, M4, `{"event":"jump","url":U}` (a stopped `?fleet-jump=` link, `U` the `file://` URL with its query; to
every connection, the window keeps its page). The attach side forwards a jump to the server as the `0x04` event of
§6.3, mapping a pushed copy in `page/` back to the server's path; a client whose server had no `"pages":1` never
asks for it. Line limit: 1 MiB.

### 15.4 GUI mode (M2.6)

```text
fleetmux-web --gui --dir <web runtime dir> [--run-dir <run dir>] --prefix <key> [--theme JSON] [--devtools]
             [--test-hooks]
```

One window with one WebView: the terminal page `fleetmux://app/index.html` (xterm.js, full window) and, laid over
the lower-left cell, a sandboxed iframe with the history page (design.md §3.6.4). Started only by `fleetmux gui`
(§9.5); directories, permissions and `web.sock` rules as §15.2. `--prefix` is required (the prefix key name, §9.1,
caught in the iframe). `--run-dir` is the first page scope (`scope` below can set it later). `--devtools` as §15.1.
`--test-hooks` (graphical tests only; `fleetmux gui` never passes it) enables `js` below. Usage error: exit 2.
`FLEETMUX_WEB_QUIET=1` (both modes; tests only, `fleetmux` never sets it): the window is real but never shows or takes
the focus — macOS: accessory app, no launch activation, alpha 0 and click-through; `open` does not focus it
(`crates/web/src/quiet.rs`). Every window test sets it, so test runs do not flash over the person's desktop.

**`--theme JSON` (M2.6).** Optional. The object `hello.theme` carries (§4.1): all four keys `bg` / `fg` / `cursor` /
`ansi` (exactly 16 colours) are required here, colours `#rrggbb` in either case (used lowercase), unknown keys or a
bad value are a usage error (exit 2). The page sets the xterm.js theme from it — `background`, `foreground`,
`cursor`, `cursorAccent` = `bg`, `black`…`brightWhite` = `ansi[0]`…`ansi[15]` — and paints `html` and `body` with
`bg`, so the margins the terminal does not cover match too. Without `--theme` the page passes no theme to xterm.js
(its own colours: a black cell). `fleetmux gui` always passes it when the server reports one (§9.5).

**Pipes.** **stdin** carries frames client → window in the §6.2 format, only `0x81` (screen bytes, written to the
terminal as they are) and `0x85` (§6.3 overlay, applied after the screen bytes before it are written); any other
type → exit 1. EOF on stdin → exit within 1 s (as §15.1). **stdout** carries frames window → client, only `0x01`
(keys / mouse / paste from the terminal) and `0x02` (the terminal's size in cells). The **first** frame on stdout is
a `0x02`: the size of the page's first fit; the client uses it for its handshake. A frame the window cannot write
(client gone) ends it as an EOF does.

**Pipes, scroll frames (M2.6, gui-scrollbar; extends the list above, nothing in it changes).** The complete set of
types on **stdin** is therefore `0x81`, `0x85` and `0x86` (§6.3 scroll state): `0x86` is applied exactly like
`0x85`, after the screen bytes before it are written, and is not a reason to exit; any other type still exits 1.
The complete set of types on **stdout** is therefore `0x01`, `0x02` and `0x05` (§6.3 scroll to, produced when the
person drags or clicks a scrollbar): the client sends it to the server unchanged; any other type is still a bad
frame. The `0x81` / `0x85` and `0x01` / `0x02` rules above are unchanged.

**`web.sock` commands** (one JSON line each way, as §15.3; `js` only with `--test-hooks`, otherwise unknown):

| Request | Reply | Effect |
|---|---|---|
| `{"cmd":"open","url":U}` / `{"cmd":"goto","url":U}` | `{"ok":true}` | `U` is a `file://` URL (as §15.3). The window maps it to `fleetmux://page/…` (below), checks it against the GUI allow-list and loads it in the iframe; answered when the iframe has loaded (or changed URL for a fragment-only change), within 3 s. `open` also brings the window to the front |
| `{"cmd":"url"}` | `{"ok":true,"url":U}` | the iframe's current URL in `file://` form (query and fragment kept); `about:blank` before the first load |
| `{"cmd":"reload"}` | `{"ok":true}` | reload the iframe |
| `{"cmd":"quit"}` | `{"ok":true}` | reply, then exit |
| `{"cmd":"scope","run_dir":R}` | `{"ok":true}` | `R` (absolute path) is the run directory whose pages may load. The client sends it before the first load and whenever the run directory of `history_url` changes |
| `{"cmd":"focus"}` | `{"ok":true}` | keyboard to the embedded page; if the overlay is hidden now, done when it becomes visible within 2 s |
| `{"cmd":"text"}` | `{"ok":true,"rows":[…]}` | the terminal's visible rows as strings (`rows` entries, top first) |
| `{"cmd":"overlay"}` | `{"ok":true,"visible":b,"cells":{"x","y","w","h"}\|null,"px":{"x","y","w","h"}\|null,"src":S}` | the overlay now: shown or not, its cell rectangle (last `0x85`), its pixel rectangle, the iframe's `fleetmux://page/…` address (`""` before the first load). `px` is in CSS pixels **relative to the top-left corner of xterm.js's `.xterm-screen` element** (the iframe's own `left` / `top` / `width` / `height` inside it, design.md §3.6.4), not viewport coordinates; numbers, not necessarily integers |
| `{"cmd":"js","code":C}` | `{"ok":true,"result":V}` | **`--test-hooks` only**: evaluate `C` in the terminal page; `V` = its completion value as JSON. In this mode only, `window.close()` called by the terminal page's own main frame closes the window through the **same path as the person closing it** (the window's real close request: `closed` event to every connection, exit 0); graphical tests use it |

A refused URL, a relative `run_dir`, an unknown `cmd` or a line that is not a JSON object gets
`{"ok":false,"error":"<English sentence>"}` and changes nothing. Unsolicited lines from the window:
`{"event":"window","state":"closed"}` (the person closed the window, then exit 0, as §15.1) and, new,
`{"event":"history_focus"}` (the person clicked into the embedded page; the client focuses the history pane with
`pane.focus`), and (M4) `{"event":"jump","url"}` as in §15.3: after an `open` with `"pages":1`, a `?fleet-jump=` link
in the embedded page is stopped — checked before the GUI allow-list, because the scope follows the shown page and a
link back to the parent fleet lies outside it — and reported as the `file://` form of its `fleetmux://page/…` URL.

**Fleet pages (M4).** When a `0x85` names another `url` document than the previous `0x85` did (the server selected
another fleet page, §4.25) and a page has been loaded already, the window loads it into the iframe itself — only when
the GUI allow-list (current scope) lets it in and the file is on this host; a cross-host page comes through the
client's page exchange. The client still scopes and loads a `url` in another run directory as before, and the
server's `0x84 goto` after `page.select` reaches the window as any `goto` does — except that a client whose server
said `"pages":1` first exchanges a `goto` page that is neither on its host nor its pushed copy (`{"op":"page"}`,
§6.3) and loads the pushed copy, for the M2.5 window and the GUI alike; the window is never sent a server path that
does not exist on its host. Without `"pages":1` a `goto` goes out as before.

**`fm.snapshot()` (`--test-hooks` only).** With `--test-hooks` the terminal page's `window.fm` also has
`snapshot()`, a read-only sample taken in **one synchronous page execution**:
`{cols, rows, textRows, screen:{x,y,w,h}, renderer, scrollbars:[…]}` — the live `term.cols` / `term.rows`, the
visible-row count (`fm.text().length`), the `.xterm-screen` rect and the same bar payload as `fm.scrollbars()`.
It never mutates state, never calls `refit` / `reportSize` / `placeScrollbars`, sends no ipc and returns no terminal
text. Without `--test-hooks` the property is **absent** (not a present `undefined` key).

**`term.onData` diagnostic (`--test-hooks` only).** A window launched with `--test-hooks` exposes
`window.__fmOnDataDiag` (`schema "fm-ondata-diag/1"`), a **default-OFF** passive tap on the terminal page's real
`term.onData` callback: it records only the bytes that callback hands to the unchanged input path
(`{seq,t,hex,bytes,source:"term.onData"}`, bounded ring of 500, `state()` / `dump()`), it never changes, delays or
re-sends input, never synthesizes an event and never copies the ipc token. `enable()` refuses unless the page's
`window.__FM_ON_DATA_DIAG_TARGET__` names the explicitly selected isolated window. A window without `--test-hooks`
has no such global, no `js` command to reach it and an unchanged input path; contract and the exact requests are in
`workspace/impl-1/keyboard-hook.md`.

**Closing the window from a page.** Without `--test-hooks` nothing a page does closes the window: `window.close()`
in the terminal page does nothing, and the iframe (history page) can never close it in any mode — no navigation,
message or ipc of a sub-frame is a close request. The `--test-hooks` `window.close()` entry above is the only
exception; it is taken only from the trusted terminal page's main frame.

**Custom protocol `fleetmux:`** (served from memory or the local disk; nothing comes from the network):

- `fleetmux://app/<name>` — built-in assets only (`assets::get`): the terminal page (`index.html`, its script and
  style, the iframe script `frame.js`) and the vendored xterm.js files as `xterm/<file>`; anything else 404.
- `fleetmux://page/<absolute path>?q#h` — the file at that path (percent-decoded, `.` / `..` resolved lexically,
  query and fragment ignored) when it is under the scope run directory or under `<dir>/page/`; anything else 404.
  `file:///a/b?q#h` ↔ `fleetmux://page/a/b?q#h` maps both ways byte for byte (`policy::to_page_url` /
  `to_file_url`); `file://localhost/…` counts as local, any other host does not map.

**GUI allow-list** (`policy::allow_gui`): `fleetmux://app/index.html`, `about:blank` and `fleetmux://page/…` under the
two directories above load; `http://` / `https://` go to the system opener; `file:` and everything else are
refused. It applies to the iframe too. New-window requests (`window.open`, `target=_blank`) are refused;
`http(s)` ones go to the system opener.

**iframe sandbox:** `sandbox="allow-scripts allow-popups"`, never `allow-same-origin` / `allow-top-navigation`. The
terminal page accepts only three messages from it, checking that the sender is that iframe: `{fm:"prefix"}` (the
prefix key, caught by `frame.js` in the iframe: the terminal takes the focus back and sends the prefix byte as
`0x01`), `{fm:"url",href}` and `{fm:"history_focus"}`.

**ipc** (terminal page → window process, `window.ipc.postMessage`). Each message is one JSON object carrying the
window's token, `{"token":"<32 lowercase hex>","type":T,…}`; the token is 128 random bits made per window start and
handed only to the terminal page's main frame. A message whose token is missing or wrong, or whose shape is not one
of these, is dropped (`gui::check_ipc`); on macOS the message must also come from `fleetmux://app/`.

| `type` | Other keys | Meaning |
|---|---|---|
| `input` | `data`: base64 (§4.7 alphabet) | bytes for a `0x01` |
| `resize` | `cols`, `rows`: int 1–65535 | the terminal's size for a `0x02` |
| `ack` | `seq`: int ≥ 0 | the page has written screen batch `seq` (back-pressure, design.md §3.6.5) |
| `copy` | `text`: string | put `text` on the system clipboard |
| `paste` | — | read the system clipboard into the terminal |
| `history_focus` | — | DOM focus went into the iframe |
| `page_url` | `url`: string | the iframe's current `fleetmux://page/…` URL |
| `page_loaded` | `ok`: bool | the iframe finished loading |
| `scroll` | `pane`: string, `offset`: int ≥ 0 | a scrollbar was dragged or clicked: scroll that pane to `offset` lines above the bottom, sent to the client as one `0x05` (§6.3). A missing key, a negative or non-integer `offset` → the message is dropped |
| `drag_window` | — | (S1e; macOS) the person pressed on a row-0 cell of the screen that is blank or `─`: drag the window (`Window::drag_window`). The page takes it in the capture phase and keeps it from xterm.js; button cells, label cells and the history iframe are not intercepted. Linux has no row 0 to drag (its headerbar is the titlebar), so its page never sends this |
| `zoom_window` | — | (S1e; macOS) a double click on such a row-0 cell: `set_maximized(!is_maximized())` — the macOS zoom |
| `inset` | `cols`: int 0–40, `cell`?: number 1–256 | (S1e) the columns row 0 leaves for the window controls; `cols` is one less than the first cell whose left edge is past 81 px (measured system controls: 9 px margin + 3 × 14 px + 2 × 9 px = 69 px, plus a 12 px gap), sent right after the first fit and again after a font-size change, a window resize or a display-scale change. The window process hands it to the client as one `0x06` (§6.3), only when the server's `ok` line carried `"inset":1`; the first one is held until after the page's first `resize`, so the first frame on the window's stdout stays the page's first fit. A missing, non-integer or out-of-range `cols` → dropped. `cell` is one cell's width in px as the page renders it, and is **not** forwarded: the window keeps it for the Linux headerbar's boundary, which is a column of the page's own canvas `cols · cell` (a few px narrower than the window, because the page fits whole cells), and for the header floor it reports back on `0x06` as `sidebar_min` (§6.3). Optional — an older page omits it and the window falls back to the headerbar's width. A present but non-numeric, non-finite or out-of-range `cell` → dropped |
| `caret` | `x`, `y`, `w`, `h`: int (all four, or none) | the focused terminal textarea's cell in the page's own CSS px, sent when it changes: on focus in/out, a cursor move, a fed screen (a pane switch), a font-size change, a resize, a zoom, and when the history iframe takes or loses the focus. No keys at all = the terminal is no longer the real focus target. The window process uses it on **Linux only**: it hands the page's own CSS rect to WebKit's input method context (`WebKitInputMethodContext::notify_cursor_area`) unchanged, because WebKit itself refreshes the GTK cursor area only on focus and selection changes, so a caret that moved kept the old area. That rect is already **client-window-relative** — the IM's client window is the webview's own `GdkWindow` and the page's CSS origin coincides with it — so GTK/ibus adds the window origin and applies the window's scale factor when it turns the rect into root coordinates (adding the window origin here made IBus place the candidate one `(5, 51)` duplicate below the caret). No scale/DPR term belongs here either (a scale-2 window delivers the same logical numbers). It is never forwarded — no frame, no `0x06`, no server field — and macOS ignores it, keeping WebKit's native behaviour. A partial rect, a non-integer or a value outside ±100000 → the message is dropped |

**Linux layout update (overrides historical right-sidebar descriptions below and in §16).**
Linux defaults to left sidebar | supervisor/lower stack | workers. Native GTK tools are left-aligned,
followed by a 1px separator at `(sidebar + 0.5) * cell` and fleet title/status. GTK retains its system-configured
window buttons on either side. The `0x06.sidebar_min` floor uses actual toolbar width plus the row's measured
left offset (including left-native controls), divided by cell width; right-native controls do not widen the
left sidebar. Width persistence, viewer aggregation, narrow-window hiding and overflow checks are unchanged.

The view hello accepts optional `native_header`: absent/integer 0 means false, integer 1 means true, other
values/types are rejected (same rule as `overlay`). Linux GUI sends 1. Native-header servers reserve one row;
ordinary attach/old clients use that same row for clickable tools/title, while GUI leaves it blank. Shared
pane/PTY geometry does not change. Before sidebar metadata exists, no toolbar is drawn and hit geometry stays
unchanged. `0x85.topbar` remains the existing UI metadata. Runtime native-header/side/mirror flags are not
serialized. Left sidebar drags measure the column itself on both platforms. Use matching server/GUI/web builds;
upgrading a running server requires separate operator authorization. macOS accepted-base geometry is unchanged.

**Topbar (S1e, both platforms; historical Linux direction superseded above).** Row 0 is one header container: the server's toolbar (B, `◫ + ↺ ← →`),
the divider on the sidebar's drag line and the right-hand current-fleet title + status (C). On macOS the native
window controls (A) are on that same line.

- **A stays the platform's own control and is never moved.** macOS keeps the system default of a
  full-size-content, title-less, transparent-titlebar window: no `traffic_light_inset` is ever set and the page
  never moves the lights. The measured default with this style mask (macOS 26.6.2) is 14 px buttons at
  x = 9 / 32 / 55, top = 9 px, i.e. centre 16 px below the window top; the window process passes that centre to
  the page as `__fmBoot.controlCenter` (`CONTROL_CENTER`). Linux draws **one** native header row:
  the window's titlebar is a GTK
  headerbar (`gtk_window_set_titlebar`), so GTK's own close/minimize/maximize stay native widgets at the
  OS-default top-right anchor (`gtk-decoration-layout`). The same row carries the approved layout's two containers:
  the **sidebar header** on the right (above the mirrored layout's sidebar band) with a `│` boundary separator and
  the app's toolbar `◫ + ↺ ← →` in that order, right-aligned beside those native buttons, and the **workspace
  header** on the left with the current fleet's title/status (C), left-aligned. Both fill the row (same top, bottom,
  height and centre by construction) and their boundary is recomputed from the headerbar's allocation, the `0x85`
  `topbar` `sidebar`/`cols` geometry **and the page's own `cell` width** (the `inset` message, §15.4), so it is a
  column of the canvas the page really draws (`cols · cell`, a few px narrower than the window) and stays on the
  grid's sidebar drag line across sidebar drags, collapse, window resize and font changes. The five tool cells are
  the layout's 28 px (a class-scoped rule on their own style contexts — the theme's padded headerbar button would
  otherwise be 50 px each and the toolbar 274 px wide), so five cells + 8 px of margins = 148 px fit the sidebar
  band beside GTK's window buttons at the default font; only a band that really cannot hold that floor (a
  collapsed or very narrow sidebar) clamps, and it keeps the toolbar whole instead of squeezing a control away.
  The buttons send the prefix keys the row-0 cells call (`^b s`, `^b c`, `^b r`, `^b (`, `^b )`). **No HTML lookalikes**: the page draws no controls
  and no resize grips, reports no `inset`, adds no pad, and `win` / `resize_window` are not ipc messages at all.
  A mirrored (Linux) layout therefore keeps **no** topbar row in the grid, so no second band can duplicate the
  header, and C reaches that window through the `0x85` `topbar` key (§6.3). That headerbar is painted from the
  palette the window resolved (`GuiOpts::theme`, the same one the page uses) through a CSS provider attached to the
  headerbar's own style context: background, foreground, the grey status text, the boundary and the button states
  follow the fleetmux theme, no GTK/GNOME setting is written and no colour is hardcoded.
- **Header band.** The grid keeps a header band above the content: on macOS the topbar row 0
  plus one blank separation row (`TOPBAR_ROWS` = 2, only while a topbar is set); on Linux one blank row under the
  native headerbar (`HEADER_GAP` = 1). `Layout::rects` starts the content on the band's last row, so the workspace's
  topmost tab row and the sidebar's first fleet card begin on the same row with one clear separation row under the
  global header; `Layout::hit_test` returns no tab, separator or pane inside the band and `ratio_from_y_in` measures
  the horizontal-separator drag from the band's last row, so dragging, collapsing, a font change and a window resize
  keep the alignment.
- **B and C adjust to A on macOS.** The page pads the terminal grid down (`placeTopbar`) so row 0's centre equals
  `controlCenter` (`pad = controlCenter − cellH/2`, allowed a few px negative to use the empty space above the
  glyphs when the cell is taller than 2 × the centre) and takes that pad off the grid's height, so the last row is
  never clipped. The toolbar starts one cell past `inset` (81 px of native controls + gap), the server draws B at
  `cols + 1` and C from `left + 2`; both are on row 0, so they follow that centre at any font size, and a font
  change re-reports `inset` and re-pads. On Linux the grid has no topbar row at all (no pad, no `inset`): the
  headerbar above it is that row, nothing in it follows the terminal font, and the grid's own first row is the
  header band's blank separation row (see the header-band bullet).
- **One container, one centre.** B and C are row 0 of the same grid, so they share one height and centre by
  construction; a font change, a window resize, hiding or showing the sidebar and dragging its divider move only
  the divider and the title, never the toolbar's columns and never the native controls. Linux has the same rule in
  its headerbar: the workspace header and the sidebar header fill the one row (equal top/bottom/height/centre) and
  their `│` boundary follows the grid's sidebar divider through the `0x85` `topbar` geometry.

Blank and `─` cells of row 0 drag the window, a double click there zooms it, and on macOS the strip the page pads
above row 0 is part of the same drag/zoom band; `◫` and the labels still reach the server.

**Scrollbars (M2.6, gui-scrollbar).** The view handshake also says `"scroll":1` (§6.1), so the server sends `0x86`
(§6.3) for the window's cells. The client relays `0x86` from the server into the window pipe as it is, like `0x85`,
and relays a `0x05` coming back from the window to the server unchanged (it neither parses it nor remembers any
state). The window draws one scrollbar per cell and sends `0x05` only after the first `0x86` (§15.4); a drag
changes the same `display_offset` the wheel and page mode use, so the screen and the `0x86` that follows stay in
step. A `fleetmux gui` older than this change sends `"overlay":1` without `"scroll":1` and gets no `0x86`; a server
older than it never sends one, and then the page shows no scrollbar at all.

**Theme (M2.6).** The `hello` reply's optional `theme` (§4.1) is handed to the window verbatim as `--theme JSON`
(§15.4), which is how the terminal cell's background / foreground / cursor / 16 ANSI colours and the page background
match the server's OSC answers (§9.1). A server that sends no `theme` (an older one): no `--theme`, the window keeps
xterm.js's own colours. `--theme` is not a `fleetmux gui` flag: the palette belongs to the server, so every viewer of
one server shows the same colours.

| Exit | Meaning |
|---|---|
| 0 | the user detached (prefix + `d`), the person closed the window, or the server exited (`0x82`) |
| 2 | usage error |
| 3 | cannot connect (as §9.2) |
| 4 | cannot open the window: no graphical session, `fleetmux-web` missing or of another version, WebKitGTK missing, web runtime path too long, or the window not ready within 3 s |

On exit 4 stderr gets one line with the reason (for a window that failed to start: the tail of its stderr /
`web.log`, e.g. `libwebkit2gtk-4.1.so.0: cannot open shared object file`) and the hint
`use: fleetmux attach --runtime D`. `fleetmux gui` never falls back to a terminal attach by itself. Every way out
stops the window first (`quit`, close its stdin, wait); no window outlives its client.

---

## 10. Keys (P-5)

The server interprets keys; `fleetmux attach` only forwards the outer terminal's bytes as `0x01` frames.

- **Prefix**: `C-b` (byte 0x02) by default, `--prefix` to change (§9.1). After the prefix the server waits for one
  more key; if none comes within **1 s** the prefix is cancelled and nothing is written.
- Pressing the prefix twice writes the prefix byte itself to the focused pane.

| After the prefix | Action |
|---|---|
| `1` … `9` | select the n-th tab of the focused group |
| `Tab` / `BTab` (Shift-Tab) | next / previous tab of the focused group |
| `Left` / `Right` | move focus between the left column and `workers` |
| `Up` / `Down` | move focus between `supervisor` and `lower` |
| `z` | toggle zoom of the focused pane |
| `[` | enter page mode (§13) |
| `<` / `>` | `h_ratio` −5 / +5 (clamped 10–90) |
| `+` / `-` | `v_ratio` +5 / −5 (clamped 10–90) |
| `=` | reset ratios to 50 / 60 |
| `o` | send `0x83` to this viewer (§6.3): payload = kv `history_url` without `file://`, empty if unset (M4: the current page's `history_url`) |
| `s` | toggle the fleet sidebar: only while kv `sidebar` is set (§4.2); the shown/hidden flag is shared by every viewer and not saved in `layout.json`. Hiding folds the cards away only: the sidebar's width becomes 0, the panes fill the screen from column 0, and row 0's topbar left part stays at the toolbar's own `inset + 15` columns, so `◫ + ↺ ← →` keep their columns and nothing is drawn below row 0. Without the kv key there is no topbar and row 0 is the tab row again |
| `r` | restore the newest closed fleet: the same as clicking the topbar's `↺`, it spawns the first kv `restorable` run (§4.2) as a pane titled `resume`; with nothing to restore it writes the status-line notice `没有可恢复的 fleet` ("no restorable fleet"), and while the sidebar still lists a `running` / `paused` / `stopping` card it only writes `先关闭正在运行的 fleet 再恢复` ("close the running fleet before restoring"; one session holds one fleet) |
| `(` / `)` | step the sidebar's selection back / forward — the same as clicking `←` / `→` on its toolbar. With one fleet both arrows are grey and nothing happens. M4: a step onto a card that carries `page` switches to that fleet page (§11); a card without `page` only writes `切换 fleet：S3 开放` ("switch fleet: opens in S3") as before |
| `X` | close this fleet: open the close confirmation box, the same as clicking the `×` on the card; `y` confirms (kv `stop_request`, §4.2), any other key cancels |
| `d` | detach: the client sends `0x03` and exits 0; panes keep running |
| the prefix again | write the prefix byte to the focused pane |

**Topbar (S1f).** While kv `sidebar` is set, row 0 is a topbar across the whole viewer, split in two at the
sidebar's drag line (§11). Its left part is `max(sidebar width, inset + 15)` columns while the sidebar is shown and
`inset + 15` while it is hidden; it holds the toolbar `◫  +  ↺  ←  →` at `inset + 1 / 4 / 7 / 10 / 13` (`inset`
is the gui window-control room, §6.3), each button a three-column click area. The `│` divider sits at the sidebar's
edge (column `sidebar width`, only while the sidebar is drawn); the right part starts two columns after it and
shows the current fleet's `title` (bold white) ` · ` its `line1` (grey), cut with `…` at the last column before the
right edge. With a sidebar the groups' border and tab-label row is row 1 and their panes start on row 2, where the
cards still start. The topbar is derived runtime state: no new frame, handshake key or kv key, and `layout.get` /
`layout.json` do not carry it.

Ratio, zoom, focus and selection changes are shared by every viewer (§14) and rewrite `layout.json` (§8.3).

---

## 11. Mouse (P-6)

- `fleetmux attach` turns on outer mouse modes **1002** (button-event tracking) and **1006** (SGR encoding) unless
  `--no-mouse`; the server only parses SGR mouse reports (`ESC [ < b ; x ; y M|m`). Hit testing uses
  `fleetmux_layout::Layout::hit_test`.
- Left click on a tab → select that tab and focus its group. Left click inside a group's pane → focus that group.
- Left click in the fleet sidebar (only while kv `sidebar` is set, §4.2). Row 0 is a two-segment topbar across
  the viewer, split at the sidebar's drag line (`Layout::topbar_left`). Its left part is the toolbar
  `◫  +  ↺  ←  →` at `k+1 / k+4 / k+7 / k+10 / k+13` (`k` = the viewer's `inset`, §6.3), with three-column click
  areas: `◫` `k..=k+2`, `+` `k+3..=k+5`, `↺` `k+6..=k+8`, `←` `k+9..=k+11`, `→` `k+12..=k+14`. `◫` folds the sidebar
  away — width 0, the panes fill the screen from column 0 — and a click on it again, or `C-b s`, shows it at the
  width it had; the buttons keep these columns in both states. `+` opens a bash, as `C-b c` does (below); `↺` restores the newest closed fleet — the same
  as `C-b r` (§10): it spawns the first kv `restorable` run's `argv` in its `cwd` with its `env` as a pane titled
  `resume`, zooms it and writes `正在恢复…` ("restoring…") on the status line; with nothing to restore it writes
  `没有可恢复的 fleet` ("no restorable fleet"), and while a `running` / `paused` / `stopping` card is listed it writes
  `先关闭正在运行的 fleet 再恢复` ("close the running fleet before restoring") and spawns nothing (one session holds one fleet); `←` / `→` step the card selection and, when there is a card before / after
  the current one, select it like a click on that card (below; a card without `page` only writes the notice
  `切换 fleet：S3 开放` ("switch fleet: opens in S3"); grey arrows do nothing). The right part, from two columns
  after the divider to the last column before the right edge, shows the current fleet's `title` + ` · ` + `line1`,
  cut with `…`; a press there is `Outside` — the server does nothing and, in the gui, a title cell is not a
  window-drag cell either. With a sidebar the groups' border and tab-label row is row 1 and their panes start on
  row 2, where the cards still start; without the kv key row 0 stays the tab row. A click on a card (M4, sidebar
  S3) whose kv `sidebar` entry carries the optional `page` key switches every viewer to that fleet page, exactly
  as `page.select` (§4.25; the web windows get `0x84 goto` to its `history_url`); a `page` the server does not
  have only writes `这个 fleet 没有画面可看` ("this fleet has nothing to show"). With cards that carry `page`, the highlighted card and the topbar's
  right part are the card of the **current page**, whatever kv `current` names. A card without `page` keeps the
  M3 behaviour: a click on the current fleet's card shows its normal grid, another card writes
  `切换 fleet：S3 开放` ("switch fleet: opens in S3"). Each bash card's shell is a tab of the root page's `workers` group (every new bash is
  mapped into the main fleet's worker area): `+`, the blank double click and `C-b c` add it last in
  that group, and opening one or picking its card shows the root page, cancels a zoom of another pane, selects the
  tab and focuses its group. A bash card is the selected card while the root page's zoomed pane — else its focused
  group's selected tab — is that bash; the root fleet's card then focuses the `supervisor` group and cancels a
  zoom, the bash staying a tab of the worker area. While a bash card is selected the topbar (and the Linux
  headerbar) reads `bash-<n> · 终端 · 运行中` ("bash-<n> · terminal · running"). A shell that is gone takes its tab and its card with it. A current page that is a reclaimed child fleet (`page.close {keep:true}` /
  `page.open {reclaimed:true}`) is a read-only record: its empty supervisor and worker groups show
  `子 fleet <title> 已收回 · 只读` ("sub-fleet <title> reclaimed · read-only"; White, bold), `下面是它的运行记录；点侧栏别的卡片切回` ("below is its run record; click another sidebar card to switch back"; Gray) and
  `同名再起时这一页会重新启用` ("this page is reused when a fleet of the same name starts again"; DarkGray); a child page's title also goes on the status line as `page: <title>`
  right before `focus:`. A click on a card's `×` —
  drawn on its first row, columns 30–32, only for a `running` / `paused` fleet that has no stop request yet, and
  (M4) never for a child card that carries a non-empty `page` (child fleets start and stop through their roster
  and `subfleet.up` / `down`) —
  opens the close confirmation box (§4.2 `stop_request`); while the box is open a press on `[ 关闭 fleet ]` ("close fleet") confirms
  and any other press cancels. A `closed` card's first row draws `↺` at column 31 (columns 30–32 click) instead of
  the `×` and restores that card's run by its `id` in kv `restorable`; a closed fleet's cards keep the grey dot and
  the `已关闭 · 可恢复` ("closed · restorable") line (a run a lifecycle timeout stopped says `到点停 · 可恢复` ("stopped on timeout · restorable"); a run stopped otherwise while a
  busy bash page kept its server reads `state` `stopped` / `已停止` ("stopped"): grey, neither `×` nor `↺`, not live), and while the fleet has no
  pane (bash tabs do not count) the pane area shows the three-line closed-fleet
  placeholder (White title, Gray restore hint, DarkGray exit hint), except while a bash card is selected. A sidebar that holds only grey cards — the
  history list `./start.sh gui` writes — has no closed fleet of its own: with no pane and no `running` / `paused` /
  `stopping` card its pane area shows `没有在跑的 fleet；点侧栏卡片上的 ↺ 恢复` ("no fleet running; click ↺ on a sidebar card to restore"; White, then the DarkGray exit hint)
  instead, and such a server exits 10 s after the last pane and viewer like a closed one; a
  server without kv `sidebar` never exits by itself.
- Double click on a tab (two clicks within **300 ms**) → toggle zoom of that pane.
- Drag the vertical separator → `h_ratio = ratio_from_x(cols, x)`; drag the left column's horizontal separator →
  `v_ratio = ratio_from_y(left_rows, y)`. Both clamp to 10–90; `layout.json` is written when the button is released.
- Drag the sidebar's inner edge → resize the sidebar. The edge is the gap column plus the content's border column
  next to it on any row above the status line, while the sidebar is shown and at least 2 columns wide: columns
  `{width−1, width}` on macOS (the left band) and `{columns−width−1, columns−width}` on Linux (the right
  band). On row 0 those two cells are the topbar's `│` divider and the blank column beside it, so the two
  segments split exactly where the drag happens and the divider resizes the sidebar like the border below;
  hidden (width 0) there is no edge to drag, the topbar's left part is only the toolbar. A press there starts the
  drag, every move sets the width to the dragged distance from the sidebar's own side of the window (the column
  itself on macOS, `columns − column` on Linux) clamped to `24..=min(60, columns − 60)`, and the release saves it
  to kv `sidebar_width` (§4.2). A window whose maximum is below 24 columns cannot drag at all. While the drag is held, that border column — row 1 (the groups' first row) down to the row above the status
  line, which is never painted — is `Yellow` bold; row 0's divider is not repainted during the drag, it keeps its
  `Gray` (S1f). The grid, the pane PTY sizes, the `0x85` history rectangle and the `0x86` scrollbar follow
  automatically because they all read `Layout::rects()`. The gui cannot drag it: it draws one xterm.js canvas and
  the page does not know which column is the border, so it shows no resize cursor there either.
- Wheel over a pane: if the inner program has mouse reporting on (`modes.mouse`), the event is re-encoded for it
  (`encode_mouse`, coordinates relative to the pane's origin) and written to the pane; otherwise the wheel moves
  that pane's view `display_offset` by ±3 rows (an implicit page view; any input written to that pane first resets
  the offset to 0).
- Wheel over the fleet sidebar's card rows (from the first card's row down to the row above the status line, the
  gap column included; Linux: the right band) scrolls the card list, like cmux: one card per wheel step, stopping at
  the first and the last card. Only whole cards are drawn and nothing counts the rest (there is no `… 还有 N 个` ("… N more")
  line). The scroll offset is server state shared by every viewer, like the card selection and shown/hidden — not
  per connection, not in `layout.json`. While the cards overflow, a grey `┃` thumb sits in the band's column next
  to the content (the gap column `width − 1`; Linux: the band's first column), over frameless content on the rows
  of the `│` divider it replaces; its height is visible / total cards of the rows from the first card's first row
  to the last drawn card's last row (at least one row) and its place follows the offset. A click on a card or its
  `×` acts on the card drawn under the pointer. A changed selection — a click, `←` / `→`, `C-b (` / `C-b )`, a new
  bash page — or a card that comes or goes scrolls the selected card into view; a taller window clamps the offset
  so no card slot is left blank.
- Left button inside a pane whose inner program has mouse reporting **off** selects text in that pane's view
  (whatever its `display_offset`): a press starts a selection, every move while the button is held extends it,
  and the release copies it (§12). A second press on the same cell within **300 ms** selects the word under it, a
  third press on the same row within 300 ms of the second selects the whole line; both copy at once, and the press
  after a triple click starts over. A word is the longest run of cells around the pressed one holding neither
  whitespace nor one of `'` `"` `│` `` ` `` `|` `:` `;` `,` `(` `)` `[` `]` `{` `}` `<` `>` `$` (Ghostty's default `selection-word-chars`, as in
  cmux), crossing soft-wrapped row ends; a press on such a character or a blank selects nothing. A line is the
  logical line: soft-wrapped rows (the inner program's text ran past the right edge) are followed up and down
  within the view. The selection is drawn reverse-video on the selecting viewer's canvas only and stays after the
  copy, until that viewer's next mouse press or key (typed, pasted or prefix key), until the pane's view is scrolled
  to another offset, or until the pane closes. With the inner program's mouse reporting on, all of this goes to the
  program instead (re-encoded as above).
- Any mouse event with **Shift** held is neither handled nor forwarded by the server (the outer terminal's own
  selection takes it).

---

## 12. Copy (P-7)

- When the inner program's mouse reporting is off, a left-button drag, double click or triple click inside a pane
  selects text (§11) and copies it at once: on release (drag) or on the press (double / triple click) the server
  takes `TermParser::selection_text(from, to)` and sends a `0x81` frame with
  `ESC ] 52 ; c ; <base64 of the text> BEL` to **every** view connection. Each row's trailing whitespace is
  stripped (a blank row copies as an empty line); rows are joined with `\n`, except that a soft-wrapped row runs on
  into the next one with no `\n`. A click without a drag copies nothing.
- The gui page (`fleetmux gui`) also copies a selection xterm.js makes itself (Option-click forced selection,
  including its own double-click word select) on mouseup; Cmd-C / Ctrl-Shift-C copies it as before.
- The server also copies on its own host: macOS → `pbcopy`; else with `WAYLAND_DISPLAY` set → `wl-copy`; else with
  `DISPLAY` set → `xclip -selection clipboard`; none of these → skip and log one `INFO` line. If the tool fails,
  one `WARN` line is logged; copying never fails the selection.

---

## 13. Page mode (P-8)

- Entered with prefix `[` on the focused pane. While in page mode, keys go to page mode, never to the pane:

| Key | Action |
|---|---|
| `PgUp` / `PgDn` | offset ± (rows − 1) |
| `Up` / `Down` | offset ± 1 |
| `/` | start typing a search string on the status line; `Enter` jumps to the nearest match above (`search_up`) |
| `n` / `N` | next match further up / next match further down |
| `q` / `Esc` | leave page mode; offset back to 0 |

- The status line shows `[offset/scrollback]` while in page mode. Every other key is ignored (not written to the
  pane). The offset is clamped to the pane's scrollback length.
