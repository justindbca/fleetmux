//! Mouse hit testing and drag-to-ratio (brief-m03 §3.3 / §3.4; PROTOCOL.md §11; design.md §1 鼠标, §3.1).
//! Red in stage A (`todo!()`), green from M1 (`layout-hit`, B4).

use fleetmux_layout::{
    Group, HEADER_GAP, Hit, Layout, PaneId, PaneType, TOPBAR_ROWS, ratio_from_x, ratio_from_y,
};

fn id(s: &str) -> PaneId {
    s.to_string()
}

/// A filled run: supervisor + advisor, history + control + one tab, three workers (same as `model.rs`).
fn full_run() -> Layout {
    let mut l = Layout::new();
    l.insert(id("supervisor-1"), PaneType::Supervisor);
    l.insert(id("advisor-1"), PaneType::Advisor);
    l.insert(id("history"), PaneType::History);
    l.insert(id("control"), PaneType::Daemon);
    l.insert(id("evermem-shell"), PaneType::Tab);
    for w in ["worker-1", "worker-2", "worker-3"] {
        l.insert(id(w), PaneType::Worker);
    }
    l
}

#[test]
#[ignore = "M1: layout-hit"]
fn hit_test_tab_pane_separator_status() {
    let l = full_run();
    assert_eq!(l.ratios(), (50, 60));
    assert_eq!(
        l.hit_test(240, 60, 5, 0),
        Hit::Tab {
            group: Group::Supervisor,
            index: 0
        }
    );
    assert_eq!(l.hit_test(240, 60, 60, 10), Hit::Pane(Group::Supervisor));
    assert_eq!(l.hit_test(240, 60, 119, 30), Hit::VSep);
    assert_eq!(l.hit_test(240, 60, 50, 35), Hit::HSep);
    assert_eq!(l.hit_test(240, 60, 10, 59), Hit::Status);
}

#[test]
#[ignore = "M1: layout-hit"]
fn drag_x_maps_to_h_ratio_clamped() {
    assert_eq!(ratio_from_x(240, 120), 50);
    assert_eq!(ratio_from_x(240, 5), 10);
    assert_eq!(ratio_from_x(240, 239), 90);
}

#[test]
#[ignore = "M1: layout-hit"]
fn drag_y_maps_to_v_ratio_clamped() {
    // Exact divisions only, so no rounding rule is pinned here.
    assert_eq!(ratio_from_y(50, 25), 50);
    assert_eq!(ratio_from_y(50, 30), 60);
    assert_eq!(ratio_from_y(50, 2), 10);
    assert_eq!(ratio_from_y(50, 49), 90);
}

#[test]
#[ignore = "M1: layout-hit"]
fn tab_rects_follow_tab_order_and_truncate_titles() {
    // Four 30-character titles in the supervisor group (inner width ≈118 at 240×60). `Layout` knows panes
    // by id, so the ids stand in for the titles.
    let titles: Vec<PaneId> = (1..=4)
        .map(|n| format!("supervisor-tab-{n}-{}", "x".repeat(13)))
        .collect();
    assert!(titles.iter().all(|t| t.chars().count() == 30));
    let mut l = Layout::new();
    l.insert(titles[0].clone(), PaneType::Supervisor);
    for t in &titles[1..] {
        l.insert(t.clone(), PaneType::Advisor);
    }
    assert_eq!(l.group(Group::Supervisor), titles.as_slice());

    let inner = l.rects(240, 60).supervisor;
    let tabs: Vec<_> = l
        .tab_rects(240, 60)
        .into_iter()
        .filter(|(g, _, _)| *g == Group::Supervisor)
        .collect();
    assert_eq!(
        tabs.iter().map(|(_, i, _)| *i).collect::<Vec<_>>(),
        vec![0, 1, 2, 3]
    );
    for (_, i, r) in &tabs {
        assert!(r.w >= 1 && r.w <= 28 && r.h == 1, "tab {i}: {r:?}");
        assert!(
            r.x >= inner.x && r.x + r.w <= inner.x + inner.w,
            "tab {i} outside {inner:?}: {r:?}"
        );
        assert_eq!(
            l.hit_test(240, 60, r.x, r.y),
            Hit::Tab {
                group: Group::Supervisor,
                index: *i
            }
        );
    }
    for pair in tabs.windows(2) {
        let (a, b) = (pair[0].2, pair[1].2);
        assert!(
            a.x + a.w <= b.x,
            "tabs overlap or out of order: {a:?} {b:?}"
        );
    }
}

