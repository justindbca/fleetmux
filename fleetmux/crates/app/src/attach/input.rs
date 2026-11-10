//! The client's terminal side: raw mode + outer mouse reporting, and what to do with each server frame.
//!
//! Also the one input tokenizer both ends use ([`InputScanner`]): the server's `view::on_input` dispatches its
//! tokens, and the client runs the same scanner over the same bytes only to spot prefix + `d` (detach,
//! PROTOCOL.md §10; supervisor ruling D3: the client recognises it and sends `0x03`). Because both sides run
//! one implementation over one byte stream, they agree on prefix pairs across frames, on bytes inside outer
//! paste blocks, on a doubled prefix and on the 1 s prefix timeout.
//!
//! B7 (attach-render); B14 adds the mouse parts.
use std::io::{self, Write as _};
use std::time::{Duration, Instant};

use fleetmux_proto::view::Frame;

/// Outer terminal in raw mode (and, if asked, mouse reporting `1002` + `1006` on). Dropping it restores the
/// previous terminal state and turns mouse reporting off again.
pub struct RawGuard {
    mouse: bool,
}

/// Outer modes set while attached: alternate screen, bracketed paste (so outer pastes arrive as packets the
/// server re-wraps per the inner program's `?2004`).
const ENTER: &[u8] = b"\x1b[?1049h\x1b[?2004h";
const LEAVE: &[u8] = b"\x1b[?2004l\x1b[0m\x1b[?25h\x1b[?1049l";
const MOUSE_ON: &[u8] = b"\x1b[?1002h\x1b[?1006h";
const MOUSE_OFF: &[u8] = b"\x1b[?1006l\x1b[?1002l";

/// Enter raw mode; `mouse` = also enable `1002` + `1006` (off with `--no-mouse`).
pub fn enter_raw(mouse: bool) -> io::Result<RawGuard> {
    crossterm::terminal::enable_raw_mode()?;
    let mut out = io::stdout().lock();
    let mut seq = ENTER.to_vec();
    if mouse {
        seq.extend_from_slice(MOUSE_ON);
    }
    if let Err(e) = out.write_all(&seq).and_then(|_| out.flush()) {
        // Part of the sequence may have reached the terminal: undo all of it, best effort.
        if mouse {
            let _ = out.write_all(MOUSE_OFF);
        }
        let _ = out.write_all(LEAVE).and_then(|_| out.flush());
        let _ = crossterm::terminal::disable_raw_mode();
        return Err(e);
    }
    Ok(RawGuard { mouse })
}

impl Drop for RawGuard {
    fn drop(&mut self) {
        let mut out = io::stdout().lock();
        if self.mouse {
            let _ = out.write_all(MOUSE_OFF);
        }
        let _ = out.write_all(LEAVE);
        let _ = out.flush();
        let _ = crossterm::terminal::disable_raw_mode();
    }
}

/// What the client does with one server frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClientAction {
    /// `0x81`: write these bytes to the outer terminal as they are.
    Write(Vec<u8>),
    /// `0x83`: open this path locally (`clipboard::open_local`); empty = show "no history url".
    Open(String),
    /// `0x82`: restore the terminal, print the reason (may be empty), exit 0.
    Exit(String),
    /// `0x84` (M2.5): one JSON line for this client's web window (`attach::web::WebClient::on_server_line`).
    Web(String),
    /// `0x85` (M2.6): one overlay JSON line (PROTOCOL.md §6.3); only `fleetmux gui` asks for it.
    Overlay(String),
    /// `0x86` (gui-scrollbar): one scroll-state JSON line (PROTOCOL.md §6.3); only `fleetmux gui` asks for it.
    Scroll(String),
}

pub fn on_frame(frame: Frame) -> ClientAction {
    match frame {
        Frame::Screen(b) => ClientAction::Write(b),
        Frame::Open(p) => ClientAction::Open(p),
        Frame::Exit(r) => ClientAction::Exit(r),
        Frame::Web(line) => ClientAction::Web(line),
        Frame::Overlay(line) => ClientAction::Overlay(line),
        Frame::Scroll(line) => ClientAction::Scroll(line),
        // Client → server types coming from the server are a protocol error: leave. `0x05`
        // (`Frame::ScrollTo`) is client → server as well (gui-scrollbar), and so is `0x06` (`Frame::Inset`, S1e).
        Frame::Input(_)
        | Frame::Resize { .. }
        | Frame::Detach
        | Frame::WebReply(_)
        | Frame::ScrollTo(_)
        | Frame::Inset(_) => {
            ClientAction::Exit(String::from("protocol error: unexpected frame from server"))
        }
    }
}

