//! Colour depth of a viewer's outer terminal, and the RGB → 256 downgrade (brief-m03 §3.2 P-9, design.md §7.3 M1).
//!
//! A viewer's handshake reports the outer terminal's `TERM` / `COLORTERM`; when it cannot show truecolour the
//! server downgrades every `Color::Rgb` it draws for that viewer with [`to_256`].

use crate::Color;

/// What the outer terminal can show.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Depth {
    /// 24-bit truecolour: `Color::Rgb` is sent as `38;2;r;g;b` / `48;2;r;g;b`.
    Rgb,
    /// 256-colour palette: `Color::Rgb` is downgraded with [`to_256`].
    Indexed256,
}

/// `Depth::Rgb` when `colorterm` is `truecolor` or `24bit`; `Depth::Indexed256` otherwise.
/// `term` does not change the result (a 256-colour `TERM` is assumed either way).
pub fn depth_from_env(term: &str, colorterm: &str) -> Depth {
    let _ = term;
    match colorterm {
        "truecolor" | "24bit" => Depth::Rgb,
        _ => Depth::Indexed256,
    }
}

/// Channel levels of the xterm 6×6×6 cube (indices 16–231).
const CUBE: [u8; 6] = [0, 95, 135, 175, 215, 255];

/// Map `Color::Rgb` to an entry of the xterm 6×6×6 cube (16–231) or grey ramp (232–255); `Indexed` and
/// `Default` pass through unchanged.
///
/// Two candidates, the closer one (squared RGB distance; a tie keeps the cube) wins:
/// - cube: each channel quantized **linearly** to 6 steps, `round(v · 5 / 255)` (so `128` → step 3 = 175);
/// - grey: `232 + round((avg − 8) / 10)`, clamped to 232–255 (value `8 + 10·k`).
///
/// This is not the globally nearest palette entry: the linear step is not always the closest cube level (for
/// `(255, 128, 0)` it gives 214 = `(255, 175, 0)`, where 208 = `(255, 135, 0)` is closer). The values are fixed by
/// brief-m03 §3.4 and the supervisor's M10 ruling.
pub fn to_256(c: Color) -> Color {
    let Color::Rgb(r, g, b) = c else {
        return c;
    };
    let step = |v: u8| (usize::from(v) * 5 + 127) / 255;
    let (ri, gi, bi) = (step(r), step(g), step(b));
    let cube = (16 + 36 * ri + 6 * gi + bi) as u8;
    let cube_rgb = (CUBE[ri], CUBE[gi], CUBE[bi]);

    let avg = (u32::from(r) + u32::from(g) + u32::from(b) + 1) / 3;
    let k = if avg <= 8 {
        0
    } else {
        ((avg - 8 + 5) / 10).min(23)
    };
    let grey = 232 + k as u8;
    let v = (8 + 10 * k) as u8;

    if distance((r, g, b), (v, v, v)) < distance((r, g, b), cube_rgb) {
        Color::Indexed(grey)
    } else {
        Color::Indexed(cube)
    }
}

/// Squared RGB distance.
fn distance(a: (u8, u8, u8), b: (u8, u8, u8)) -> u32 {
    let d = |x: u8, y: u8| u32::from(x.abs_diff(y)).pow(2);
    d(a.0, b.0) + d(a.1, b.1) + d(a.2, b.2)
}