/// fix-m0-4 (supervisor ruling [M31], a per-group refinement of [M30]): at the common 196×50 and 240×60 viewer
/// sizes a rotated supervisor's full generation label (`fx-supervisor-1.1`, 17 columns) fits on the top
/// border row, both while the two generations share the group and after the old one is gone, and so do three
/// workers' labels. The lower group's labels sit on the draggable separator row and keep the 15-column cap, so
/// (50,35) at 240×60 is still the separator; many workers still truncate by their equal share.
#[test]
fn generation_titles_fit_on_top_rows_at_196_and_240() {
    const FULL: u16 = 17; // "fx-supervisor-1.1"
    assert_eq!("fx-supervisor-1.1".chars().count(), FULL as usize);
    let page = |workers: usize| {
        let mut l = Layout::new();
        l.insert(id("fx-supervisor-1"), PaneType::Supervisor);
        l.insert(id("fx-supervisor-1.1"), PaneType::Supervisor); // the successor, alive next to gen 0
        l.insert(id("history"), PaneType::History);
        l.insert(id("control"), PaneType::Daemon);
        l.insert(id("evermem-shell"), PaneType::Tab);
        for n in 1..=workers {
            l.insert(id(&format!("fx-worker-{n}")), PaneType::Worker);
        }
        l
    };
    let in_group = |l: &Layout, cols: u16, rows: u16, g: Group| -> Vec<_> {
        l.tab_rects(cols, rows)
            .into_iter()
            .filter(|(tg, _, _)| *tg == g)
            .map(|(_, i, r)| (i, r))
            .collect()
    };

    for (cols, rows) in [(196, 50), (240, 60)] {
        let mut l = page(3);
        assert_eq!(l.ratios(), (50, 60));
        let r = l.rects(cols, rows);
        for (g, inner, n) in [
            (Group::Supervisor, r.supervisor, 2),
            (Group::Workers, r.workers, 3),
        ] {
            let tabs = in_group(&l, cols, rows, g);
            assert_eq!(tabs.len(), n, "{cols}x{rows} {g:?}: {tabs:?}");
            for (i, t) in &tabs {
                assert!(
                    t.w >= FULL && t.h == 1,
                    "{cols}x{rows} {g:?} tab {i}: {t:?} cannot hold {FULL} columns"
                );
                assert!(
                    t.y == 0 && t.x > inner.x && t.x + t.w <= inner.x + inner.w,
                    "{cols}x{rows} {g:?} tab {i} outside {inner:?}: {t:?}"
                );
                assert_eq!(
                    l.hit_test(cols, rows, t.x, t.y),
                    Hit::Tab {
                        group: g,
                        index: *i
                    }
                );
                assert_eq!(
                    l.hit_test(cols, rows, t.x + t.w - 1, t.y),
                    Hit::Tab {
                        group: g,
                        index: *i
                    }
                );
            }
        }
        let lower = in_group(&l, cols, rows, Group::Lower);
        assert_eq!(lower.len(), 3, "{cols}x{rows} lower: {lower:?}");
        assert!(
            lower.iter().all(|(_, t)| t.w <= 15),
            "{cols}x{rows} lower labels wider than 15: {lower:?}"
        );

        // Gen 0 goes away: the successor keeps tab 0 of the supervisor group and its full width.
        l.remove(id("fx-supervisor-1"));
        assert_eq!(
            l.group(Group::Supervisor),
            [id("fx-supervisor-1.1")].as_slice()
        );
        let sup = in_group(&l, cols, rows, Group::Supervisor);
        assert_eq!(sup.len(), 1);
        assert!(
            sup[0].0 == 0 && sup[0].1.w >= FULL,
            "{cols}x{rows} successor tab: {sup:?}"
        );
        assert_eq!(
            l.hit_test(cols, rows, sup[0].1.x, 0),
            Hit::Tab {
                group: Group::Supervisor,
                index: 0
            }
        );

        // Eight workers: labels still truncate by the equal share (share − 1), as before.
        let many = page(8);
        let inner = many.rects(cols, rows).workers;
        let share_title = inner.w / 8 - 1;
        let workers = in_group(&many, cols, rows, Group::Workers);
        assert_eq!(workers.len(), 8);
        assert!(
            share_title < FULL,
            "{cols}x{rows}: 8 workers should not fit {FULL} columns each"
        );
        assert!(
            workers.iter().all(|(_, t)| t.w == share_title),
            "{cols}x{rows} 8 workers: {workers:?}"
        );
    }

    // The lower group's cap is what keeps (50,35) the horizontal separator with three lower tabs.
    assert_eq!(page(3).hit_test(240, 60, 50, 35), Hit::HSep);
}

// ---- fix-m1-tabs (supervisor ruling [M49], option B): titled tab blocks ----
//
// `tab_rects_titled` / `hit_test_titled` take the display width of the text inside each block (unread `• ` +
// title, no pads). A block is `min(label + 2, share − 1, cap + 2)` columns from the group's inner x, one
// column of gap between blocks, nothing after the last block; cap 24 on the top row, 15 on the separator row.

/// Display width of an ASCII id, standing in for its title.
fn ascii_cols(id: &PaneId) -> u16 {
    id.chars().count() as u16
}

