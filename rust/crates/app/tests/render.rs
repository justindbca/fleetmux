//! What a view connection shows (brief-m03 §3.4 `tests/render.rs`, plan.md §4.1 attach-render, design.md §2.7,
//! §3.1). M0: red until `attach-render` (B7). Assertions read the viewer's cells through `alacritty`, not the
//! escape bytes; positions are the 240x60 contract cells of `common` (default 50 / 60 ratios).
mod common;
use common::*;

const T: Duration = Duration::from_secs(10);

fn is_box(ch: char) -> bool {
    ('\u{2500}'..='\u{257f}').contains(&ch)
}

/// One supervisor, two workers and a history pane, each printing a marker and then idling.
/// Returns [supervisor, worker-1, worker-2, history].
fn page(srv: &Server) -> [String; 4] {
    [
        sh(srv, "fx-supervisor-1", "supervisor", "echo SUP-BODY; exec sleep 1000"),
        sh(srv, "fx-worker-1", "worker", "echo W1-BODY; exec sleep 1000"),
        sh(srv, "fx-worker-2", "worker", "echo W2-BODY; exec sleep 1000"),
        sh(srv, "history", "history", "echo HIST-BODY; exec sleep 1000"),
    ]
}

/// What tells a highlighted border from a plain one.
fn style(c: Cell) -> (bool, Color) {
    (c.attrs.bold, c.fg)
}

#[test]
fn three_groups_and_tabs_render_at_240x60() {
    let srv = Server::start();
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    for marker in ["SUP-BODY", "W1-BODY", "HIST-BODY"] {
        v.wait_text(marker, T);
    }
    let screen = v.text();

    // Tab rows: supervisor and worker tabs on the top border, history on the left column's separator row.
    let top = v.row_text(0);
    for title in ["fx-supervisor-1", "fx-worker-1", "fx-worker-2"] {
        assert!(top.contains(title), "{title} missing from the tab row {top:?}\n{screen}");
    }
    let mid = v.row_text(HSEP_ROW);
    assert!(mid.contains("history"), "history tab missing from row {HSEP_ROW}: {mid:?}\n{screen}");

    // Three group borders: outer left / right edges, the vertical separator, the horizontal separator and
    // the bottom edge are box-drawing cells.
    let border_cells = [
        (20, 0),
        (45, 0),
        (30, W - 1),
        (20, VSEP_COL),
        (45, VSEP_COL),
        (HSEP_ROW, 100),
        (0, 100),
        (0, 230),
        (STATUS_ROW - 1, 60),
        (STATUS_ROW - 1, 180),
    ];
    for (row, col) in border_cells {
        let c = v.cell(row, col);
        assert!(is_box(c.ch), "({row},{col}) is not a border cell: {c:?}\n{screen}");
    }

    // Each body is drawn inside its own group; only the selected worker tab is drawn.
    let at = |v: &mut Viewer, s: &str| v.find(s).unwrap_or_else(|| panic!("{s} not on screen"));
    let (r, c) = at(&mut v, "SUP-BODY");
    assert!(r < HSEP_ROW && c < VSEP_COL, "SUP-BODY at ({r},{c}) is outside the supervisor group");
    let (r, c) = at(&mut v, "HIST-BODY");
    assert!(r > HSEP_ROW && c < VSEP_COL, "HIST-BODY at ({r},{c}) is outside the lower group");
    let (r, c) = at(&mut v, "W1-BODY");
    assert!(r > 0 && r < STATUS_ROW && c > VSEP_COL, "W1-BODY at ({r},{c}) is outside the workers group");
    assert!(!screen.contains("W2-BODY"), "a background worker tab was drawn:\n{screen}");

    let status = v.status_line();
    assert!(status.contains("^b"), "status line lacks the prefix hint: {status:?}");
}

#[test]
fn focused_pane_border_highlight() {
    let srv = Server::start();
    let [sup, w1, _, _] = page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);

    // Left edge of the supervisor and lower groups, right edge of the workers group.
    let borders = |v: &mut Viewer| {
        (
            style(v.cell(20, 0)),
            style(v.cell(45, 0)),
            style(v.cell(30, W - 1)),
        )
    };
    srv.ok("pane.focus", json!({"pane": w1}));
    wait_until("the workers border to be highlighted", T, || {
        let (s, l, w) = borders(&mut v);
        (w != s && s == l).then_some(())
    });
    srv.ok("pane.focus", json!({"pane": sup}));
    wait_until("the supervisor border to be highlighted", T, || {
        let (s, l, w) = borders(&mut v);
        (s != l && l == w).then_some(())
    });
}

#[test]
fn diff_only_sends_changed_cells() {
    let srv = Server::start();
    sh(&srv, "fx-supervisor-1", "supervisor", "echo SUP-BODY; exec sleep 1000");
    sh(&srv, "history", "history", "echo HIST-BODY; exec sleep 1000");
    // No tty echo, so the write below changes exactly one line of one pane.
    let w = sh(
        &srv,
        "fx-worker-1",
        "worker",
        "stty -echo; echo W1-READY; read x; echo CHANGED-LINE; exec sleep 1000",
    );
    let mut v = Viewer::attach(&srv, W, H);
    for marker in ["SUP-BODY", "HIST-BODY", "W1-READY"] {
        v.wait_text(marker, T);
    }
    let full = v.wait_quiet(Duration::from_millis(300), T);

    srv.write(&w, b"go\n", "raw");
    v.wait_text("CHANGED-LINE", T);
    let after = v.wait_quiet(Duration::from_millis(300), T);
    let delta = after - full;
    assert!(
        delta * 5 < full,
        "one changed line cost {delta} screen bytes, full screen was {full} (must be < 20%)"
    );
}

// ---- Coverage additions (fix-a-coverage-view, reviewer-1 coverage-a.md G1 / G2). ----

/// G1, PROTOCOL.md §10: after the prefix, `Left` / `Right` move focus between the left column and `workers`,
/// `Up` / `Down` between `supervisor` and `lower`; `1`–`9` select the n-th tab of the focused group (out of
/// range: nothing), `Tab` / `BTab` the next / previous one, wrapping.
#[test]
fn prefix_digits_tab_btab_and_arrows_switch_tabs_and_groups() {
    let srv = Server::start();
    let sup = sh(&srv, "fx-supervisor-1", "supervisor", "exec sleep 1000");
    sh(&srv, "history", "history", "exec sleep 1000");
    let w1 = sh(&srv, "fx-worker-1", "worker", "exec sleep 1000");
    let w2 = sh(&srv, "fx-worker-2", "worker", "exec sleep 1000");
    let w3 = sh(&srv, "fx-worker-3", "worker", "exec sleep 1000");
    srv.ok("pane.focus", json!({"pane": w1}));
    srv.ok("pane.focus", json!({"pane": sup}));
    srv.wait_layout("focus on the supervisor, worker-1 selected", T, |l| {
        l["focus"] == json!("supervisor") && l["selected"]["workers"] == json!(w1)
    });
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("fx-worker-3", T);

    let focus = |g: &'static str| move |l: &Value| l["focus"] == json!(g);
    for (key, group) in [
        (&b"\x1b[C"[..], "workers"),
        (b"\x1b[D", "supervisor"),
        (b"\x1b[B", "lower"),
        (b"\x1b[A", "supervisor"),
        (b"\x1b[C", "workers"),
    ] {
        v.input(&prefixed(key));
        srv.wait_layout(&format!("prefix {key:?} -> focus {group}"), T, focus(group));
    }

    let worker = |id: &String| {
        let id = json!(id);
        move |l: &Value| l["focus"] == json!("workers") && l["selected"]["workers"] == id
    };
    for (key, want, what) in [
        (&b"3"[..], &w3, "3 -> worker-3"),
        (b"1", &w1, "1 -> worker-1"),
        // `9` is out of range: nothing changes, so the next Tab starts from worker-1.
        (b"9", &w1, "9 -> unchanged"),
        (b"\t", &w2, "Tab -> worker-2"),
        (b"\t", &w3, "Tab -> worker-3"),
        (b"\t", &w1, "Tab wraps -> worker-1"),
        (b"\x1b[Z", &w3, "BTab wraps -> worker-3"),
        (b"\x1b[Z", &w2, "BTab -> worker-2"),
    ] {
        v.input(&prefixed(key));
        srv.wait_layout(what, T, worker(want));
    }
}

/// G2, design.md §2.6 / PROTOCOL.md §6.3: bytes of a viewer's `0x01` frame that are not the prefix, an SGR mouse
/// report or an outer paste block reach the focused pane unchanged, and no other pane. The panes run with
/// `stty raw -echo` and record what they read, so tty echo and line editing play no part.
#[test]
fn viewer_input_bytes_reach_only_the_focused_pane_unchanged() {
    let srv = Server::start();
    let recorder = |file: &Path| {
        format!(
            "stty raw -echo; echo READY; head -c 7 > '{}'; echo GOT; exec sleep 1000",
            file.display()
        )
    };
    let (sup_file, w_file) = (
        srv.tmp.path().join("g2-supervisor.bin"),
        srv.tmp.path().join("g2-worker.bin"),
    );
    let sup = sh(&srv, "fx-supervisor-1", "supervisor", &recorder(&sup_file));
    let w = sh(&srv, "fx-worker-1", "worker", &recorder(&w_file));
    srv.wait_text(&sup, "READY", T);
    srv.wait_text(&w, "READY", T);
    srv.ok("pane.focus", json!({"pane": w}));
    srv.wait_layout("focus on workers", T, |l| l["focus"] == json!("workers"));

    let mut v = Viewer::attach(&srv, W, H);
    v.wait_quiet(Duration::from_millis(200), T);
    // Printable bytes, a control byte that is not the prefix (C-a), DEL and CR.
    const BYTES: &[u8] = b"g2:x\x01\x7f\r";
    v.input(BYTES);
    wait_file_eq(&w_file, BYTES, T);
    srv.wait_text(&w, "GOT", T);
    std::thread::sleep(Duration::from_millis(300));
    let other = std::fs::read(&sup_file).unwrap_or_default();
    assert!(
        other.is_empty(),
        "an unfocused pane received viewer input: {}",
        hex(&other)
    );
}

// ---- fix-m1-tabs (supervisor [M48], operator Mac acceptance §7 / real-mouse P2): tab blocks on a tab bar. ----

/// The palette index a 16-colour terminal is sure to show for `c` (`None` for 256-colour / RGB / default).
fn base16(c: Color) -> Option<u8> {
    match c {
        Color::Indexed(n) if n < 16 => Some(n),
        _ => None,
    }
}

/// A horizontal box-drawing line glyph.
fn is_hline(ch: char) -> bool {
    matches!(ch, '─' | '━' | '╌' | '╍' | '┄' | '┅' | '┈' | '┉')
}

/// The four-pane page with the supervisor group focused; waits until every body is on screen.
fn tab_page(srv: &Server, v: &mut Viewer) -> [String; 4] {
    let p = page(srv);
    srv.ok("pane.focus", json!({"pane": p[0]}));
    srv.wait_layout("focus on supervisor", T, |l| {
        l["focus"] == json!("supervisor")
    });
    for marker in ["SUP-BODY", "W1-BODY", "HIST-BODY"] {
        v.wait_text(marker, T);
    }
    v.wait_quiet(Duration::from_millis(200), T);
    p
}

/// The first cell of `title` on `row` (tab rows only).
fn title_at(v: &mut Viewer, title: &str) -> (u16, u16) {
    let (row, col) = v.wait_find(title, T);
    assert!(
        row == 0 || row == HSEP_ROW,
        "{title} found off the tab rows at ({row},{col})"
    );
    (row, col)
}

