//! Encoder contract (plan.md §3.2): key names, mouse re-encoding, paste wrapping. Red until M0.

use std::collections::BTreeMap;

use fleetmux_term::{
    Modes, MouseButton, MouseEvent, MouseKind, MouseMode, Warning, WriteMode, encode_key,
    encode_mouse, wrap_paste,
};

const PASTE_START: &[u8] = b"\x1b[200~";
const PASTE_END: &[u8] = b"\x1b[201~";

fn modes() -> Modes {
    Modes::default()
}

fn app_cursor() -> Modes {
    Modes {
        app_cursor: true,
        ..Modes::default()
    }
}

fn mouse(mode: MouseMode, sgr: bool) -> Modes {
    Modes {
        mouse: Some(mode),
        sgr_mouse: sgr,
        ..Modes::default()
    }
}

fn ev(kind: MouseKind, col: u16, row: u16) -> MouseEvent {
    MouseEvent {
        kind,
        col,
        row,
        shift: false,
        alt: false,
        ctrl: false,
    }
}

fn wrapped(data: &[u8]) -> Vec<u8> {
    [PASTE_START, data, PASTE_END].concat()
}

// ---- keys.json: the one key table shared with cmux_fleet/backends/fleetmux.py ----

/// `fleetmux/protocol/keys.json` → name → bytes. The crate has no JSON dev-dependency yet, so this reads the
/// file with the small reader below (objects, arrays, strings with escapes, numbers, literals).
fn key_table() -> BTreeMap<String, Vec<u8>> {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../protocol/keys.json");
    let src = std::fs::read_to_string(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
    let mut p = Json {
        s: src.as_bytes(),
        i: 0,
    };
    let root = p.value();
    p.ws();
    assert_eq!(p.i, p.s.len(), "trailing data in {path}");
    let JsonValue::Object(top) = root else {
        panic!("{path}: top level is not an object")
    };
    let (_, keys) = top
        .into_iter()
        .find(|(k, _)| k == "keys")
        .expect("keys.json has no \"keys\" member");
    let JsonValue::Object(keys) = keys else {
        panic!("\"keys\" is not an object")
    };
    keys.into_iter()
        .map(|(name, v)| match v {
            JsonValue::String(s) => (name, s.into_bytes()),
            other => panic!("key {name:?} maps to {other:?}, not a string"),
        })
        .collect()
}

#[derive(Debug)]
enum JsonValue {
    Object(Vec<(String, JsonValue)>),
    String(String),
    /// Arrays, numbers, literals: skipped, keys.json does not use them.
    Other,
}

struct Json<'a> {
    s: &'a [u8],
    i: usize,
}

impl Json<'_> {
    fn ws(&mut self) {
        while self.i < self.s.len() && self.s[self.i].is_ascii_whitespace() {
            self.i += 1;
        }
    }

    fn eat(&mut self, b: u8) {
        self.ws();
        assert_eq!(
            self.s.get(self.i),
            Some(&b),
            "keys.json: expected {:?} at byte {}",
            b as char,
            self.i
        );
        self.i += 1;
    }

    fn peek(&mut self) -> u8 {
        self.ws();
        *self.s.get(self.i).expect("keys.json: unexpected end")
    }

    fn value(&mut self) -> JsonValue {
        match self.peek() {
            b'{' => {
                self.eat(b'{');
                let mut members = Vec::new();
                if self.peek() == b'}' {
                    self.i += 1;
                    return JsonValue::Object(members);
                }
                loop {
                    let key = self.string();
                    self.eat(b':');
                    members.push((key, self.value()));
                    if self.peek() == b',' {
                        self.i += 1;
                    } else {
                        self.eat(b'}');
                        return JsonValue::Object(members);
                    }
                }
            }
            b'[' => {
                self.eat(b'[');
                if self.peek() == b']' {
                    self.i += 1;
                    return JsonValue::Other;
                }
                loop {
                    self.value();
                    if self.peek() == b',' {
                        self.i += 1;
                    } else {
                        self.eat(b']');
                        return JsonValue::Other;
                    }
                }
            }
            b'"' => JsonValue::String(self.string()),
            _ => {
                while self.i < self.s.len() && !b",}] \t\r\n".contains(&self.s[self.i]) {
                    self.i += 1;
                }
                JsonValue::Other
            }
        }
    }

    fn hex4(&mut self) -> u32 {
        let h =
            std::str::from_utf8(&self.s[self.i..self.i + 4]).expect("keys.json: bad \\u escape");
        self.i += 4;
        u32::from_str_radix(h, 16).expect("keys.json: bad \\u escape")
    }

    fn string(&mut self) -> String {
        self.eat(b'"');
        let mut out = String::new();
        loop {
            let rest = std::str::from_utf8(&self.s[self.i..]).expect("keys.json is not UTF-8");
            let c = rest.chars().next().expect("keys.json: unterminated string");
            self.i += c.len_utf8();
            match c {
                '"' => return out,
                '\\' => {
                    let e = self.s[self.i];
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let mut cp = self.hex4();
                            if (0xD800..0xDC00).contains(&cp)
                                && self.s[self.i..].starts_with(b"\\u")
                            {
                                self.i += 2;
                                let lo = self.hex4();
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                            }
                            out.push(char::from_u32(cp).expect("keys.json: bad code point"));
                        }
                        other => panic!("keys.json: bad escape \\{}", other as char),
                    }
                }
                c => out.push(c),
            }
        }
    }
}