fn titled_in_group(
    l: &Layout,
    cols: u16,
    rows: u16,
    g: Group,
    label_cols: impl Fn(&PaneId) -> u16,
) -> Vec<(usize, fleetmux_layout::Rect)> {
    l.tab_rects_titled(cols, rows, label_cols)
        .into_iter()
        .filter(|(tg, _, _)| *tg == g)
        .map(|(_, i, r)| (i, r))
        .collect()
}

fn is_tab(h: Hit) -> bool {
    matches!(h, Hit::Tab { .. })
}

/// Operator bug ③: clicking the blank tail of the tab row after the last (short) label must not select it.
#[test]
fn tail_blank_after_last_tab_hits_no_tab() {
    let mut l = full_run();
    l.insert(id("fx-ci-1"), PaneType::Tab); // a short lower title, like the operator's `fx-ci-1`
    let r = l.rects(240, 60);
    for (g, inner) in [
        (Group::Supervisor, r.supervisor),
        (Group::Lower, r.lower),
        (Group::Workers, r.workers),
    ] {
        let tabs = titled_in_group(&l, 240, 60, g, ascii_cols);
        let (_, last) = *tabs.last().unwrap_or_else(|| panic!("{g:?}: no tabs"));
        assert!(
            last.x + last.w < inner.x + inner.w,
            "{g:?}: last block fills the row: {last:?}"
        );
        for x in last.x + last.w..inner.x + inner.w {
            let h = l.hit_test_titled(240, 60, x, last.y, ascii_cols);
            assert!(!is_tab(h), "{g:?}: tail column ({x},{}) hits {h:?}", last.y);
        }
    }
    // A lone short supervisor title: one block of label + 2 at the inner x, the rest of the row is no tab.
    let mut one = Layout::new();
    one.insert(id("fx-ci-1"), PaneType::Supervisor);
    let inner = one.rects(240, 60).supervisor;
    assert_eq!(
        titled_in_group(&one, 240, 60, Group::Supervisor, ascii_cols),
        vec![(
            0,
            fleetmux_layout::Rect {
                x: inner.x,
                y: 0,
                w: 9,
                h: 1
            }
        )]
    );
    assert!(!is_tab(one.hit_test_titled(
        240,
        60,
        inner.x + 9,
        0,
        ascii_cols
    )));
    assert!(!is_tab(one.hit_test_titled(
        240,
        60,
        inner.x + 20,
        0,
        ascii_cols
    )));
}

/// Each block is label + 2 pads from the inner x, blocks are one gap column apart; both block edges hit the
/// tab, the gap hits none; an unread `• ` (2 columns) widens its block by 2.
#[test]
fn titled_block_and_gap_boundaries() {
    let l = full_run();
    let unread = |p: &PaneId| ascii_cols(p) + if p == "worker-2" { 2 } else { 0 };
    let r = l.rects(240, 60);
    let hsep = r.supervisor.y + r.supervisor.h;
    for (g, inner, row) in [
        (Group::Supervisor, r.supervisor, 0),
        (Group::Lower, r.lower, hsep),
        (Group::Workers, r.workers, 0),
    ] {
        let tabs = titled_in_group(&l, 240, 60, g, unread);
        assert_eq!(tabs.len(), l.group(g).len(), "{g:?}: {tabs:?}");
        let mut x = inner.x;
        for ((i, t), pane) in tabs.iter().zip(l.group(g)) {
            let want = fleetmux_layout::Rect {
                x,
                y: row,
                w: unread(pane) + 2,
                h: 1,
            };
            assert_eq!(*t, want, "{g:?} tab {i} ({pane})");
            for cx in [t.x, t.x + t.w - 1] {
                assert_eq!(
                    l.hit_test_titled(240, 60, cx, row, unread),
                    Hit::Tab {
                        group: g,
                        index: *i
                    },
                    "{g:?} tab {i} edge x={cx}"
                );
            }
            let gap = t.x + t.w;
            let h = l.hit_test_titled(240, 60, gap, row, unread);
            assert!(!is_tab(h), "{g:?} gap after tab {i} (x={gap}) hits {h:?}");
            x = gap + 1;
        }
    }
}

