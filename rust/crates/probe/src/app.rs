//! The probe's life (README "Start-up, in this order", "Exit"): raw mode, modes on, `start`, draw, the
//! three start-up queries, `ready`, then read / log / redraw until `q`, SIGTERM or `--exit-after`.
use super::input::{Await, Parser, Token};
use super::log::{J, Log};
use super::screen;
use super::sys::{self, SignalPipe};
use super::{MouseMode, Opts};
use std::fs::File;
use std::io::{self, Read, Write};
use std::mem::ManuallyDrop;
use std::os::fd::FromRawFd;
use std::path::Path;
use std::process::ExitCode;
use std::time::{Duration, Instant};

/// How long each start-up query waits for its reply.
const QUERY_WAIT: Duration = Duration::from_secs(2);
/// How long a sequence cut off at the end of a read is held before its bytes count as keys.
const HOLD: Duration = Duration::from_millis(30);
/// `--flood` batch interval (about 10 batches a second).
const FLOOD_TICK: Duration = Duration::from_millis(100);

#[derive(Clone, Copy)]
enum Quit {
    Q,
    Term,
    ExitAfter,
}

impl Quit {
    fn reason(self) -> &'static str {
        match self {
            Quit::Q => "q",
            Quit::Term => "sigterm",
            Quit::ExitAfter => "exit-after",
        }
    }
}

struct App<'a> {
    opts: &'a Opts,
    log: Log,
    tty_in: ManuallyDrop<File>,
    tty_out: ManuallyDrop<File>,
    signals: SignalPipe,
    parser: Parser,
    /// Start-up replies that arrived in the same read as the one being waited for (kitty + DA1).
    stash: Vec<Token>,
    /// When the bytes held by `parser` arrived.
    held_since: Option<Instant>,
    cols: u16,
    rows: u16,
    last_ev: Option<&'static str>,
    /// DEC private modes turned on, in order (turned off in reverse).
    modes: Vec<u16>,
    alt_screen: bool,
    raw: bool,
    exit_at: Option<Instant>,
    quit: Option<Quit>,
    /// Set at `ready` with `--flood`: when flooding started, how many lines went out, the last batch.
    flood: Option<(Instant, u64, Instant)>,
}

/// Runs the probe; see `super::run`.
pub fn run(opts: &Opts, log_path: &Path) -> ExitCode {
    let started = Instant::now();
    let log = match Log::open(log_path, started) {
        Ok(log) => log,
        Err(e) => {
            eprintln!("ft-probe: cannot open FT_PROBE_LOG {}: {e}", log_path.display());
            return ExitCode::from(2);
        }
    };
    let signals = match SignalPipe::install() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("ft-probe: cannot install signal handlers: {e}");
            return ExitCode::from(1);
        }
    };
    let mut app = App {
        opts,
        log,
        // fds 0 / 1 are the pane's tty; read and written unbuffered so `poll` sees every pending byte.
        tty_in: ManuallyDrop::new(unsafe { File::from_raw_fd(0) }),
        tty_out: ManuallyDrop::new(unsafe { File::from_raw_fd(1) }),
        signals,
        parser: Parser::default(),
        stash: Vec::new(),
        held_since: None,
        cols: 0,
        rows: 0,
        last_ev: None,
        modes: Vec::new(),
        alt_screen: false,
        raw: false,
        exit_at: opts.exit_after.map(|d| started + d),
        quit: None,
        flood: None,
    };
    let result = app.main();
    let teardown = app.teardown();
    match result.and(teardown) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("ft-probe: {e}");
            ExitCode::from(1)
        }
    }
}

fn is_awaited(tok: &Token, awaiting: Option<Await>) -> bool {
    matches!(
        (tok, awaiting),
        (Token::Dsr { .. }, Some(Await::Dsr))
            | (Token::Osc11 { .. }, Some(Await::Osc11))
            | (Token::KittyFlags { .. } | Token::Da1 { .. }, Some(Await::Kitty))
    )
}