#[test]
fn tabs_are_text_on_the_border_row_with_thin_separators() {
    let srv = Server::start();
    let mut v = Viewer::attach(&srv, W, H);
    tab_page(&srv, &mut v);
    let screen = v.text();

    // An unselected tab (fx-worker-2, which may carry the unseen `•` before its title) is plain text on the
    // group's border row: no background of its own, a visible foreground, not bold.
    let (row, col) = title_at(&mut v, "fx-worker-2");
    let len = "fx-worker-2".len() as u16;
    let t = v.cell(row, col);
    assert_eq!(
        t.bg,
        Color::Default,
        "unselected tab has a background of its own: {t:?}\n{screen}"
    );
    assert_ne!(
        t.fg,
        Color::Default,
        "unselected tab text has no colour: {t:?}\n{screen}"
    );
    assert!(
        !t.attrs.bold,
        "unselected tab text is bold: {t:?}\n{screen}"
    );

    // A selected tab of a group without focus is one block: a single background, one blank padding column at
    // each end of the block.
    let (srow, scol) = title_at(&mut v, "fx-worker-1");
    let slen = "fx-worker-1".len() as u16;
    let s = v.cell(srow, scol);
    assert_ne!(
        s.bg,
        Color::Default,
        "selected tab has no block background: {s:?}\n{screen}"
    );
    let mut left = scol;
    while left > 0 && v.cell(srow, left - 1).bg == s.bg {
        left -= 1;
    }
    let mut right = scol + slen - 1;
    while right + 1 < W && v.cell(srow, right + 1).bg == s.bg {
        right += 1;
    }
    for (what, c) in [("left", left), ("right", right)] {
        let p = v.cell(srow, c);
        assert!(
            p.ch == ' ' && c != scol && c != scol + slen - 1,
            "{what} end of fx-worker-1's block (cols {left}..={right}) is not a blank padding column: {p:?}\n{screen}"
        );
    }

    // The tab rows are the groups' border rows: content background, not a bar of their own.
    for (r, c) in [(0, 100), (HSEP_ROW, 100)] {
        let cell = v.cell(r, c);
        assert_eq!(
            cell.bg,
            Color::Default,
            "tab row ({r},{c}) is not the content background: {cell:?}\n{screen}"
        );
    }

    // The gap column before fx-worker-2's block holds a thin separator, darker than the group's border.
    let edge = v.cell(20, W - 1);
    let mut x = col;
    let mut seen_sep = false;
    while x > 0 {
        let c = v.cell(row, x - 1);
        if c.ch == ' ' || c.ch == '•' {
            x -= 1;
            continue;
        }
        assert!(
            c.ch == '│',
            "the column before fx-worker-2's block is {:?}, not a thin separator: {}\n{screen}",
            c.ch,
            v.row_text(row)
        );
        assert_eq!(
            c.bg,
            Color::Default,
            "the thin separator has a background: {c:?}\n{screen}"
        );
        assert_ne!(
            c.fg,
            Color::Default,
            "the thin separator has no colour: {c:?}\n{screen}"
        );
        assert_ne!(
            c.fg, edge.fg,
            "the thin separator is as bright as the group border: {c:?} vs {edge:?}\n{screen}"
        );
        seen_sep = true;
        break;
    }
    assert!(
        seen_sep,
        "no thin separator left of fx-worker-2's block: {}\n{screen}",
        v.row_text(row)
    );

    // After the group's last tab the row is the border line again, not another separator.
    let mut x = col + len - 1;
    let mut seen_line = false;
    while x + 1 < W {
        let c = v.cell(row, x + 1);
        if c.ch == ' ' {
            x += 1;
            continue;
        }
        assert_eq!(
            c.ch,
            '─',
            "after the last tab the row is {:?}, not the border line: {}\n{screen}",
            c.ch,
            v.row_text(row)
        );
        seen_line = true;
        break;
    }
    assert!(
        seen_line,
        "no border line after the last tab: {}\n{screen}",
        v.row_text(row)
    );
}

#[test]
fn selected_tabs_show_focus_and_non_focus_styles() {
    let srv = Server::start();
    let mut v = Viewer::attach(&srv, W, H);
    let [_, w1, _, _] = tab_page(&srv, &mut v);
    let idle = {
        let (r, c) = title_at(&mut v, "fx-worker-2");
        v.cell(r, c)
    };
    let screen = v.text();
    assert_eq!(
        idle.bg,
        Color::Default,
        "unselected tab has a background of its own: {idle:?}\n{screen}"
    );
    let check = |v: &mut Viewer, focused: &str, other: &str| {
        let (r, c) = title_at(v, focused);
        let f = v.cell(r, c);
        let (r, c) = title_at(v, other);
        let o = v.cell(r, c);
        let screen = v.text();
        assert!(
            f.attrs.bold,
            "selected tab of the focused group is not bold: {f:?}\n{screen}"
        );
        assert!(
            !o.attrs.bold,
            "selected tab of an unfocused group is bold: {o:?}\n{screen}"
        );
        assert!(
            f.bg == o.bg && f.bg != Color::Default,
            "the two selected tabs are not the same block: focused {f:?} / unfocused {o:?}\n{screen}"
        );
        assert_ne!(
            f.fg, o.fg,
            "the two selected tabs have the same text colour: focused {f:?} / unfocused {o:?}\n{screen}"
        );
        (f.bg, f.fg, o.fg)
    };
    // Supervisor focused: supervisor tab = focused style, fx-worker-1 = unfocused-selected style.
    let a = check(&mut v, "fx-supervisor-1", "fx-worker-1");
    srv.ok("pane.focus", json!({"pane": w1}));
    srv.wait_layout("focus on workers", T, |l| l["focus"] == json!("workers"));
    wait_until("fx-worker-1 to take the focused style", T, || {
        let (r, c) = v.find("fx-worker-1")?;
        v.cell(r, c).attrs.bold.then_some(())
    });
    let b = check(&mut v, "fx-worker-1", "fx-supervisor-1");
    assert_eq!(a, b, "the two selected styles swapped groups unevenly");
}

#[test]
fn unfocused_borders_are_visible_grey_not_colour_8() {
    let srv = Server::start();
    let mut v = Viewer::attach(&srv, W, H);
    tab_page(&srv, &mut v);
    let focused = v.cell(20, 0);
    for (row, col) in [(30, W - 1), (45, 0), (STATUS_ROW - 1, 180)] {
        let c = v.cell(row, col);
        assert!(is_box(c.ch), "({row},{col}) is not a border cell: {c:?}");
        assert!(
            c.fg != Color::Indexed(8) && c.fg != Color::Default && c.fg != Color::Indexed(0),
            "unfocused border ({row},{col}) is colour 8 / default / black: {c:?}"
        );
        assert_ne!(
            (c.attrs.bold, c.fg),
            (focused.attrs.bold, focused.fg),
            "unfocused border looks focused"
        );
    }
    // The separator column stays a visible line through the tab bar and down the screen.
    for row in [0, 20, 45] {
        let c = v.cell(row, VSEP_COL);
        assert!(
            is_box(c.ch) && !is_hline(c.ch) && c.fg != c.bg,
            "separator at ({row},{VSEP_COL}) not visible: {c:?}"
        );
    }
}

#[test]
fn ratio_changes_leave_no_stray_blocks_on_the_tab_rows() {
    let srv = Server::start();
    let mut v = Viewer::attach(&srv, W, H);
    tab_page(&srv, &mut v);
    // Every cell of a tab row is the content background, except the one block a selected tab draws. A block is
    // a maximal run of one non-default background whose text is a selected tab's title; any other run of a
    // non-default background (a leftover bar segment at the tail or between blocks) is a stray block.
    let row_clean = |v: &mut Viewer, row: u16, to: u16| -> Vec<String> {
        let rowtext = v.row_text(row);
        let cells: Vec<Cell> = (0..to).map(|x| v.cell(row, x)).collect();
        let mut bad = Vec::new();
        let mut x = 0usize;
        while x < cells.len() {
            let bg = cells[x].bg;
            if bg == Color::Default {
                x += 1;
                continue;
            }
            let start = x;
            while x < cells.len() && cells[x].bg == bg {
                x += 1;
            }
            let run = &cells[start..x];
            let text: String = run.iter().map(|c| c.ch).collect();
            if !["fx-supervisor-1", "fx-worker-1", "history"]
                .iter()
                .any(|t| text.contains(t))
            {
                for (i, c) in run.iter().enumerate() {
                    bad.push(format!(
                        "row {row} {rowtext:?}: ({row},{}) {:?} fg {:?} bg {:?} (run {text:?})",
                        start + i,
                        c.ch,
                        c.fg,
                        c.bg
                    ));
                }
            }
        }
        bad
    };
    let mut bad = Vec::new();
    for (h, v_ratio) in [(40, 60), (70, 40), (50, 60)] {
        srv.ok("layout.set", json!({"h_ratio": h, "v_ratio": v_ratio}));
        srv.wait_layout("ratios applied", T, |l| {
            l["h_ratio"] == json!(h) && l["v_ratio"] == json!(v_ratio)
        });
        v.wait_quiet(Duration::from_millis(300), T);
        // The lower tab row follows v_ratio; find it by the history tab.
        let (hrow, _) = v.wait_find("history", T);
        // The lower tab bar spans the left column: up to and including the vertical separator.
        let vsep = (1..W - 1)
            .find(|&x| {
                let c = v.cell(20, x);
                is_box(c.ch) && !is_hline(c.ch)
            })
            .expect("vertical separator on row 20");
        bad.extend(
            row_clean(&mut v, 0, W)
                .into_iter()
                .map(|s| format!("h={h} v={v_ratio}: {s}")),
        );
        bad.extend(
            row_clean(&mut v, hrow, vsep + 1)
                .into_iter()
                .map(|s| format!("h={h} v={v_ratio}: {s}")),
        );
    }
    assert!(
        bad.is_empty(),
        "stray non-default-background cells on the tab rows (not part of a selected tab's block):\n{}",
        bad.join("\n")
    );
}

#[test]
fn tab_styles_stay_distinct_in_256_and_16_colours() {
    let srv = Server::start();
    let p = page(&srv);
    srv.ok("pane.focus", json!({"pane": p[0]}));
    srv.wait_layout("focus on supervisor", T, |l| {
        l["focus"] == json!("supervisor")
    });
    // No COLORTERM: the 256-colour viewer.
    let mut v = Viewer::attach_with(&srv, W, H, "");
    for marker in ["SUP-BODY", "W1-BODY", "HIST-BODY"] {
        v.wait_text(marker, T);
    }
    v.wait_quiet(Duration::from_millis(200), T);
    let raw = v.raw();
    assert!(
        !raw.windows(5).any(|w| w == b"38;2;" || w == b"48;2;"),
        "24-bit SGR sent to a viewer without COLORTERM"
    );
    // Focused-selected, unfocused-selected and unselected tabs differ in what a 16-colour terminal shows.
    let state = |v: &mut Viewer, t: &str| {
        let (r, c) = title_at(v, t);
        let x = v.cell(r, c);
        (base16(x.bg), base16(x.fg), x.attrs.bold)
    };
    let s = [
        ("focused selected", state(&mut v, "fx-supervisor-1")),
        ("unfocused selected", state(&mut v, "fx-worker-1")),
        ("unselected", state(&mut v, "fx-worker-2")),
    ];
    for i in 0..s.len() {
        for j in i + 1..s.len() {
            assert_ne!(
                s[i].1, s[j].1,
                "{} and {} look the same in 16 colours: {s:?}",
                s[i].0, s[j].0
            );
        }
    }
}

// ---- fix-m1-tabs revision (supervisor [M59]): a visible grip on the horizontal separator; a 16-colour palette. ----

/// Whether a terminal that only has the 16 ANSI colours can show `c` as sent (the default colour or palette
/// entries 0–15; anything else is a 256-colour / 24-bit value it cannot display).
fn shows_in_16(c: Color) -> bool {
    matches!(c, Color::Default) || matches!(c, Color::Indexed(n) if n < 16)
}

/// The visible grip cells (`═`, fg ≠ bg) on `row`.
fn grip(v: &mut Viewer, row: u16) -> Vec<u16> {
    (0..W)
        .filter(|&x| {
            let c = v.cell(row, x);
            c.ch == '═' && c.fg != c.bg
        })
        .collect()
}

#[test]
fn hsep_shows_a_visible_drag_grip_outside_the_tab_blocks() {
    let srv = Server::start();
    let mut v = Viewer::attach(&srv, W, H);
    tab_page(&srv, &mut v);
    let bar = v.cell(0, 100).bg;
    // Not dragging: the horizontal separator row (the lower tab bar) shows a grip, on bar cells (not on a tab
    // block), clear of the history tab's title.
    let check = |v: &mut Viewer, what: &str| -> (u16, u16) {
        let (row, hcol) = v.wait_find("history", T);
        let xs = grip(v, row);
        let text = v.row_text(row);
        assert!(
            xs.len() >= 3 && xs.windows(2).all(|p| p[1] == p[0] + 1),
            "{what}: no visible drag grip on the horizontal separator row {row}: {text:?}"
        );
        for &x in &xs {
            let c = v.cell(row, x);
            assert_eq!(
                c.bg, bar,
                "{what}: grip cell ({row},{x}) is not on the bar: {c:?}"
            );
            assert!(
                x + 1 < hcol || x > hcol + "history".len() as u16,
                "{what}: grip at {x} touches the history title at {hcol}"
            );
        }
        (xs[xs.len() / 2], row)
    };
    let (gx, gy) = check(&mut v, "default ratios");
    assert_eq!(gy, HSEP_ROW);
    // The grip is a drag point of the separator: dragging it moves the horizontal separator.
    v.input(&sgr_drag((gx, gy), (gx, 25), 0));
    srv.wait_layout("v_ratio changed by dragging the grip", T, |l| {
        l["v_ratio"] != json!(60)
    });
    v.wait_quiet(Duration::from_millis(300), T);
    let (_, row) = check(&mut v, "after the drag");
    assert_ne!(row, HSEP_ROW, "the separator row did not move");
}