/// A title wider than the block is cut to the block's inner width, so both pads stay: the block is
/// share − 1 wide with many tabs, cap + 2 with one tab (26 on the top row, 17 on the separator row).
#[test]
fn truncated_title_keeps_both_pads() {
    let long = |_: &PaneId| 40;
    let mut l = Layout::new();
    l.insert(id("supervisor-1"), PaneType::Supervisor);
    l.insert(id("history"), PaneType::History);
    for n in 1..=8 {
        l.insert(id(&format!("w-{n}")), PaneType::Worker);
    }
    let r = l.rects(240, 60);
    let sup = titled_in_group(&l, 240, 60, Group::Supervisor, long);
    assert_eq!(sup.len(), 1);
    assert_eq!((sup[0].1.x, sup[0].1.w), (r.supervisor.x, 26));
    let lower = titled_in_group(&l, 240, 60, Group::Lower, long);
    assert_eq!(lower.len(), 1);
    assert_eq!((lower[0].1.x, lower[0].1.w), (r.lower.x, 17));

    let share = r.workers.w / 8;
    let w = (share - 1).min(26);
    assert!((3..42).contains(&w), "share {share}");
    let workers = titled_in_group(&l, 240, 60, Group::Workers, long);
    assert_eq!(workers.len(), 8);
    for (k, (i, t)) in workers.iter().enumerate() {
        assert_eq!(*i, k);
        assert_eq!(t.w, w, "worker tab {i}: {t:?}");
        assert_eq!(
            t.x,
            r.workers.x + k as u16 * (w + 1),
            "worker tab {i}: {t:?}"
        );
        assert!(t.x + t.w <= r.workers.x + r.workers.w);
    }
}

/// After a ratio drag the blocks are recomputed from the new group areas: the workers' first block starts at
/// the new inner x, the lower blocks sit on the new separator row, and the old block columns no longer hit.
#[test]
fn titled_rects_follow_set_ratios() {
    let mut l = full_run();
    let before = titled_in_group(&l, 240, 60, Group::Workers, ascii_cols);
    l.set_ratios(30, 40);
    assert_eq!(l.ratios(), (30, 40));
    let r = l.rects(240, 60);
    let hsep = r.supervisor.y + r.supervisor.h;
    for (g, inner, row) in [
        (Group::Supervisor, r.supervisor, 0),
        (Group::Lower, r.lower, hsep),
        (Group::Workers, r.workers, 0),
    ] {
        let tabs = titled_in_group(&l, 240, 60, g, ascii_cols);
        assert_eq!(tabs[0].1.x, inner.x, "{g:?}: {tabs:?}");
        for (i, t) in &tabs {
            assert!(
                t.y == row && t.x + t.w <= inner.x + inner.w,
                "{g:?} tab {i}: {t:?}"
            );
        }
        let last = tabs.last().unwrap().1;
        for x in last.x + last.w..inner.x + inner.w {
            assert!(
                !is_tab(l.hit_test_titled(240, 60, x, row, ascii_cols)),
                "{g:?} tail x={x}"
            );
        }
    }
    for (i, t) in before {
        let h = l.hit_test_titled(240, 60, t.x, 0, ascii_cols);
        assert_ne!(
            h,
            Hit::Tab {
                group: Group::Workers,
                index: i
            },
            "old block {i} at x={} still hits",
            t.x
        );
    }
}

/// The lower group's 15-column cap keeps (50,35) the draggable separator with the usual three lower tabs.
#[test]
fn titled_lower_cap_keeps_hsep_at_50_35() {
    let l = full_run();
    let r = l.rects(240, 60);
    let lower = titled_in_group(&l, 240, 60, Group::Lower, ascii_cols);
    assert_eq!(lower.len(), 3);
    assert_eq!(lower[0].1.x, r.lower.x);
    assert!(
        lower.iter().all(|(_, t)| t.y == 35 && t.w <= 17),
        "{lower:?}"
    );
    assert_eq!(l.hit_test_titled(240, 60, 50, 35, ascii_cols), Hit::HSep);
    let long = |_: &PaneId| 40;
    assert!(
        titled_in_group(&l, 240, 60, Group::Lower, long)
            .iter()
            .all(|(_, t)| t.w <= 17),
        "lower cap"
    );
}

