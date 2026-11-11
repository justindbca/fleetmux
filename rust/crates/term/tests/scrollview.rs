//! Scrollback view, selection and search on `alacritty::Alacritty` (brief-m03 §3.4; package B2
//! `term-alacritty`, M0). Red until B2.

use fleetmux_term::TermParser;
use fleetmux_term::alacritty::{self, Alacritty};

/// View row `row` as text: placeholder cells skipped, trailing blanks stripped.
fn row(t: &Alacritty, row: u16, cols: u16) -> String {
    let s: String = (0..cols)
        .map(|c| t.cell(row, c))
        .filter(|c| c.width != 0)
        .map(|c| if c.ch == '\0' { ' ' } else { c.ch })
        .collect();
    s.trim_end().to_string()
}

/// A 20 × 24 terminal fed `lines` each followed by `\r\n`.
fn fed_lines(lines: &[String]) -> Alacritty {
    let mut t = alacritty::new(20, 24, 1000);
    for l in lines {
        t.feed(format!("{l}\r\n").as_bytes());
    }
    t
}

#[test]
fn display_offset_shows_older_rows_and_clamps() {
    // 30 lines `L0`..`L29`, each ending in `\r\n`, on a 24-row screen: the cursor ends on a 31st (blank) line, so
    // 31 − 24 = 7 lines (`L0`..`L6`) are in the scrollback and live row 0 is `L7`.
    let lines: Vec<String> = (0..30).map(|i| format!("L{i}")).collect();
    let mut t = fed_lines(&lines);
    assert_eq!(t.scrollback_len(), 7);
    assert_eq!(t.display_offset(), 0);
    assert_eq!(row(&t, 0, 20), "L7");

    t.set_display_offset(3);
    assert_eq!(t.display_offset(), 3);
    assert_eq!(row(&t, 0, 20), "L4");
    assert_eq!(row(&t, 3, 20), "L7");

    t.set_display_offset(999);
    assert_eq!(t.display_offset(), t.scrollback_len());
    assert_eq!(row(&t, 0, 20), "L0");

    t.set_display_offset(0);
    assert_eq!(row(&t, 0, 20), "L7");
}

#[test]
fn selection_text_spans_rows_and_strips_trailing_blanks() {
    let mut t = alacritty::new(20, 5, 100);
    t.feed("hello world\r\nsecond line   \r\nthird\r\n漢字ab".as_bytes());
    // (row, col), both ends inclusive, either order.
    assert_eq!(t.selection_text((0, 6), (2, 2)), "world\nsecond line\nthi");
    assert_eq!(t.selection_text((2, 2), (0, 6)), "world\nsecond line\nthi");
    assert_eq!(t.selection_text((1, 0), (1, 19)), "second line");
    assert_eq!(t.selection_text((1, 0), (1, 5)), "second");
    assert_eq!(t.selection_text((3, 0), (3, 19)), "漢字ab");
}

#[test]
fn search_up_finds_latest_match_first() {
    // Same geometry as above: `L0`..`L6` in the scrollback, live row 0 = `L7`; line `Lk` (k ≤ 7) sits at view
    // row 0 at offset 7 − k. Hits at lines 2 and 5 → offsets 5 and 2; the newer (line 5) comes first.
    let lines: Vec<String> = (0..30)
        .map(|i| match i {
            2 => "needle two".to_string(),
            5 => "needle five".to_string(),
            _ => format!("L{i}"),
        })
        .collect();
    let mut t = fed_lines(&lines);
    assert_eq!(t.search_up("needle", 0), Some(2));
    assert_eq!(t.search_up("needle", 2), Some(5));
    assert_eq!(t.search_up("needle", 5), None);
    assert_eq!(t.search_up("absent", 0), None);
    t.set_display_offset(2);
    assert_eq!(row(&t, 0, 20), "needle five");
}

/// The text of the word under view cell `at` (double click, PROTOCOL.md §11), `None` on a boundary cell.
fn word(t: &Alacritty, at: (u16, u16)) -> Option<String> {
    t.word_at(at).map(|(from, to)| t.selection_text(from, to))
}

