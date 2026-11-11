//! The pane ledger (design.md §2.4, PROTOCOL.md §4.0): one PTY child, one terminal parser and one bounded write
//! queue per pane; `panes.json` (PROTOCOL.md §8.2).
//!
//! Package B5 `server-pty`. Threads: each pane has one reader thread (PTY output → [`PaneEvent`] on the store's
//! bounded channel, taken once by the event loop with [`PaneStore::take_events`]) and one writer thread (drains
//! the pane's own queue into the PTY, so a pane that never reads its input blocks nobody else). Everything else
//! runs on the caller's thread under the `State` lock; nothing here sleeps except the old async [`PaneStore::kill`].
//! B20 `record-hook`: the recording hook is in the PTY reader thread ([`read_loop`]), not in [`PaneStore::feed_output`].

use std::collections::{BTreeMap, VecDeque};
use std::io::{self, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use fleetmux_proto::{Group, PaneId, PaneInfo, Spawn, WriteMode};
use fleetmux_term::{Palette, TermParser};
use tokio::sync::mpsc;

use super::pty::{self, PtyChild, Reaped};
use super::record::{RecordFile, Recorder};

pub use super::pty::SpawnError;

/// Most bytes one pane may have queued and not yet written to its PTY (PROTOCOL.md §4.7: 16 MiB pending).
pub const WRITE_QUEUE_MAX: usize = 16 << 20;

/// `pane.kill` escalation (PROTOCOL.md §4.13, R4): SIGHUP, SIGTERM, SIGKILL at these ms after the request; a step
/// is sent only if its time is ≤ `timeout_ms`.
pub const KILL_SCHEDULE_MS: [u32; 3] = [0, 2000, 5000];
/// After the last allowed step: wait for the reap until `timeout_ms` + this, sending nothing.
pub const KILL_REAP_GRACE_MS: u32 = 500;

const KILL_SIGNALS: [i32; 3] = [libc::SIGHUP, libc::SIGTERM, libc::SIGKILL];
/// How often a kill in progress looks for the reap.
const KILL_POLL: Duration = Duration::from_millis(20);
/// Capacity of the [`PaneEvent`] channel; a full channel blocks only the reader threads (back-pressure).
const EVENT_CAPACITY: usize = 256;
const READ_BUF: usize = 64 << 10;

/// Why `PaneStore::write` refused. Added in stage A: §3.3 names the type but does not define it.
#[derive(Debug)]
pub enum WriteError {
    /// `no_such_pane`.
    NoSuchPane,
    /// The pane's queue would exceed [`WRITE_QUEUE_MAX`]; nothing was queued. `write_failed`.
    QueueFull,
    /// The pane's PTY is closed. `write_failed`.
    Closed,
}

/// Why `PaneStore::key` refused. Added in stage A: §3.3 names the type but does not define it.
#[derive(Debug)]
pub enum KeyError {
    /// `no_such_pane`.
    NoSuchPane,
    /// The name is not in `protocol/keys.json`; nothing was written. `unknown_key`.
    UnknownKey,
    /// Queueing the encoded bytes failed. `write_failed`.
    Write(WriteError),
}

/// Why `PaneStore::kill` failed. Added in stage A: §3.3 names the type but does not define it.
#[derive(Debug)]
pub enum KillError {
    /// `no_such_pane`.
    NoSuchPane,
    /// The process group was still alive at `timeout_ms` + 500 ms; the pane stays. `kill_timeout`.
    KillTimeout,
    /// Signalling the group failed for a reason other than "no such process". `internal`.
    Signal(io::Error),
}

/// What the per-pane reader threads report (B5 seam, resume.md §5.1). Consumed by the event loop (B6).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PaneEvent {
    /// Bytes from one PTY read; pass them to [`PaneStore::feed_output`].
    Output { pane: PaneId, bytes: Vec<u8> },
    /// The PTY master hit EOF / EIO: the child side is gone or going. Call [`PaneStore::reap_exited`] (the
    /// child may become waitable only a moment later — SIGCHLD is the backstop).
    Closed { pane: PaneId },
}

/// One `pane.kill` in progress (B5 seam, resume.md §5.2). Plain data, no borrow of the store: the caller drops the
/// `State` lock between [`PaneStore::kill_step`] calls and sleeps for the returned [`KillPoll::Wait`].
#[derive(Debug, Clone)]
pub struct KillTicket {
    pane: PaneId,
    pgid: u32,
    /// The PTY's foreground process group at the request when it is not the pane's own (a job an interactive
    /// shell put in a group of its own), read before the first signal: it is signalled with the pane's group
    /// while it is verifiably still in the pane's session (PROTOCOL.md §4.13).
    fg: Option<u32>,
    started: Instant,
    timeout_ms: u32,
    /// Index into [`KILL_SCHEDULE_MS`] of the next step not yet sent.
    next_step: usize,
}