/// `label_cols` is display columns, not characters: a CJK title of 4 characters / 6 columns gets an 8-column
/// block. Extreme inputs saturate: 0 columns (block of 2) is not drawn, u16::MAX is capped, a tiny viewer
/// yields no overlapping or out-of-group block and never panics.
#[test]
fn titled_label_cols_are_display_columns_and_saturate() {
    let mut l = Layout::new();
    l.insert(id("监工-1"), PaneType::Supervisor);
    assert_eq!("监工-1".chars().count(), 4);
    let cjk = |p: &PaneId| if p == "监工-1" { 6 } else { ascii_cols(p) };
    let inner = l.rects(240, 60).supervisor;
    assert_eq!(
        titled_in_group(&l, 240, 60, Group::Supervisor, cjk),
        vec![(
            0,
            fleetmux_layout::Rect {
                x: inner.x,
                y: 0,
                w: 8,
                h: 1
            }
        )]
    );
    let unread_cjk = |p: &PaneId| cjk(p) + 2;
    assert_eq!(
        titled_in_group(&l, 240, 60, Group::Supervisor, unread_cjk)[0]
            .1
            .w,
        10
    );
    assert!(!is_tab(l.hit_test_titled(240, 60, inner.x + 8, 0, cjk)));

    assert!(titled_in_group(&l, 240, 60, Group::Supervisor, |_| 0).is_empty());
    assert_eq!(
        titled_in_group(&l, 240, 60, Group::Supervisor, |_| 1)[0]
            .1
            .w,
        3
    );
    assert_eq!(
        titled_in_group(&l, 240, 60, Group::Supervisor, |_| u16::MAX)[0]
            .1
            .w,
        26
    );

    let many = {
        let mut m = full_run();
        for n in 4..=12 {
            m.insert(id(&format!("worker-{n}")), PaneType::Worker);
        }
        m
    };
    for (cols, rows) in [(0, 0), (1, 1), (4, 3), (12, 6), (30, 8), (80, 24)] {
        let r = many.rects(cols, rows);
        for big in [0u16, 5, u16::MAX] {
            let tabs = many.tab_rects_titled(cols, rows, |_| big);
            for (g, i, t) in &tabs {
                let inner = match g {
                    Group::Supervisor => r.supervisor,
                    Group::Lower => r.lower,
                    Group::Workers => r.workers,
                };
                assert!(t.w >= 3 && t.h == 1, "{cols}x{rows} {g:?} {i}: {t:?}");
                assert!(
                    t.x >= inner.x && t.x + t.w <= inner.x + inner.w,
                    "{cols}x{rows} {g:?} {i} outside {inner:?}: {t:?}"
                );
            }
            for pair in tabs.windows(2) {
                let ((ga, _, a), (gb, _, b)) = (pair[0], pair[1]);
                assert!(ga != gb || a.x + a.w < b.x, "{cols}x{rows}: {a:?} {b:?}");
            }
            for x in 0..cols {
                let _ = many.hit_test_titled(cols, rows, x, 0, |_| big);
            }
        }
    }
}

// S1-① sidebar geometry (plan §4.1): the sidebar columns are their own hit target (the status row still wins),
// separators and tab blocks move right with it, and `ratio_from_x_in` inverts `rects` in content coordinates.

#[test]
fn sidebar_columns_hit_sidebar_and_the_status_row_still_wins() {
    let mut l = Layout::new();
    l.set_sidebar(34);
    let label = |_: &PaneId| 10;
    assert_eq!(
        l.hit_test_titled(240, 60, 0, 10, label),
        Hit::Sidebar,
        "the first sidebar column"
    );
    assert_eq!(
        l.hit_test_titled(240, 60, 33, 10, label),
        Hit::SidebarEdge,
        "column 33 is the sidebar gap, the draggable edge"
    );
    assert_eq!(
        l.hit_test_titled(240, 60, 34, 10, label),
        Hit::SidebarEdge,
        "column 34 is the supervisor's left border, the draggable edge"
    );
    assert_eq!(
        l.hit_test_titled(240, 60, 10, 59, label),
        Hit::Status,
        "the status row wins over the sidebar"
    );
    assert_ne!(
        Layout::new().hit_test_titled(240, 60, 0, 10, label),
        Hit::Sidebar,
        "without a sidebar column 0 is not the sidebar"
    );
}

#[test]
fn separators_and_tabs_move_right_with_the_sidebar() {
    let mut l = Layout::new();
    l.insert(id("supervisor-1"), PaneType::Supervisor);
    l.insert(id("worker-1"), PaneType::Worker);
    l.set_sidebar(34);
    let r = l.rects(240, 60);
    let vsep = r.supervisor.x + r.supervisor.w;
    assert_eq!(vsep, 136, "{r:?}");
    let label = |_: &PaneId| 10;
    assert_eq!(
        l.hit_test_titled(240, 60, vsep, 30, label),
        Hit::VSep,
        "the vertical separator sits at {vsep}: {r:?}"
    );
    assert_eq!(
        l.hit_test_titled(240, 60, 50, 35, label),
        Hit::HSep,
        "the horizontal separator is still row 35: {r:?}"
    );
    let tabs = l.tab_rects_titled(240, 60, label);
    assert_eq!(tabs.len(), 2, "one block per group, not an empty set: {tabs:?}");
    let block_x = |g: Group| {
        tabs.iter()
            .find(|(tg, _, _)| *tg == g)
            .unwrap_or_else(|| panic!("no {g:?} block: {tabs:?}"))
            .2
            .x
    };
    assert_eq!(block_x(Group::Supervisor), 35, "{tabs:?}");
    assert_eq!(block_x(Group::Workers), 137, "{tabs:?}");
}

#[test]
fn ratio_from_x_in_inverts_rects_with_and_without_sidebar() {
    let mut l = Layout::new();
    let vsep = |l: &Layout| {
        let r = l.rects(240, 60);
        r.supervisor.x + r.supervisor.w
    };
    assert_eq!(vsep(&l), 119, "no sidebar: the separator is column 119");
    assert_eq!(l.ratio_from_x_in(240, 119), 50, "no sidebar");
    assert_eq!(ratio_from_x(240, 119), 50, "no sidebar, old helper");
    l.set_sidebar(34);
    assert_eq!(vsep(&l), 136, "sidebar: the separator is column 136");
    assert_eq!(l.ratio_from_x_in(240, 136), 50, "sidebar");
    l.set_ratios(70, 60);
    let at = vsep(&l);
    assert_eq!(
        l.ratio_from_x_in(240, at),
        70,
        "the separator column of a 70/60 layout is {at}"
    );
}