impl App<'_> {
    fn main(&mut self) -> io::Result<()> {
        // 1. Raw mode, size, `--lines` into scrollback.
        crossterm::terminal::enable_raw_mode()?;
        self.raw = true;
        (self.cols, self.rows) = crossterm::terminal::size()?;
        if self.opts.lines > 0 {
            let mut text = String::new();
            for k in 0..self.opts.lines {
                text.push_str(&format!("LINE {k}\r\n"));
            }
            text.push_str(&"\r\n".repeat(self.rows.saturating_sub(1) as usize));
            text.push_str("\x1b[H");
            self.out(text.as_bytes())?;
        }
        // 2. Modes.
        if self.opts.lines == 0 {
            self.mode_on(1049)?;
            self.alt_screen = true;
        }
        self.mode_on(1004)?;
        if self.opts.mouse {
            self.mode_on(match self.opts.mouse_mode {
                MouseMode::Click => 1000,
                MouseMode::Drag => 1002,
                MouseMode::Motion => 1003,
            })?;
            if self.opts.sgr {
                self.mode_on(1006)?;
            }
        }
        if self.opts.paste {
            self.mode_on(2004)?;
        }
        if self.opts.app_cursor {
            self.mode_on(1)?;
        }
        // 3. `start`; 4. draw.
        let mouse_mode = match (self.opts.mouse, self.opts.mouse_mode) {
            (false, _) => J::Null,
            (true, MouseMode::Click) => J::Str("click"),
            (true, MouseMode::Drag) => J::Str("drag"),
            (true, MouseMode::Motion) => J::Str("motion"),
        };
        self.log(
            "start",
            &[],
            &[
                ("cols", J::Int(self.cols as u64)),
                ("rows", J::Int(self.rows as u64)),
                ("mouse", J::Bool(self.opts.mouse)),
                ("paste", J::Bool(self.opts.paste)),
                ("app_cursor", J::Bool(self.opts.app_cursor)),
                ("mouse_mode", mouse_mode),
                ("sgr", J::Bool(self.opts.mouse && self.opts.sgr)),
                ("alt_screen", J::Bool(self.alt_screen)),
            ],
        )?;
        // 5. Cursor position report.
        self.out(b"\x1b[6n")?;
        match self.pump(Some(Await::Dsr), Some(Instant::now() + QUERY_WAIT))? {
            Some(Token::Dsr { raw, row, col }) => {
                self.log("dsr", &raw, &[("row", J::Int(row as u64)), ("col", J::Int(col as u64))])?
            }
            _ => self.timeout("dsr")?,
        }
        // 6. Background colour.
        self.out(b"\x1b]11;?\x07")?;
        match self.pump(Some(Await::Osc11), Some(Instant::now() + QUERY_WAIT))? {
            Some(Token::Osc11 { raw, color }) => self.log("osc11", &raw, &[("color", J::Str(&color))])?,
            _ => self.timeout("osc11")?,
        }
        // 7. Kitty keyboard query, decided by whichever of its reply and the DA1 reply comes first.
        if self.opts.kitty_query {
            self.out(b"\x1b[?u\x1b[c")?;
            let deadline = Instant::now() + QUERY_WAIT;
            match self.pump(Some(Await::Kitty), Some(deadline))? {
                Some(Token::KittyFlags { raw, flags }) => {
                    // Swallow the DA1 reply that follows, so it does not show up as key input.
                    let da1 = self.pump(Some(Await::Kitty), Some(deadline))?;
                    let mut bytes = raw;
                    if let Some(Token::Da1 { raw }) = &da1 {
                        bytes.extend_from_slice(raw);
                    }
                    self.log("kitty", &bytes, &[("answered", J::Bool(true)), ("flags", J::Int(flags as u64))])?;
                }
                Some(Token::Da1 { raw }) => {
                    self.log("kitty", &raw, &[("answered", J::Bool(false)), ("flags", J::Null)])?
                }
                _ => {
                    self.timeout("kitty")?;
                    self.log("kitty", &[], &[("answered", J::Bool(false)), ("flags", J::Null)])?;
                }
            }
        }
        // 8. Ready; then serve until told to quit. A reply left over from start-up is plain input.
        for tok in std::mem::take(&mut self.stash) {
            self.input(tok)?;
        }
        self.log("ready", &[], &[])?;
        if self.opts.flood.is_some() {
            self.out(screen::flood_region(self.rows).as_bytes())?;
            let now = Instant::now();
            self.flood = Some((now, 0, now));
            self.redraw()?;
        }
        self.pump(None, None)?;
        Ok(())
    }

    /// Reads, logs and redraws until a quit reason is set, `until` passes, or (while `awaiting`) the awaited
    /// reply arrives — returned. Other input that arrives meanwhile is logged as ordinary events.
    fn pump(&mut self, awaiting: Option<Await>, until: Option<Instant>) -> io::Result<Option<Token>> {
        let stdin_fd = 0;
        let sig_fd = self.signals.fd();
        if let Some(k) = self.stash.iter().position(|t| is_awaited(t, awaiting)) {
            return Ok(Some(self.stash.remove(k)));
        }
        loop {
            if self.quit.is_some() {
                return Ok(None);
            }
            let now = Instant::now();
            if until.is_some_and(|u| now >= u) {
                return Ok(None);
            }
            let flood_at = self.flood.map(|(_, _, last)| last + FLOOD_TICK);
            let wake = [until, self.exit_at, self.held_since.map(|t| t + HOLD), flood_at]
                .into_iter()
                .flatten()
                .min();
            let ready = sys::wait_readable(&[stdin_fd, sig_fd], wake.map(|w| w.saturating_duration_since(now)))?;

            let sig = self.signals.drain();
            if sig.term {
                self.quit = Some(Quit::Term);
            }
            if sig.winch {
                self.resized()?;
            }
            if ready[0] {
                let mut buf = [0u8; 4096];
                let n = match self.tty_in.read(&mut buf) {
                    Ok(0) => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "tty closed")),
                    Ok(n) => n,
                    Err(e) if e.kind() == io::ErrorKind::Interrupted => 0,
                    Err(e) => return Err(e),
                };
                self.parser.push(&buf[..n]);
            }
            let flush = self.held_since.is_some_and(|t| Instant::now() >= t + HOLD);
            let mut got = None;
            for tok in self.parser.take(awaiting, flush) {
                if !is_awaited(&tok, awaiting) {
                    self.input(tok)?;
                } else if got.is_none() {
                    got = Some(tok);
                } else {
                    self.stash.push(tok);
                }
            }
            // The hold clock restarts with new bytes; after a flush only an unfinished paste is left, and
            // that waits for its end marker without a deadline.
            self.held_since = if !self.parser.pending() {
                None
            } else if ready[0] {
                Some(Instant::now())
            } else if flush {
                None
            } else {
                self.held_since.or(Some(Instant::now()))
            };
            if self.exit_at.is_some_and(|t| Instant::now() >= t) && self.quit.is_none() {
                self.quit = Some(Quit::ExitAfter);
            }
            self.flood_tick()?;
            if got.is_some() {
                return Ok(got);
            }
        }
    }

    /// Logs one input token (README event table) and redraws.
    fn input(&mut self, tok: Token) -> io::Result<()> {
        match tok {
            Token::Key(raw) => {
                if raw == b"q" {
                    self.quit = Some(Quit::Q);
                }
                self.log("key", &raw, &[])?;
            }
            Token::Mouse(m) => self.log(
                "mouse",
                &m.raw,
                &[
                    ("button", J::Int(m.button as u64)),
                    ("col", J::Int(m.col as u64)),
                    ("row", J::Int(m.row as u64)),
                    ("press", J::Bool(m.press)),
                    ("motion", J::Bool(m.motion)),
                    ("encoding", J::Str(if m.sgr { "sgr" } else { "x10" })),
                ],
            )?,
            Token::Paste { raw, text } => {
                self.log("paste", &raw, &[("text_hex", J::Str(&super::log::hex(&text)))])?
            }
            Token::Focus { raw, focused } => self.log("focus", &raw, &[("in", J::Bool(focused))])?,
            // Replies nobody waits for any more (a late or second answer) are plain input.
            Token::Dsr { raw, .. }
            | Token::Osc11 { raw, .. }
            | Token::KittyFlags { raw, .. }
            | Token::Da1 { raw } => self.log("key", &raw, &[])?,
        }
        Ok(())
    }

    fn resized(&mut self) -> io::Result<()> {
        (self.cols, self.rows) = crossterm::terminal::size()?;
        if self.flood.is_some() {
            self.out(screen::flood_region(self.rows).as_bytes())?;
        }
        self.log("resize", &[], &[("cols", J::Int(self.cols as u64)), ("rows", J::Int(self.rows as u64))])
    }

    fn flood_tick(&mut self) -> io::Result<()> {
        let (Some(per_s), Some((since, sent, last))) = (self.opts.flood, self.flood) else {
            return Ok(());
        };
        let now = Instant::now();
        if now < last + FLOOD_TICK {
            return Ok(());
        }
        let due = (now.duration_since(since).as_millis() as u64).saturating_mul(per_s as u64) / 1000;
        // Never more than one second's worth at once, even after a stall.
        let count = due.saturating_sub(sent).min(per_s as u64);
        if count > 0 {
            let bytes = screen::flood_lines(self.cols, self.rows, sent, count);
            self.out(&bytes)?;
        }
        self.flood = Some((since, sent + count, now));
        Ok(())
    }

    fn timeout(&mut self, query: &str) -> io::Result<()> {
        self.log("timeout", &[], &[("query", J::Str(query))])
    }

    fn log(&mut self, ev: &'static str, raw: &[u8], extra: &[(&str, J)]) -> io::Result<()> {
        self.log.event(ev, raw, extra)?;
        self.last_ev = Some(ev);
        if self.quit.is_some() { Ok(()) } else { self.redraw() }
    }

    fn redraw(&mut self) -> io::Result<()> {
        let bytes = screen::draw(self.cols, self.rows, self.last_ev, self.opts.wide);
        self.out(&bytes)
    }

    fn mode_on(&mut self, mode: u16) -> io::Result<()> {
        self.out(format!("\x1b[?{mode}h").as_bytes())?;
        if mode != 1049 {
            self.modes.push(mode);
        }
        Ok(())
    }

    fn out(&mut self, bytes: &[u8]) -> io::Result<()> {
        self.tty_out.write_all(bytes)?;
        self.tty_out.flush()
    }

    /// README "Exit": log `exit` (when quitting normally), modes off in reverse, scroll region reset, leave
    /// the alternate screen, tty restored. Runs on error paths too, best effort.
    fn teardown(&mut self) -> io::Result<()> {
        let mut result = match self.quit {
            Some(q) => self.log.event("exit", &[], &[("reason", J::Str(q.reason()))]),
            None => Ok(()),
        };
        let mut seq = String::new();
        for mode in self.modes.drain(..).rev() {
            seq.push_str(&format!("\x1b[?{mode}l"));
        }
        if self.flood.is_some() {
            seq.push_str("\x1b[r");
        }
        if self.alt_screen {
            seq.push_str("\x1b[?1049l");
        }
        if !seq.is_empty() {
            result = result.and(self.out(seq.as_bytes()));
        }
        if self.raw {
            self.raw = false;
            result = result.and(crossterm::terminal::disable_raw_mode());
        }
        result
    }
}