#[test]
fn word_at_stops_at_the_ghostty_boundary_set() {
    // Ghostty's default `selection-word-chars`: space, tab, ' " │ ` | : ; , ( ) [ ] { } < > $
    let mut t = alacritty::new(60, 5, 100);
    t.feed("see src/a.rs:12 (x) $HOME│b 'q' a,b;c `d` <e>|f{g}\r\n".as_bytes());
    assert_eq!(word(&t, (0, 4)).as_deref(), Some("src/a.rs"), "'.' and '/' are word chars");
    assert_eq!(word(&t, (0, 11)).as_deref(), Some("src/a.rs"));
    assert_eq!(word(&t, (0, 14)).as_deref(), Some("12"), "':' is a boundary");
    assert_eq!(word(&t, (0, 17)).as_deref(), Some("x"));
    assert_eq!(word(&t, (0, 22)).as_deref(), Some("HOME"), "'$' is a boundary");
    assert_eq!(word(&t, (0, 26)).as_deref(), Some("b"), "'│' is a boundary");
    assert_eq!(word(&t, (0, 29)).as_deref(), Some("q"));
    assert_eq!(word(&t, (0, 34)).as_deref(), Some("b"), "',' and ';' are boundaries");
    assert_eq!(word(&t, (0, 39)).as_deref(), Some("d"));
    assert_eq!(word(&t, (0, 43)).as_deref(), Some("e"));
    assert_eq!(word(&t, (0, 46)).as_deref(), Some("f"));
    assert_eq!(word(&t, (0, 48)).as_deref(), Some("g"));
    assert_eq!(t.word_at((0, 3)), None, "a space selects nothing");
    assert_eq!(t.word_at((0, 12)), None, "a boundary char selects nothing");
    assert_eq!(t.word_at((0, 59)), None, "the blank row end selects nothing");
}

#[test]
fn word_at_takes_wide_cells_and_follows_soft_wraps() {
    let mut t = alacritty::new(10, 5, 100);
    t.feed("漢字ab cd\r\n".as_bytes());
    // Column 1 is the placeholder of 漢: it belongs to the word too.
    assert_eq!(word(&t, (0, 1)).as_deref(), Some("漢字ab"));
    assert_eq!(word(&t, (0, 7)).as_deref(), Some("cd"));
    // 25 characters on a 10-column screen: rows 1–3 are one logical line (`x /usr/loc`, `al/lib/abc`, `def y`).
    t.feed(b"x /usr/local/lib/abcdef y\r\n");
    for at in [(1, 4), (2, 3), (3, 1)] {
        assert_eq!(word(&t, at).as_deref(), Some("/usr/local/lib/abcdef"), "{at:?}");
    }
}

#[test]
fn line_at_follows_soft_wraps_and_trims_trailing_spaces() {
    let mut t = alacritty::new(10, 6, 100);
    t.feed(b"short   \r\nabcdefghijklmnopqrstuvw\r\nlast\r\n");
    let line = |t: &Alacritty, row| {
        let (from, to) = t.line_at(row);
        t.selection_text(from, to)
    };
    assert_eq!(line(&t, 0), "short");
    // Rows 1–3 are one logical line (two soft wraps); any of them selects all of it, joined without `\n`.
    assert_eq!(t.line_at(1), ((1, 0), (3, 9)));
    assert_eq!(t.line_at(3), ((1, 0), (3, 9)));
    assert_eq!(line(&t, 2), "abcdefghijklmnopqrstuvw");
    assert_eq!(line(&t, 4), "last");
    assert_eq!(line(&t, 5), "", "a blank row copies as empty");
}

#[test]
fn word_and_line_read_the_scrolled_back_view() {
    let lines: Vec<String> = (0..30).map(|i| format!("L{i} tail{i}")).collect();
    let mut t = fed_lines(&lines);
    t.set_display_offset(3);
    // At offset 3 view row 0 is `L4` (see `display_offset_shows_older_rows_and_clamps`).
    assert_eq!(word(&t, (0, 0)).as_deref(), Some("L4"));
    assert_eq!(word(&t, (1, 5)).as_deref(), Some("tail5"));
    let (from, to) = t.line_at(2);
    assert_eq!(t.selection_text(from, to), "L6 tail6");
}