impl KillTicket {
    pub fn pane(&self) -> &str {
        &self.pane
    }

    pub fn pgid(&self) -> u32 {
        self.pgid
    }

    /// `timeout_ms` + grace after the request: the `kill_timeout` deadline.
    fn deadline(&self) -> Instant {
        self.started
            + Duration::from_millis(u64::from(self.timeout_ms) + u64::from(KILL_REAP_GRACE_MS))
    }

    /// When the next allowed step is due, if any is left.
    fn next_due(&self) -> Option<Instant> {
        let at = *KILL_SCHEDULE_MS.get(self.next_step)?;
        (at <= self.timeout_ms).then(|| self.started + Duration::from_millis(u64::from(at)))
    }
}

/// Result of one [`PaneStore::kill_step`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KillPoll {
    /// The process group is gone and the child reaped; the pane was removed from the store (the caller removes
    /// it from the layout and rewrites `panes.json`). Also returned when the pane had already left the store.
    Done,
    /// Call `kill_step` again after this long.
    Wait(Duration),
    /// The deadline passed with the group still alive: `kill_timeout`; the pane stays, no further signals.
    TimedOut,
}

/// A pane that exited on its own, as [`PaneStore::reap_exited`] reports it (A42-LINUX-RESTORE-FAILURE-04). The
/// caller needs the id, the title the pane had and what the child exited with to tell a sidebar restore attempt
/// from any other pane — a title alone is not an identity. `status` is `None` when the exit code is unknown, and
/// `tail` is the pane's last screen lines, so why a restore failed stays traceable after the pane is gone.
#[derive(Debug)]
pub struct Exited {
    pub pane: PaneId,
    pub title: String,
    pub status: Option<portable_pty::ExitStatus>,
    pub tail: String,
    /// The PTY had not reached EOF when the pane was finalised (a background child still holds the slave): `tail`
    /// is what had been parsed by then, and output that arrives later is not included (P-04-REAP).
    pub pty_open: bool,
}

/// Screen lines kept from an exiting pane for the log and the status line.
pub const EXIT_TAIL_LINES: usize = 8;

/// Every live pane of this server generation.
pub struct PaneStore {
    epoch: String,
    seq: u32,
    scrollback: usize,
    /// `--theme`: the palette every pane's parser answers OSC 10 / 11 / 12 / 4 with (PROTOCOL.md §9.1).
    palette: Palette,
    /// `--record`: at spawn one `RecordFile` per pane is opened from it and handed to the pane's PTY reader thread
    /// (B20 `record-hook`, [`read_loop`]).
    record: Option<Recorder>,
    panes: BTreeMap<PaneId, Pane>,
    events_tx: mpsc::Sender<PaneEvent>,
    events_rx: Option<mpsc::Receiver<PaneEvent>>,
}

/// One pane's state.
struct Pane {
    info: PaneInfo,
    child: PtyChild,
    term: Box<dyn TermParser + Send>,
    queue: Arc<WriteQueue>,
    /// The PTY child was reaped (it never is signalled again through `pid`; the group only while it is alive).
    reaped: bool,
    /// The reaped child's exit status; `None` = still running or the OS gave no usable status.
    exit: Option<portable_pty::ExitStatus>,
    /// The latest deadline of a kill in progress: until then `reap_exited` leaves the pane to the kill.
    killing_until: Option<Instant>,
    /// The pane's PTY reached EOF (its reader thread finished and sent [`PaneEvent::Closed`]). Set by the reader
    /// thread itself, so `reap_exited` can finalise in the same turn the exit is observed without waiting for the
    /// event loop to deliver anything else (A42-LINUX-RESTORE-FAILURE-04 P-04-REAP).
    eof: Arc<AtomicBool>,
    /// Mirror of the layout: the selected tab of its group and not hidden by another pane's zoom.
    displayed: bool,
    /// M4: the fleet page this pane belongs to (`""` = the root page; design m4 R4).
    page: String,
}

impl Drop for Pane {
    fn drop(&mut self) {
        self.queue.close();
    }
}

/// A pane's pending input. `pending` counts queued bytes plus the chunk the writer thread is writing.
struct WriteQueue {
    state: Mutex<QueueState>,
    ready: Condvar,
}

struct QueueState {
    chunks: VecDeque<Vec<u8>>,
    pending: usize,
    closed: bool,
}

impl WriteQueue {
    fn new() -> Arc<WriteQueue> {
        Arc::new(WriteQueue {
            state: Mutex::new(QueueState {
                chunks: VecDeque::new(),
                pending: 0,
                closed: false,
            }),
            ready: Condvar::new(),
        })
    }