#[test]
fn tab_rows_use_only_colours_a_16_colour_terminal_shows() {
    let srv = Server::start();
    let p = page(&srv);
    srv.ok("pane.focus", json!({"pane": p[0]}));
    srv.wait_layout("focus on supervisor", T, |l| {
        l["focus"] == json!("supervisor")
    });
    let mut v = Viewer::attach_with(&srv, W, H, "");
    for marker in ["SUP-BODY", "W1-BODY", "HIST-BODY"] {
        v.wait_text(marker, T);
    }
    v.wait_quiet(Duration::from_millis(200), T);
    // Simulated 16-colour display: every colour on both tab rows must be one it can show.
    let mut bad = Vec::new();
    for row in [0, HSEP_ROW] {
        for x in 0..W {
            let c = v.cell(row, x);
            if !shows_in_16(c.fg) || !shows_in_16(c.bg) {
                bad.push(format!(
                    "({row},{x}) {:?} fg {:?} bg {:?}",
                    c.ch, c.fg, c.bg
                ));
            }
        }
    }
    assert!(
        bad.is_empty(),
        "tab-row colours a 16-colour terminal cannot show:\n{}",
        bad.join("\n")
    );
    // The tab row is the group's border row: the content's own background, not a bar.
    let screen = v.text();
    let row = v.cell(0, 100);
    assert_eq!(
        row.bg,
        Color::Default,
        "the tab row has a background of its own: {row:?}\n{screen}"
    );
    // Focused-selected, unfocused-selected and unselected: three different (background, foreground, bold)
    // triples in 16 colours, and only the selected ones have a background.
    let state = |v: &mut Viewer, t: &str| {
        let (r, c) = title_at(v, t);
        let x = v.cell(r, c);
        (base16(x.bg), base16(x.fg), x.attrs.bold)
    };
    let s = [
        ("focused selected", state(&mut v, "fx-supervisor-1")),
        ("unfocused selected", state(&mut v, "fx-worker-1")),
        ("unselected", state(&mut v, "fx-worker-2")),
    ];
    assert!(
        s[0].1.0.is_some(),
        "the focused selected tab has the content's default background: {s:?}\n{screen}"
    );
    assert!(
        s[2].1.0.is_none(),
        "the unselected tab has a background in 16 colours: {s:?}\n{screen}"
    );
    for i in 0..s.len() {
        for j in i + 1..s.len() {
            assert_ne!(
                s[i].1, s[j].1,
                "{} and {} look the same in 16 colours: {s:?}\n{screen}",
                s[i].0, s[j].0
            );
        }
    }
}

// ------------------------------------------- fleet sidebar (S1-③, plan §4.3) -------------------------

/// The kv `sidebar` value the fleet daemon pushes (plan §4.5): one fleet, the current one.
const ONE: &str = r#"{"v":1,"current":"fleet-20261029-231820-ab12cd34","fleets":[{"id":"fleet-20261029-231820-ab12cd34","title":"fleetmux-demo","state":"running","line1":"运行中 · 监工+2 worker（1忙）","line2":"~/.fleetmux/fleets/fmx-demo"}]}"#;

fn set_sidebar(srv: &Server) {
    srv.ok("kv.set", json!({ "key": "sidebar", "value": ONE }));
}

#[test]
fn no_sidebar_without_the_kv_key() {
    let srv = Server::start();
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    let screen = v.text();
    let c = v.cell(0, 0);
    assert!(
        is_box(c.ch),
        "(0,0) is not the groups' frame without kv sidebar: {c:?}\n{screen}"
    );
    let status = v.status_line();
    assert!(
        !status.contains("侧栏"),
        "the status line mentions the sidebar without kv sidebar: {status:?}\n{screen}"
    );
}

#[test]
fn sidebar_kv_draws_the_card_and_moves_the_groups_right() {
    let srv = Server::start();
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    let screen = v.wait_text("fleetmux-demo", T);
    let top = v.row_text(0);
    assert!(
        top.starts_with(" ◫  +  ↺  ←  →"),
        "row 0 does not start with ' ◫  +  ↺  ←  →': {top:?}\n{screen}"
    );
    let left = v.cell(0, 34);
    assert!(
        is_box(left.ch),
        "(0,34) is not the groups' left border with the sidebar: {left:?}\n{screen}"
    );
    let origin = v.cell(0, 0);
    assert!(
        !is_box(origin.ch),
        "(0,0) is still a group border with the sidebar: {origin:?}\n{screen}"
    );
    assert_eq!(
        v.cell(2, 3).ch,
        'f',
        "the card title does not start at (2,3): {:?}\n{screen}",
        v.cell(2, 3)
    );
    assert!(
        v.row_text(2).contains("fleetmux-demo"),
        "row 2 (the card's first row) lacks the card title: {:?}\n{screen}",
        v.row_text(2)
    );
    let dot = v.cell(2, 1);
    assert_eq!(dot.ch, '●', "the state dot is not at (2,1): {dot:?}\n{screen}");
    assert_eq!(
        dot.fg,
        Color::Indexed(10),
        "the running dot is not light green: {dot:?}\n{screen}"
    );
    for x in 0..=32u16 {
        let c = v.cell(2, x);
        assert_eq!(
            c.bg,
            Color::Indexed(4),
            "the selected card is not a blue bar at (2,{x}): {c:?}\n{screen}"
        );
    }
    let gap = v.cell(2, 33);
    assert_eq!(
        gap.bg,
        Color::Default,
        "the gap column 33 has a background: {gap:?}\n{screen}"
    );
    let status = v.status_line();
    assert!(
        status.contains("^b s 隐藏侧栏"),
        "the status line lacks the sidebar hint: {status:?}\n{screen}"
    );
}

#[test]
fn sidebar_shrinks_the_ptys() {
    let srv = Server::start();
    let p = page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    let before = pane_size(&srv, &p[0]);
    assert_eq!(
        before.0, 118,
        "the supervisor PTY width without the sidebar: {before:?}"
    );
    set_sidebar(&srv);
    let sup = wait_until("the supervisor PTY shrinks to 101 columns", T, || {
        let (w, _) = pane_size(&srv, &p[0]);
        (w == 101).then_some(w)
    });
    let worker = wait_until("a worker PTY shrinks to 102 columns", T, || {
        let (w, _) = pane_size(&srv, &p[1]);
        (w == 102).then_some(w)
    });
    assert_eq!(
        (sup, worker),
        (101, 102),
        "PTY widths with the sidebar: supervisor {sup}, worker {worker}"
    );
}

#[test]
fn bad_sidebar_kv_is_ignored() {
    let srv = Server::start();
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    srv.ok("kv.set", json!({ "key": "sidebar", "value": "not json" }));
    v.wait_quiet(Duration::from_millis(300), T);
    let screen = v.text();
    let c = v.cell(0, 0);
    assert!(
        is_box(c.ch),
        "(0,0) is not the groups' frame after a bad kv sidebar: {c:?}\n{screen}"
    );
    assert!(
        !screen.contains("◫"),
        "a bad kv sidebar was drawn:\n{screen}"
    );
}

#[test]
fn sidebar_survives_zoom() {
    let srv = Server::start();
    let p = page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("SUP-BODY", T);
    srv.ok("pane.focus", json!({ "pane": p[0] }));
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    v.input(&prefixed(b"z"));
    v.wait_quiet(Duration::from_millis(300), T);
    let screen = v.text();
    assert!(
        screen.contains("fleetmux-demo"),
        "the sidebar is gone while zoomed:\n{screen}"
    );
    let (_, col) = v.wait_find("SUP-BODY", T);
    assert!(
        col >= 34,
        "the zoomed pane starts at column {col}, inside the sidebar:\n{screen}"
    );
}

// ------------------------------- fleet sidebar toolbar (S1b-③, plan §11.1) ---------------------------

/// S1b drops the `FLEETS` caption and the `‹` hide glyph: row 0 is the toolbar `◫  +  ←  →` (plan §11.1).
#[test]
fn no_fleets_title_and_the_toolbar_is_on_row_0() {
    let srv = Server::start();
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    let screen = v.wait_text("fleetmux-demo", T);
    let top = v.row_text(0);
    assert!(
        top.starts_with(" ◫  +  ↺  ←  →"),
        "row 0 does not start with the S1b toolbar ' ◫  +  ↺  ←  →': {top:?}\n{screen}"
    );
    assert!(
        !screen.contains("FLEETS"),
        "the S1b sidebar still draws the FLEETS caption:\n{screen}"
    );
    assert!(
        !screen.contains('‹'),
        "the S1b sidebar still draws the '‹' hide glyph:\n{screen}"
    );
}

/// Hidden, the sidebar is a one-column rail whose only glyph is the white `◫` toggle on row 0 (plan §11.1).
#[test]
fn hidden_rail_shows_the_toggle_on_row_0() {
    let srv = Server::start();
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    v.input(&prefixed(b"s"));
    let screen = v.wait_no_text("~/.fleetmux/fleets/fmx-demo", T);
    let toggle = v.cell(0, 1);
    assert_eq!(
        toggle.ch, '◫',
        "the hidden rail's toggle is not at (0,1): {toggle:?}\n{screen}"
    );
    assert_eq!(
        toggle.fg,
        Color::Indexed(15),
        "the rail's toggle is not white: {toggle:?}\n{screen}"
    );
    assert_eq!(
        toggle.bg,
        Color::Indexed(8),
        "the rail's toggle does not sit on dark grey: {toggle:?}\n{screen}"
    );
    // A26 rev7: rows 0 and 1 are the header band (topbar + its blank separation row), so the groups' top-left
    // corner is the first content row, 2.
    let below = v.cell(2, 0);
    assert!(
        is_box(below.ch),
        "隐藏后格子从第 0 列铺满: (2,0) is not the groups' left border while hidden: {below:?}\n{screen}"
    );
}

/// A26 rev7 (CURRENT_REQUIREMENTS rev 10 `macos_header_tab_separation` / rev 11
/// `workspace_tabs_align_first_fleet`): the header band is its own height — a blank separation row under the
/// global header — and the workspace's topmost tab row and the sidebar's first fleet card start on the same row,
/// the band's last row, on both platforms (row 2 on macOS under the in-grid topbar, row 1 on Linux under the
/// native headerbar).
#[test]
fn workspace_tab_row_and_first_fleet_card_start_on_the_same_row() {
    let srv = Server::start();
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    let screen = v.wait_text("fleetmux-demo", T);
    // Server::start explicitly selects the accepted base's in-grid header fixture.
    let row = fleetmux_layout::TOPBAR_ROWS;
    let line = v.row_text(row);
    assert!(
        line.contains("fx-supervisor-1"),
        "row {row} lacks the workspace's topmost tab label: {line:?}\n{screen}"
    );
    assert!(
        line.contains("fleetmux-demo"),
        "row {row} lacks the first fleet card's title: {line:?}\n{screen}"
    );
    let gap = v.row_text(row - 1);
    assert!(
        gap.trim().is_empty(),
        "row {} is not the header band's blank separation row: {gap:?}\n{screen}",
        row - 1
    );
}