// S1d-① (plan §16.4): the sidebar's right edge is draggable — the gap column plus the content's left
// border column — and the widest draggable sidebar follows the window (SIDEBAR_MIN_CONTENT kept right of it).

#[test]
fn sidebar_edge_is_the_gap_and_the_border_column() {
    let mut l = full_run();
    l.set_sidebar(34);
    let label = |_: &PaneId| 10;
    assert_eq!(
        l.hit_test_titled(240, 60, 33, 10, label),
        Hit::SidebarEdge,
        "240x60 sidebar 34: (33,10) is the gap column, the draggable edge"
    );
    assert_eq!(
        l.hit_test_titled(240, 60, 34, 10, label),
        Hit::SidebarEdge,
        "240x60 sidebar 34: (34,10) is the supervisor's left border column, the draggable edge"
    );
    assert_eq!(
        l.hit_test_titled(240, 60, 32, 10, label),
        Hit::Sidebar,
        "240x60 sidebar 34: (32,10) is still sidebar, one column left of the edge"
    );
    assert_eq!(
        l.hit_test_titled(240, 60, 35, 10, label),
        Hit::Pane(Group::Supervisor),
        "240x60 sidebar 34: (35,10) is inside the supervisor pane, one column right of the edge"
    );
    assert_eq!(
        l.hit_test_titled(240, 60, 34, 59, label),
        Hit::Status,
        "240x60 sidebar 34: (34,59) is the status row, which wins over the edge"
    );
    l.set_sidebar(1);
    assert_eq!(
        l.hit_test_titled(240, 60, 0, 10, label),
        Hit::Sidebar,
        "240x60 sidebar 1: (0,10) is still the one-column sidebar rail"
    );
    assert_ne!(
        l.hit_test_titled(240, 60, 1, 10, label),
        Hit::SidebarEdge,
        "240x60 sidebar 1: (1,10) is not a draggable edge, the rail cannot be dragged"
    );
}

#[test]
fn sidebar_max_follows_the_window() {
    assert_eq!(
        fleetmux_layout::sidebar_max(240),
        60,
        "sidebar_max(240) is capped at SIDEBAR_MAX_W = 60"
    );
    assert_eq!(
        fleetmux_layout::sidebar_max(100),
        40,
        "sidebar_max(100) keeps SIDEBAR_MIN_CONTENT = 60 columns: 100 - 60 = 40"
    );
    assert_eq!(
        fleetmux_layout::sidebar_max(80),
        20,
        "sidebar_max(80) keeps SIDEBAR_MIN_CONTENT = 60 columns: 80 - 60 = 20, below SIDEBAR_MIN_W = 24"
    );
}

// ---- S1f (plan §20.6): row 0 is the topbar, not a tab row, a vsep column or the pane ----

#[test]
fn row_0_is_the_topbar_left_part_divider_and_outside() {
    let mut l = Layout::new();
    l.insert(id("supervisor-1"), PaneType::Supervisor);
    l.insert(id("worker-1"), PaneType::Worker);
    l.set_sidebar(34);
    l.set_topbar(15);
    for (x, why) in [(5, "inside the left part"), (32, "the last left column")] {
        assert_eq!(
            l.hit_test(240, 60, x, 0),
            Hit::Sidebar,
            "sidebar 34 topbar: ({x},0) is {why} of the topbar left part, not a tab or a pane"
        );
    }
    for (x, why) in [
        (33, "the sidebar's gap column sb - 1"),
        (34, "the divider column sb"),
    ] {
        assert_eq!(
            l.hit_test(240, 60, x, 0),
            Hit::SidebarEdge,
            "sidebar 34 topbar: ({x},0) is {why} and stays draggable on row 0"
        );
    }
    for (x, why) in [
        (35, "the first column after the divider"),
        (136, "the vsep column"),
        (200, "the right part"),
    ] {
        assert_eq!(
            l.hit_test(240, 60, x, 0),
            Hit::Outside,
            "sidebar 34 topbar: ({x},0) is {why}, row 0 is never a tab or a pane"
        );
    }
    assert_eq!(
        l.hit_test(240, 60, 136, 30),
        Hit::VSep,
        "the vsep column is still the vsep below the topbar"
    );
    let mut hidden = Layout::new();
    hidden.insert(id("supervisor-1"), PaneType::Supervisor);
    hidden.insert(id("worker-1"), PaneType::Worker);
    hidden.set_sidebar(0);
    hidden.set_topbar(15);
    for (x, why) in [(1, "inside the hidden toolbar"), (14, "the last toolbar column")] {
        assert_eq!(
            hidden.hit_test(240, 60, x, 0),
            Hit::Sidebar,
            "hidden sidebar topbar: ({x},0) is {why}, the toolbar stays clickable at width 0"
        );
    }
    assert_eq!(
        hidden.hit_test(240, 60, 15, 0),
        Hit::Outside,
        "hidden sidebar topbar: (15,0) is past the 15-column toolbar"
    );
    assert_ne!(
        hidden.hit_test(240, 60, 0, 10),
        Hit::Sidebar,
        "hidden sidebar: (0,10) is the content's left border, not the sidebar"
    );
}

