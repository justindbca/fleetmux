//! Cell width of a character (design.md §7.4 #2, plan.md §10 #8). Re-exported from the crate root.
//!
//! One rule for the whole crate: `unicode-width` 0.2 `UnicodeWidthChar::width` (non-CJK). It is the same call,
//! from the same crate version (Cargo.lock has a single `unicode-width`), that `alacritty_terminal` uses to lay
//! out the grid, so [`cell_width`] always agrees with the cells [`crate::alacritty::Alacritty`] produces.

use unicode_width::UnicodeWidthChar;

/// Screen cells `ch` occupies, by `unicode-width` (non-CJK rules): 2 for East Asian Wide / Fullwidth, 0 for
/// zero-width characters (combining marks; control characters also 0), 1 otherwise — so East Asian Ambiguous
/// characters (`│`, `■`, `α`, …) take one cell. The parser's grid follows the same rule: a width-2 character is
/// always followed by one placeholder cell (`Cell::width == 0`).
pub fn cell_width(ch: char) -> u8 {
    // `None` = control character (the grid does not print it either).
    ch.width().map_or(0, |w| w.min(2) as u8)
}