    /// Queue `bytes` whole or not at all.
    fn push(&self, bytes: Vec<u8>) -> Result<(), WriteError> {
        if bytes.is_empty() {
            return Ok(());
        }
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if st.closed {
            return Err(WriteError::Closed);
        }
        if st.pending + bytes.len() > WRITE_QUEUE_MAX {
            return Err(WriteError::QueueFull);
        }
        st.pending += bytes.len();
        st.chunks.push_back(bytes);
        self.ready.notify_one();
        Ok(())
    }

    fn close(&self) {
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        st.closed = true;
        st.chunks.clear();
        st.pending = 0;
        self.ready.notify_all();
    }

    /// The writer thread: blocks on the PTY, never on the caller.
    fn run(&self, mut out: Box<dyn Write + Send>) {
        loop {
            let chunk = {
                let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
                loop {
                    if st.closed {
                        return;
                    }
                    if let Some(c) = st.chunks.pop_front() {
                        break c;
                    }
                    st = self.ready.wait(st).unwrap_or_else(|e| e.into_inner());
                }
            };
            let ok = out.write_all(&chunk).and_then(|()| out.flush()).is_ok();
            let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if !ok {
                st.closed = true;
                st.chunks.clear();
                st.pending = 0;
                return;
            }
            st.pending = st.pending.saturating_sub(chunk.len());
        }
    }
}

/// The pane's PTY reader thread. B20 `record-hook`, the single recording point: under `--record` each successful
/// read is appended to `rec` (raw bytes, `offset_us` = time since `spawned_at`) before its [`PaneEvent::Output`]
/// is sent; a write error logs one warning and stops this pane's recording only.
fn read_loop(
    pane: PaneId,
    mut input: Box<dyn Read + Send>,
    tx: mpsc::Sender<PaneEvent>,
    mut rec: Option<RecordFile>,
    spawned_at: Instant,
    eof: Arc<AtomicBool>,
) {
    let mut buf = vec![0u8; READ_BUF];
    loop {
        match input.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                if let Some(f) = rec.as_mut() {
                    let offset_us =
                        u64::try_from(spawned_at.elapsed().as_micros()).unwrap_or(u64::MAX);
                    if let Err(e) = f.append(offset_us, &buf[..n]) {
                        warn(&format!("recording of pane {pane} stopped: {e}"));
                        rec = None;
                    }
                }
                let ev = PaneEvent::Output {
                    pane: pane.clone(),
                    bytes: buf[..n].to_vec(),
                };
                if tx.blocking_send(ev).is_err() {
                    return;
                }
            }
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
    // Every read this pane will ever produce has been sent: the store may now finalise a reaped child in the same
    // turn it observes the exit, without waiting for another PTY / SIGCHLD / other-pane event (P-04-REAP).
    eof.store(true, Ordering::Release);
    let _ = tx.blocking_send(PaneEvent::Closed { pane });
}

fn proto_group(g: fleetmux_layout::Group) -> Group {
    match g {
        fleetmux_layout::Group::Supervisor => Group::Supervisor,
        fleetmux_layout::Group::Lower => Group::Lower,
        fleetmux_layout::Group::Workers => Group::Workers,
    }
}

fn term_mode(mode: WriteMode) -> fleetmux_term::WriteMode {
    match mode {
        WriteMode::Raw => fleetmux_term::WriteMode::Raw,
        WriteMode::Bracketed => fleetmux_term::WriteMode::Bracketed,
        WriteMode::Paste => fleetmux_term::WriteMode::Paste,
    }
}

/// One `server.log` line on stderr (`--daemonize` redirects it there): `<RFC 3339 UTC> WARN <message>`.
fn warn(msg: &str) {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let (days, rem) = (secs / 86_400, secs % 86_400);
    // Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
    let z = days as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    eprintln!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z WARN {msg}",
        rem / 3600,
        rem / 60 % 60,
        rem % 60
    );
}

impl PaneStore {
    /// An empty store. Pane ids are `<epoch>-<seq>` (PROTOCOL.md §1.4, `paths::pane_id`); each parser keeps
    /// `scrollback` lines; `record` = `--record`, when given; each parser answers colour queries with the cmux
    /// default palette (see [`PaneStore::with_palette`]).
    pub fn new(epoch: String, scrollback: usize, record: Option<Recorder>) -> Self {
        PaneStore::with_palette(epoch, scrollback, record, Palette::CMUX)
    }

    /// [`PaneStore::new`] with `palette` as the colour palette of every pane's parser (`--theme`).
    pub fn with_palette(
        epoch: String,
        scrollback: usize,
        record: Option<Recorder>,
        palette: Palette,
    ) -> Self {
        let (events_tx, events_rx) = mpsc::channel(EVENT_CAPACITY);
        PaneStore {
            epoch,
            seq: 0,
            scrollback,
            palette,
            record,
            panes: BTreeMap::new(),
            events_tx,
            events_rx: Some(events_rx),
        }
    }