/// The byte a prefix key name stands for (PROTOCOL.md §9.1 `--prefix`, `protocol/keys.json`): `C-<x>` for a
/// letter or one of `@[\]^_` (`C-b` → 0x02). `None` for anything else.
pub fn prefix_byte(name: &str) -> Option<u8> {
    let rest = name
        .strip_prefix("C-")
        .or_else(|| name.strip_prefix("Ctrl-"))?;
    let mut chars = rest.chars();
    let c = chars.next()?;
    if chars.next().is_some() || !c.is_ascii() {
        return None;
    }
    match c.to_ascii_lowercase() {
        l @ 'a'..='z' => Some(l as u8 - b'a' + 1),
        s @ ('@' | '[' | '\\' | ']' | '^' | '_') => Some(s as u8 & 0x1f),
        _ => None,
    }
}

/// After the prefix, the server waits this long for the second key (PROTOCOL.md §10).
pub const PREFIX_TIMEOUT: Duration = Duration::from_secs(1);

const PASTE_START: &[u8] = b"\x1b[200~";
const PASTE_END: &[u8] = b"\x1b[201~";
/// Longest incomplete sequence held over to the next input frame.
const MAX_CARRY: usize = 64;

/// One piece of viewer input.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Token {
    /// Ordinary bytes for the focused pane.
    Plain(Vec<u8>),
    /// Bytes inside an outer bracketed-paste block (markers removed); a block may arrive in several pieces.
    Paste(Vec<u8>),
    /// The end marker of the current paste block.
    PasteEnd,
    /// One complete SGR mouse report `ESC [ < b ; x ; y M|m`.
    Mouse(Vec<u8>),
    /// The complete key that followed the prefix (the prefix byte itself = prefix pressed twice).
    PrefixKey(Vec<u8>),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PrefixState {
    Idle,
    /// Prefix seen at this time; the next key must come within [`PREFIX_TIMEOUT`].
    Pending(Instant),
    /// The second key started in time but is incomplete (held in `carry`).
    KeyStarted,
}

/// Splits viewer input into [`Token`]s; keeps state across frames (pending prefix, open paste block,
/// incomplete escape sequences).
#[derive(Debug, Clone)]
pub struct InputScanner {
    prefix: Option<u8>,
    state: PrefixState,
    in_paste: bool,
    carry: Vec<u8>,
}

enum KeyLen {
    Complete(usize),
    Incomplete,
}

/// Length of the key starting at `data[i]`.
fn key_len(data: &[u8], i: usize) -> KeyLen {
    let n = data.len();
    let b = data[i];
    let need = |len: usize| {
        if i + len <= n {
            KeyLen::Complete(len)
        } else {
            KeyLen::Incomplete
        }
    };
    match b {
        0x1b => {
            if i + 1 >= n {
                return KeyLen::Incomplete;
            }
            match data[i + 1] {
                b'[' => {
                    for (j, &c) in data.iter().enumerate().skip(i + 2) {
                        match c {
                            0x20..=0x3f => {}
                            0x40..=0x7e => return KeyLen::Complete(j - i + 1),
                            _ => return KeyLen::Complete(j - i), // malformed: stop before it
                        }
                    }
                    KeyLen::Incomplete
                }
                b'O' => need(3),
                _ => KeyLen::Complete(2),
            }
        }
        0xc0..=0xdf => need(2),
        0xe0..=0xef => need(3),
        0xf0..=0xf7 => need(4),
        _ => KeyLen::Complete(1),
    }
}

