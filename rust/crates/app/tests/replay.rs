//! Recording replay snapshots (plan.md §6 `replay` package, design §7.2 item 3, brief-m03 §3.4 / P-10).
//! M3 (B24): the 9 redacted recordings are in place, so these tests run without `#[ignore]`. Fixtures:
//! `rust/fixtures/recordings/<cli>-<n>.rec` + `<cli>-<n>.meta.json` (`{cols, rows, cli_version, recorded_at,
//! redacted: true}`), cli ∈ claude / codex / pi, n = 1..3; snapshots `tests/snapshots/replay__<cli>_<n>.snap`
//! (insta).
use fleetmux_term::TermParser;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::time::Duration;

const CLIS: [&str; 3] = ["claude", "codex", "pi"];

fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/recordings")
}

/// The 9 expected fixture names (`claude-1` … `pi-3`).
fn expected() -> Vec<String> {
    CLIS.iter()
        .flat_map(|cli| (1..=3).map(move |n| format!("{cli}-{n}")))
        .collect()
}

/// Panics listing every expected fixture whose `.rec` or `.meta.json` is missing.
fn require_fixtures() -> Vec<String> {
    let dir = fixtures_dir();
    let missing: Vec<String> = expected()
        .into_iter()
        .filter(|n| {
            !dir.join(format!("{n}.rec")).is_file() || !dir.join(format!("{n}.meta.json")).is_file()
        })
        .collect();
    assert!(
        missing.is_empty(),
        "{} of 9 recordings missing in {}: {missing:?}",
        missing.len(),
        dir.display()
    );
    expected()
}

/// Entries of a `.rec` file (PROTOCOL.md §7): `offset_us` u64 BE + `length` u32 BE + bytes, no header.
/// EOF inside an entry is an error.
fn read_rec(path: &Path) -> Vec<(u64, Vec<u8>)> {
    let data = std::fs::read(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    let mut entries = Vec::new();
    let mut at = 0usize;
    while at < data.len() {
        assert!(at + 12 <= data.len(), "{}: truncated entry header at byte {at}", path.display());
        let offset = u64::from_be_bytes(data[at..at + 8].try_into().unwrap());
        let len = u32::from_be_bytes(data[at + 8..at + 12].try_into().unwrap()) as usize;
        assert!((1..=1 << 20).contains(&len), "{}: entry length {len} at byte {at}", path.display());
        let body = at + 12;
        assert!(body + len <= data.len(), "{}: truncated entry at byte {at}", path.display());
        entries.push((offset, data[body..body + len].to_vec()));
        at = body + len;
    }
    entries
}

/// Size the fixture was recorded at.
fn meta(name: &str) -> (u16, u16) {
    let path = fixtures_dir().join(format!("{name}.meta.json"));
    let m: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap())
        .unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    assert_eq!(m["redacted"], Value::Bool(true), "{name} is not marked redacted: {m}");
    (m["cols"].as_u64().unwrap() as u16, m["rows"].as_u64().unwrap() as u16)
}

/// Replays a fixture into a fresh parser of its recorded size.
fn replay(name: &str) -> (fleetmux_term::alacritty::Alacritty, u16, u16) {
    let (cols, rows) = meta(name);
    let mut t = fleetmux_term::alacritty::new(cols, rows, 10_000);
    for (_, bytes) in read_rec(&fixtures_dir().join(format!("{name}.rec"))) {
        t.feed(&bytes);
    }
    t.feed(&[]);
    (t, cols, rows)
}

#[test]
fn recorded_sessions_match_snapshots() {
    for name in require_fixtures() {
        let (t, _, rows) = replay(&name);
        insta::assert_snapshot!(name.replace('-', "_"), t.text(rows as usize));
    }
}

#[test]
fn sync_output_held_until_end_or_150ms() {
    let row0 = |t: &fleetmux_term::alacritty::Alacritty| -> String {
        (0..20).map(|c| t.cell(0, c).ch).collect::<String>().trim_end().to_string()
    };
    // Held until `?2026l`.
    let mut t = fleetmux_term::alacritty::new(40, 5, 100);
    t.feed(b"\x1b[?2026h\x1b[HHELD");
    assert_eq!(row0(&t), "", "drawn inside a synchronized update");
    t.feed(b"\x1b[?2026l");
    assert_eq!(row0(&t), "HELD");
    // Or until the 150 ms timeout, which `feed(&[])` runs.
    let mut t = fleetmux_term::alacritty::new(40, 5, 100);
    t.feed(b"\x1b[?2026h\x1b[HLATE");
    t.feed(&[]);
    assert_eq!(row0(&t), "", "released before the timeout");
    std::thread::sleep(Duration::from_millis(200));
    t.feed(&[]);
    assert_eq!(row0(&t), "LATE", "not released after 150 ms");
    // Every recorded session ends with nothing held back.
    for name in require_fixtures() {
        let (t, _, _) = replay(&name);
        assert!(!t.modes().sync_update, "{name} ends inside a synchronized update");
    }
}

#[test]
fn wide_char_cells_align_with_unicode_width() {
    for name in require_fixtures() {
        let (t, cols, rows) = replay(&name);
        for row in 0..rows {
            for col in 0..cols {
                let c = t.cell(row, col);
                if c.width == 2 {
                    assert!(col + 1 < cols, "{name}: wide {:?} in the last column of row {row}", c.ch);
                    let next = t.cell(row, col + 1);
                    assert_eq!(next.width, 0, "{name}: ({row},{col}) {:?} not followed by a placeholder: {next:?}", c.ch);
                }
                if c.width == 0 {
                    assert!(
                        col > 0 && t.cell(row, col - 1).width == 2,
                        "{name}: placeholder at ({row},{col}) without a wide char before it"
                    );
                }
            }
        }
    }
}