// ---- keys ----

#[test]
fn arrow_keys_use_esc_o_in_app_cursor_mode() {
    let m = app_cursor();
    assert_eq!(encode_key("Up", &m), Some(b"\x1bOA".to_vec()));
    assert_eq!(encode_key("Down", &m), Some(b"\x1bOB".to_vec()));
    assert_eq!(encode_key("Right", &m), Some(b"\x1bOC".to_vec()));
    assert_eq!(encode_key("Left", &m), Some(b"\x1bOD".to_vec()));
}

#[test]
fn arrow_keys_use_csi_otherwise() {
    let m = modes();
    assert_eq!(encode_key("Up", &m), Some(b"\x1b[A".to_vec()));
    assert_eq!(encode_key("Down", &m), Some(b"\x1b[B".to_vec()));
    assert_eq!(encode_key("Right", &m), Some(b"\x1b[C".to_vec()));
    assert_eq!(encode_key("Left", &m), Some(b"\x1b[D".to_vec()));
}

#[test]
fn every_name_in_the_python_key_table_encodes() {
    let table = key_table();
    // The reader itself must have produced the table (guards against an empty / misparsed file).
    assert!(
        table.len() >= 20,
        "keys.json table too small: {} names",
        table.len()
    );
    assert_eq!(table.get("Enter").map(Vec::as_slice), Some(&b"\r"[..]));
    assert_eq!(table.get("Escape").map(Vec::as_slice), Some(&b"\x1b"[..]));

    // In the default modes (app cursor off) every name encodes to exactly the bytes the table lists.
    let m = modes();
    let wrong: Vec<String> = table
        .iter()
        .filter_map(|(name, want)| {
            let got = encode_key(name, &m);
            (got.as_deref() != Some(want.as_slice()))
                .then(|| format!("{name}: want {want:?}, got {got:?}"))
        })
        .collect();
    assert!(
        wrong.is_empty(),
        "key names that do not encode as keys.json says:\n{}",
        wrong.join("\n")
    );
}

#[test]
fn unknown_key_name_is_none() {
    let m = modes();
    for name in ["", "NoSuchKey", "F99", "C-", "C-ab", "Enter ", "Ctrl-"] {
        assert_eq!(encode_key(name, &m), None, "{name:?} must be unknown");
    }
}

#[test]
fn ctrl_letters_encode_to_control_bytes() {
    let m = modes();
    for c in b'a'..=b'z' {
        let name = format!("C-{}", c as char);
        assert_eq!(encode_key(&name, &m), Some(vec![c - b'a' + 1]), "{name}");
    }
}

// ---- mouse ----

#[test]
fn sgr_mouse_press_release_drag_encoding() {
    let m = mouse(MouseMode::Drag, true);
    let o = (0, 0);
    assert_eq!(
        encode_mouse(&ev(MouseKind::Press(MouseButton::Left), 4, 2), &m, o),
        Some(b"\x1b[<0;5;3M".to_vec())
    );
    assert_eq!(
        encode_mouse(&ev(MouseKind::Drag(MouseButton::Left), 6, 2), &m, o),
        Some(b"\x1b[<32;7;3M".to_vec())
    );
    assert_eq!(
        encode_mouse(&ev(MouseKind::Release(MouseButton::Left), 6, 2), &m, o),
        Some(b"\x1b[<0;7;3m".to_vec())
    );
    assert_eq!(
        encode_mouse(&ev(MouseKind::Press(MouseButton::Middle), 0, 0), &m, o),
        Some(b"\x1b[<1;1;1M".to_vec())
    );
    assert_eq!(
        encode_mouse(&ev(MouseKind::Press(MouseButton::Right), 0, 0), &m, o),
        Some(b"\x1b[<2;1;1M".to_vec())
    );
    assert_eq!(
        encode_mouse(&ev(MouseKind::ScrollUp, 4, 2), &m, o),
        Some(b"\x1b[<64;5;3M".to_vec())
    );
    assert_eq!(
        encode_mouse(&ev(MouseKind::ScrollDown, 4, 2), &m, o),
        Some(b"\x1b[<65;5;3M".to_vec())
    );
    // Modifiers add shift 4 / alt 8 / ctrl 16 to the button code.
    let ctrl_click = MouseEvent {
        ctrl: true,
        ..ev(MouseKind::Press(MouseButton::Left), 4, 2)
    };
    assert_eq!(
        encode_mouse(&ctrl_click, &m, o),
        Some(b"\x1b[<16;5;3M".to_vec())
    );
    // SGR has no 223-column limit.
    assert_eq!(
        encode_mouse(&ev(MouseKind::Press(MouseButton::Left), 299, 0), &m, o),
        Some(b"\x1b[<0;300;1M".to_vec())
    );
}