    /// The receiving end of every pane's [`PaneEvent`]s; `Some` exactly once (the event loop takes it).
    pub fn take_events(&mut self) -> Option<mpsc::Receiver<PaneEvent>> {
        self.events_rx.take()
    }

    /// The id the next successful [`PaneStore::spawn`] will assign; does not reserve it. A failed spawn does not
    /// use up the number, so the caller can place the id in the layout first (to size the PTY from its group's
    /// rectangle) and remove it again if the spawn fails.
    pub fn next_pane_id(&self) -> PaneId {
        format!("{}-{}", self.epoch, self.seq + 1)
    }

    /// Spawn a pane on a `cols` × `rows` PTY (PROTOCOL.md §4.4). Placement in the layout is the caller's; until
    /// [`PaneStore::sync_layout`] runs, the record says `workers` / index 0 / not focused.
    pub fn spawn(&mut self, s: &Spawn, cols: u16, rows: u16) -> Result<PaneInfo, SpawnError> {
        // The parser first: nothing to undo if it cannot be built.
        let term = Box::new(fleetmux_term::alacritty::with_palette(
            cols,
            rows,
            self.scrollback,
            self.palette,
        ));
        let cwd = s.cwd.as_deref().map(Path::new);
        let mut child = pty::spawn(&s.argv, cwd, &s.env, cols, rows)?;
        let spawned_at = Instant::now();
        let id = self.next_pane_id();
        let queue = WriteQueue::new();
        let eof = Arc::new(AtomicBool::new(false));
        if let Err(e) = self.start_threads(&id, &child, &queue, spawned_at, eof.clone()) {
            queue.close();
            let _ = pty::signal_group(child.pgid, libc::SIGKILL);
            while matches!(child.try_reap(), Reaped::No) {
                std::thread::sleep(Duration::from_millis(5));
            }
            return Err(SpawnError::Io(e));
        }
        self.seq += 1;

        let info = PaneInfo {
            pane: id.clone(),
            title: s.title.clone(),
            ty: s.ty.clone(),
            group: Group::Workers,
            index: 0,
            pid: child.pid,
            pgid: child.pgid,
            tty: child.tty.clone(),
            cols,
            rows,
            focused: false,
            unseen: false,
            scrollback: 0,
        };
        self.panes.insert(
            id,
            Pane {
                info: info.clone(),
                child,
                term,
                queue,
                reaped: false,
                exit: None,
                killing_until: None,
                eof,
                displayed: false,
                page: String::new(),
            },
        );
        Ok(info)
    }

    /// The pane's writer thread (its queue → PTY) and reader thread (PTY → [`PaneEvent`]s). Under `--record` the
    /// reader thread gets the pane's `RecordFile`; a `.rec` left by a failed start is removed.
    fn start_threads(
        &self,
        id: &str,
        child: &PtyChild,
        queue: &Arc<WriteQueue>,
        spawned_at: Instant,
        eof: Arc<AtomicBool>,
    ) -> io::Result<()> {
        let reader = child.reader()?;
        let writer = child.writer()?;
        let q = queue.clone();
        std::thread::Builder::new()
            .name(format!("pty-w {id}"))
            .spawn(move || q.run(writer))?;
        let (pane, tx) = (id.to_string(), self.events_tx.clone());
        let rec = self.record.as_ref().and_then(|r| match r.open(id) {
            Ok(f) => Some(f),
            Err(e) => {
                warn(&format!("recording of pane {id} disabled: {e}"));
                None
            }
        });
        let started = std::thread::Builder::new()
            .name(format!("pty-r {id}"))
            .spawn(move || read_loop(pane, reader, tx, rec, spawned_at, eof));
        if let Err(e) = started {
            if let Some(r) = &self.record {
                r.remove(id);
            }
            return Err(e);
        }
        Ok(())
    }

    /// Every live pane. A pane is reported here until [`PaneStore::reap_exited`] finalises it in the turn that
    /// observes its reaped child, and the event loop removes it from the layout in that same turn, so `list` and
    /// the layout never disagree (P-04-REAP).
    pub fn list(&self) -> Vec<PaneInfo> {
        self.panes.values().map(|p| p.info.clone()).collect()
    }

    /// Whether the store still holds this pane's PTY/parser — including a pane reaped this turn whose last output
    /// is still in flight (A42-LINUX-RESTORE-FAILURE-04). Protocol callers want [`PaneStore::info`] instead.
    pub fn contains(&self, pane: &str) -> bool {
        self.panes.contains_key(pane)
    }

    pub fn info(&self, pane: &str) -> Option<PaneInfo> {
        self.panes.get(pane).map(|p| p.info.clone())
    }

