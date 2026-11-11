//! What the probe draws (README "Screen"). Every row is rewritten in place (`CSI <row>;1H` + text +
//! `CSI K`), never with `CSI 2J` / `3J`, so with `--lines` the scrollback above stays intact. Text is cut
//! to the screen width; rows below the bottom of a small window are skipped.

/// First row of the `--flood` scroll region (README: rows 1–13 stay put).
pub const FLOOD_TOP: u16 = 14;
const WIDE: &str = "WIDE 漢字😀│end";
const TRUECOLOR_ON: &str = "\x1b[38;2;255;128;0m\x1b[48;2;0;64;128m";

/// Cell width of the characters the probe itself prints (ASCII, CJK, the emoji and `│`).
fn width(c: char) -> u16 {
    match c as u32 {
        0x1100..=0x115f | 0x2e80..=0xa4cf | 0xac00..=0xd7a3 | 0xf900..=0xfaff | 0xfe30..=0xfe4f
        | 0xff00..=0xff60 | 0xffe0..=0xffe6 | 0x1f300..=0x1f64f | 0x1f900..=0x1f9ff | 0x20000..=0x3fffd => 2,
        _ => 1,
    }
}

/// `text` cut to `cols` cells (a wide character that does not fit is dropped), and its width.
fn fit(text: &str, cols: u16) -> (String, u16) {
    let mut out = String::new();
    let mut w = 0;
    for c in text.chars() {
        let cw = width(c);
        if w + cw > cols {
            break;
        }
        out.push(c);
        w += cw;
    }
    (out, w)
}

/// Row `row` (1-based) set to `text` with optional SGR around it. `CSI K` only when the text leaves room:
/// erasing from a cursor parked on the last column would wipe that column's character.
fn put_row(out: &mut String, row: u16, cols: u16, text: &str, sgr: Option<&str>) {
    let (text, w) = fit(text, cols);
    out.push_str(&format!("\x1b[{row};1H"));
    if let Some(sgr) = sgr {
        out.push_str(sgr);
        out.push_str(&text);
        out.push_str("\x1b[0m");
    } else {
        out.push_str(&text);
    }
    if w < cols {
        out.push_str("\x1b[K");
    }
}

/// One full redraw, wrapped in synchronized output.
pub fn draw(cols: u16, rows: u16, last: Option<&str>, wide: bool) -> Vec<u8> {
    let mut out = String::from("\x1b[?2026h");
    if cols > 0 {
        let mut lines: Vec<(String, Option<&str>)> = (0..10).map(|k| (format!("ROW {k}"), None)).collect();
        lines.push(("TRUECOLOR".into(), Some(TRUECOLOR_ON)));
        lines.push((last.map(|ev| format!("LAST {ev}")).unwrap_or_default(), None));
        if wide {
            lines.push((WIDE.into(), None));
        }
        for (k, (text, sgr)) in lines.iter().enumerate() {
            let row = k as u16 + 1;
            if row > rows {
                break;
            }
            put_row(&mut out, row, cols, text, *sgr);
        }
        out.push_str(&format!("\x1b[{};1H", rows.clamp(1, FLOOD_TOP)));
    }
    out.push_str("\x1b[?2026l");
    out.into_bytes()
}

/// How `--flood` lines go out at this size.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Flood {
    /// Scroll region `FLOOD_TOP..=rows`, lines printed at its bottom (README).
    Region,
    /// Exactly one row below the fixed rows: each line overwrites row `FLOOD_TOP`.
    OneRow,
    /// No row below the fixed rows: nothing is printed (the counter still advances).
    Hidden,
}

pub fn flood_mode(rows: u16) -> Flood {
    if rows > FLOOD_TOP {
        Flood::Region
    } else if rows == FLOOD_TOP {
        Flood::OneRow
    } else {
        Flood::Hidden
    }
}

/// The scroll-region setting for this size (`CSI r` resets it when the window is too small).
pub fn flood_region(rows: u16) -> String {
    match flood_mode(rows) {
        Flood::Region => format!("\x1b[{FLOOD_TOP};{rows}r"),
        _ => "\x1b[r".into(),
    }
}

/// Lines `FLOOD first` … `FLOOD first+count-1` for a `cols` × `rows` screen.
pub fn flood_lines(cols: u16, rows: u16, first: u64, count: u64) -> Vec<u8> {
    let mut out = String::new();
    match flood_mode(rows) {
        Flood::Region => {
            out.push_str(&format!("\x1b[{rows};1H"));
            for k in first..first + count {
                out.push_str("\r\n");
                out.push_str(&fit(&format!("FLOOD {k}"), cols).0);
            }
        }
        Flood::OneRow if count > 0 => put_row(&mut out, FLOOD_TOP, cols, &format!("FLOOD {}", first + count - 1), None),
        _ => {}
    }
    out.into_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rows_fit_small_windows() {
        let s = String::from_utf8(draw(4, 3, None, true)).unwrap();
        assert!(s.contains("\x1b[1;1HROW \x1b[2;1HROW \x1b[3;1HROW \x1b[3;1H"), "{s:?}");
        let s = String::from_utf8(draw(8, 60, Some("ready"), true)).unwrap();
        assert!(s.contains("WIDE 漢") && !s.contains("WIDE 漢字"), "{s:?}");
        assert!(s.contains("LAST rea") && !s.contains("\x1b[2J"), "{s:?}");
        assert_eq!(flood_region(57), "\x1b[14;57r");
        assert_eq!(flood_region(14), "\x1b[r");
        assert_eq!(flood_mode(10), Flood::Hidden);
        assert!(flood_lines(80, 10, 0, 5).is_empty());
        assert_eq!(String::from_utf8(flood_lines(80, 14, 0, 3)).unwrap(), "\x1b[14;1HFLOOD 2\x1b[K");
    }
}
