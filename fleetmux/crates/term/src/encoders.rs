//! Pure input encoders (design.md §2.6): fleet / viewer input → the bytes written to a pane's PTY, following the
//! inner program's current [`Modes`]. Re-exported from the crate root; this module is private (A-R3 split).

use crate::{Modes, MouseMode};

/// Encode a key name (`fleetmux/protocol/keys.json` = wezterm.py `KEYS` + `C-<letter>`) for the inner program.
/// Arrow keys follow `modes.app_cursor` (`ESC O A` vs `ESC [ A`). Unknown name → `None`.
pub fn encode_key(name: &str, modes: &Modes) -> Option<Vec<u8>> {
    let arrow = match name {
        "Up" => Some(b'A'),
        "Down" => Some(b'B'),
        "Right" => Some(b'C'),
        "Left" => Some(b'D'),
        _ => None,
    };
    if let Some(final_byte) = arrow {
        let intro = if modes.app_cursor { b'O' } else { b'[' };
        return Some(vec![0x1b, intro, final_byte]);
    }
    // `C-<a..z>` (lower case, as in keys.json) → control byte 1..26.
    if let Some(&[c]) = name.strip_prefix("C-").map(str::as_bytes) {
        return c.is_ascii_lowercase().then(|| vec![c - b'a' + 1]);
    }
    let bytes: &[u8] = match name {
        "Escape" | "Esc" => b"\x1b",
        "Enter" | "Return" => b"\r",
        "Tab" => b"\t",
        "BTab" => b"\x1b[Z",
        "Space" => b" ",
        "BSpace" | "Backspace" => b"\x7f",
        "Home" => b"\x1b[H",
        "End" => b"\x1b[F",
        "PageUp" => b"\x1b[5~",
        "PageDown" => b"\x1b[6~",
        "Delete" => b"\x1b[3~",
        "Insert" => b"\x1b[2~",
        "Ctrl-C" => b"\x03",
        "Ctrl-D" => b"\x04",
        _ => return None,
    };
    Some(bytes.to_vec())
}

/// Mouse button of a [`MouseEvent`].
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum MouseButton {
    Left,
    Middle,
    Right,
}

/// What happened.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum MouseKind {
    Press(MouseButton),
    Release(MouseButton),
    /// Motion with the button held.
    Drag(MouseButton),
    /// Motion with no button held.
    Move,
    ScrollUp,
    ScrollDown,
}

/// A mouse event in outer-screen coordinates (0-based, as the viewer's terminal reported it).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct MouseEvent {
    pub kind: MouseKind,
    pub col: u16,
    pub row: u16,
    pub shift: bool,
    pub alt: bool,
    pub ctrl: bool,
}

/// Re-encode a mouse event for the inner program: SGR when `modes.sgr_mouse`, X10 otherwise.
/// `origin` = (col, row) of the pane's top-left content cell in outer-screen coordinates (0-based); the
/// encoded position is relative to it (1-based on the wire). `None` when the inner program did not ask for
/// this kind of event, or the event lies above / left of `origin`.
pub fn encode_mouse(ev: &MouseEvent, modes: &Modes, origin: (u16, u16)) -> Option<Vec<u8>> {
    // ?1000: press / release / wheel; ?1002: + motion with a button held; ?1003: + any motion.
    let wanted = matches!(
        (modes.mouse?, ev.kind),
        (
            _,
            MouseKind::Press(_)
                | MouseKind::Release(_)
                | MouseKind::ScrollUp
                | MouseKind::ScrollDown
        ) | (MouseMode::Drag | MouseMode::Motion, MouseKind::Drag(_))
            | (MouseMode::Motion, MouseKind::Move)
    );
    if !wanted {
        return None;
    }
    // 1-based position relative to the pane's content origin.
    let x = u32::from(ev.col.checked_sub(origin.0)?) + 1;
    let y = u32::from(ev.row.checked_sub(origin.1)?) + 1;

    let button = |b: MouseButton| match b {
        MouseButton::Left => 0,
        MouseButton::Middle => 1,
        MouseButton::Right => 2,
    };
    let (code, release) = match ev.kind {
        MouseKind::Press(b) => (button(b), false),
        // SGR keeps the button and marks release with `m`; X10 reports release as button 3.
        MouseKind::Release(b) if modes.sgr_mouse => (button(b), true),
        MouseKind::Release(_) => (3, true),
        MouseKind::Drag(b) => (button(b) + 32, false),
        MouseKind::Move => (3 + 32, false),
        MouseKind::ScrollUp => (64, false),
        MouseKind::ScrollDown => (65, false),
    };
    let code = code
        + if ev.shift { 4 } else { 0 }
        + if ev.alt { 8 } else { 0 }
        + if ev.ctrl { 16 } else { 0 };

    if modes.sgr_mouse {
        let end = if release { 'm' } else { 'M' };
        return Some(format!("\x1b[<{code};{x};{y}{end}").into_bytes());
    }
    // X10: one byte per value, 32 + value; a position past 223 cannot be encoded.
    let byte = |v: u32| u8::try_from(32 + v).ok();
    Some(vec![0x1b, b'[', b'M', byte(code)?, byte(x)?, byte(y)?])
}

/// How data from `pane.write` is written (PROTOCOL.md `pane.write` `mode`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum WriteMode {
    /// Never wrapped.
    Raw,
    /// Always wrapped in `ESC[200~ … ESC[201~` (fleet's `bracketed=True`).
    Bracketed,
    /// Wrapped only when the inner program enabled `?2004` (a human paste).
    Paste,
}

/// Something worth a line in `server.log`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Warning {
    /// `WriteMode::Bracketed` wrapped the data although the inner program has `?2004` off.
    BracketedWithout2004,
}

/// Bytes to write for `data` under `mode`, plus a warning when wrapping disagrees with the inner program.
pub fn wrap_paste(data: &[u8], mode: WriteMode, modes: &Modes) -> (Vec<u8>, Option<Warning>) {
    let wrap = |data: &[u8]| [&b"\x1b[200~"[..], data, &b"\x1b[201~"[..]].concat();
    match mode {
        WriteMode::Raw => (data.to_vec(), None),
        WriteMode::Bracketed => {
            let warning = (!modes.bracketed_paste).then_some(Warning::BracketedWithout2004);
            (wrap(data), warning)
        }
        WriteMode::Paste if modes.bracketed_paste => (wrap(data), None),
        WriteMode::Paste => (data.to_vec(), None),
    }
}