/// A26 rev7 (CURRENT_REQUIREMENTS rev 10 `macos_header_tab_separation` / rev 11
/// `workspace_tabs_align_first_fleet`): the header band is its own height — a blank separation row under the
/// global header — and the workspace's topmost tab row and the sidebar's first fleet card start on the same row,
/// the band's last row, on both platforms (row 2 on macOS under the in-grid topbar, row 1 on Linux under the
/// native headerbar).
#[cfg(target_os = "linux")]
#[test]
fn native_linux_workspace_tabs_and_cards_share_the_header_gap() {
    let srv = Server::start_with_env(&[]);
    page(&srv);
    let mut v = Viewer::attach_native(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    let screen = v.wait_text("fleetmux-demo", T);
    let row = if cfg!(target_os = "linux") { 1 } else { 2 };
    let line = v.row_text(row);
    assert!(
        line.contains("fx-supervisor-1"),
        "row {row} lacks the workspace's topmost tab label: {line:?}\n{screen}"
    );
    assert!(
        line.contains("fleetmux-demo"),
        "row {row} lacks the first fleet card's title: {line:?}\n{screen}"
    );
    let gap = v.row_text(row - 1);
    assert!(
        gap.trim().is_empty(),
        "row {} is not the header band's blank separation row: {gap:?}\n{screen}",
        row - 1
    );
}

// ------------------------------ fleet sidebar close box (S1b-④, plan §2.5 / §11.1) -------------------------

/// `^b X` draws the close confirmation box over the panes; `n` closes it and every cell comes back exactly as it
/// was — the panes are re-read, not copied from the last frame, so no box character survives (plan §3.5 / §11.4).
#[test]
fn close_dialog_draws_over_the_panes_and_leaves_no_ghost() {
    use fleetmux::render::sidebar::{Side, modal_rect, modal_text, parse, wrap};

    let srv = Server::start();
    // Long lines, so the rows the box lands on really hold pane characters.
    sh(
        &srv,
        "fx-supervisor-1",
        "supervisor",
        "seq -f 'SUP-ROW-%02g xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' 1 40; exec sleep 1000",
    );
    sh(
        &srv,
        "fx-worker-1",
        "worker",
        "seq -f 'W1-ROW-%02g xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' 1 40; exec sleep 1000",
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("SUP-ROW-40", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    v.wait_quiet(Duration::from_millis(300), T);
    let before = v.text();

    // Where the plan puts the box: `min(60, cols − 4)` wide, centered above the status row.
    let side = Side {
        data: Some(parse(ONE).expect(ONE)),
        ..Side::default()
    };
    let (_, body) = modal_text(&side);
    let r = modal_rect(W, H, wrap(&body, 56).len() as u16);
    let covered = v.cell(r.y, r.x);
    assert_ne!(
        covered.ch, '╭',
        "the box's corner ({},{}) already holds a box glyph before it opens: {covered:?}\n{before}",
        r.y, r.x
    );
    assert!(
        !covered.ch.is_whitespace(),
        "the box's corner ({},{}) covers no pane character, so this test could not see a ghost: {covered:?}",
        r.y, r.x
    );

    v.input(&prefixed(b"X"));
    let screen = v.wait_text("关闭 fleet", T);
    let corner = v.cell(r.y, r.x);
    assert_eq!(
        corner.ch, '╭',
        "the box's corner is not at ({},{}): {corner:?}\n{screen}",
        r.y, r.x
    );
    assert!(
        screen.contains("关闭整个 fleet「fleetmux-demo」？"),
        "the box does not name the fleet:\n{screen}"
    );
    assert!(
        screen.contains("按 y 确认，其他任意键取消"),
        "the box does not say which keys confirm and cancel:\n{screen}"
    );

    v.input(b"n");
    let screen = v.wait_no_text("关闭 fleet", T);
    v.wait_quiet(Duration::from_millis(300), T);
    let after = v.text();
    assert_eq!(
        after, before,
        "cancelling the box did not restore the panes cell for cell (a ghost is left):\n{after}"
    );
    assert!(
        !screen.contains("按 y 确认"),
        "the box's hint is still on screen after `n`:\n{screen}"
    );
}

// ------------------------------ S1f topbar (plan §20.2 / §20.6) ------------------------------

/// With kv `sidebar` the whole row 0 is the topbar: the toolbar left part, the `│` divider on the sidebar's
/// drag line and the current fleet's `title · line1`; row 1 is the header band's blank separation row and every
/// group starts on row 2, the same row as the sidebar's first card (A26 rev7, plan §20.2).
#[test]
fn topbar_is_row_0_and_the_groups_start_on_row_2() {
    let srv = Server::start();
    let p = page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    let screen = v.wait_text("fleetmux-demo", T);
    let top = v.row_text(0);
    assert!(
        top.starts_with(" ◫  +  ↺  ←  →"),
        "row 0 does not start with the S1f toolbar ' ◫  +  ↺  ←  →': {top:?}\n{screen}"
    );
    assert!(
        top.contains("fleetmux-demo · 运行中"),
        "the topbar's right part lacks 'fleetmux-demo · 运行中': {top:?}\n{screen}"
    );
    let divider = v.cell(0, 34);
    assert_eq!(
        divider.ch, '│',
        "the topbar divider is not at (0,34) on the sidebar's drag line: {divider:?}\n{screen}"
    );
    let border = v.cell(2, 34);
    assert_eq!(
        border.ch, '┌',
        "the groups' top border is not on row 2 at column 34: {border:?}\n{screen}"
    );
    let dot = v.cell(2, 1);
    assert_eq!(
        dot.ch, '●',
        "the card's state dot is not at (2,1): CARD_ROW0 stays 2: {dot:?}\n{screen}"
    );
    assert_eq!(
        v.cell(2, 3).ch,
        'f',
        "the card title does not start at (2,3): CARD_ROW0 stays 2: {:?}\n{screen}",
        v.cell(2, 3)
    );
    let sup = wait_until("the supervisor PTY is 101x33 below the header band", T, || {
        let s = pane_size(&srv, &p[0]);
        (s == (101, 33)).then_some(s)
    });
    assert_eq!(
        sup,
        (101, 33),
        "the supervisor PTY below the header band: {sup:?}"
    );
    let worker = wait_until("a worker PTY is 102x55 below the header band", T, || {
        let s = pane_size(&srv, &p[1]);
        (s == (102, 55)).then_some(s)
    });
    assert_eq!(
        worker,
        (102, 55),
        "a worker PTY below the topbar: {worker:?}"
    );
}

/// Collapsed, the sidebar is 0 columns wide and the groups fill from column 0; the toolbar keeps its columns on
/// row 0 and there is no divider (plan §20.2).
#[test]
fn hidden_topbar_keeps_the_buttons_and_the_groups_fill_from_column_0() {
    let srv = Server::start();
    let p = page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    v.input(&prefixed(b"s"));
    let screen = v.wait_no_text("~/.fleetmux/fleets/fmx-demo", T);
    let toggle = v.cell(0, 1);
    assert_eq!(
        toggle.ch, '◫',
        "the hidden topbar's toggle is not at (0,1): {toggle:?}\n{screen}"
    );
    let reopen = v.cell(0, 7);
    assert_eq!(
        reopen.ch, '↺',
        "the hidden topbar's ↺ is not at (0,7): the button columns must not move: {reopen:?}\n{screen}"
    );
    let after = v.cell(0, 15);
    assert!(
        !is_box(after.ch),
        "隐藏不画分隔格: (0,15) is a box character with the sidebar hidden: {after:?}\n{screen}"
    );
    let border = v.cell(2, 0);
    assert_eq!(
        border.ch, '┌',
        "隐藏后格子从第 0 列铺满: (2,0) is not the groups' top-left corner: {border:?}\n{screen}"
    );
    assert!(
        v.row_text(0).contains("fleetmux-demo"),
        "the hidden topbar lost the current fleet's title: {:?}\n{screen}",
        v.row_text(0)
    );
    let sup = wait_until("the supervisor PTY widens to 118 columns", T, || {
        let s = pane_size(&srv, &p[0]);
        (s.0 == 118).then_some(s)
    });
    assert_eq!(
        sup.0, 118,
        "the supervisor PTY width with the sidebar hidden: {sup:?}"
    );
}

// ------------------------------ S2b: restoring a closed fleet (plan §13.5 / §13.6) ------------------------------

/// The kv `sidebar` a closer leaves behind after it kept the server (plan §13.3 / §13.6-1): one closed card.
const CLOSED_ONE: &str = r#"{"v":1,"current":"fleet-20261030-010101-ab12cd34","fleets":[{"id":"fleet-20261030-010101-ab12cd34","title":"fleetmux-demo","state":"closed","line1":"已关闭 · 可恢复","line2":"~/.fleetmux/fleets/fmx-demo"}]}"#;
const CLOSED_ID: &str = "fleet-20261030-010101-ab12cd34";
/// Two closed cards, `fleet-a` first and `fleet-b` second.
const CLOSED_TWO: &str = r#"{"v":1,"current":"fleet-a","fleets":[{"id":"fleet-a","title":"fmx-a","state":"closed","line1":"已关闭 · 可恢复","line2":"~/.fleetmux/fleets/a"},{"id":"fleet-b","title":"fmx-b","state":"closed","line1":"已关闭 · 可恢复","line2":"~/.fleetmux/fleets/b"}]}"#;

fn set_kv(srv: &Server, key: &str, value: &str) {
    srv.ok("kv.set", json!({ "key": key, "value": value }));
}

/// One kv `restorable` item (plan §18.3).
fn run(id: &str, title: &str, argv: &[&str], cwd: &str, env: Value) -> Value {
    json!({"id": id, "title": title, "closed_at": 1790000000, "argv": argv, "cwd": cwd, "env": env})
}

fn restorable(runs: Vec<Value>) -> String {
    json!({"v": 1, "runs": runs}).to_string()
}

/// A closed card is grey with `↺` where the `×` was, and the topbar's right segment shows the closed line
/// (plan §13.6-1 / §13.6-3).
#[test]
fn closed_kv_turns_the_card_into_a_restore_button() {
    let srv = Server::start();
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_kv(&srv, "sidebar", CLOSED_ONE);
    let screen = v.wait_text("已关闭 · 可恢复", T);
    let dot = v.cell(2, 1);
    assert_eq!(
        dot.ch, '●',
        "the closed card's state dot is not at (2,1): {dot:?}\n{screen}"
    );
    assert_eq!(
        dot.fg,
        Color::Indexed(7),
        "the closed card's dot is not Gray: {dot:?}\n{screen}"
    );
    let restore = v.cell(2, 31);
    assert_eq!(
        restore.ch, '↺',
        "the closed card has no ↺ at (2,31): {restore:?}\n{screen}"
    );
    assert_eq!(
        restore.fg,
        Color::Indexed(15),
        "the selected closed card's ↺ is not White: {restore:?}\n{screen}"
    );
    let top = v.row_text(0);
    assert!(
        top.contains("fleetmux-demo · 已关闭 · 可恢复"),
        "the topbar's right part lacks 'fleetmux-demo · 已关闭 · 可恢复': {top:?}\n{screen}"
    );
}

/// A closed card's `↺` spawns the registered command as a `resume` tab of this server, with its cwd and env; the
/// pane leaves again when the command exits (plan §13.5 / §13.6-4).
#[test]
fn restore_spawns_the_registered_command_in_this_server() {
    let srv = Server::start();
    let dir = std::env::temp_dir().join(format!("fmx-restore-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("temp dir for the restore cwd");
    set_kv(&srv, "sidebar", CLOSED_ONE);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            CLOSED_ID,
            "fleetmux-demo",
            &[
                "sh",
                "-c",
                "echo RESUMED; echo ENV-$FX_RESTORE_MARK; pwd > marker.txt; sleep 2",
            ],
            dir.to_str().expect("utf-8 temp path"),
            json!({"FX_RESTORE_MARK": "yes"}),
        )]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("已关闭 · 可恢复", T);
    v.input(&sgr_click(31, 2, 0));
    let screen = v.wait_text("RESUMED", T);
    assert!(
        screen.contains("ENV-yes"),
        "the resume pane did not get the registered env: {screen}"
    );
    let panes = srv.panes();
    assert!(
        panes.iter().any(|p| p["title"] == json!("resume")
            && p["type"] == json!("tab")
            && p["group"] == json!("lower")),
        "no 'resume' tab in the lower group of this server: {panes:?}\n{screen}"
    );
    let marker = std::fs::read_to_string(dir.join("marker.txt")).unwrap_or_default();
    let want = std::fs::canonicalize(&dir).expect("canonical temp dir");
    assert_eq!(
        marker.trim(),
        want.to_str().expect("utf-8 path"),
        "the resume pane did not start in the registered cwd: {marker:?}\n{screen}"
    );
    wait_until("the resume pane to leave after its command exited", T, || {
        (!srv
            .panes()
            .iter()
            .any(|p| p["title"] == json!("resume")))
        .then_some(())
    });
}

/// `^b r` restores `runs[0]` (the newest), says `正在恢复…` while the pane runs, and clearing kv `restorable`
/// (the daemon's cards are back) drops that status again; an empty list only says there is nothing to restore
/// (plan §13.6-2 / §13.6-4).
#[test]
fn reopen_restores_runs_zero_and_an_empty_list_says_so() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            CLOSED_ID,
            "fleetmux-demo",
            &["sh", "-c", "echo TOPBAR; sleep 2"],
            "/tmp",
            json!({}),
        )]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("已关闭 · 可恢复", T);
    v.input(&prefixed(b"r"));
    let screen = v.wait_text("TOPBAR", T);
    let status = v.wait_status("正在恢复…", T, |s| s.contains("正在恢复…"));
    assert!(
        status.contains("正在恢复…"),
        "^b r did not say it is resuming: {status:?}\n{screen}"
    );
    set_kv(&srv, "restorable", "");
    let cleared = v.wait_status("正在恢复… to clear once the daemon's cards are back", T, |s| {
        !s.contains("正在恢复…")
    });
    assert!(
        !cleared.contains("正在恢复…"),
        "clearing kv restorable left '正在恢复…' on the status line: {cleared:?}"
    );
    set_kv(&srv, "restorable", &restorable(vec![]));
    v.input(&sgr_click(7, 0, 0));
    let status = v.wait_status("the nothing-to-restore notice", T, |s| {
        s.contains("没有可恢复的 fleet")
    });
    assert!(
        status.contains("没有可恢复的 fleet"),
        "the topbar ↺ with an empty restorable list did not say so: {status:?}"
    );
}

/// A42-LINUX-RESTORE-FAILURE-04: a restore pane that exits nonzero clears `正在恢复…` and keeps an actionable
/// reason on the status line (the pane's own last output line), while the daemon-owned kv `restorable` is not
/// touched — the operator's next `^b r` is the retry, and there is no automatic loop.
#[test]
fn a_failed_restore_clears_resuming_and_shows_the_reason() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            CLOSED_ID,
            "fleetmux-demo",
            &[
                "sh",
                "-c",
                "echo 'cannot resume /run: the task record is damaged (no usable deadline)' >&2; exit 1",
            ],
            "/tmp",
            json!({}),
        )]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("已关闭 · 可恢复", T);
    v.input(&prefixed(b"r"));
    let status = v.wait_status("the failed restore's reason", T, |s| s.contains("恢复失败"));
    assert!(
        status.contains("the task record is damaged"),
        "the status line does not carry the pane's own error: {status:?}\n{}",
        srv.diagnostics()
    );
    assert!(
        !status.contains("正在恢复…"),
        "a failed restore left '正在恢复…' up: {status:?}"
    );
    assert!(
        srv.panes().iter().all(|p| p["title"] != json!("resume")),
        "the failed restore pane is still listed"
    );
    let kv = srv.ok("kv.get", json!({"key": "restorable"}));
    assert!(
        kv["value"].as_str().is_some_and(|s| s.contains(CLOSED_ID)),
        "the server touched the daemon-owned kv restorable: {kv}"
    );
}