#[test]
fn tab_labels_and_the_hsep_drag_follow_the_topbar() {
    let mut l = full_run();
    l.set_sidebar(34);
    l.set_topbar(15);
    // A26 rev7 (CURRENT_REQUIREMENTS rev 10/11): the topbar row and one blank separation row form the header band,
    // so the tab label row is the band's last row (2), not the row right under the topbar.
    assert_eq!(l.header_rows(), TOPBAR_ROWS);
    for g in [Group::Supervisor, Group::Workers] {
        let tabs: Vec<fleetmux_layout::Rect> = l
            .tab_rects_titled(240, 60, ascii_cols)
            .into_iter()
            .filter(|(tg, _, _)| *tg == g)
            .map(|(_, _, r)| r)
            .collect();
        assert!(!tabs.is_empty(), "{g:?}: the group has tabs");
        for r in tabs {
            assert_eq!(
                r.y, 2,
                "{g:?}: the tab label row is row 2, the header band's last row: {r:?}"
            );
        }
    }
    // The separation row is no tab, separator or pane.
    assert_eq!(l.hit_test(240, 60, 5, 1), Hit::Outside, "row 1 is the blank header row");
    assert_eq!(l.hit_test(240, 60, 5, 0), Hit::Sidebar, "row 0 is the topbar's toolbar");
    assert_eq!(
        l.ratio_from_y_in(60, 37),
        ratio_from_y(57, 35),
        "with the two-row header band the hsep drag at row 37 is content row 35 of 57"
    );
    let plain = Layout::new();
    assert_eq!(
        plain.ratio_from_y_in(60, 35),
        ratio_from_y(59, 35),
        "without a header ratio_from_y_in is the old ratio_from_y(rows - 1, y)"
    );
}

/// A26 rev7 (CURRENT_REQUIREMENTS rev 10 `macos_header_tab_separation` / rev 11
/// `workspace_tabs_align_first_fleet`): the workspace's topmost tab row and the sidebar's first card start on the
/// same row — the header band's last row — with one blank row of separation under the header on both platforms.
#[test]
fn header_band_puts_the_tab_row_and_the_first_card_on_one_line() {
    // macOS: the in-grid topbar (row 0) plus its separation row; the tab row is row 2.
    let mut mac = full_run();
    mac.set_sidebar(34);
    mac.set_topbar(15);
    assert_eq!(mac.header(), 0);
    assert_eq!(mac.header_rows(), 2);
    let sup = mac.rects(240, 60).supervisor;
    assert_eq!(sup.y, 3, "the supervisor pane starts one row under its tab row");
    let tab = mac
        .tab_rects(240, 60)
        .into_iter()
        .find(|(g, i, _)| *g == Group::Supervisor && *i == 0)
        .map(|(_, _, r)| r)
        .expect("the supervisor has a tab");
    assert_eq!(tab.y, 2, "the topmost workspace tab row is the header band's last row");
    assert_eq!(mac.hit_test(240, 60, tab.x, 2), Hit::Tab { group: Group::Supervisor, index: 0 });
    // The tab row (2) is where the sidebar's first card starts (render::sidebar CARD row = header_rows), and row 1
    // stays blank between the topbar and that line.
    assert_eq!(mac.hit_test(240, 60, 3, 1), Hit::Outside);
    // Linux: the native headerbar is outside the grid, so the band is its one blank separation row and the tab row
    // is row 1.
    let mut linux = full_run();
    linux.set_sidebar(34);
    linux.set_mirror(true);
    linux.set_header(HEADER_GAP);
    linux.set_topbar(15);
    assert_eq!(
        linux.header_rows(),
        HEADER_GAP + TOPBAR_ROWS,
        "the two add up: a grid topbar on top of the native headerbar's gap (the Linux window never sets one, \
         `sidebar::topbar_cols` is 0 while mirrored)"
    );
    let mut native = full_run();
    native.set_sidebar(34);
    native.set_mirror(true);
    native.set_header(HEADER_GAP);
    assert_eq!(native.header_rows(), HEADER_GAP);
    assert_eq!(native.rects(240, 60).supervisor.y, 2, "tab row 1, pane from 2");
    assert_eq!(native.hit_test(240, 60, 5, 0), Hit::Outside, "row 0 is the blank row under the headerbar");
    assert_eq!(native.ratio_from_y_in(60, 36), ratio_from_y(58, 35));
}