#[test]
fn x10_mouse_when_sgr_off() {
    let m = mouse(MouseMode::Click, false);
    let o = (0, 0);
    // ESC [ M Cb Cx Cy, each byte = 32 + value, positions 1-based; release is button 3.
    assert_eq!(
        encode_mouse(&ev(MouseKind::Press(MouseButton::Left), 4, 2), &m, o),
        Some(vec![0x1b, b'[', b'M', 32, 32 + 5, 32 + 3])
    );
    assert_eq!(
        encode_mouse(&ev(MouseKind::Release(MouseButton::Left), 4, 2), &m, o),
        Some(vec![0x1b, b'[', b'M', 32 + 3, 32 + 5, 32 + 3])
    );
    assert_eq!(
        encode_mouse(&ev(MouseKind::Press(MouseButton::Right), 0, 0), &m, o),
        Some(vec![0x1b, b'[', b'M', 32 + 2, 32 + 1, 32 + 1])
    );
    assert_eq!(
        encode_mouse(&ev(MouseKind::ScrollUp, 0, 0), &m, o),
        Some(vec![0x1b, b'[', b'M', 32 + 64, 33, 33])
    );
}

#[test]
fn mouse_coordinates_are_translated_by_origin() {
    let m = mouse(MouseMode::Click, true);
    let o = (60, 3); // (col, row) of the pane's top-left content cell on the outer screen
    assert_eq!(
        encode_mouse(&ev(MouseKind::Press(MouseButton::Left), 60, 3), &m, o),
        Some(b"\x1b[<0;1;1M".to_vec())
    );
    assert_eq!(
        encode_mouse(&ev(MouseKind::Press(MouseButton::Left), 70, 10), &m, o),
        Some(b"\x1b[<0;11;8M".to_vec())
    );

    let x10 = mouse(MouseMode::Click, false);
    assert_eq!(
        encode_mouse(&ev(MouseKind::Press(MouseButton::Left), 70, 10), &x10, o),
        Some(vec![0x1b, b'[', b'M', 32, 32 + 11, 32 + 8])
    );
}

#[test]
fn mouse_outside_inner_mode_is_none() {
    let o = (0, 0);
    let press = ev(MouseKind::Press(MouseButton::Left), 4, 2);
    let drag = ev(MouseKind::Drag(MouseButton::Left), 4, 2);
    let moved = ev(MouseKind::Move, 4, 2);

    // Inner program has mouse reporting off: nothing is forwarded (the server handles it).
    assert_eq!(
        encode_mouse(
            &press,
            &Modes {
                sgr_mouse: true,
                ..modes()
            },
            o
        ),
        None
    );
    // ?1000 only: no motion of any kind.
    let click = mouse(MouseMode::Click, true);
    assert_eq!(encode_mouse(&drag, &click, o), None);
    assert_eq!(encode_mouse(&moved, &click, o), None);
    // ?1002: drag yes, bare motion no.
    let drag_mode = mouse(MouseMode::Drag, true);
    assert!(encode_mouse(&drag, &drag_mode, o).is_some());
    assert_eq!(encode_mouse(&moved, &drag_mode, o), None);
    // ?1003: bare motion is button 3 + 32.
    assert_eq!(
        encode_mouse(&moved, &mouse(MouseMode::Motion, true), o),
        Some(b"\x1b[<35;5;3M".to_vec())
    );
    // Above / left of the pane's origin: not the pane's event.
    assert_eq!(
        encode_mouse(
            &ev(MouseKind::Press(MouseButton::Left), 9, 5),
            &click,
            (10, 5)
        ),
        None
    );
    assert_eq!(
        encode_mouse(
            &ev(MouseKind::Press(MouseButton::Left), 10, 4),
            &click,
            (10, 5)
        ),
        None
    );
}

// ---- paste ----

#[test]
fn bracketed_mode_always_wraps_even_when_2004_off_with_warning() {
    let data = b"line one\nline two";
    let off = modes();
    assert_eq!(
        wrap_paste(data, WriteMode::Bracketed, &off),
        (wrapped(data), Some(Warning::BracketedWithout2004))
    );
    let on = Modes {
        bracketed_paste: true,
        ..modes()
    };
    assert_eq!(
        wrap_paste(data, WriteMode::Bracketed, &on),
        (wrapped(data), None)
    );
}

#[test]
fn raw_mode_never_wraps() {
    let data = b"-leading dash\nsecond line";
    for m in [
        modes(),
        Modes {
            bracketed_paste: true,
            ..modes()
        },
    ] {
        assert_eq!(
            wrap_paste(data, WriteMode::Raw, &m),
            (data.to_vec(), None),
            "{m:?}"
        );
    }
}

#[test]
fn paste_mode_follows_2004() {
    let data = b"pasted\ntext";
    assert_eq!(
        wrap_paste(data, WriteMode::Paste, &modes()),
        (data.to_vec(), None)
    );
    let on = Modes {
        bracketed_paste: true,
        ..modes()
    };
    assert_eq!(
        wrap_paste(data, WriteMode::Paste, &on),
        (wrapped(data), None)
    );
}