/// A42-LINUX-RESTORE-FAILURE-04: exit 0 is not a failure — the daemon is starting asynchronously, so
/// `正在恢复…` stays until kv `restorable` is cleared, and an unknown exit changes nothing either.
#[test]
fn a_restore_that_exits_zero_keeps_resuming_until_the_daemon_clears_restorable() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            CLOSED_ID,
            "fleetmux-demo",
            &["sh", "-c", "echo DAEMON-STARTED"],
            "/tmp",
            json!({}),
        )]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("已关闭 · 可恢复", T);
    v.input(&prefixed(b"r"));
    v.wait_status("正在恢复…", T, |s| s.contains("正在恢复…"));
    // The pane is gone (exit 0) but the daemon has not cleared kv `restorable` yet.
    let end = Instant::now() + T;
    while srv.panes().iter().any(|p| p["title"] == json!("resume")) {
        assert!(Instant::now() < end, "the exit-0 restore pane never left");
        std::thread::sleep(Duration::from_millis(20));
    }
    std::thread::sleep(Duration::from_millis(300));
    let status = v.status_line();
    assert!(
        status.contains("正在恢复…") && !status.contains("恢复失败"),
        "exit 0 was read as a failed restore: {status:?}"
    );
    set_kv(&srv, "restorable", "");
    let cleared = v.wait_status("the daemon's cards to clear 正在恢复…", T, |s| {
        !s.contains("正在恢复…")
    });
    assert!(!cleared.contains("正在恢复…"), "{cleared:?}");
}

/// A42-LINUX-RESTORE-FAILURE-04: a pane that merely carries the title `resume` is not the restore attempt — the
/// pane id the server spawned is the identity, so a foreign pane's nonzero exit never clears a live restore.
#[test]
fn a_foreign_pane_titled_resume_does_not_clear_a_live_restore() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            CLOSED_ID,
            "fleetmux-demo",
            &["sh", "-c", "echo STILL-RESUMING; exec sleep 30"],
            "/tmp",
            json!({}),
        )]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("已关闭 · 可恢复", T);
    v.input(&prefixed(b"r"));
    v.wait_text("STILL-RESUMING", T);
    let foreign = srv.spawn_id("resume", "tab", &["sh", "-c", "exit 1"]);
    let end = Instant::now() + T;
    while srv.listed(&foreign) {
        assert!(Instant::now() < end, "the foreign pane never exited");
        std::thread::sleep(Duration::from_millis(20));
    }
    // P-04-REAP: an unrelated pane's exit is finalised in that turn and leaves list and layout together; the
    // live restore attempt (identified by its own pane id) is untouched.
    wait_until("the foreign pane to leave pane.list and the layout", T, || {
        let layout = srv.ok("layout.get", json!({}));
        let groups = layout["groups"].as_object().cloned().unwrap_or_default();
        let lower = groups.get("lower").and_then(|g| g.as_array()).cloned().unwrap_or_default();
        (!lower.iter().any(|p| p == &json!(foreign))).then_some(())
    });
    assert!(
        srv.panes().iter().any(|p| p["title"] == json!("resume")),
        "the foreign exit removed the live restore pane: {:?}",
        srv.panes()
    );
    std::thread::sleep(Duration::from_millis(300));
    let status = v.status_line();
    assert!(
        status.contains("正在恢复…") && !status.contains("恢复失败"),
        "a foreign 'resume' pane's exit changed the restore state: {status:?}"
    );
}

/// A42-LINUX-RESTORE-FAILURE-04: after a failed restore the operator's own retry works — exactly one new pane,
/// no automatic loop, and the old reason leaves the status line.
#[test]
fn a_failed_restore_can_be_retried_by_the_operator() {
    let dir = TempDir::new();
    let script = "n=$(cat n 2>/dev/null || echo 0); n=$((n+1)); echo $n > n; \
                  if [ \"$n\" -eq 1 ]; then echo FIRST-FAIL >&2; exit 1; fi; echo SECOND-ATTEMPT; exec sleep 30";
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            CLOSED_ID,
            "fleetmux-demo",
            &["sh", "-c", script],
            dir.path().to_str().expect("utf-8 temp path"),
            json!({}),
        )]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("已关闭 · 可恢复", T);
    v.input(&prefixed(b"r"));
    v.wait_status("the first attempt's failure", T, |s| s.contains("恢复失败"));
    v.input(&prefixed(b"r"));
    let screen = v.wait_text("SECOND-ATTEMPT", T);
    let status = v.status_line();
    assert!(
        status.contains("正在恢复…") && !status.contains("恢复失败"),
        "the retry did not replace the old reason: {status:?}\n{screen}"
    );
    let resume_panes = srv
        .panes()
        .iter()
        .filter(|p| p["title"] == json!("resume"))
        .count();
    assert_eq!(resume_panes, 1, "the retry did not spawn exactly one restore pane");
    let n = std::fs::read_to_string(dir.path().join("n")).unwrap_or_default();
    assert_eq!(n.trim(), "2", "the retry did not run the resume argv a second time: {n:?}");
}

/// A42-LINUX-RESTORE-FAILURE-04 P-04-REAP: the restore pane's PTY reaches EOF *before* its child exits (the
/// child closes 0/1/2, then exits nonzero a moment later). The attempt must still complete in the turn that
/// observes the exit — no third PTY / SIGCHLD / other-pane event — with the pane's own error on the status line,
/// the pane gone from `pane.list` *and* from the layout, and an `exited` line in `server.log`.
#[test]
fn a_restore_whose_pty_closes_before_its_exit_still_completes() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            CLOSED_ID,
            "fleetmux-demo",
            &[
                "sh",
                "-c",
                "echo 'cannot resume /run: the task record is damaged (no usable deadline)' >&2; \
                 exec 0<&- 1>&- 2>&-; sleep 0.4; exit 7",
            ],
            "/tmp",
            json!({}),
        )]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("已关闭 · 可恢复", T);
    v.input(&prefixed(b"r"));
    let status = v.wait_status("the EOF-before-exit restore to complete", T, |s| {
        s.contains("恢复失败")
    });
    assert!(
        status.contains("the task record is damaged"),
        "the status line does not carry the pane's own error: {status:?}\n{}",
        srv.diagnostics()
    );
    assert!(
        !status.contains("正在恢复…"),
        "the failed restore left '正在恢复…' up: {status:?}"
    );
    // list and layout agree: the dead pane is in neither.
    wait_until("the exited pane to leave pane.list and the layout", T, || {
        let listed = srv.panes().iter().any(|p| p["title"] == json!("resume"));
        let layout = srv.ok("layout.get", json!({}));
        let in_layout = layout["groups"].as_object().is_some_and(|g| {
            g.values()
                .any(|v| v.as_array().is_some_and(|a| !a.is_empty()))
        }) || layout["selected"]
            .as_object()
            .is_some_and(|s| s.values().any(|v| !v.is_null()));
        (!listed && !in_layout).then_some(())
    });
    let log = std::fs::read_to_string(srv.runtime.join("server.log")).unwrap_or_default();
    assert!(
        log.contains("exited"),
        "server.log has no exit line for the reaped pane:\n{log}"
    );
}

/// A42-LINUX-RESTORE-FAILURE-04 P-04-REAP: the launcher exits nonzero while a background child still holds the PTY
/// slave, so the PTY stays open well past the observation window. The attempt must fail promptly with the
/// launcher's own error (no waiting for the PTY to close), the dead pane must leave `pane.list` and the layout
/// together, and `server.log` must say the tail was partial.
///
/// The launcher leaves only once the holder has reported ready: the holder writes `ready` after it has installed
/// `trap '' HUP`, so it really survives the session leader's exit and keeps the slave open. Exiting straight after
/// the fork races the kernel's SIGHUP (the holder dies before its `trap` runs, the slave closes and the case
/// degenerates into the EOF-first one); the wait is bounded and fails loudly instead of sleeping a fixed time.
///
/// Cleanup follows the fixture rule, never a command name: the holder is inside the restore pane's own process
/// group and goes with this test's normal server teardown; the ready file sits in this test's `TempDir`. No
/// `pkill`/`killall`/name pattern is used, so no process outside this test's runtime can be hit.
#[test]
fn a_restore_whose_child_exits_before_its_pty_closes_still_completes() {
    let dir = TempDir::new();
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            CLOSED_ID,
            "fleetmux-demo",
            &[
                "sh",
                "-c",
                "echo EXIT-FIRST >&2; r=ready; (trap '' HUP; : > \"$r\"; sleep 12) & \
                 i=0; while [ ! -e \"$r\" ]; do i=$((i+1)); \
                 if [ \"$i\" -gt 300 ]; then echo 'the holder never reported ready' >&2; exit 9; fi; \
                 sleep 0.01; done; exit 7",
            ],
            dir.path().to_str().expect("utf-8 temp path"),
            json!({}),
        )]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("已关闭 · 可恢复", T);
    v.input(&prefixed(b"r"));
    let status = v.wait_status("the exit-before-EOF restore to fail without waiting for the PTY", T, |s| {
        s.contains("恢复失败")
    });
    assert!(
        status.contains("EXIT-FIRST"),
        "the launcher's own error line is missing: {status:?}\n{}",
        srv.diagnostics()
    );
    wait_until("the exited pane to leave pane.list and the layout", T, || {
        let listed = srv.panes().iter().any(|p| p["title"] == json!("resume"));
        let layout = srv.ok("layout.get", json!({}));
        let in_layout = layout["groups"].as_object().is_some_and(|g| {
            g.values()
                .any(|v| v.as_array().is_some_and(|a| !a.is_empty()))
        });
        (!listed && !in_layout).then_some(())
    });
    // macOS hangs the terminal up when the pane's session leader exits (the slave cannot stay open there); on
    // Linux the background child keeps the PTY open, and the log must then say the tail was partial.
    #[cfg(target_os = "linux")]
    {
        let log = std::fs::read_to_string(srv.runtime.join("server.log")).unwrap_or_default();
        assert!(
            log.contains("later output is not included"),
            "the partial-tail boundary was not recorded:\n{log}"
        );
    }
}

/// A42-LINUX-RESTORE-FAILURE-04 P-04-REAP: the SIGCHLD turn applies only a bounded batch of the PTY events its
/// reader threads already sent before it reaps. Without that cap an unrelated pane that never stops producing output
/// keeps the event queue non-empty, so the drain never ends, the state lock is never released and a known exit is
/// never reported. The attempt must still fail promptly — status line, `pane.list` and layout — while another pane
/// floods the channel.
#[test]
fn a_known_exit_still_completes_while_another_pane_keeps_producing_output() {
    let srv = Server::start();
    // Unrelated panes that never stop writing: their reader threads keep the event queue non-empty for the whole
    // test, so a drain-to-empty turn never ends. They are torn down with this test's own server (each pgid was
    // captured by `spawn_id`).
    let mut chatty: Vec<String> = Vec::new();
    for i in 0..3 {
        chatty.push(srv.spawn_id(&format!("chatty-{i}"), "worker", &["sh", "-c", "yes OUTPUT-FLOOD"]));
    }
    chatty.sort();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            CLOSED_ID,
            "fleetmux-demo",
            &["sh", "-c", "echo FLOOD-EXIT >&2; exit 7"],
            "/tmp",
            json!({}),
        )]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("已关闭 · 可恢复", T);
    v.input(&prefixed(b"r"));
    let status = v.wait_status("the known exit to complete under continuous unrelated output", T, |s| {
        s.contains("恢复失败")
    });
    // The batch is capped, so under a flood the pane's own last line may still be queued when the reap happens: the
    // status line then carries the exit code instead of the text. Both are the known exit 7; the no-flood tests pin
    // the tail text, and `pty_open`/the partial-tail log keep the "what the parser had" boundary honest.
    assert!(
        status.contains("FLOOD-EXIT") || status.contains("exit code 7"),
        "the known exit did not reach the status line: {status:?}\n{}",
        srv.diagnostics()
    );
    // The attempt's pane leaves both views; the unrelated flood pane keeps its own place. The attempt exits within
    // milliseconds, so it is identified by what is left rather than by catching its id first.
    wait_until("the exited pane to leave pane.list and the layout", T, || {
        let listed: Vec<String> = srv
            .panes()
            .iter()
            .filter_map(|p| p["pane"].as_str().map(str::to_string))
            .collect();
        let layout = srv.ok("layout.get", json!({}));
        let mut in_layout: Vec<String> = Vec::new();
        if let Some(groups) = layout["groups"].as_object() {
            for v in groups.values() {
                if let Some(a) = v.as_array() {
                    in_layout.extend(a.iter().filter_map(|x| x.as_str().map(str::to_string)));
                }
            }
        }
        if let Some(sel) = layout["selected"].as_object() {
            in_layout.extend(sel.values().filter_map(|x| x.as_str().map(str::to_string)));
        }
        let mut listed = listed;
        listed.sort();
        listed.dedup();
        in_layout.sort();
        in_layout.dedup();
        (listed == chatty && in_layout == chatty).then_some(())
    });
}