/// Split ordinary input into single keys (one byte, one UTF-8 character, or one escape sequence), e.g. for
/// page mode, which takes one key at a time. An incomplete trailing sequence is returned as the last key.
pub fn split_keys(bytes: &[u8]) -> Vec<&[u8]> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        let len = match key_len(bytes, i) {
            KeyLen::Complete(len) => len.max(1),
            KeyLen::Incomplete => bytes.len() - i,
        };
        out.push(&bytes[i..i + len]);
        i += len;
    }
    out
}

/// `Some(len)` when `data[i..]` starts with a complete SGR mouse report, `None` when it is not one, and
/// `Some(0)` when it could still become one (ran out of bytes).
fn mouse_len(data: &[u8], i: usize) -> Option<usize> {
    if !data[i..].starts_with(b"\x1b[<") {
        return None;
    }
    for (j, &c) in data.iter().enumerate().skip(i + 3) {
        match c {
            b'0'..=b'9' | b';' => {}
            b'M' | b'm' => return Some(j - i + 1),
            _ => return None,
        }
    }
    Some(0)
}

/// Whether `rest` (the rest of the input) is a non-empty proper prefix of `pat`, i.e. `pat` may still follow.
fn is_partial(rest: &[u8], pat: &[u8]) -> bool {
    !rest.is_empty() && rest.len() < pat.len() && pat.starts_with(rest)
}

impl InputScanner {
    /// `prefix` = the prefix byte (`None` = no prefix key).
    pub fn new(prefix: Option<u8>) -> Self {
        InputScanner {
            prefix,
            state: PrefixState::Idle,
            in_paste: false,
            carry: Vec::new(),
        }
    }

    /// Whether a prefix is pending (waiting for the second key).
    pub fn prefix_pending(&self) -> bool {
        self.state != PrefixState::Idle
    }

    /// Tokenize one input frame received at `now`. Each token comes with the offset in `bytes` just past its
    /// last byte (a token completed from bytes held over from an earlier frame ends at offset ≥ 0 of this one).
    pub fn scan(&mut self, bytes: &[u8], now: Instant) -> Vec<(Token, usize)> {
        let base = self.carry.len();
        let mut data = std::mem::take(&mut self.carry);
        data.extend_from_slice(bytes);
        let n = data.len();
        let off = |pos: usize| pos.saturating_sub(base);
        let mut out: Vec<(Token, usize)> = Vec::new();
        let mut plain: Vec<u8> = Vec::new();
        let flush = |plain: &mut Vec<u8>, out: &mut Vec<(Token, usize)>, pos: usize| {
            if !plain.is_empty() {
                out.push((Token::Plain(std::mem::take(plain)), off(pos)));
            }
        };
        let mut i = 0;
        while i < n {
            if self.in_paste {
                match data[i..]
                    .windows(PASTE_END.len())
                    .position(|w| w == PASTE_END)
                {
                    Some(k) => {
                        if k > 0 {
                            out.push((Token::Paste(data[i..i + k].to_vec()), off(i + k)));
                        }
                        i += k + PASTE_END.len();
                        out.push((Token::PasteEnd, off(i)));
                        self.in_paste = false;
                    }
                    None => {
                        // Keep a possible partial end marker for the next frame.
                        let keep = (1..PASTE_END.len())
                            .rev()
                            .find(|&k| k <= n - i && PASTE_END.starts_with(&data[n - k..]))
                            .unwrap_or(0);
                        if n - keep > i {
                            out.push((Token::Paste(data[i..n - keep].to_vec()), off(n - keep)));
                        }
                        self.carry = data[n - keep..].to_vec();
                        return out;
                    }
                }
                continue;
            }
            if let PrefixState::Pending(since) = self.state
                && now.duration_since(since) > PREFIX_TIMEOUT
            {
                self.state = PrefixState::Idle; // timed out: nothing written, this byte is ordinary
            }
            if self.state != PrefixState::Idle {
                match key_len(&data, i) {
                    KeyLen::Complete(len) => {
                        flush(&mut plain, &mut out, i);
                        out.push((Token::PrefixKey(data[i..i + len].to_vec()), off(i + len)));
                        self.state = PrefixState::Idle;
                        i += len;
                    }
                    KeyLen::Incomplete => {
                        flush(&mut plain, &mut out, i);
                        self.state = PrefixState::KeyStarted;
                        self.carry = data[i..].to_vec();
                        return out;
                    }
                }
                continue;
            }
            let b = data[i];
            if Some(b) == self.prefix {
                flush(&mut plain, &mut out, i);
                self.state = PrefixState::Pending(now);
                i += 1;
                continue;
            }
            if b == 0x1b {
                let rest = &data[i..];
                if rest.starts_with(PASTE_START) {
                    flush(&mut plain, &mut out, i);
                    self.in_paste = true;
                    i += PASTE_START.len();
                    continue;
                }
                if rest.len() >= 3 && is_partial(rest, PASTE_START) {
                    flush(&mut plain, &mut out, i);
                    self.carry = rest.to_vec();
                    return out;
                }
                match mouse_len(&data, i) {
                    Some(0) if rest.len() <= MAX_CARRY => {
                        flush(&mut plain, &mut out, i);
                        self.carry = rest.to_vec();
                        return out;
                    }
                    Some(len) if len > 0 => {
                        flush(&mut plain, &mut out, i);
                        out.push((Token::Mouse(data[i..i + len].to_vec()), off(i + len)));
                        i += len;
                        continue;
                    }
                    _ => {}
                }
            }
            plain.push(b);
            i += 1;
        }
        flush(&mut plain, &mut out, n);
        out
    }
}

