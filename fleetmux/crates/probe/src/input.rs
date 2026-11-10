//! Cuts the probe's input into the README's event kinds (README "Event log"): mouse reports (SGR / X10),
//! bracketed pastes, focus reports, the start-up query replies the probe is waiting for, and plain key
//! bytes for everything else. A sequence cut off at the end of a read is held until more bytes arrive or
//! the caller flushes it (a lone `ESC` key then becomes a key byte).

/// A start-up reply the probe is waiting for; only then are these shapes recognised (a `CSI 1;2 R` is
/// also Shift+F3, so it is a key unless a DSR reply is due).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Await {
    Dsr,
    Osc11,
    /// `CSI ? <flags> u` or the DA1 reply `CSI ? … c` that follows the kitty query.
    Kitty,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Token {
    Key(Vec<u8>),
    Mouse(Mouse),
    Paste { raw: Vec<u8>, text: Vec<u8> },
    Focus { raw: Vec<u8>, focused: bool },
    Dsr { raw: Vec<u8>, row: u32, col: u32 },
    Osc11 { raw: Vec<u8>, color: String },
    KittyFlags { raw: Vec<u8>, flags: u32 },
    Da1 { raw: Vec<u8> },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mouse {
    pub raw: Vec<u8>,
    /// Low 2 bits of the button byte; wheel reports keep 64 / 65 (README).
    pub button: u32,
    pub col: u32,
    pub row: u32,
    pub press: bool,
    pub motion: bool,
    pub sgr: bool,
}

const ESC: u8 = 0x1b;
const PASTE_START: &[u8] = b"\x1b[200~";
const PASTE_END: &[u8] = b"\x1b[201~";

enum Step {
    /// A whole sequence of this many bytes.
    Done(usize, Token),
    /// Could still become this shape once more bytes arrive.
    Partial,
    No,
}

/// Bytes read so far that have not become tokens yet.
#[derive(Default)]
pub struct Parser {
    buf: Vec<u8>,
}

impl Parser {
    pub fn push(&mut self, bytes: &[u8]) {
        self.buf.extend_from_slice(bytes);
    }

    /// Something is held back waiting for more bytes.
    pub fn pending(&self) -> bool {
        !self.buf.is_empty()
    }

    /// Takes every complete token. Plain bytes between sequences are merged into one `Key`. With `flush`,
    /// a held prefix that is not inside a paste is given up and becomes key bytes.
    pub fn take(&mut self, awaiting: Option<Await>, flush: bool) -> Vec<Token> {
        let mut out = Vec::new();
        let mut keys: Vec<u8> = Vec::new();
        let mut i = 0;
        let buf = std::mem::take(&mut self.buf);
        while i < buf.len() {
            if buf[i] != ESC {
                keys.push(buf[i]);
                i += 1;
                continue;
            }
            match recognise(&buf[i..], awaiting) {
                Step::Done(n, tok) => {
                    if !keys.is_empty() {
                        out.push(Token::Key(std::mem::take(&mut keys)));
                    }
                    out.push(tok);
                    i += n;
                }
                Step::Partial if !flush || buf[i..].starts_with(PASTE_START) => {
                    self.buf = buf[i..].to_vec();
                    break;
                }
                Step::Partial | Step::No => {
                    keys.push(ESC);
                    i += 1;
                }
            }
        }
        if !keys.is_empty() {
            out.push(Token::Key(keys));
        }
        out
    }
}

/// `s` starts with ESC.
fn recognise(s: &[u8], awaiting: Option<Await>) -> Step {
    let mut partial = false;
    let shapes: [fn(&[u8]) -> Step; 4] = [paste, sgr_mouse, x10_mouse, focus];
    for shape in shapes {
        match shape(s) {
            Step::Done(n, t) => return Step::Done(n, t),
            Step::Partial => partial = true,
            Step::No => {}
        }
    }
    let reply = match awaiting {
        Some(Await::Dsr) => dsr(s),
        Some(Await::Osc11) => osc11(s),
        Some(Await::Kitty) => kitty_or_da1(s),
        None => Step::No,
    };
    match reply {
        Step::Done(n, t) => Step::Done(n, t),
        Step::Partial => Step::Partial,
        Step::No if partial => Step::Partial,
        Step::No => Step::No,
    }
}

/// `s` is a strict prefix of `lit`, or starts with it (`Some(len)`).
fn literal(s: &[u8], lit: &[u8]) -> Result<usize, bool> {
    if s.len() >= lit.len() {
        if &s[..lit.len()] == lit { Ok(lit.len()) } else { Err(false) }
    } else if lit.starts_with(s) {
        Err(true)
    } else {
        Err(false)
    }
}

/// After `prefix`: decimal fields separated by `;`, then one byte of `finals`. Returns (length, fields,
/// final byte).
fn csi_numbers(s: &[u8], prefix: &[u8], finals: &[u8], max_fields: usize) -> Result<(usize, Vec<u32>, u8), bool> {
    let start = literal(s, prefix)?;
    let mut fields = vec![0u32];
    let mut digits = false;
    for (k, &b) in s[start..].iter().enumerate() {
        match b {
            b'0'..=b'9' => {
                let f = fields.last_mut().unwrap();
                *f = f.saturating_mul(10).saturating_add((b - b'0') as u32);
                digits = true;
            }
            b';' if fields.len() < max_fields => {
                fields.push(0);
                digits = false;
            }
            _ if finals.contains(&b) && (digits || fields.len() == 1) => {
                return Ok((start + k + 1, fields, b));
            }
            _ => return Err(false),
        }
        if k > 32 {
            return Err(false);
        }
    }
    Err(true)
}

fn step_of<T>(r: Result<T, bool>, f: impl FnOnce(T) -> Step) -> Step {
    match r {
        Ok(v) => f(v),
        Err(true) => Step::Partial,
        Err(false) => Step::No,
    }
}

fn button_of(b: u32) -> u32 {
    if b & 64 != 0 { 64 | (b & 3) } else { b & 3 }
}

fn paste(s: &[u8]) -> Step {
    step_of(literal(s, PASTE_START), |n| {
        match s[n..].windows(PASTE_END.len()).position(|w| w == PASTE_END) {
            Some(k) => {
                let end = n + k + PASTE_END.len();
                Step::Done(end, Token::Paste { raw: s[..end].to_vec(), text: s[n..n + k].to_vec() })
            }
            None => Step::Partial,
        }
    })
}

fn sgr_mouse(s: &[u8]) -> Step {
    step_of(csi_numbers(s, b"\x1b[<", b"Mm", 3), |(n, f, fin)| {
        if f.len() != 3 {
            return Step::No;
        }
        Step::Done(
            n,
            Token::Mouse(Mouse {
                raw: s[..n].to_vec(),
                button: button_of(f[0]),
                col: f[1],
                row: f[2],
                press: fin == b'M',
                motion: f[0] & 32 != 0,
                sgr: true,
            }),
        )
    })
}

fn x10_mouse(s: &[u8]) -> Step {
    step_of(literal(s, b"\x1b[M"), |n| {
        if s.len() < n + 3 {
            return Step::Partial;
        }
        let [b, x, y] = [s[n], s[n + 1], s[n + 2]].map(|v| (v as u32).saturating_sub(32));
        Step::Done(
            n + 3,
            Token::Mouse(Mouse {
                raw: s[..n + 3].to_vec(),
                button: button_of(b),
                col: x,
                row: y,
                press: b & 64 != 0 || b & 3 != 3,
                motion: b & 32 != 0,
                sgr: false,
            }),
        )
    })
}

fn focus(s: &[u8]) -> Step {
    for (lit, focused) in [(b"\x1b[I", true), (b"\x1b[O", false)] {
        match literal(s, lit) {
            Ok(n) => return Step::Done(n, Token::Focus { raw: s[..n].to_vec(), focused }),
            Err(true) => return Step::Partial,
            Err(false) => {}
        }
    }
    Step::No
}

fn dsr(s: &[u8]) -> Step {
    step_of(csi_numbers(s, b"\x1b[", b"R", 2), |(n, f, _)| {
        if f.len() != 2 {
            return Step::No;
        }
        Step::Done(n, Token::Dsr { raw: s[..n].to_vec(), row: f[0], col: f[1] })
    })
}

fn osc11(s: &[u8]) -> Step {
    step_of(literal(s, b"\x1b]11;"), |n| {
        for k in n..s.len() {
            let (end, body_end) = match s[k] {
                0x07 => (k + 1, k),
                ESC if s.len() == k + 1 => return Step::Partial,
                ESC if s[k + 1] == b'\\' => (k + 2, k),
                ESC => return Step::No,
                _ => continue,
            };
            let color = String::from_utf8_lossy(&s[n..body_end]).into_owned();
            return Step::Done(end, Token::Osc11 { raw: s[..end].to_vec(), color });
        }
        if s.len() - n > 256 { Step::No } else { Step::Partial }
    })
}

fn kitty_or_da1(s: &[u8]) -> Step {
    step_of(csi_numbers(s, b"\x1b[?", b"uc", 16), |(n, f, fin)| {
        let raw = s[..n].to_vec();
        match fin {
            b'u' if f.len() == 1 => Step::Done(n, Token::KittyFlags { raw, flags: f[0] }),
            b'c' => Step::Done(n, Token::Da1 { raw }),
            _ => Step::No,
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn all(bytes: &[u8], awaiting: Option<Await>) -> Vec<Token> {
        let mut p = Parser::default();
        p.push(bytes);
        p.take(awaiting, false)
    }

    #[test]
    fn keys_and_sequences() {
        assert_eq!(all(b"\x1bOA", None), vec![Token::Key(b"\x1bOA".to_vec())]);
        assert_eq!(all(b"\x1b[A", None), vec![Token::Key(b"\x1b[A".to_vec())]);
        let t = all(b"x\x1b[<64;5;6Mq", None);
        assert_eq!(t.len(), 3);
        assert!(matches!(&t[1], Token::Mouse(m) if m.button == 64 && m.col == 5 && m.row == 6 && m.press));
        assert_eq!(all(b"\x1b[1;2R", None), vec![Token::Key(b"\x1b[1;2R".to_vec())]);
        assert!(matches!(&all(b"\x1b[12;40R", Some(Await::Dsr))[0], Token::Dsr { row: 12, col: 40, .. }));
    }

    #[test]
    fn split_sequences_are_held() {
        let mut p = Parser::default();
        p.push(b"\x1b[<0;1");
        assert!(p.take(None, false).is_empty());
        p.push(b"0;2m\x1b[200~a\nb");
        let t = p.take(None, false);
        assert!(matches!(&t[0], Token::Mouse(m) if !m.press && m.col == 10));
        assert!(p.pending());
        p.push(b"\x1b[201~");
        assert!(matches!(&p.take(None, true)[0], Token::Paste { text, .. } if text == b"a\nb"));
        p.push(b"\x1b");
        assert!(p.take(None, false).is_empty());
        assert_eq!(p.take(None, true), vec![Token::Key(vec![0x1b])]);
        assert!(matches!(
            &all(b"\x1b]11;rgb:0000/0000/0000\x1b\\", Some(Await::Osc11))[0],
            Token::Osc11 { color, .. } if color == "rgb:0000/0000/0000"
        ));
        let t = all(b"\x1b[?0u\x1b[?6c", Some(Await::Kitty));
        assert!(matches!(t[..], [Token::KittyFlags { flags: 0, .. }, Token::Da1 { .. }]));
    }
}