/// A42-LINUX-RESTORE-FAILURE-04 P-04-REAP: a restore with no output at all still completes with a visible
/// reason (the exit code), never a hung `正在恢复…`.
#[test]
fn a_restore_with_no_output_still_shows_the_exit_code() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            CLOSED_ID,
            "fleetmux-demo",
            &["sh", "-c", "exec 0<&- 1>&- 2>&-; sleep 0.4; exit 5"],
            "/tmp",
            json!({}),
        )]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("已关闭 · 可恢复", T);
    v.input(&prefixed(b"r"));
    let status = v.wait_status("the silent restore to complete", T, |s| {
        s.contains("恢复失败")
    });
    assert!(
        status.contains("exit code 5") && status.contains("没有输出"),
        "a silent failure does not say how it failed: {status:?}\n{}",
        srv.diagnostics()
    );
    assert!(!status.contains("正在恢复…"), "{status:?}");
}

/// A card's `↺` looks the run up by the card's `id`, not by its position: card 0 is `fleet-a`, while `runs[0]`
/// is `fleet-b` (plan §13.6-2).
#[test]
fn restore_uses_the_card_id_not_the_card_position() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_TWO);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![
            run(
                "fleet-b",
                "fmx-b",
                &["sh", "-c", "echo SECOND-ID; sleep 2"],
                "/tmp",
                json!({}),
            ),
            run(
                "fleet-a",
                "fmx-a",
                &["sh", "-c", "echo FIRST-ID; sleep 2"],
                "/tmp",
                json!({}),
            ),
        ]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("fmx-a", T);
    v.input(&sgr_click(31, 2, 0));
    let screen = v.wait_text("FIRST-ID", T);
    assert!(
        !screen.contains("SECOND-ID"),
        "card 0 restored runs[0] (fleet-b) instead of its own id (fleet-a):\n{screen}"
    );
}

/// With kv `sidebar` and no pane at all, the space right of the sidebar says the fleet is closed and how to
/// restore it; the empty grid draws no frame (plan §13.3 / §13.6-3).
#[test]
fn empty_server_with_a_closed_card_shows_the_placeholder() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    let mut v = Viewer::attach(&srv, W, H);
    let screen = v.wait_text("fleet「fleetmux-demo」已关闭", T);
    assert!(
        screen.contains("运行记录保留。点侧栏卡片上的 ↺ 或按 ^b r 恢复，接着原来的活。"),
        "the placeholder lacks the restore hint:\n{screen}"
    );
    assert!(
        screen.contains("关掉这个窗口（或 ^b d）后 10 秒服务退出；之后仍可在终端 start --resume。"),
        "the placeholder lacks the 10-second hint:\n{screen}"
    );
    let top = v.row_text(0);
    assert!(
        top.starts_with(" ◫  +  ↺  ←  →"),
        "row 0 is not the topbar with a closed fleet: {top:?}\n{screen}"
    );
    assert!(
        top.contains("fleetmux-demo · 已关闭 · 可恢复"),
        "the topbar's right part lacks 'fleetmux-demo · 已关闭 · 可恢复': {top:?}\n{screen}"
    );
    let (row, col) = v.wait_find("fleet「", T);
    assert!(
        row > 0 && col >= 34,
        "the placeholder is not right of the sidebar (row {row}, col {col}):\n{screen}"
    );
    let first = v.cell(row, col);
    assert_eq!(
        first.fg,
        Color::Indexed(15),
        "the first placeholder line is not White: {first:?}\n{screen}"
    );
    assert!(
        first.attrs.bold,
        "the first placeholder line is not bold: {first:?}\n{screen}"
    );
    // No group frame; row 1 holds only the line under the topbar (`├` then `─`, cmux look, user 2026-11-08).
    let row1 = v.row_text(1);
    assert!(
        row1.trim_start()
            .strip_prefix('├')
            .is_some_and(|r| r.chars().all(|c| c == '─')),
        "the empty grid still draws a frame: row 1 is {row1:?}\n{screen}"
    );
}

/// A26 rev5 (`CURRENT_REQUIREMENTS` rev 6 `utm_topbar`): a mirrored (Linux) layout keeps no topbar row, so the
/// closed-fleet placeholder owns the whole height — and the fleet title/status appears in **no** grid row: it is
/// the native headerbar's text (`0x85` `topbar`, `overlay::topbar_for`), never a second band in the grid.
#[test]
fn mirrored_closed_fleet_placeholder_uses_the_whole_grid() {
    let srv = Server::start_with_env(&[("FLEETMUX_LAYOUT_MIRROR", "1"), ("FLEETMUX_NATIVE_HEADER", "1")]);
    set_kv(&srv, "sidebar", CLOSED_ONE);
    let mut v = Viewer::attach_native(&srv, W, H);
    let screen = v.wait_text("fleet「fleetmux-demo」已关闭", T);
    for y in 0..H {
        let row = v.row_text(y);
        assert!(
            !row.starts_with(" ◫"),
            "row {y} drew the toolbar while mirrored: {row:?}\n{screen}"
        );
        assert!(
            !row.contains("fleetmux-demo · 已关闭"),
            "row {y} drew the topbar's fleet title/status while mirrored: {row:?}\n{screen}"
        );
    }
    let (row, col) = v.wait_find("fleet「", T);
    assert!(
        if cfg!(target_os = "linux") {
            col >= 34
        } else {
            col < 206
        },
        "the placeholder is not beside the platform sidebar (row {row}, col {col}):\n{screen}"
    );
    assert!(
        row < H - 1,
        "the placeholder is not above the status line (row {row}):\n{screen}"
    );
}

/// A bare server with no pane and no kv `sidebar` keeps its screen: no topbar, no placeholder (plan §13.6-3).
#[test]
fn empty_server_without_kv_sidebar_is_unchanged() {
    let srv = Server::start();
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_quiet(Duration::from_millis(300), T);
    let screen = v.text();
    assert!(
        !screen.contains("已关闭") && !screen.contains("fleet「"),
        "a bare empty server drew the S2b placeholder:\n{screen}"
    );
    let corner = v.cell(0, 0);
    assert!(
        is_box(corner.ch),
        "(0,0) is not the groups' frame on a bare empty server: {corner:?}\n{screen}"
    );
}

/// A closed fleet's server (kv `restorable` set, no pane) exits by itself once the last viewer is gone: plan
/// §13.4 / §13.6-6 — 10 consecutive 1 s checks with 0 panes and 0 viewers, then `server.shutdown`.
#[test]
fn closed_server_exits_ten_seconds_after_the_last_viewer() {
    let mut srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            CLOSED_ID,
            "fleetmux-demo",
            &["sh", "-c", "echo RESUMED"],
            "/tmp",
            json!({}),
        )]),
    );
    let sock = srv.sock();
    let mut viewer = Viewer::attach(&srv, W, H);
    let screen = viewer.wait_text("fleet「fleetmux-demo」已关闭", T);
    drop(viewer);
    // The 10 s grace starts once the last viewer is gone; 15 s covers the 10 ticks plus the shutdown handshake.
    let what = format!(
        "the closed server to exit 10 s after the last viewer left (plan §13.4); closed-fleet screen was:\n{screen}"
    );
    wait_until(&what, Duration::from_secs(15), || {
        try_hello(&sock).is_none().then_some(())
    });
    assert!(
        wait_exit(&mut srv.child, Duration::from_secs(5)).is_some(),
        "the reaper stopped answering `hello` but the server process is still running; \
         closed-fleet screen was:\n{screen}"
    );
    assert!(
        !sock.exists(),
        "the reaper left {} behind; closed-fleet screen was:\n{screen}",
        sock.display()
    );
}

/// The other half of plan §13.4: while a viewer is attached the closed server must stay (the 10 s count only
/// runs with 0 panes *and* 0 viewers), so a gui reload or a re-attach never loses the fleet.
#[test]
fn closed_server_stays_while_a_viewer_is_attached() {
    let mut srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            CLOSED_ID,
            "fleetmux-demo",
            &["sh", "-c", "echo RESUMED"],
            "/tmp",
            json!({}),
        )]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    let screen = v.wait_text("fleet「fleetmux-demo」已关闭", T);
    // Longer than the 10 s grace: an attached viewer must keep the server alive.
    std::thread::sleep(Duration::from_secs(12));
    assert!(
        try_hello(&srv.sock()).is_some(),
        "the closed server exited although a viewer is still attached; closed-fleet screen was:\n{screen}"
    );
    assert!(
        srv.child.try_wait().expect("try_wait").is_none(),
        "the server process exited although a viewer is still attached; closed-fleet screen was:\n{screen}"
    );
    let screen = v.wait_text("运行记录保留", T);
    assert!(
        screen.contains("已关闭 · 可恢复"),
        "the surviving viewer lost the closed card after 12 s:\n{screen}"
    );
}

// --------------------- S2c: the history list restores by id, refuses while one runs (plan §18.5 / §18.7) -------------

/// A running fleet on card 0 and a closed history entry on card 1: the case where a second `↺` must not act
/// (plan §18.2 "一次只恢复一个" / §18.7-5).
const RUNNING_AND_CLOSED: &str = r#"{"v":1,"current":"fleet-run-now","fleets":[{"id":"fleet-run-now","title":"fmx-run","state":"running","line1":"运行中 · 监工+1 worker（0忙）","line2":"~/.fleetmux/fleets/run"},{"id":"fleet-old-gone","title":"fmx-old","state":"closed","line1":"已关闭 · 可恢复","line2":"~/.fleetmux/fleets/old"}]}"#;

/// Every closed card of kv `restorable`'s history list draws a grey dot and a `↺` on its first row (white while the
/// card is `current`, grey otherwise), and the topbar's right part names the current one (plan §18.3 / §18.7-1 /
/// §18.7-6).
#[test]
fn restorable_kv_lists_grey_cards_with_restore_buttons() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_TWO);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![
            run("fleet-a", "fmx-a", &["sh", "-c", "echo A"], "/tmp", json!({})),
            run("fleet-b", "fmx-b", &["sh", "-c", "echo B"], "/tmp", json!({})),
        ]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    let screen = v.wait_text("已关闭 · 可恢复", T);
    for (i, title) in [(0u16, "fmx-a"), (1u16, "fmx-b")] {
        let y = 2 + 4 * i;
        let dot = v.cell(y, 1);
        assert_eq!(
            dot.ch, '●',
            "the history card {i} ({title}) has no state dot at ({y},1): {dot:?}\n{screen}"
        );
        assert_eq!(
            dot.fg,
            Color::Indexed(7),
            "the history card {i} ({title}) dot at ({y},1) is not Gray: {dot:?}\n{screen}"
        );
        let btn = v.cell(y, 31);
        assert_eq!(
            btn.ch, '↺',
            "the history card {i} ({title}) has no ↺ at ({y},31): {btn:?}\n{screen}"
        );
        let want = if i == 0 {
            Color::Indexed(15)
        } else {
            Color::Indexed(7)
        };
        assert_eq!(
            btn.fg, want,
            "the history card {i} ({title}) ↺ at ({y},31) has the wrong colour (current = White, others Gray): \
             {btn:?}\n{screen}"
        );
        assert!(
            v.row_text(y).contains(title),
            "the history card {i} ({title}) does not name it on row {y}: {:?}\n{screen}",
            v.row_text(y)
        );
    }
    let top = v.row_text(0);
    assert!(
        top.contains("fmx-a · 已关闭 · 可恢复"),
        "the topbar's right part lacks 'fmx-a · 已关闭 · 可恢复': {top:?}\n{screen}"
    );
}

/// With a fleet still running, a second `↺` (the closed card's and the topbar's) only says
/// `先关闭正在运行的 fleet 再恢复` and spawns nothing (plan §18.2 / §18.7-4 / §18.7-5).
#[test]
fn restore_is_refused_while_a_fleet_is_running() {
    let srv = Server::start();
    page(&srv);
    set_kv(&srv, "sidebar", RUNNING_AND_CLOSED);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![run(
            "fleet-old-gone",
            "fmx-old",
            &["sh", "-c", "echo RESTORED; sleep 5"],
            "/tmp",
            json!({}),
        )]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    let screen = v.wait_text("已关闭 · 可恢复", T);
    let before = srv.panes().len();
    assert!(
        before >= 4,
        "the test page did not spawn its four panes: {before}\n{screen}"
    );
    // Card 1 is the closed one: its first row is 6 (plan §18.7-5: `↺` at (31,6), card 0's `×` at (31,2)).
    v.input(&sgr_click(31, 6, 0));
    let status = v.wait_status("the restore-while-running notice", T, |s| {
        s.contains("先关闭正在运行的 fleet 再恢复")
    });
    assert!(
        status.contains("先关闭正在运行的 fleet 再恢复"),
        "the closed card's ↺ did not say a fleet is still running: {status:?}\n{screen}"
    );
    let after = srv.panes();
    assert_eq!(
        after.len(),
        before,
        "the closed card's ↺ spawned a pane although a fleet is running: {after:?}\n{screen}"
    );
    assert!(
        !after.iter().any(|p| p["title"] == json!("resume")),
        "the closed card's ↺ spawned a 'resume' pane although a fleet is running: {after:?}\n{screen}"
    );
    // The next press clears the one-shot notice, so the second click has to write it again.
    v.input(&sgr_click(1, 1, 0));
    v.wait_status("the notice to clear", T, |s| {
        !s.contains("先关闭正在运行的 fleet 再恢复")
    });
    v.input(&sgr_click(7, 0, 0));
    let status = v.wait_status("the same notice from the topbar ↺", T, |s| {
        s.contains("先关闭正在运行的 fleet 再恢复")
    });
    assert!(
        status.contains("先关闭正在运行的 fleet 再恢复"),
        "the topbar ↺ did not say a fleet is still running: {status:?}\n{screen}"
    );
    assert_eq!(
        srv.panes().len(),
        before,
        "the topbar ↺ spawned a pane although a fleet is running\n{screen}"
    );
}