// ---- A26 rev3 (user requirement A42-LAYOUT-LINUX-MIRROR): hit testing, the separator drag and the topbar row
// follow the mirrored rects, so a click on what is drawn lands on the same group.

#[test]
fn mirrored_hit_test_follows_the_mirrored_rects() {
    let mut l = full_run();
    l.set_sidebar(34);
    l.set_mirror(true);
    // Workers left (1..=102), the separator on 103, the stack 104..=204, the sidebar 206..=239.
    assert_eq!(l.hit_test(240, 60, 5, 10), Hit::Pane(Group::Workers));
    assert_eq!(l.hit_test(240, 60, 150, 10), Hit::Pane(Group::Supervisor));
    assert_eq!(l.hit_test(240, 60, 150, 45), Hit::Pane(Group::Lower));
    assert_eq!(l.hit_test(240, 60, 103, 30), Hit::VSep);
    assert_eq!(l.hit_test(240, 60, 200, 35), Hit::HSep);
    assert_eq!(l.hit_test(240, 60, 220, 10), Hit::Sidebar);
    assert_eq!(l.hit_test(240, 60, 205, 10), Hit::SidebarEdge);
    assert_eq!(l.hit_test(240, 60, 206, 10), Hit::SidebarEdge);
    assert_eq!(l.hit_test(240, 60, 10, 59), Hit::Status);
    // The tab labels are inside the mirrored group rectangles.
    for (g, _, r) in l.tab_rects_titled(240, 60, ascii_cols) {
        let inside = match g {
            Group::Supervisor => (104..=204).contains(&r.x),
            Group::Lower => (104..=204).contains(&r.x),
            Group::Workers => (1..=102).contains(&r.x),
        };
        assert!(inside, "{g:?}: tab label at {r:?} is inside its mirrored group");
    }
}

#[test]
fn mirrored_ratio_from_x_in_is_the_mirror_of_the_separator() {
    let mut l = full_run();
    l.set_sidebar(34);
    l.set_mirror(true);
    // 206 content columns: the separator is on column 103 for 50%.
    assert_eq!(l.ratio_from_x_in(240, 103), 50, "the mirrored separator");
    assert_eq!(
        l.ratio_from_x_in(240, 205),
        10,
        "the content's right border is the narrowest stack (10%)"
    );
    assert_eq!(
        l.ratio_from_x_in(240, 1),
        90,
        "the content's left border is the widest stack (90%)"
    );
}

#[test]
fn mirrored_topbar_row_hits_the_toolbar_and_leaves_the_rest_outside() {
    let mut l = full_run();
    l.set_sidebar(34);
    l.set_topbar(15);
    l.set_mirror(true);
    for x in 0..15 {
        assert_eq!(
            l.hit_test(240, 60, x, 0),
            Hit::Sidebar,
            "the mirrored topbar keeps the toolbar on the left: ({x},0)"
        );
    }
    assert_eq!(
        l.hit_test(240, 60, 15, 0),
        Hit::Outside,
        "past the toolbar row 0 is the header, not the sidebar band"
    );
    assert_eq!(
        l.hit_test(240, 60, 230, 0),
        Hit::Outside,
        "the right-hand sidebar band starts on row 1, row 0 is the header"
    );
}

/// A26 rev7 (CURRENT_REQUIREMENTS rev 10 `macos_header_tab_separation` / rev 11
/// `workspace_tabs_align_first_fleet`): the workspace's topmost tab row and the sidebar's first card start on the
/// same row — the header band's last row — with one blank row of separation under the header on both platforms.
#[test]
fn header_band_preserves_mac_and_aligns_linux_cards_and_tabs() {
    let mut mac = full_run();
    mac.set_sidebar(34);
    mac.set_topbar(15);
    assert_eq!(mac.header_rows(), TOPBAR_ROWS);
    assert_eq!(mac.rects(240, 60).supervisor.y, TOPBAR_ROWS + 1);
    let tab = mac.tab_rects(240, 60).into_iter()
        .find(|(g, i, _)| *g == Group::Supervisor && *i == 0).unwrap().2;
    assert_eq!(tab.y, TOPBAR_ROWS, "accepted base's macOS tabs stay on row 2");
    assert_eq!(mac.hit_test(240, 60, tab.x, TOPBAR_ROWS), Hit::Tab { group: Group::Supervisor, index: 0 });
    let mut linux = full_run();
    linux.set_native_header(true);
    linux.set_sidebar_left(true);
    linux.set_sidebar(34);
    linux.set_header(HEADER_GAP);
    assert_eq!(linux.header_rows(), HEADER_GAP);
    assert_eq!(linux.rects(240, 60).supervisor.y, 2);
    assert_eq!(linux.hit_test(240, 60, 5, 0), Hit::Outside);
    assert_eq!(linux.ratio_from_y_in(60, 36), ratio_from_y(58, 35));
}