    /// The foreground process group of `pane`'s PTY (plan §14.2: `tcgetpgrp` on the master). `Some(pgid)` equal to
    /// the pane's own `pgid` means the shell sits at its prompt; anything else means a program is running in front
    /// of it. `None` when the pane is unknown or the PTY reports no group.
    pub fn foreground_pgid(&self, pane: &str) -> Option<u32> {
        self.panes.get(pane)?.child.process_group_leader()
    }

    /// Queue `data` for the pane's PTY under `mode` (PROTOCOL.md §4.7); never blocks on the PTY. Returns the
    /// number of `data` bytes accepted (paste markers not counted).
    pub fn write(&mut self, pane: &str, data: &[u8], mode: WriteMode) -> Result<usize, WriteError> {
        let p = self.panes.get(pane).ok_or(WriteError::NoSuchPane)?;
        let bytes = match mode {
            WriteMode::Raw => data.to_vec(),
            _ => {
                let (bytes, warning) =
                    fleetmux_term::wrap_paste(data, term_mode(mode), &p.term.modes());
                if warning.is_some() {
                    warn(&format!(
                        "bracketed paste sent but pane {pane} has 2004 off"
                    ));
                }
                bytes
            }
        };
        p.queue.push(bytes)?;
        Ok(data.len())
    }

    /// Queue the encoding of key `name` for the inner program's current modes (PROTOCOL.md §4.8).
    pub fn key(&mut self, pane: &str, name: &str) -> Result<(), KeyError> {
        let p = self.panes.get(pane).ok_or(KeyError::NoSuchPane)?;
        let bytes = fleetmux_term::encode_key(name, &p.term.modes()).ok_or(KeyError::UnknownKey)?;
        p.queue.push(bytes).map_err(KeyError::Write)
    }

    /// The last `lines` physical rows of scrollback + screen (PROTOCOL.md §4.9); `None` = no such pane.
    pub fn read_text(&self, pane: &str, lines: usize) -> Option<String> {
        self.panes.get(pane).map(|p| p.term.text(lines))
    }

    /// Set the tab title; `false` = no such pane.
    pub fn rename(&mut self, pane: &str, title: &str) -> bool {
        match self.panes.get_mut(pane) {
            Some(p) => {
                p.info.title = title.to_string();
                true
            }
            None => false,
        }
    }

    /// Resize the PTY (`TIOCSWINSZ`) and the parser; `false` = no such pane.
    pub fn resize(&mut self, pane: &str, cols: u16, rows: u16) -> bool {
        let Some(p) = self.panes.get_mut(pane) else {
            return false;
        };
        if (p.info.cols, p.info.rows) != (cols, rows) {
            if let Err(e) = p.child.resize(cols, rows) {
                warn(&format!("resize pane {pane} to {cols}x{rows}: {e}"));
            }
            p.term.resize(cols, rows);
            p.info.cols = cols;
            p.info.rows = rows;
            p.info.scrollback = p.term.scrollback_len() as u32;
        }
        true
    }

    /// SIGHUP / SIGTERM / SIGKILL the pane's process group per PROTOCOL.md §4.13 (R4) and remove the pane once
    /// its child is reaped. On `KillTimeout` the pane stays.
    ///
    /// Holds `&mut self` across its sleeps: the server's handlers use [`PaneStore::kill_begin`] /
    /// [`PaneStore::kill_step`] instead and sleep outside the `State` lock. Same state machine.
    pub async fn kill(&mut self, pane: &str, timeout_ms: u32) -> Result<(), KillError> {
        let mut t = self.kill_begin(pane, timeout_ms)?;
        loop {
            match self.kill_step(&mut t)? {
                KillPoll::Done => return Ok(()),
                KillPoll::TimedOut => return Err(KillError::KillTimeout),
                KillPoll::Wait(d) => tokio::time::sleep(d).await,
            }
        }
    }

    /// Start a `pane.kill`: check the pane, send the t = 0 step (SIGHUP) and return the ticket. A later kill of
    /// the same pane (e.g. after `kill_timeout`) starts again at SIGHUP.
    pub fn kill_begin(&mut self, pane: &str, timeout_ms: u32) -> Result<KillTicket, KillError> {
        let p = self.panes.get_mut(pane).ok_or(KillError::NoSuchPane)?;
        let fg = p
            .child
            .process_group_leader()
            .filter(|&g| g != p.info.pgid && in_session(g, p.info.pid));
        let mut t = KillTicket {
            pane: pane.to_string(),
            pgid: p.info.pgid,
            fg,
            started: Instant::now(),
            timeout_ms,
            next_step: 0,
        };
        let deadline = t.deadline();
        p.killing_until = Some(p.killing_until.map_or(deadline, |d| d.max(deadline)));
        // A step already due (always the SIGHUP) is sent now; `Done` here is reported by the next step.
        self.kill_step(&mut t)?;
        Ok(t)
    }