/// What the client does with one chunk of its own stdin: forward `forward` bytes as a `0x01` frame, and
/// detach (send `0x03`, exit 0) when `detach` is set. The `d` itself is not forwarded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Outgoing {
    pub forward: Vec<u8>,
    pub detach: bool,
}

/// Run the scanner over one stdin chunk and decide what to send (D3: prefix + `d` detaches).
pub fn client_input(scanner: &mut InputScanner, bytes: &[u8], now: Instant) -> Outgoing {
    for (tok, end) in scanner.scan(bytes, now) {
        if tok == Token::PrefixKey(b"d".to_vec()) {
            return Outgoing {
                forward: bytes[..end.saturating_sub(1)].to_vec(),
                detach: true,
            };
        }
    }
    Outgoing {
        forward: bytes.to_vec(),
        detach: false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn toks(s: &mut InputScanner, b: &[u8], now: Instant) -> Vec<Token> {
        s.scan(b, now).into_iter().map(|(t, _)| t).collect()
    }

    #[test]
    fn prefix_byte_names() {
        assert_eq!(prefix_byte("C-b"), Some(0x02));
        assert_eq!(prefix_byte("C-a"), Some(0x01));
        assert_eq!(prefix_byte("Ctrl-B"), Some(0x02));
        assert_eq!(prefix_byte("C-["), Some(0x1b));
        assert_eq!(prefix_byte("b"), None);
        assert_eq!(prefix_byte("C-bb"), None);
    }

    #[test]
    fn plain_prefix_keys_and_double_prefix() {
        let t = Instant::now();
        let mut s = InputScanner::new(Some(2));
        assert_eq!(
            toks(&mut s, b"ab\x02d\x02\x02x", t),
            vec![
                Token::Plain(b"ab".to_vec()),
                Token::PrefixKey(b"d".to_vec()),
                Token::PrefixKey(vec![2]),
                Token::Plain(b"x".to_vec()),
            ]
        );
    }

    #[test]
    fn prefix_pair_across_frames_and_multibyte_keys() {
        let t = Instant::now();
        let mut s = InputScanner::new(Some(2));
        assert_eq!(toks(&mut s, b"q\x02", t), vec![Token::Plain(b"q".to_vec())]);
        assert!(s.prefix_pending());
        // Arrow key split over two frames after the prefix.
        assert_eq!(toks(&mut s, b"\x1b[", t), vec![]);
        assert_eq!(
            toks(&mut s, b"Dz", t),
            vec![
                Token::PrefixKey(b"\x1b[D".to_vec()),
                Token::Plain(b"z".to_vec())
            ]
        );
        assert_eq!(
            toks(&mut s, b"\x02\x1b[Z", t),
            vec![Token::PrefixKey(b"\x1b[Z".to_vec())]
        );
    }

    #[test]
    fn prefix_times_out_after_one_second() {
        let t = Instant::now();
        let mut s = InputScanner::new(Some(2));
        assert_eq!(toks(&mut s, b"\x02", t), vec![]);
        assert_eq!(
            toks(&mut s, b"d", t + Duration::from_millis(1100)),
            vec![Token::Plain(b"d".to_vec())]
        );
        assert_eq!(toks(&mut s, b"\x02", t), vec![]);
        assert_eq!(
            toks(&mut s, b"d", t + Duration::from_millis(900)),
            vec![Token::PrefixKey(b"d".to_vec())]
        );
    }

    #[test]
    fn paste_blocks_hide_the_prefix_and_may_span_frames() {
        let t = Instant::now();
        let mut s = InputScanner::new(Some(2));
        assert_eq!(
            toks(&mut s, b"\x1b[200~a\x02d", t),
            vec![Token::Paste(b"a\x02d".to_vec())]
        );
        // End marker split across frames.
        assert_eq!(
            toks(&mut s, b"b\x1b[20", t),
            vec![Token::Paste(b"b".to_vec())]
        );
        assert_eq!(
            toks(&mut s, b"1~c", t),
            vec![Token::PasteEnd, Token::Plain(b"c".to_vec())]
        );
        // Start marker split across frames.
        assert_eq!(toks(&mut s, b"\x1b[20", t), vec![]);
        assert_eq!(
            toks(&mut s, b"0~x\x1b[201~", t),
            vec![Token::Paste(b"x".to_vec()), Token::PasteEnd]
        );
    }

    #[test]
    fn sgr_mouse_reports_are_split_out_whole() {
        let t = Instant::now();
        let mut s = InputScanner::new(Some(2));
        assert_eq!(
            toks(&mut s, b"a\x1b[<0;5;1Mb\x1b[<0;5", t),
            vec![
                Token::Plain(b"a".to_vec()),
                Token::Mouse(b"\x1b[<0;5;1M".to_vec()),
                Token::Plain(b"b".to_vec())
            ]
        );
        assert_eq!(
            toks(&mut s, b";1m", t),
            vec![Token::Mouse(b"\x1b[<0;5;1m".to_vec())]
        );
        // A lone Escape and other CSI keys are not held back.
        assert_eq!(
            toks(&mut s, b"\x1b", t),
            vec![Token::Plain(b"\x1b".to_vec())]
        );
        assert_eq!(
            toks(&mut s, b"\x1b[A", t),
            vec![Token::Plain(b"\x1b[A".to_vec())]
        );
    }

    #[test]
    fn client_detaches_on_prefix_d_and_forwards_what_came_before() {
        let t = Instant::now();
        let mut s = InputScanner::new(Some(2));
        let o = client_input(&mut s, b"ls\x02dmore", t);
        assert_eq!(
            o,
            Outgoing {
                forward: b"ls\x02".to_vec(),
                detach: true
            }
        );

        // Prefix in one read, `d` in the next.
        let mut s = InputScanner::new(Some(2));
        assert!(!client_input(&mut s, b"\x02", t).detach);
        assert_eq!(
            client_input(&mut s, b"d", t),
            Outgoing {
                forward: vec![],
                detach: true
            }
        );

        // Doubled prefix, a paste and a late `d` do not detach.
        let mut s = InputScanner::new(Some(2));
        assert!(!client_input(&mut s, b"\x02\x02d", t).detach);
        assert!(!client_input(&mut s, b"\x1b[200~\x02d\x1b[201~", t).detach);
        assert!(!client_input(&mut s, b"\x02", t).detach);
        assert!(!client_input(&mut s, b"d", t + Duration::from_secs(2)).detach);
    }

    #[test]
    fn on_frame_maps_server_frames() {
        assert_eq!(
            on_frame(Frame::Screen(b"x".to_vec())),
            ClientAction::Write(b"x".to_vec())
        );
        assert_eq!(
            on_frame(Frame::Exit("bye".into())),
            ClientAction::Exit("bye".into())
        );
        assert_eq!(
            on_frame(Frame::Open("/x".into())),
            ClientAction::Open("/x".into())
        );
        assert!(matches!(on_frame(Frame::Detach), ClientAction::Exit(_)));
    }
}