/// A window opened on the history list with no fleet running says `没有在跑的 fleet；点侧栏卡片上的 ↺ 恢复` right of
/// the sidebar from row 1 down, and the empty grid still draws no frame (plan §18.4-5 / §18.7-6).
#[test]
fn empty_server_shows_the_no_fleet_placeholder() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_TWO);
    set_kv(
        &srv,
        "restorable",
        &restorable(vec![
            run("fleet-a", "fmx-a", &["sh", "-c", "echo A"], "/tmp", json!({})),
            run("fleet-b", "fmx-b", &["sh", "-c", "echo B"], "/tmp", json!({})),
        ]),
    );
    let mut v = Viewer::attach(&srv, W, H);
    let screen = v.wait_text("没有在跑的 fleet；点侧栏卡片上的 ↺ 恢复", T);
    let (row, col) = v.wait_find("没有在跑的 fleet", T);
    assert!(
        row > 0 && col >= 34,
        "the no-fleet placeholder is not right of the sidebar from row 1 (row {row}, col {col}):\n{screen}"
    );
    let first = v.cell(row, col);
    assert_eq!(
        first.fg,
        Color::Indexed(15),
        "the no-fleet placeholder's first cell is not White: {first:?}\n{screen}"
    );
    assert!(
        first.attrs.bold,
        "the no-fleet placeholder's first cell is not bold: {first:?}\n{screen}"
    );
    // No group frame; row 1 holds only the line under the topbar (`├` then `─`, cmux look, user 2026-11-08).
    let row1 = v.row_text(1);
    assert!(
        row1.trim_start()
            .strip_prefix('├')
            .is_some_and(|r| r.chars().all(|c| c == '─')),
        "the empty grid still draws a frame: row 1 is {row1:?}\n{screen}"
    );
    assert!(
        !screen.contains("fleet「fmx-a」已关闭") && !screen.contains("fleet「fmx-b」已关闭"),
        "a window opened on the history list still shows the S2b just-closed line:\n{screen}"
    );
    let top = v.row_text(0);
    assert!(
        top.starts_with(" ◫  +  ↺  ←  →"),
        "row 0 is not the topbar with a history list: {top:?}\n{screen}"
    );
}

/// The generalized reaper (plan §18.4-5 / §18.7-6): kv `sidebar` holding only grey cards — no kv `restorable` at
/// all — is enough; once the last viewer is gone, 10 quiet 1 s checks exit the server.
#[test]
fn empty_server_with_only_grey_cards_exits_after_ten_seconds() {
    let mut srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_TWO);
    let sock = srv.sock();
    let mut viewer = Viewer::attach(&srv, W, H);
    let screen = viewer.wait_text("没有在跑的 fleet；点侧栏卡片上的 ↺ 恢复", T);
    drop(viewer);
    let what = format!(
        "the server with only grey cards to exit 10 s after the last viewer left (plan §18.4-5); \
         history-list screen was:\n{screen}"
    );
    wait_until(&what, Duration::from_secs(15), || {
        try_hello(&sock).is_none().then_some(())
    });
    assert!(
        wait_exit(&mut srv.child, Duration::from_secs(5)).is_some(),
        "the reaper stopped answering `hello` but the server process is still running; \
         history-list screen was:\n{screen}"
    );
    assert!(
        !sock.exists(),
        "the reaper left {} behind; history-list screen was:\n{screen}",
        sock.display()
    );
}

/// A fleet stopped while a busy bash page kept its server (2026-11-08): the closer turns the root card `stopped` and
/// the tree's cards `reclaimed` — no card is `running` / `paused` / `stopping` — so once that bash page is closed
/// too, the server with 0 panes and 0 viewers exits after the reaper's 10 quiet seconds.
#[test]
fn stopped_fleet_server_exits_once_its_last_bash_page_is_closed() {
    let mut srv = Server::start();
    let shell = srv.spawn_id("bash-1", "shell", &["sleep", "1000"]);
    set_kv(
        &srv,
        "sidebar",
        r#"{"v":1,"current":"fleet-s","fleets":[{"id":"fleet-s","title":"fmx-s","state":"stopped","line1":"已停止","line2":"~/.fleetmux/fleets/s"},{"id":"M0","title":"M0","state":"reclaimed","line1":"已收回 · 随根关闭","line2":"只读，可看记录"}]}"#,
    );
    let sock = srv.sock();
    srv.ok("pane.kill", json!({ "pane": shell }));
    wait_until(
        "the stopped fleet's server to exit 10 s after its last bash page closed (plan §13.4)",
        Duration::from_secs(15),
        || try_hello(&sock).is_none().then_some(()),
    );
    assert!(
        wait_exit(&mut srv.child, Duration::from_secs(5)).is_some(),
        "the reaper stopped answering `hello` but the server process is still running"
    );
}

// ------------------------- S1c: selecting and closing bash cards (plan §14.2 / §14.5) --------------------------

/// The sidebar's bash panes (`type "shell"`, plan §14.3). User 2026-11-09: each is a tab in the root page's `workers`
/// group (the fleet registers it); listed with `all` so a bash elsewhere would still be seen.
fn shell_panes(srv: &Server) -> Vec<Value> {
    srv.panes_all()
        .into_iter()
        .filter(|p| p["type"] == json!("shell"))
        .collect()
}

/// Whether pane `id` is listed on any page.
fn listed_anywhere(srv: &Server, id: &str) -> bool {
    srv.panes_all().iter().any(|p| p["pane"] == json!(id))
}

/// Whether bash pane `id` is shown (user 2026-11-09): the root page is current, `id` sits in its `workers` group as
/// that group's selected tab, the focus is there and nothing is zoomed.
fn bash_shown(srv: &Server, id: &str) -> bool {
    let l = srv.layout();
    srv.current_page().is_empty()
        && l["groups"]["workers"].as_array().is_some_and(|w| w.contains(&json!(id)))
        && l["focus"] == json!("workers")
        && l["selected"]["workers"] == json!(id)
        && l["zoom"] == Value::Null
}

/// Wait until bash pane `id` is shown ([`bash_shown`]).
fn wait_bash_shown(srv: &Server, id: &str, what: &str) {
    wait_until(what, T, || bash_shown(srv, id).then_some(()));
}

/// Wait for the one bash page (`bash-1`) and return its pane id.
fn wait_bash_pane(srv: &Server) -> String {
    wait_until("one type shell pane titled bash-1", T, || {
        let sh = shell_panes(srv);
        (sh.len() == 1 && sh[0]["title"] == json!("bash-1")).then_some(())
    });
    shell_panes(srv)[0]["pane"]
        .as_str()
        .expect("a bash pane has an id")
        .to_string()
}

/// `+` opens a bash as a tab at the end of the root page's `workers` group and shows it (selected, focused); clicking
/// the fleet card hands the focus back to the supervisor, the bash tab staying in the worker area; clicking the bash
/// card focuses its tab again (user 2026-11-09: every new bash is mapped into the main fleet's worker area). The
/// topbar names the selected card; the status line claims no zoom and no page.
#[test]
fn clicking_a_bash_card_focuses_its_tab_in_the_workers_group() {
    let srv = Server::start();
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    v.input(&sgr_click(4, 0, 0));
    let id = wait_bash_pane(&srv);
    let screen = v.wait_text("bash-1", T);
    wait_bash_shown(&srv, &id, "the new bash tab to be selected and focused in the workers group");
    let workers = srv.layout()["groups"]["workers"].clone();
    assert_eq!(
        workers.as_array().and_then(|w| w.last()),
        Some(&json!(id)),
        "the bash tab is not last in the workers group: {workers}"
    );
    assert_eq!(srv.page_ids(), vec![String::new()], "the bash opened a page of its own");
    wait_until("the bash card at (6,5) to take the blue bar", T, || {
        (v.cell(6, 5).bg == Color::Indexed(4)).then_some(())
    });
    wait_until("the topbar to name the bash card", T, || {
        v.row_text(0).contains("bash-1 · 终端 · 运行中").then_some(())
    });
    let status = v.status_line();
    assert!(
        !status.contains("zoom:") && !status.contains("page:"),
        "the status line claims a zoom / page for the bash tab: {status:?}\n{screen}"
    );

    // The fleet card's body hands the focus back to the supervisor; the bash stays a tab of the worker area.
    v.input(&sgr_click(10, 2, 0));
    wait_until("the fleet card to take the blue bar", T, || {
        (v.cell(2, 5).bg == Color::Indexed(4) && v.cell(6, 5).bg != Color::Indexed(4))
            .then_some(())
    });
    let l = srv.layout();
    assert_eq!(l["focus"], json!("supervisor"), "the fleet card left the focus on the bash tab: {l}");
    assert_eq!(l["zoom"], Value::Null, "the fleet's own layout got a zoom: {l}");
    assert_eq!(srv.current_page(), "", "the fleet card left the root page");
    let screen = v.wait_text("SUP-BODY", T);
    let (tabs, _) = v.wait_find("fx-worker-1", T);
    assert!(
        v.row_text(tabs).contains("bash-1") && !v.row_text(0).contains("bash-1"),
        "the worker area's tab row lost the bash tab, or the topbar still names it:\n{screen}"
    );

    // And the bash card's body focuses its tab again.
    v.input(&sgr_click(10, 6, 0));
    wait_bash_shown(&srv, &id, "the bash card to focus its tab again");
    wait_until("the bash card to take the blue bar again", T, || {
        (v.cell(6, 5).bg == Color::Indexed(4)).then_some(())
    });
}

/// `←` / `→` and `^b (` / `^b )` walk the merged list (plan §14.2 / §14.5-3): the fleet card is before the bash
/// card, so one step back focuses the supervisor and one step forward the bash tab. User 2026-11-09: the bash is a tab
/// of the same page, so a zoom of another pane would hide it — opening or picking the bash cancels that zoom.
#[test]
fn the_arrows_and_the_parens_walk_the_merged_cards() {
    let srv = Server::start();
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    // The fleet's own view has a zoom of its own; the new bash cancels it, so the bash tab is seen.
    let w1 = srv
        .panes()
        .into_iter()
        .find(|p| p["title"] == json!("fx-worker-1"))
        .and_then(|p| p["pane"].as_str().map(String::from))
        .expect("fx-worker-1 is listed");
    srv.ok("layout.set", json!({"zoom": w1}));
    v.input(&sgr_click(4, 0, 0));
    let id = wait_bash_pane(&srv);
    v.wait_text("bash-1", T);
    wait_bash_shown(&srv, &id, "the new bash tab shown (the w1 zoom cancelled)");
    let supervisor_focused =
        || (srv.layout()["focus"] == json!("supervisor") && !bash_shown(&srv, &id)).then_some(());

    v.input(&sgr_click(10, 0, 0));
    wait_until("the ← button to select the fleet card", T, supervisor_focused);
    v.input(&sgr_click(13, 0, 0));
    wait_bash_shown(&srv, &id, "the → button to select the bash card");
    v.input(&prefixed(b"("));
    wait_until("^b ( to select the fleet card", T, supervisor_focused);
    v.input(&prefixed(b")"));
    wait_bash_shown(&srv, &id, "^b ) to select the bash card");
    // The bash card is the last one: `→` has nowhere to go.
    v.input(&sgr_click(13, 0, 0));
    v.wait_quiet(Duration::from_millis(300), T);
    assert!(bash_shown(&srv, &id), "→ at the end of the merged list moved the selection: {}", srv.layout());
}

/// An idle shell closes right away from its card's `×` (plan §14.2): the pane goes, and the card and the workers tab
/// go with it.
#[test]
fn closing_an_idle_bash_card_kills_its_shell() {
    let srv = Server::start();
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    v.input(&sgr_click(4, 0, 0));
    let id = wait_bash_pane(&srv);
    v.wait_text("bash-1", T);
    // `echo` is a builtin, so the shell never leaves the foreground: once its output shows, the shell is idle and
    // the `×` must not ask (plan §14.2). The typed line carries the quotes, so only the output has `SHELL-READY`.
    srv.write(&id, b"echo SHELL-\"READY\"\n", "raw");
    srv.wait_text(&id, "SHELL-READY", T);

    v.input(&sgr_click(31, 6, 0));
    let screen = v.wait_no_text("bash-1", T);
    wait_until("the idle shell to leave every page", T, || {
        (!listed_anywhere(&srv, &id)).then_some(())
    });
    assert!(
        shell_panes(&srv).is_empty(),
        "a bash pane survived the close: {:?}\n{screen}",
        srv.panes_all()
    );
    let workers = srv.layout()["groups"]["workers"].clone();
    assert!(
        !workers.as_array().is_some_and(|w| w.contains(&json!(id))),
        "the closed bash is still a workers tab: {workers}"
    );
    assert_eq!(srv.current_page(), "", "closing the bash left the root page");
}