    /// Advance a kill: reap if possible, send every step now due (only while the group is verifiably ours), and
    /// say what to do next. Never signals after the pane left the store or once its group is gone, so a reused
    /// pgid is never hit; never signals during the grace (R4).
    ///
    /// The recorded foreground group ([`KillTicket`] `fg`) gets the same steps, each only while its leader is
    /// still in the pane's session. The pane is done once its own group is gone and the foreground group is gone
    /// too or no longer verifiable; at the deadline a foreground group still alive does not hold the pane (only the
    /// pane's own group makes a `kill_timeout`): it is logged and the pane is done.
    pub fn kill_step(&mut self, t: &mut KillTicket) -> Result<KillPoll, KillError> {
        let Some(p) = self.panes.get_mut(&t.pane) else {
            // Removed by a concurrent kill or by `reap_exited`: nothing left to signal.
            return Ok(KillPoll::Done);
        };
        if !p.reaped {
            if let Reaped::Yes { status } = p.child.try_reap() {
                p.reaped = true;
                p.exit = status;
            }
        }
        // While the child is unreaped its pid (= pgid) cannot be reused. After the reap the group still counts
        // only while it answers and no new process holds the leader's pid.
        let ours = !p.reaped || (pty::group_alive(p.info.pgid) && !pid_exists(p.info.pid));
        let fg =
            t.fg.filter(|&g| pty::group_alive(g) && in_session(g, p.info.pid));
        let now = Instant::now();
        if !ours && (fg.is_none() || now >= t.deadline()) {
            if let Some(g) = fg {
                warn(&format!(
                    "pane {} killed; its foreground job's process group {g} outlived the kill",
                    t.pane
                ));
            }
            self.panes.remove(&t.pane);
            return Ok(KillPoll::Done);
        }
        if now >= t.deadline() {
            return Ok(KillPoll::TimedOut);
        }
        while let Some(due) = t.next_due() {
            if due > now {
                break;
            }
            let sig = KILL_SIGNALS[t.next_step];
            for g in [ours.then_some(p.info.pgid), fg].into_iter().flatten() {
                match pty::signal_group(g, sig) {
                    Ok(()) => {}
                    // ESRCH: the group just ended. EPERM: nothing in it could be signalled — on macOS that is
                    // what `kill(-pgid)` returns while only a zombie is left. Either way the next poll decides.
                    Err(e) if matches!(e.raw_os_error(), Some(libc::ESRCH | libc::EPERM)) => {}
                    Err(e) => return Err(KillError::Signal(e)),
                }
            }
            t.next_step += 1;
        }
        let until = t.next_due().unwrap_or(t.deadline()).min(t.deadline());
        let wait = until.saturating_duration_since(now).min(KILL_POLL);
        Ok(KillPoll::Wait(wait.max(Duration::from_millis(1))))
    }

    /// Bytes read from the pane's PTY: feed the parser, queue its replies (DSR / DA / OSC answers) to the PTY, and
    /// mark the pane unseen when it is not displayed. `bytes` empty = only run the parser's timers (the `?2026`
    /// 150 ms flush). Nothing is recorded here: `--record` appends in the PTY reader thread ([`read_loop`]).
    pub fn feed_output(&mut self, pane: &str, bytes: &[u8]) {
        let Some(p) = self.panes.get_mut(pane) else {
            return;
        };
        p.term.feed(bytes);
        let replies = p.term.take_replies();
        if let Err(e) = p.queue.push(replies) {
            warn(&format!("terminal reply to pane {pane} dropped: {e:?}"));
        }
        p.info.scrollback = p.term.scrollback_len() as u32;
        if !bytes.is_empty() && !p.displayed {
            p.info.unseen = true;
        }
    }

    pub fn term(&self, pane: &str) -> Option<&dyn TermParser> {
        self.panes
            .get(pane)
            .map(|p| p.term.as_ref() as &dyn TermParser)
    }

    pub fn term_mut(&mut self, pane: &str) -> Option<&mut dyn TermParser> {
        match self.panes.get_mut(pane) {
            Some(p) => Some(p.term.as_mut()),
            None => None,
        }
    }