/// A shell with a program in the foreground asks first (plan §14.2): the same box says `bash-1 里还有程序在跑，关掉会
/// 结束它。`, any key but `y` keeps the page, `y` kills it.
#[test]
fn closing_a_busy_bash_card_asks_first_and_y_kills_it() {
    let srv = Server::start();
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    v.input(&sgr_click(4, 0, 0));
    let id = wait_bash_pane(&srv);
    v.wait_text("bash-1", T);
    // The job prints `JOB-STARTED` itself (the typed line carries the quotes), so by the time it shows, the shell
    // has put that job's process group in the foreground (plan §14.2: the `×` must ask then).
    srv.write(
        &id,
        b"sh -c 'echo JOB-\"STARTED\"; exec sleep 1000'\n",
        "raw",
    );
    srv.wait_text(&id, "JOB-STARTED", T);

    v.input(&sgr_click(31, 6, 0));
    let screen = v.wait_text("bash-1 里还有程序在跑，关掉会结束它。", T);
    assert!(
        listed_anywhere(&srv, &id),
        "the busy shell {id} was killed without asking:\n{screen}"
    );
    assert!(
        screen.contains("关闭 bash 页「bash-1」？"),
        "the box does not name the bash page it would close:\n{screen}"
    );

    // Anything but `y` keeps the page (plan §2.5: the box cancels).
    v.input(b"n");
    let screen = v.wait_no_text("里还有程序在跑", T);
    assert!(
        listed_anywhere(&srv, &id),
        "cancelling the box killed the shell {id}:\n{screen}"
    );

    v.input(&sgr_click(31, 6, 0));
    v.wait_text("bash-1 里还有程序在跑，关掉会结束它。", T);
    v.input(b"y");
    let screen = v.wait_no_text("bash-1", T);
    wait_until("the confirmed close to kill the shell", T, || {
        shell_panes(&srv).is_empty().then_some(())
    });
    assert!(
        !listed_anywhere(&srv, &id),
        "the confirmed close left a bash pane behind: {:?}\n{screen}",
        srv.panes_all()
    );
}

/// Plan §14.5-6: the closed-fleet placeholder belongs to the fleet's card. A bash opened with no fleet running still
/// lands in the root page's worker area (user 2026-11-09); while its card is selected the placeholder steps aside, and
/// the closed card's body brings the placeholder back.
#[test]
fn a_bash_tab_keeps_the_closed_placeholder_away_while_it_is_shown() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("fleet「fleetmux-demo」已关闭", T);

    v.input(&sgr_click(4, 0, 0));
    let id = wait_bash_pane(&srv);
    wait_bash_shown(&srv, &id, "the new bash tab to be shown in the root page's workers group");
    srv.write(&id, b"echo BASH-BODY\n", "raw");
    let screen = v.wait_text("BASH-BODY", T);
    assert!(
        !screen.contains("fleet「fleetmux-demo」已关闭"),
        "the closed placeholder still covers the selected bash tab:\n{screen}"
    );
    let (row, col) = v.wait_find("BASH-BODY", T);
    assert!(
        row >= 1 && col >= 34,
        "the bash tab is not right of the sidebar from row 1 (row {row}, col {col}):\n{screen}"
    );

    // The closed card's body hands the focus back: the placeholder is back, the bash content is not drawn.
    v.input(&sgr_click(10, 2, 0));
    let screen = v.wait_text("fleet「fleetmux-demo」已关闭", T);
    assert!(
        !screen.contains("BASH-BODY"),
        "the deselected bash tab still shows under the placeholder:\n{screen}"
    );
    // Selecting the bash card again brings the shell back and drops the placeholder.
    v.input(&sgr_click(10, 6, 0));
    let screen = v.wait_text("BASH-BODY", T);
    assert!(
        !screen.contains("fleet「fleetmux-demo」已关闭"),
        "the placeholder came back with the bash card selected:\n{screen}"
    );
}

// ---- A26 rev3 (user requirement A42-LAYOUT-LINUX-MIRROR): the mirrored content layout is drawn, not only
// computed — workers left and the supervisor/lower stack right within the workspace. Since 2026-11-01,
// Linux keeps the sidebar on the left independently; the macOS forced-mirror test seam keeps its right sidebar.
// A26 rev5 (`CURRENT_REQUIREMENTS` rev 6 `utm_topbar`): the mirrored layout keeps **no** topbar row in the grid —
// the window's one header row is the native GTK headerbar (`crates/web/src/gui.rs`), which carries `◫ + ↺ ← →`
// and the fleet title/status — so row 0 is content here and nothing of the toolbar or the title is drawn in it.

#[test]
fn mirrored_layout_keeps_the_pane_order_with_the_platform_sidebar() {
    let srv = Server::start_with_env(&[("FLEETMUX_LAYOUT_MIRROR", "1"), ("FLEETMUX_NATIVE_HEADER", "1")]);
    page(&srv);
    let mut v = Viewer::attach_native(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    let screen = v.wait_text("fleetmux-demo", T);
    let top = v.row_text(0);
    assert!(
        !top.starts_with(" ◫"),
        "the mirrored grid must not draw a toolbar row (the native headerbar carries it): {top:?}\n{screen}"
    );
    assert!(
        !top.contains("fleetmux-demo · "),
        "row 0 carries the topbar's `title · line1` while mirrored, not content: {top:?}\n{screen}"
    );
    // 240 − 34 = 206 content columns at 50%: the separator is column 103, the stack 104..=204, the content's
    // right border 205 and the sidebar band 206..=239. The grid keeps no topbar row; on Linux it keeps the
    // header band's one blank separation row under the native headerbar (`Layout::header_rows`), so the groups
    // start on that row (their tab row, which is their top border) — row 0 on a macOS test run with the mirror
    // override, row 1 on Linux.
    let top = 1;
    for (row, col, what) in [
        (top, 0u16, "the workers' left border on the tab row"),
        (top, 103, "the vertical separator on the tab row"),
        (top, 205, "the content's right border on the tab row"),
        (top + 20, 0, "the workers' left border"),
        (top + 20, 103, "the vertical separator"),
        (top + 20, 205, "the content's right border"),
    ] {
        let col = col + if cfg!(target_os = "linux") { 34 } else { 0 };
        let c = v.cell(row, col);
        assert!(
            is_box(c.ch),
            "row {row}, column {col} is not {what} while mirrored: {c:?}\n{screen}"
        );
    }
    // The sidebar's first card starts on the same row as that tab row (A26 rev7 alignment).
    let row2 = v.row_text(top);
    let at = row2.find("fleetmux-demo").unwrap_or_else(|| {
        panic!("row {top} lacks the card title while mirrored: {row2:?}\n{screen}")
    });
    assert!(
        if cfg!(target_os = "linux") {
            at < 34
        } else {
            at >= 206
        },
        "the card title is outside the platform sidebar at column {at}: {row2:?}\n{screen}"
    );
}

#[cfg(target_os = "linux")]
#[test]
fn linux_default_uses_macos_pane_order_without_a_second_toolbar() {
    let srv = Server::start_with_env(&[("FLEETMUX_LAYOUT_MIRROR", "")]);
    page(&srv);
    let mut v = Viewer::attach_native(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    let (_, supervisor) = v.wait_find("SUP-BODY", T);
    let (_, worker) = v.wait_find("W1-BODY", T);
    assert!(34 < supervisor && supervisor < worker, "sidebar | supervisor | workers");
    assert!(v.row_text(0).trim().is_empty(), "native header must not duplicate a grid toolbar");
    let tabs = v.row_text(1);
    assert!(tabs.contains("fleetmux-demo") && tabs.contains("fx-supervisor-1") && tabs.contains("fx-worker-1"));
    v.input(&prefixed(b"s"));
    v.wait_quiet(Duration::from_millis(100), T);
    let (_, hidden_supervisor) = v.wait_find("SUP-BODY", T);
    let (_, hidden_worker) = v.wait_find("W1-BODY", T);
    assert!(hidden_supervisor < supervisor && hidden_supervisor < hidden_worker);
    assert!(v.row_text(0).trim().is_empty());
    v.input(&prefixed(b"s"));
    v.wait_text("fleetmux-demo", T);
    assert_eq!(v.wait_find("SUP-BODY", T).1, supervisor);
}

/// A26 rev3: with no override the server takes the platform's ordering from `Layout::for_platform` — the sidebar
/// is on the left on both platforms, followed by the supervisor/history stack, then workers.
#[test]
fn sidebar_band_follows_the_platform_by_default() {
    let srv = Server::start_with_env(&[]);
    page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    let screen = v.wait_text("fleetmux-demo", T);
    // A26 rev7: the first card starts on the header band's last row — 2 on macOS (topbar + gap), 1 on Linux
    // (the blank row under the native headerbar).
    let card_row = if cfg!(target_os = "linux") { 1 } else { 2 };
    let row2 = v.row_text(card_row);
    let at = row2
        .find("fleetmux-demo")
        .unwrap_or_else(|| panic!("row {card_row} lacks the card title: {row2:?}\n{screen}"));
    if cfg!(target_os = "linux") {
        assert!(
            at < 34,
            "Linux: the card title must be in the left band, not at column {at}: {row2:?}"
        );
        let left = v.cell(20, 34);
        assert!(
            is_box(left.ch),
            "Linux: row 20, column 34 must be the supervisor's left border: {left:?}\n{screen}"
        );
    } else {
        assert!(
            at < 40,
            "macOS: the card title must stay in the left band, not at column {at}: {row2:?}"
        );
        let right = v.cell(20, W - 1);
        assert!(
            is_box(right.ch),
            "macOS: row 20, the last column must be the workers' right border: {right:?}\n{screen}"
        );
    }
}

/// User feedback (2026-11-08): the closed-fleet placeholder draws no frame, so nothing separated the sidebar from the
/// content. The sidebar's gap column (33 for the 34-column band) now carries a `│` from the content's top row (2,
/// below the topbar and its blank row) down to the row above the status line. A bash opened there is a framed tab of
/// the worker area (user 2026-11-09), so the content's own border is the line again.
#[test]
fn the_sidebar_has_a_divider_beside_the_placeholder_and_frames_a_bash_tab() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("fleet「fleetmux-demo」已关闭", T);
    let divider = |v: &mut Viewer| (2..H - 1).all(|y| v.cell(y, 33).ch == '│');
    wait_until("the divider beside the placeholder", T, || divider(&mut v).then_some(()));

    v.input(&sgr_click(4, 0, 0));
    let id = wait_bash_pane(&srv);
    wait_bash_shown(&srv, &id, "the new bash tab to be shown");
    srv.write(&id, b"echo BASH-BODY\n", "raw");
    v.wait_text("BASH-BODY", T);
    let (_, col) = v.wait_find("BASH-BODY", T);
    assert!(col > 34, "the bash tab starts right of the content's left border (col {col})");
}

/// User feedback (2026-11-08, "完全参照 cmux"): with the closed placeholder the sidebar divider is one `│` in column 33
/// from row 0 (the topbar's own `│` moves there from column 34, the title stays at column 36) down to the row above
/// the status line, and the topbar's blank separation row (row 1) carries `─` across the content (columns 34–239)
/// meeting it in `├`. The framed layout — a bash tab of the worker area shown (user 2026-11-09) — keeps the topbar
/// `│` at column 34.
#[test]
fn the_topbar_has_a_divider_above_the_placeholder_and_frames_a_bash_tab() {
    let srv = Server::start();
    set_kv(&srv, "sidebar", CLOSED_ONE);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("fleet「fleetmux-demo」已关闭", T);
    let cmux = |v: &mut Viewer| {
        v.cell(1, 33).ch == '├'
            && (34..W).all(|x| v.cell(1, x).ch == '─')
            && (0..H - 1).filter(|y| *y != 1).all(|y| v.cell(y, 33).ch == '│')
            && v.cell(0, 34).ch == ' '
    };
    wait_until("the cmux lines around the placeholder", T, || cmux(&mut v).then_some(()));
    assert_eq!(v.find("fleetmux-demo"), Some((0, 36)), "the topbar title stays at column 36");

    v.input(&sgr_click(4, 0, 0));
    let id = wait_bash_pane(&srv);
    wait_bash_shown(&srv, &id, "the new bash tab to be shown");
    srv.write(&id, b"echo BASH-BODY\n", "raw");
    v.wait_text("BASH-BODY", T);
    let (row, col) = v.wait_find("BASH-BODY", T);
    assert!(row >= 2 && col > 34, "the bash tab starts below / right of the frame (row {row}, col {col})");
    // Framed again: the topbar `│` sits on the boxes' left border column 34 —
    // as in `topbar_is_row_0_and_the_groups_start_on_row_2`.
    wait_until("the topbar divider back on column 34", T, || (v.cell(0, 34).ch == '│').then_some(()));
}