    /// Reap every child that exited on its own (`waitpid` without blocking) and drop its pane; what was removed,
    /// with the title it had and its exit status (A42-LINUX-RESTORE-FAILURE-04).
    ///
    /// A pane is finalised in the turn that observes its reaped child — whether or not its PTY already reached EOF.
    /// The EOF flag (`eof`, set by the reader thread itself) only says whether the tail is complete: with EOF the
    /// parser has everything the child wrote; without it a background child still holds the slave, so `tail` is
    /// what had been parsed by then and later output is dropped (`pty_open`). Nothing waits for the PTY to close:
    /// a launcher that exits nonzero while a child keeps the slave must fail its restore attempt promptly.
    ///
    /// No sleeping, no polling and no second dispatcher is involved; the SIGCHLD path drains the events its reader
    /// threads already sent and then calls this, so the pane's last output is normally in the parser first.
    ///
    /// A pane with a kill in progress is left to the kill (it removes the pane once the whole group is gone).
    pub fn reap_exited(&mut self) -> Vec<Exited> {
        let now = Instant::now();
        let mut gone = Vec::new();
        let mut drop = Vec::new();
        for (id, p) in self.panes.iter_mut() {
            if p.killing_until.is_some_and(|d| d > now) {
                continue;
            }
            if !p.reaped {
                match p.child.try_reap() {
                    Reaped::Yes { status } => {
                        p.reaped = true;
                        p.exit = status;
                    }
                    Reaped::No => continue,
                }
            }
            gone.push(Exited {
                pane: id.clone(),
                title: p.info.title.clone(),
                status: p.exit.clone(),
                pty_open: !p.eof.load(Ordering::Acquire),
                // The whole visible screen, not just its last rows: a short error sits at the top of an otherwise
                // blank screen, and `text(n)` counts from the bottom (`exit_reason` picks the last non-empty line).
                tail: p.term.text(usize::from(p.info.rows).max(EXIT_TAIL_LINES)),
            });
            drop.push(id.clone());
        }
        for id in &drop {
            self.panes.remove(id);
        }
        gone
    }

    /// Atomically rewrite `panes.json` (PROTOCOL.md §8.2): `<path>.tmp` in the same directory, mode 0600,
    /// `fsync`, `rename`.
    pub fn write_panes_json(&self, path: &Path) -> io::Result<()> {
        let panes: Vec<serde_json::Value> = self
            .panes
            .values()
            .map(|p| {
                let mut line = serde_json::json!({
                    "pane": p.info.pane,
                    "pid": p.info.pid,
                    "pgid": p.info.pgid,
                    "start_time": p.child.start_time,
                    "tty": p.info.tty,
                    "group": p.info.group,
                    "index": p.info.index,
                    "title": p.info.title,
                    "type": p.info.ty,
                });
                // M4 (design m4 R4): only a pane outside the root page carries `page`, so a tree without child
                // pages writes the M3 file byte for byte.
                if !p.page.is_empty() {
                    line["page"] = serde_json::json!(p.page);
                }
                line
            })
            .collect();
        let doc = serde_json::json!({ "epoch": self.epoch, "panes": panes });
        let mut tmp = path.as_os_str().to_owned();
        tmp.push(".tmp");
        let tmp = Path::new(&tmp);
        let result = (|| {
            let mut f = std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .open(tmp)?;
            serde_json::to_writer(&mut f, &doc)?;
            f.write_all(b"\n")?;
            f.sync_all()?;
            std::fs::rename(tmp, path)
        })();
        if result.is_err() {
            let _ = std::fs::remove_file(tmp);
        }
        result
    }

    /// The pane got output while not displayed (the tab's `•`, `PaneInfo.unseen`).
    pub fn mark_unseen(&mut self, pane: &str) {
        if let Some(p) = self.panes.get_mut(pane) {
            p.info.unseen = true;
        }
    }

    /// The pane is displayed again.
    pub fn clear_unseen(&mut self, pane: &str) {
        if let Some(p) = self.panes.get_mut(pane) {
            p.info.unseen = false;
        }
    }

    /// Mirror the layout into every pane record (B5 seam, resume.md §5.3): `group`, `index`, `focused` (selected
    /// tab of the focused group) and `displayed` (selected tab of its group and not hidden by another pane's
    /// zoom). A displayed pane's `unseen` is cleared. Call after every layout change; `list` / `info` /
    /// `write_panes_json` then report the layout's values. Panes the layout does not hold keep their values.
    pub fn sync_layout(&mut self, layout: &fleetmux_layout::Layout) {
        self.sync_layout_shown(layout, true);
    }

    /// M4 (design m4 F8 / R4): as [`PaneStore::sync_layout`] for the layout of a page that is not the current page:
    /// group / index / focused follow that page's own layout, and none of its panes counts as displayed (their
    /// output marks them `unseen`, as a background tab's does).
    pub fn sync_layout_hidden(&mut self, layout: &fleetmux_layout::Layout) {
        self.sync_layout_shown(layout, false);
    }

    /// M4: the page `id` belongs to (`""` = root), or `None` for no such pane.
    pub fn page_of(&self, id: &str) -> Option<&str> {
        self.panes.get(id).map(|p| p.page.as_str())
    }

    /// M4: move pane `id` to `page` in the ledger (the layouts are the caller's); `false` = no such pane.
    pub fn set_page(&mut self, id: &str, page: &str) -> bool {
        match self.panes.get_mut(id) {
            Some(p) => {
                p.page = page.to_string();
                true
            }
            None => false,
        }
    }

    fn sync_layout_shown(&mut self, layout: &fleetmux_layout::Layout, shown: bool) {
        let focus = layout.focus();
        let zoom = layout.zoom().cloned();
        for g in fleetmux_layout::Group::ALL {
            let selected = layout.selected(g).cloned();
            for (index, id) in layout.group(g).iter().enumerate() {
                let Some(p) = self.panes.get_mut(id) else {
                    continue;
                };
                let is_selected = selected.as_ref() == Some(id);
                p.info.group = proto_group(g);
                p.info.index = index as u32;
                p.info.focused = is_selected && g == focus;
                p.displayed = shown
                    && match &zoom {
                        Some(z) => z == id,
                        None => is_selected,
                    };
                if p.displayed {
                    p.info.unseen = false;
                }
            }
        }
    }
}

/// Whether process `pid` exists and belongs to session `sid` (`getsid`). The pane's child leads its session, so
/// its pid is the session id, and it stays that while any member of the session lives — even after the child is
/// reaped. A pgid reused by a process outside the pane's session never passes.
fn in_session(pid: u32, sid: u32) -> bool {
    pid > 1
        && pid <= i32::MAX as u32
        && unsafe { libc::getsid(pid as libc::pid_t) } == sid as libc::pid_t
}

/// Whether a process with this pid exists (any owner).
fn pid_exists(pid: u32) -> bool {
    pid > 0
        && pid <= i32::MAX as u32
        && (unsafe { libc::kill(pid as i32, 0) } == 0
            || io::Error::last_os_error().raw_os_error() == Some(libc::EPERM))
}

#[cfg(test)]
mod record_hook_tests {
    use super::*;
    use fleetmux_proto::record;

    fn spawn_args(argv: &[&str]) -> Spawn {
        Spawn {
            argv: argv.iter().map(|s| s.to_string()).collect(),
            cwd: None,
            env: BTreeMap::new(),
            title: "rec".to_string(),
            ty: "worker".to_string(),
            group: None,
            anchor: None,
            before: false,
        }
    }

    fn rec_entries(path: &Path) -> Vec<record::Entry> {
        let mut f = std::fs::File::open(path).unwrap();
        let mut es = Vec::new();
        while let Some(e) = record::read(&mut f).unwrap() {
            es.push(e);
        }
        es
    }

    /// B20: under `--record` the PTY reader thread records every read before sending its event, with no
    /// `feed_output` call at all; bytes later fed through `feed_output` are not recorded a second time.
    #[test]
    fn pty_reads_are_recorded_before_their_event_without_feed_output() {
        let dir = std::env::temp_dir().join(format!("frh{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut store = PaneStore::new("rh".into(), 100, Some(Recorder::new(dir.clone())));
        let mut rx = store.take_events().unwrap();
        let info = store
            .spawn(
                &spawn_args(&["bash", "-c", "printf 'one\\n'; sleep 0.2; printf 'two\\n'"]),
                80,
                24,
            )
            .unwrap();
        let path = dir.join(format!("{}.rec", info.pane));
        assert!(path.exists(), "opened at spawn");

        let mut sent = Vec::new();
        let end = Instant::now() + Duration::from_secs(5);
        loop {
            match rx.try_recv() {
                Ok(PaneEvent::Output { bytes, .. }) => {
                    sent.extend_from_slice(&bytes);
                    let on_disk: Vec<u8> = rec_entries(&path)
                        .iter()
                        .flat_map(|e| e.bytes.clone())
                        .collect();
                    assert!(
                        on_disk.starts_with(&sent),
                        "a read is recorded before its event is sent: disk {on_disk:?}, events {sent:?}"
                    );
                }
                Ok(PaneEvent::Closed { .. }) => break,
                Err(_) => {
                    assert!(Instant::now() < end, "no Closed within 5 s");
                    std::thread::sleep(Duration::from_millis(10));
                }
            }
        }
        store.feed_output(&info.pane, b"FED-NOT-RECORDED");
        store.feed_output(&info.pane, &[]);

        let es = rec_entries(&path);
        let got: Vec<u8> = es.iter().flat_map(|e| e.bytes.clone()).collect();
        assert_eq!(got, sent, "exactly the raw PTY reads, in order, once");
        assert!(String::from_utf8_lossy(&got).contains("two"));
        assert!(es.iter().all(|e| !e.bytes.is_empty()));
        assert!(es.windows(2).all(|w| w[0].offset_us <= w[1].offset_us));
        assert!(
            es.last().unwrap().offset_us >= 150_000,
            "offsets are time since spawn: {es:?}"
        );
        while store.info(&info.pane).is_some() {
            store.reap_exited();
            assert!(
                Instant::now() < end + Duration::from_secs(5),
                "pane never reaped"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
