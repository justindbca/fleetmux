//! Mouse handling on the outer screen (brief-m03 §3.4 `tests/mouse.rs`, PROTOCOL.md §11, plan.md §5
//! mouse-input). M1: `#[ignore = "M1: mouse-input"]` until B14. The viewer sends SGR reports (1-based on the
//! wire, built from the 0-based 240x60 contract cells in `common`).
use std::os::unix::net::UnixStream;

mod common;
use common::*;

const T: Duration = Duration::from_secs(10);

/// Clicks the first cell of `title` in the tab row.
fn click_title(v: &mut Viewer, title: &str) {
    let (row, col) = v.wait_find(title, T);
    v.input(&sgr_click(col, row, 0));
}

#[test]
#[ignore = "M1: mouse-input"]
fn click_tab_selects() {
    let srv = Server::start();
    let sup = sh(&srv, "fx-supervisor-1", "supervisor", "exec sleep 1000");
    let adv = sh(&srv, "fx-advisor-1", "advisor", "exec sleep 1000");
    let w1 = sh(&srv, "fx-worker-1", "worker", "exec sleep 1000");
    let w2 = sh(&srv, "fx-worker-2", "worker", "exec sleep 1000");
    let l = srv.layout();
    assert_eq!((l["selected"]["supervisor"].clone(), l["selected"]["workers"].clone()), (json!(sup), json!(w1)));
    let mut v = Viewer::attach(&srv, W, H);

    click_title(&mut v, "fx-worker-2");
    srv.wait_layout("worker-2 selected and focused", T, |l| {
        l["selected"]["workers"] == json!(w2) && l["focus"] == json!("workers")
    });
    click_title(&mut v, "fx-advisor-1");
    srv.wait_layout("advisor selected and focused", T, |l| {
        l["selected"]["supervisor"] == json!(adv) && l["focus"] == json!("supervisor")
    });
}

#[test]
#[ignore = "M1: mouse-input"]
fn click_pane_focuses() {
    let srv = Server::start();
    sh(&srv, "fx-supervisor-1", "supervisor", "exec sleep 1000");
    sh(&srv, "fx-worker-1", "worker", "exec sleep 1000");
    sh(&srv, "history", "history", "exec sleep 1000");
    assert_eq!(srv.layout()["focus"], json!("supervisor"));
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("fx-worker-1", T);

    v.input(&sgr_click(IN_WORKERS.0, IN_WORKERS.1, 0));
    srv.wait_layout("focus on workers", T, |l| l["focus"] == json!("workers"));
    v.input(&sgr_click(IN_LOWER.0, IN_LOWER.1, 0));
    srv.wait_layout("focus on lower", T, |l| l["focus"] == json!("lower"));
    v.input(&sgr_click(IN_SUPERVISOR.0, IN_SUPERVISOR.1, 0));
    srv.wait_layout("focus on supervisor", T, |l| l["focus"] == json!("supervisor"));
}

#[test]
#[ignore = "M1: mouse-input"]
fn drag_vertical_separator_changes_h_ratio() {
    let srv = Server::start();
    sh(&srv, "fx-supervisor-1", "supervisor", "exec sleep 1000");
    sh(&srv, "fx-worker-1", "worker", "exec sleep 1000");
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("fx-worker-1", T);

    // Wire x = 120 → 168 (0-based 119 → 167) at 240 columns.
    v.input(&sgr_drag((VSEP_COL, 30), (167, 30), 0));
    srv.wait_layout("h_ratio 70", T, |l| l["h_ratio"] == json!(70));
    // Written on release (PROTOCOL.md §11).
    wait_until("layout.json to record h_ratio 70", T, || {
        let text = std::fs::read_to_string(srv.runtime.join("layout.json")).ok()?;
        let saved: Value = serde_json::from_str(&text).ok()?;
        (saved["h_ratio"] == json!(70)).then_some(())
    });
}

#[test]
#[ignore = "M1: mouse-input"]
fn drag_horizontal_separator_changes_v_ratio() {
    let srv = Server::start();
    sh(&srv, "fx-supervisor-1", "supervisor", "exec sleep 1000");
    sh(&srv, "history", "history", "exec sleep 1000");
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("history", T);

    // The left column's separator (0-based row 35 at the default 60%) dragged up to row 23 (≈ 39%).
    v.input(&sgr_drag((50, HSEP_ROW), (50, 23), 0));
    let l = srv.wait_layout("v_ratio to move off 60", T, |l| l["v_ratio"] != json!(60));
    let v_ratio = l["v_ratio"].as_u64().unwrap_or_else(|| panic!("{l}"));
    assert!((33..=45).contains(&v_ratio), "row 23 of 59 should give about 39%: {l}");
    assert_eq!(l["h_ratio"], json!(50), "a horizontal drag moved the vertical split: {l}");
    wait_until("layout.json to record the new v_ratio", T, || {
        let text = std::fs::read_to_string(srv.runtime.join("layout.json")).ok()?;
        let saved: Value = serde_json::from_str(&text).ok()?;
        (saved["v_ratio"] == json!(v_ratio)).then_some(())
    });
}

#[test]
#[ignore = "M1: mouse-input"]
fn wheel_scrolls_scrollback_when_inner_has_no_mouse() {
    let srv = Server::start();
    // `--lines` keeps the probe on the main screen with LINE 0..99 in scrollback above its rows.
    let (_, log) = start_probe(&srv, &["--no-mouse", "--lines", "100"]);
    let mut v = Viewer::attach(&srv, W, H);
    let before = v.wait_text("ROW 9", T);
    assert!(!before.contains("LINE "), "scrollback already visible:\n{before}");

    for _ in 0..3 {
        v.input(&sgr(WHEEL_UP, IN_WORKERS.0, IN_WORKERS.1, true));
    }
    let after = v.wait_text("LINE ", T);
    assert!(after.contains("LINE 99"), "3 wheel steps (9 rows) should show the newest scrollback line:\n{after}");
    std::thread::sleep(Duration::from_millis(300));
    let forwarded: Vec<Value> = probe_events(&log).into_iter().filter(|e| ev_is(e, "mouse")).collect();
    assert!(forwarded.is_empty(), "wheel forwarded to a program without mouse reporting: {forwarded:?}");
}

#[test]
#[ignore = "M1: mouse-input"]
fn wheel_forwarded_when_inner_reports_mouse() {
    let srv = Server::start();
    let (id, log) = start_probe(&srv, &[]);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("ROW 9", T);
    let (cols, rows) = pane_size(&srv, &id);

    let (x, y) = IN_WORKERS;
    v.input(&sgr(WHEEL_UP, x, y, true));
    v.input(&sgr(WHEEL_UP, x + 3, y + 2, true));
    let wheels = wait_events(&log, "2 wheel reports", 2, T, |e| {
        ev_is(e, "mouse") && e["button"].as_u64() == Some(64)
    });
    let (a, b) = (&wheels[0], &wheels[1]);
    for e in [a, b] {
        assert_eq!(e["encoding"], json!("sgr"), "{e}");
        let (c, r) = (e["col"].as_u64().unwrap(), e["row"].as_u64().unwrap());
        assert!((1..=cols).contains(&c) && (1..=rows).contains(&r), "outside the {cols}x{rows} pane: {e}");
    }
    let (ac, ar) = (a["col"].as_u64().unwrap(), a["row"].as_u64().unwrap());
    assert!(ac <= (x as u64) - (VSEP_COL as u64) && ar <= y as u64, "not translated to the pane origin: {a}");
    assert_eq!(
        (b["col"].as_u64().unwrap() - ac, b["row"].as_u64().unwrap() - ar),
        (3, 2),
        "offsets not preserved: {a} {b}"
    );
}

#[test]
#[ignore = "M1: mouse-input"]
fn shift_drag_passes_through() {
    let srv = Server::start();
    let (_, log) = start_probe(&srv, &[]);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("ROW 9", T);

    // Shift + drag on the separator and Shift + click inside the pane: neither handled nor forwarded.
    v.input(&sgr_drag((VSEP_COL, 30), (167, 30), MOD_SHIFT));
    v.input(&sgr_click(IN_WORKERS.0, IN_WORKERS.1, MOD_SHIFT));
    std::thread::sleep(Duration::from_millis(500));
    // Control: an unshifted click right after is forwarded, so the shifted ones were not merely late.
    v.input(&sgr_click(IN_WORKERS.0, IN_WORKERS.1, 0));
    wait_events(&log, "the unshifted click (press + release)", 2, T, |e| ev_is(e, "mouse"));
    std::thread::sleep(Duration::from_millis(300));
    let mouse: Vec<Value> = probe_events(&log).into_iter().filter(|e| ev_is(e, "mouse")).collect();
    assert_eq!(mouse.len(), 2, "shifted events reached the program: {mouse:?}");
    assert_eq!(srv.layout()["h_ratio"], json!(50), "a shifted drag moved the split");
}

/// Real-title coverage for the tab geometry (supervisor's layout ruling, stage A: `hit.rs` stands ids in for
/// titles, so it only proves the geometry). Four supervisor-group panes carry 30-character **titles** (the
/// pane ids stay whatever the server hands out). The expected label rectangles come from
/// `fleetmux_layout::Layout::tab_rects` on a layout holding the server's panes in the server's tab order,
/// the geometry `draw` and `hit_test` share.
#[test]
#[ignore = "M1: mouse-input"]
fn long_titles_render_and_hit_same_tab_geometry() {
    use fleetmux_layout::{Group, Hit, Layout, PaneType};
    const MIN_VISIBLE: usize = 5;

    let srv = Server::start();
    let titles: Vec<String> = ["alpha", "bravo", "charlie", "delta"]
        .iter()
        .enumerate()
        .map(|(n, w)| format!("{:-<30}", format!("{w}-real-tab-title-{n}")))
        .collect();
    assert!(titles.iter().all(|t| t.chars().count() == 30), "{titles:?}");
    let mut ids = vec![sh(&srv, &titles[0], "supervisor", "exec sleep 1000")];
    for t in &titles[1..] {
        ids.push(sh(&srv, t, "advisor", "exec sleep 1000"));
    }
    let worker = sh(&srv, "fx-worker-1", "worker", "exec sleep 1000");
    for (id, t) in ids.iter().zip(&titles) {
        let p = srv.pane(id);
        assert_eq!(p["title"], json!(t), "the title, not the id, carries the long text: {p}");
        assert_ne!(id, t, "pane id used as the title");
    }
    let order = srv.group_order("supervisor");
    assert_eq!(order.len(), 4, "supervisor group: {order:?}");
    let title_of = |id: &str| titles[ids.iter().position(|x| x == id).unwrap()].clone();

    // Same geometry as the server: the server's panes, in the server's tab order, with their types.
    let mut l = Layout::new();
    for (k, id) in order.iter().enumerate() {
        l.insert(id.clone(), if k == 0 { PaneType::Supervisor } else { PaneType::Advisor });
    }
    assert_eq!(l.group(Group::Supervisor), order.as_slice());
    let tabs: Vec<_> = l
        .tab_rects_titled(W, H, |id| title_of(id).chars().count() as u16)
        .into_iter()
        .filter(|(g, _, _)| *g == Group::Supervisor)
        .collect();
    assert_eq!(tabs.iter().map(|(_, i, _)| *i).collect::<Vec<_>>(), vec![0, 1, 2, 3]);

    let mut v = Viewer::attach(&srv, W, H);
    let prefixes: Vec<String> = order.iter().map(|id| title_of(id).chars().take(MIN_VISIBLE).collect()).collect();
    wait_until("all four title prefixes on the tab row", T, || {
        let top = v.row_text(0);
        prefixes.iter().all(|p| top.contains(p.as_str())).then_some(())
    });
    let screen = v.text();
    let row: Vec<char> = (0..W)
        .map(|c| v.cell(0, c))
        .map(|c| if c.width == 0 || c.ch == '\0' { ' ' } else { c.ch })
        .collect();
    for t in &titles {
        assert!(!screen.contains(t.as_str()), "a 30-character title was drawn untruncated: {t}\n{screen}");
    }

    // Each label: a visible title prefix that starts inside its own tab rectangle and stops at its edge.
    let mut starts = Vec::new();
    for (_, i, r) in &tabs {
        assert!(r.w >= 1 && r.w <= 28 && r.h == 1, "tab {i}: {r:?}");
        let title: Vec<char> = title_of(&order[*i]).chars().collect();
        let (x0, x1) = (r.x as usize, (r.x + r.w) as usize);
        let start = (x0..x1)
            .find(|&c| row[c..].starts_with(&title[..MIN_VISIBLE]))
            .unwrap_or_else(|| panic!("tab {i} {r:?} does not show {:?}: {:?}", &prefixes[*i], &row[x0..x1]));
        let shown = title.iter().zip(&row[start..]).take_while(|(a, b)| a == b).count();
        assert!(start + shown <= x1, "tab {i}: title runs past its rectangle {r:?} ({shown} chars from {start})");
        assert!(shown < title.len(), "tab {i}: title not truncated");
        assert_eq!(
            l.hit_test_titled(W, H, start as u16, r.y, |id| title_of(id).chars().count() as u16),
            Hit::Tab { group: Group::Supervisor, index: *i }
        );
        starts.push((start as u16, r.y));
    }
    assert!(starts.windows(2).all(|p| p[0].0 < p[1].0), "labels out of tab order: {starts:?}");

    // Clicking the first cell of each visible title selects that pane and focuses the supervisor group.
    for (i, (x, y)) in starts.iter().enumerate().rev() {
        srv.ok("pane.focus", json!({"pane": worker}));
        srv.wait_layout("focus on workers", T, |l| l["focus"] == json!("workers"));
        v.input(&sgr_click(*x, *y, 0));
        srv.wait_layout(&format!("tab {i} ({}) selected", order[i]), T, |l| {
            l["selected"]["supervisor"] == json!(order[i]) && l["focus"] == json!("supervisor")
        });
    }
}

// ---- fix-m1-tabs (supervisor ruling [M48], API B): clicks hit the drawn tab blocks only. ----

/// Real clicks on the workers tab bar: a block's padding columns select its tab; the one-column gap between two
/// blocks and the bar after the last block select nothing (the operator's real-mouse P2: a click right of
/// `fx-ci-1` selected `fx-ci-1`).
#[test]
fn tab_clicks_hit_blocks_not_gaps_or_bar_tail() {
    let srv = Server::start();
    let sup = sh(&srv, "fx-supervisor-1", "supervisor", "exec sleep 1000");
    let ctl = sh(&srv, "fx-ctl-1", "worker", "exec sleep 1000");
    let py = sh(&srv, "fx-py-1", "worker", "exec sleep 1000");
    let ci = sh(&srv, "fx-ci-1", "worker", "exec sleep 1000");
    srv.ok("pane.focus", json!({"pane": sup}));
    srv.wait_layout("supervisor focused, fx-ctl-1 selected", T, |l| {
        l["focus"] == json!("supervisor") && l["selected"]["workers"] == json!(ctl)
    });
    let mut v = Viewer::attach(&srv, W, H);
    let (row, py_col) = v.wait_find("fx-py-1", T);
    let (ci_row, ci_col) = v.wait_find("fx-ci-1", T);
    assert_eq!((row, ci_row), (0, 0), "workers tabs are on the top bar");
    let len = "fx-ci-1".len() as u16;
    assert!(
        v.cell(row, py_col - 1).ch == ' ' && v.cell(row, py_col - 2).ch != '•',
        "fx-py-1 is marked unseen; its left padding would not be at col - 1"
    );

    // Not tabs: the bar right after fx-ci-1's block (title, one padding column, then bar), further out on the
    // bar tail, and the gap column before fx-ci-1's block.
    for col in [ci_col + len + 1, ci_col + len + 6, ci_col - 2] {
        v.input(&sgr_click(col, row, 0));
    }
    // A click in the workers pane (focus only) after them: once it is applied, the clicks above were too.
    v.input(&sgr_click(200, 20, 0));
    srv.wait_layout("workers focused by the pane click", T, |l| l["focus"] == json!("workers"));
    assert_eq!(
        srv.layout()["selected"]["workers"],
        json!(ctl),
        "a click on the bar tail or a gap selected a tab"
    );

    // Padding columns belong to their block.
    v.input(&sgr_click(ci_col + len, row, 0));
    srv.wait_layout("fx-ci-1 selected by its right padding", T, |l| {
        l["selected"]["workers"] == json!(ci)
    });
    v.input(&sgr_click(py_col - 1, row, 0));
    srv.wait_layout("fx-py-1 selected by its left padding", T, |l| {
        l["selected"]["workers"] == json!(py)
    });
}

// -------------------------------- fleet sidebar input (S1-④, plan §4.4) -----------------------------

/// The kv `sidebar` value the fleet daemon pushes (plan §4.5): one fleet, the current one.
const SIDEBAR_ONE: &str = r#"{"v":1,"current":"fleet-20261029-231820-ab12cd34","fleets":[{"id":"fleet-20261029-231820-ab12cd34","title":"fleetmux-demo","state":"running","line1":"运行中 · 监工+2 worker（1忙）","line2":"~/.cmux-fleet-fmx-demo"}]}"#;

/// Publish the sidebar card through kv (the daemon's own channel, plan §4.3.3).
fn set_sidebar(srv: &Server) {
    srv.ok("kv.set", json!({ "key": "sidebar", "value": SIDEBAR_ONE }));
}

/// A supervisor and a worker, each printing a marker and then idling.
fn sidebar_page(srv: &Server) -> [String; 2] {
    [
        sh(srv, "fx-supervisor-1", "supervisor", "echo SUP-BODY; exec sleep 1000"),
        sh(srv, "fx-worker-1", "worker", "echo W1-BODY; exec sleep 1000"),
    ]
}

/// What tells a box-drawing border cell from a pane character.
fn is_box(ch: char) -> bool {
    ('\u{2500}'..='\u{257f}').contains(&ch)
}

/// `^b s` hides the sidebar into the one-column rail and brings it back; the status line says which key does
/// what (plan §2.3 / §2.4).
#[test]
fn prefix_s_hides_and_shows_the_sidebar() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);

    v.input(&prefixed(b"s"));
    let screen = v.wait_no_text("~/.cmux-fleet-fmx-demo", T);
    let rail = v.cell(0, 1);
    assert_eq!(
        rail.ch, '◫',
        "^b s did not leave the rail toggle at (0,1): {rail:?}\n{screen}"
    );
    // A26 rev7: rows 0 and 1 are the header band (the topbar and its blank separation row), so the groups' left
    // border is the first content row, 2.
    let left = v.cell(2, 0);
    assert!(
        is_box(left.ch),
        "(2,0) is not the groups' left border while hidden: {left:?}\n{screen}"
    );
    let status = v.status_line();
    assert!(
        status.contains("^b s 显示侧栏"),
        "the hidden status line lacks the show hint: {status:?}\n{screen}"
    );

    v.input(&prefixed(b"s"));
    let screen = v.wait_text("~/.cmux-fleet-fmx-demo", T);
    let left = v.cell(2, 34);
    assert!(
        is_box(left.ch),
        "(2,34) is not the groups' left border after showing again: {left:?}\n{screen}"
    );
}

/// Without kv `sidebar` there is nothing to toggle: `^b s` must not touch the layout (plan §2.4).
#[test]
fn prefix_s_without_the_kv_key_changes_nothing() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    let before = srv.layout();
    v.input(&prefixed(b"s"));
    v.wait_quiet(Duration::from_millis(300), T);
    let after = srv.layout();
    assert_eq!(
        after, before,
        "^b s without kv sidebar changed the layout: {after} (before {before})"
    );
    let screen = v.text();
    let c = v.cell(0, 0);
    assert!(
        is_box(c.ch),
        "(0,0) is not the groups' frame after ^b s without kv sidebar: {c:?}\n{screen}"
    );
}

/// A left click on the `◫` toggle (row 0, columns 0–2) hides; a left click on the hidden toolbar's toggle at
/// (0,1) shows.
#[test]
fn clicking_the_chevrons_toggles_the_sidebar() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);

    v.input(&sgr_click(1, 0, 0));
    let screen = v.wait_no_text("~/.cmux-fleet-fmx-demo", T);
    let rail = v.cell(0, 1);
    assert_eq!(
        rail.ch, '◫',
        "clicking ◫ at (1,0) did not hide the sidebar: {rail:?}\n{screen}"
    );

    v.input(&sgr_click(1, 0, 0));
    let screen = v.wait_text("~/.cmux-fleet-fmx-demo", T);
    assert!(
        screen.contains("~/.cmux-fleet-fmx-demo"),
        "clicking the hidden toolbar's ◫ at (1,0) did not show the sidebar:\n{screen}"
    );
}

/// S1 lists one fleet only: a left click on a card changes nothing (S3 makes it switch fleets).
#[test]
fn clicking_a_card_changes_nothing() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    let before = srv.layout();
    v.input(&sgr_click(5, 2, 0));
    v.wait_quiet(Duration::from_millis(300), T);
    let after = srv.layout();
    assert_eq!(
        after, before,
        "clicking a card changed the layout: {after} (before {before})"
    );
    let screen = v.text();
    assert!(
        screen.contains("fleetmux-demo"),
        "clicking a card hid the sidebar:\n{screen}"
    );
    let card = v.cell(2, 5);
    assert_eq!(
        card.bg,
        Color::Indexed(4),
        "the selected card lost its blue bar at (2,5): {card:?}\n{screen}"
    );
}

/// The vertical separator sits at column 136 with the sidebar: dragging it must keep `h_ratio` 50, i.e. the
/// ratio is computed over the content width, not the whole screen (plan §4.4.2, `ratio_from_x_in`).
#[test]
fn dragging_the_vsep_with_the_sidebar_keeps_the_ratio() {
    let srv = Server::start();
    let p = sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    wait_until("the supervisor PTY shrinks to 101 columns", T, || {
        (pane_size(&srv, &p[0]).0 == 101).then_some(())
    });

    v.input(&sgr_drag((136, 30), (136, 30), 0));
    v.wait_quiet(Duration::from_millis(300), T);
    let l = srv.layout();
    assert_eq!(
        l["h_ratio"],
        json!(50),
        "dragging the vsep at column 136 with the sidebar changed h_ratio: {l}"
    );
    assert_eq!(
        l["v_ratio"],
        json!(60),
        "dragging the vsep changed v_ratio: {l}"
    );
}

// ------------------------------ fleet sidebar toolbar (S1b-③, plan §11.1) ----------------------------

/// The `+` opens a bash (S1c, plan §14.2 / §14.5-1): a server-owned `type "shell"` pane that lands as a tab at the end of
/// the root page's `workers` group (user 2026-11-09: every new bash is mapped into the main fleet's worker area), the
/// `bash-1` card after the fleet card, and no S1b notice any more. The fleet's own panes keep their places.
#[test]
fn clicking_plus_opens_a_bash_page() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    let before = srv.panes_all().len();
    // The fleet's own panes and where they sit (other fields, e.g. `unseen` and `focused`, follow the selection).
    let places = |srv: &Server| -> Vec<(Value, Value, Value)> {
        srv.panes()
            .into_iter()
            .filter(|p| p["type"] != json!("shell"))
            .map(|p| (p["pane"].clone(), p["group"].clone(), p["index"].clone()))
            .collect()
    };
    let root_before = places(&srv);

    v.input(&sgr_click(4, 0, 0));
    let id = wait_shell_pane(&srv);
    let panes = srv.panes_all();
    assert_eq!(
        panes.len(),
        before + 1,
        "clicking + spawned more than the one bash pane: {panes:?}"
    );
    in_root_workers(&srv, &id);
    assert_eq!(places(&srv), root_before, "the bash tab moved the fleet's own (root page) panes");
    let screen = v.wait_text("bash-1", T);
    let status = v.status_line();
    assert!(
        !status.contains("新建 bash：S1c 开放"),
        "the S1b notice is still on the status line after + opened a page: {status:?}\n{screen}"
    );
}

/// With one fleet both arrows are grey (`DarkGray`) and do nothing: no notice, no layout change (plan §11.1).
#[test]
fn grey_arrows_do_nothing() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    let before = srv.layout();
    for (col, row) in [(10u16, 0u16), (13, 0)] {
        v.input(&sgr_click(col, row, 0));
    }
    v.wait_quiet(Duration::from_millis(300), T);
    let after = srv.layout();
    let screen = v.text();
    assert_eq!(
        after, before,
        "clicking a grey arrow changed the layout: {after} (before {before})"
    );
    let status = v.status_line();
    assert!(
        !status.contains("切换 fleet"),
        "a grey arrow showed the switch notice: {status:?}\n{screen}"
    );
    let prev = v.cell(0, 10);
    assert_eq!(
        prev.fg,
        Color::Indexed(8),
        "the ← arrow without a previous fleet is not DarkGray: {prev:?}\n{screen}"
    );
    let next = v.cell(0, 13);
    assert_eq!(
        next.fg,
        Color::Indexed(8),
        "the → arrow without a next fleet is not DarkGray: {next:?}\n{screen}"
    );
}

// ------------------------------ fleet sidebar close box (S1b-④, plan §2.5 / §11.1) -------------------------

/// Three cards, `current` the middle one: both arrows have a neighbour (plan §11.1).
const SIDEBAR_THREE: &str = r#"{"v":1,"current":"f2","fleets":[{"id":"f1","title":"root-fleet","state":"running","line1":"运行中 · 监工+2 worker（1忙）","line2":"~/.cmux-fleet-fmx-demo"},{"id":"f2","title":"fleetmux-demo","state":"running","line1":"运行中 · 监工+1 worker（0忙）","line2":"由 worker-1 委派"},{"id":"f3","title":"child-fleet","state":"reclaimed","line1":"已收回 · 监工+0 worker（0忙）","line2":"只读，可看记录"}]}"#;

/// The `stop_request` value the server holds; `None` while nothing was requested.
fn kv_stop(srv: &Server) -> Option<String> {
    let got = srv.ok("kv.get", json!({ "key": "stop_request" }));
    got["value"].as_str().map(str::to_string)
}

/// The card's `×` opens the box; `y` writes kv `stop_request` `{"v":1,"state":"requested","at":<unix s>}`, the box
/// closes, the status line keeps the state and the card no longer draws `×` (plan §11.1 / §11.7-6).
#[test]
fn clicking_the_card_close_opens_the_dialog_and_y_requests_the_stop() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    let close = v.cell(2, 31);
    assert_eq!(
        close.ch, '×',
        "the card's close button is not at (2,31): {close:?}"
    );

    v.input(&sgr_click(31, 2, 0));
    let screen = v.wait_text("关闭 fleet", T);
    assert!(
        screen.contains("关闭整个 fleet「fleetmux-demo」？"),
        "the box does not name the fleet:\n{screen}"
    );
    assert!(
        screen.contains("按 y 确认，其他任意键取消"),
        "the box does not say which keys confirm and cancel:\n{screen}"
    );

    v.input(b"y");
    let status = v.wait_status("已请求关闭", T, |l| l.contains("已请求关闭，等 daemon"));
    assert!(
        status.contains("已请求关闭，等 daemon"),
        "the status line does not say the stop was requested: {status:?}"
    );
    let text = kv_stop(&srv).unwrap_or_else(|| panic!("`y` did not write kv stop_request"));
    let req: Value = serde_json::from_str(&text)
        .unwrap_or_else(|e| panic!("kv stop_request is not JSON ({e}): {text}"));
    assert_eq!(req["v"], json!(1), "stop_request.v: {text}");
    assert_eq!(req["state"], json!("requested"), "stop_request.state: {text}");
    let at = req["at"].as_u64().unwrap_or(0);
    assert!(
        at > 1_700_000_000,
        "stop_request.at is not a unix second: {text}"
    );
    let screen = v.wait_no_text("关闭 fleet", T);
    v.wait_quiet(Duration::from_millis(300), T);
    let close = v.cell(2, 31);
    assert_ne!(
        close.ch, '×',
        "the card still draws `×` after the stop was requested: {close:?}\n{}",
        v.text()
    );
    assert!(
        screen.contains("已请求关闭"),
        "the status line lost the stop state:\n{screen}"
    );
}

/// Only `y` / `Y` confirms: any other key, a prefix key or a press outside `[ 关闭 fleet ]` cancels and writes
/// nothing (plan §2.5 / §11.1).
#[test]
fn any_other_key_or_a_click_outside_cancels_the_dialog() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);

    // A key that is not `y`.
    v.input(&sgr_click(31, 2, 0));
    v.wait_text("关闭 fleet", T);
    v.input(b"n");
    let screen = v.wait_no_text("关闭 fleet", T);
    assert_eq!(
        kv_stop(&srv),
        None,
        "`n` wrote kv stop_request: {:?}",
        kv_stop(&srv)
    );
    assert!(
        screen.contains("fleetmux-demo"),
        "cancelling the box hid the sidebar:\n{screen}"
    );

    // A press outside the `[ 关闭 fleet ]` button (on the card, left of the box).
    v.input(&sgr_click(31, 2, 0));
    v.wait_text("关闭 fleet", T);
    v.input(&sgr_click(5, 2, 0));
    let screen = v.wait_no_text("关闭 fleet", T);
    assert_eq!(
        kv_stop(&srv),
        None,
        "a press outside the box wrote kv stop_request: {:?}",
        kv_stop(&srv)
    );
    assert!(
        screen.contains("fleetmux-demo"),
        "a press outside the box hid the sidebar:\n{screen}"
    );

    // A prefix key cancels and does nothing else (it must not hide the sidebar).
    v.input(&sgr_click(31, 2, 0));
    v.wait_text("关闭 fleet", T);
    v.input(&prefixed(b"s"));
    let screen = v.wait_no_text("关闭 fleet", T);
    assert_eq!(
        kv_stop(&srv),
        None,
        "the prefix key wrote kv stop_request: {:?}",
        kv_stop(&srv)
    );
    assert!(
        screen.contains("fleetmux-demo"),
        "the prefix key that cancelled the box also hid the sidebar:\n{screen}"
    );
}

/// `^b (` / `^b )` are the `←` / `→` buttons and `^b X` is the root card's `×` (plan §11.2). With one fleet the
/// arrows are grey and do nothing; with a neighbour they say who will switch (S3).
#[test]
#[allow(non_snake_case)] // the plan (§11.4) names this test exactly so
fn prefix_X_opens_and_prefix_parens_are_the_arrows() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);

    let before = srv.layout();
    v.input(&prefixed(b"("));
    v.input(&prefixed(b")"));
    v.wait_quiet(Duration::from_millis(300), T);
    let status = v.status_line();
    assert!(
        !status.contains("切换 fleet"),
        "^b ( or ^b ) on a grey arrow showed the switch notice: {status:?}"
    );
    assert_eq!(
        srv.layout(),
        before,
        "^b ( or ^b ) changed the layout with one fleet: {}",
        srv.layout()
    );

    srv.ok("kv.set", json!({ "key": "sidebar", "value": SIDEBAR_THREE }));
    v.wait_text("child-fleet", T);
    v.input(&prefixed(b"("));
    let status = v.wait_status("^b ( switch notice", T, |l| l.contains("切换 fleet：S3 开放"));
    assert!(
        status.contains("切换 fleet：S3 开放"),
        "^b ( did not show the switch notice: {status:?}"
    );
    v.input(b"x");
    v.wait_no_text("切换 fleet：S3 开放", T);
    v.input(&prefixed(b")"));
    let status = v.wait_status("^b ) switch notice", T, |l| l.contains("切换 fleet：S3 开放"));
    assert!(
        status.contains("切换 fleet：S3 开放"),
        "^b ) did not show the switch notice: {status:?}"
    );

    v.input(&prefixed(b"X"));
    let screen = v.wait_text("关闭 fleet", T);
    assert!(
        screen.contains("关闭整个 fleet「root-fleet」？"),
        "^b X did not open the box for the first card:\n{screen}"
    );
    assert_eq!(
        kv_stop(&srv),
        None,
        "^b X alone wrote kv stop_request: {:?}",
        kv_stop(&srv)
    );
}

/// The daemon's answer to a close request stays on the status line until the state changes (plan §11.1).
#[test]
fn kv_stop_request_accepted_and_refused_show_on_the_status_line() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);

    srv.ok(
        "kv.set",
        json!({ "key": "stop_request", "value": r#"{"v":1,"state":"requested","at":1761792000}"# }),
    );
    let status = v.wait_status("requested", T, |l| l.contains("已请求关闭，等 daemon"));
    assert!(
        status.contains("已请求关闭，等 daemon"),
        "the requested state is not on the status line: {status:?}"
    );

    srv.ok(
        "kv.set",
        json!({ "key": "stop_request", "value": r#"{"v":1,"state":"accepted"}"# }),
    );
    let status = v.wait_status("accepted", T, |l| l.contains("正在关闭…"));
    assert!(
        status.contains("正在关闭…"),
        "the accepted state is not on the status line: {status:?}"
    );

    srv.ok(
        "kv.set",
        json!({ "key": "stop_request", "value": r#"{"v":1,"state":"refused","reason":"no lifecycle"}"# }),
    );
    let status = v.wait_status("refused", T, |l| l.contains("关闭被拒：no lifecycle"));
    assert!(
        status.contains("关闭被拒：no lifecycle"),
        "the refused state is not on the status line: {status:?}"
    );
}

// ------------------------------ fleet sidebar drag (S1d-③, plan §16.2 / §16.4) -------------------------

/// The kv `sidebar_width` the server saved when a drag was released; `None` while nothing was dragged.
fn kv_sidebar_width(srv: &Server) -> Option<String> {
    let got = srv.ok("kv.get", json!({ "key": "sidebar_width" }));
    got["value"].as_str().map(str::to_string)
}

/// The PTY width `pane.get` reports for `id` (the layout rectangle's width, `apply_pty_sizes`).
fn pane_cols(srv: &Server, id: &str) -> u64 {
    let info = srv.pane(id);
    info["cols"]
        .as_u64()
        .unwrap_or_else(|| panic!("pane.get has no cols for {id}: {info}"))
}

/// A supervisor and a worker with the sidebar card shown at the default 34-column width.
fn sidebar_shown() -> (Server, String, String, Viewer) {
    let srv = Server::start();
    let [sup, w1] = sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    (srv, sup, w1, v)
}

/// Waiting for a PTY width the drag should produce (the resize runs through `layout_changed`).
fn wait_cols(srv: &Server, id: &str, want: u64, what: &str) {
    wait_until(what, T, || (pane_cols(srv, id) == want).then_some(()));
}

/// Dragging the sidebar's edge right widens the sidebar: the content's left border moves to the new column, the
/// supervisor's and workers' PTYs get the new rectangles, and the release saves kv `sidebar_width` (plan §16.2).
#[test]
fn dragging_the_sidebar_edge_resizes_and_saves() {
    let (srv, sup, w1, mut v) = sidebar_shown();
    // The edge of the 34-column sidebar: gap column 33 and the content's left border column 34.
    v.input(&sgr_drag((34, 20), (44, 20), 0));
    wait_until("kv sidebar_width 44 after the release", T, || {
        (kv_sidebar_width(&srv).as_deref() == Some("44")).then_some(())
    });
    let border = v.wait_cell(0, 44, T, |c| is_box(c.ch));
    assert!(
        is_box(border.ch),
        "(0,44) is not the content's left border after dragging the sidebar to 44 columns: {border:?}"
    );
    let screen = v.text();
    assert!(
        !is_box(v.cell(0, 43).ch),
        "(0,43) is the 44-column sidebar's gap column but shows a border: {:?}\n{screen}",
        v.cell(0, 43)
    );
    wait_cols(&srv, &sup, 96, "the supervisor's PTY at a 44-column sidebar");
    wait_cols(&srv, &w1, 97, "the workers' PTY at a 44-column sidebar");
    assert_eq!(
        kv_sidebar_width(&srv).as_deref(),
        Some("44"),
        "the released drag did not keep kv sidebar_width: {:?}",
        kv_sidebar_width(&srv)
    );
}

/// The drag is clamped to `SIDEBAR_MIN_W` 24 and to `sidebar_max(cols)` (60 at 240 columns): a drag to column 5
/// gives 24, a drag to column 200 gives 60 (plan §16.2 / §16.5-⑤).
#[test]
fn sidebar_drag_is_clamped_to_24_and_60() {
    let (srv, sup, _w1, mut v) = sidebar_shown();
    v.input(&sgr_drag((34, 20), (5, 20), 0));
    wait_until("kv sidebar_width 24 for a drag to column 5", T, || {
        (kv_sidebar_width(&srv).as_deref() == Some("24")).then_some(())
    });
    let border = v.wait_cell(0, 24, T, |c| is_box(c.ch));
    assert!(
        is_box(border.ch),
        "(0,24) is not the content's left border after a drag to column 5: {border:?}"
    );
    wait_cols(&srv, &sup, 106, "the supervisor's PTY at the 24-column minimum");

    v.input(&sgr_drag((24, 20), (200, 20), 0));
    wait_until("kv sidebar_width 60 for a drag to column 200", T, || {
        (kv_sidebar_width(&srv).as_deref() == Some("60")).then_some(())
    });
    let border = v.wait_cell(0, 60, T, |c| is_box(c.ch));
    assert!(
        is_box(border.ch),
        "(0,60) is not the content's left border after a drag to column 200: {border:?}"
    );
    wait_cols(&srv, &sup, 88, "the supervisor's PTY at the 60-column maximum");
}

/// The width lives in the server, so `^b s` (hide) and `^b s` again (show) come back at the dragged width
/// (plan §16.2 共享 / §16.5-⑥).
#[test]
fn hide_then_show_keeps_the_dragged_width() {
    let (srv, sup, _w1, mut v) = sidebar_shown();
    v.input(&sgr_drag((34, 20), (44, 20), 0));
    wait_until("kv sidebar_width 44 before hiding", T, || {
        (kv_sidebar_width(&srv).as_deref() == Some("44")).then_some(())
    });
    v.wait_cell(0, 44, T, |c| is_box(c.ch));

    v.input(&prefixed(b"s"));
    let hidden = v.wait_no_text("~/.cmux-fleet-fmx-demo", T);
    assert_eq!(
        v.cell(0, 1).ch,
        '◫',
        "^b s did not hide the sidebar into the rail: {:?}\n{hidden}",
        v.cell(0, 1)
    );
    v.input(&prefixed(b"s"));
    let shown = v.wait_text("fleetmux-demo", T);
    let border = v.wait_cell(0, 44, T, |c| is_box(c.ch));
    assert!(
        is_box(border.ch),
        "(0,44) is not the content's left border after hiding and showing a 44-column sidebar: {border:?}\n{shown}"
    );
    wait_cols(&srv, &sup, 96, "the supervisor's PTY after hide and show");
    assert_eq!(
        kv_sidebar_width(&srv).as_deref(),
        Some("44"),
        "hide and show changed kv sidebar_width: {:?}",
        kv_sidebar_width(&srv)
    );
}

/// A kv `sidebar_width` that is not a number or outside `SIDEBAR_MIN_W..=SIDEBAR_MAX_W` is ignored: the sidebar
/// stays at the default 34 columns, while a valid one is loaded (plan §16.2 保存).
#[test]
fn kv_sidebar_width_out_of_range_is_ignored() {
    let (srv, sup, _w1, mut v) = sidebar_shown();
    assert_eq!(
        pane_cols(&srv, &sup),
        101,
        "the default 34-column sidebar gives the supervisor a 101-column PTY: {}",
        srv.pane(&sup)
    );
    for bad in ["200", "23", "abc"] {
        srv.ok("kv.set", json!({ "key": "sidebar_width", "value": bad }));
        assert_eq!(
            pane_cols(&srv, &sup),
            101,
            "kv sidebar_width {bad:?} moved the sidebar off its default 34 columns: {}",
            srv.pane(&sup)
        );
    }
    let border = v.wait_cell(0, 34, T, |c| is_box(c.ch));
    assert!(
        is_box(border.ch),
        "(0,34) is not the content's left border after ignored kv sidebar_width values: {border:?}"
    );

    srv.ok("kv.set", json!({ "key": "sidebar_width", "value": "44" }));
    wait_cols(&srv, &sup, 96, "the supervisor's PTY for a valid kv sidebar_width 44");
    let border = v.wait_cell(0, 44, T, |c| is_box(c.ch));
    assert!(
        is_box(border.ch),
        "(0,44) is not the content's left border after kv sidebar_width 44: {border:?}"
    );
}

// ------------------------------- titlebar inset (S1e-③, plan §17.2 / §17.3) ---------------------------

/// A view connection whose handshake carries `"inset": N`, standing in for the gui's page (plan §17.2: the
/// page computes `ceil(78 / cell width)` columns of traffic-light space and reports them). Kept open for as long
/// as the server should let that viewer push the toolbar right; dropping it is the disconnect.
struct InsetViewer {
    stream: UnixStream,
}

impl InsetViewer {
    fn attach(srv: &Server, inset: u16) -> InsetViewer {
        InsetViewer::attach_sized(srv, inset, W, H)
    }

    /// As [`InsetViewer::attach`], at a given viewer size (the layout follows the latest viewer's size).
    fn attach_sized(srv: &Server, inset: u16, cols: u16, rows: u16) -> InsetViewer {
        let mut stream = UnixStream::connect(srv.sock()).expect("connect the gui's view");
        let hello = json!({
            "view": 1,
            "cols": cols,
            "rows": rows,
            "term": "xterm-256color",
            "colorterm": "truecolor",
            "overlay": 1,
            "scroll": 1,
            "inset": inset,
        });
        stream
            .write_all(format!("{hello}\n").as_bytes())
            .expect("write the inset handshake");
        let mut line = Vec::new();
        let mut byte = [0u8; 1];
        while byte[0] != b'\n' {
            stream.read_exact(&mut byte).expect("the handshake reply");
            line.push(byte[0]);
        }
        let reply: serde_json::Value = serde_json::from_slice(&line).expect("the reply is JSON");
        assert_eq!(
            reply["ok"],
            json!(true),
            "the handshake with \"inset\":{inset} was refused: {reply}"
        );
        assert_eq!(
            reply["inset"],
            json!(1),
            "the ok line does not say the server understands inset (PROTOCOL.md §6.1): {reply}"
        );
        InsetViewer { stream }
    }

    /// A `0x06` inset line, as the page sends it after a font-size change (plan §17.3).
    fn send_inset(&mut self, cols: u16) {
        self.send_line(json!({ "inset": cols }));
    }

    /// A `0x06` line with an explicit payload (A42-LINUX-SIDEBAR-ALIGN-03 adds `sidebar_min`).
    fn send_line(&mut self, value: serde_json::Value) {
        let payload = value.to_string();
        let mut frame = vec![0x06u8];
        frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        frame.extend_from_slice(payload.as_bytes());
        self.stream
            .write_all(&frame)
            .expect("write the 0x06 inset frame");
    }
}

/// The toolbar's inset is the biggest one any connected viewer reported (plan §17.2): a gui connection with
/// `"inset":10` moves everyone's `◫` to column 11, a `0x06` with 4 moves it to column 5, and the disconnect puts
/// it back at column 1.
#[test]
fn a_gui_viewer_with_inset_shifts_everyones_toolbar() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut gui = InsetViewer::attach(&srv, 10);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    let screen = v.wait_text("fleetmux-demo", T);

    let toggle = v.wait_cell(0, 11, T, |c| c.ch == '◫');
    assert_eq!(
        toggle.ch, '◫',
        "with a gui at inset 10 the toolbar's ◫ is not at (0,11): {toggle:?}\n{screen}"
    );

    gui.send_inset(4);
    let moved = v.wait_cell(0, 5, T, |c| c.ch == '◫');
    assert_eq!(
        moved.ch, '◫',
        "a 0x06 inset 4 did not move the toolbar's ◫ to (0,5): {moved:?}\n{}",
        v.text()
    );

    drop(gui);
    let back = v.wait_cell(0, 1, T, |c| c.ch == '◫');
    assert_eq!(
        back.ch, '◫',
        "after the gui disconnected the toolbar's ◫ is not back at (0,1): {back:?}\n{}",
        v.text()
    );
}

/// Hidden, the sidebar's right edge is part of the collapsed toolbar, not a resize edge (plan §17.2 /
/// §17.4-5): dragging it changes nothing, writes no kv `sidebar_width` and leaves the sidebar hidden at its
/// width.
#[test]
fn edge_drag_does_nothing_while_hidden() {
    let (srv, _sup, _w1, mut v) = sidebar_shown();
    v.input(&prefixed(b"s"));
    let hidden = v.wait_no_text("~/.cmux-fleet-fmx-demo", T);
    let toggle = v.cell(0, 1);
    assert_eq!(
        toggle.ch, '◫',
        "the hidden sidebar has no ◫ at (0,1): {toggle:?}\n{hidden}"
    );

    // 隐藏态侧栏宽 0，(11,20) 落在格子里（plan §20.2）: columns 11 / 12 are its edge while shown only.
    v.input(&sgr_drag((11, 20), (40, 20), 0));
    v.wait_quiet(Duration::from_millis(400), T);
    assert_eq!(
        kv_sidebar_width(&srv),
        None,
        "dragging the hidden sidebar's edge wrote kv sidebar_width: {:?}",
        kv_sidebar_width(&srv)
    );
    let screen = v.text();
    assert!(
        !screen.contains("~/.cmux-fleet-fmx-demo"),
        "dragging the hidden sidebar's edge showed it:\n{screen}"
    );

    v.input(&prefixed(b"s"));
    let shown = v.wait_text("fleetmux-demo", T);
    let border = v.wait_cell(0, 34, T, |c| is_box(c.ch));
    assert!(
        is_box(border.ch),
        "(0,34) is not the content's left border after the ignored hidden drag: {border:?}\n{shown}"
    );
}

// ------------------------------ S1f topbar (plan §20.2 / §20.6) ------------------------------

/// The topbar's `↺` with no kv `restorable`: it writes the nothing-to-restore notice, which the next key press
/// clears, and it works the same while the sidebar is collapsed (plan §13.6-2).
#[test]
fn clicking_reopen_without_a_closed_fleet_says_so() {
    let (_srv, _sup, _w1, mut v) = sidebar_shown();
    v.input(&sgr_click(7, 0, 0));
    let status = v.wait_status("the reopen notice", T, |s| {
        s.contains("没有可恢复的 fleet")
    });
    assert!(
        status.contains("没有可恢复的 fleet"),
        "clicking ↺ at (7,0) did not show the nothing-to-restore notice: {status:?}"
    );
    v.input(&prefixed(b"s"));
    let cleared = v.wait_status("the notice to clear", T, |s| {
        !s.contains("没有可恢复的 fleet")
    });
    assert!(
        !cleared.contains("没有可恢复的 fleet"),
        "the next key press did not clear the reopen notice: {cleared:?}"
    );
    // Hidden: the toolbar keeps its columns, so the same click still hits ↺.
    let hidden = v.wait_no_text("~/.cmux-fleet-fmx-demo", T);
    let toggle = v.cell(0, 1);
    assert_eq!(
        toggle.ch, '◫',
        "the sidebar is not hidden before the second ↺ click: {toggle:?}\n{hidden}"
    );
    v.input(&sgr_click(7, 0, 0));
    let status = v.wait_status("the reopen notice while hidden", T, |s| {
        s.contains("没有可恢复的 fleet")
    });
    assert!(
        status.contains("没有可恢复的 fleet"),
        "clicking ↺ at (7,0) while hidden did not show the nothing-to-restore notice: {status:?}"
    );
}

/// Row 0 is the topbar, so the `│` divider there is the sidebar's drag line too: dragging it right widens the
/// sidebar exactly like the border below (plan §20.2, completion ⑥).
#[test]
fn dragging_the_topbar_divider_resizes_the_sidebar() {
    let (srv, sup, _w1, mut v) = sidebar_shown();
    let divider = v.cell(0, 34);
    assert_eq!(
        divider.ch, '│',
        "the topbar divider is not at (0,34): {divider:?}"
    );
    v.input(&sgr_drag((34, 0), (44, 0), 0));
    wait_until("kv sidebar_width 44 after the topbar drag", T, || {
        (kv_sidebar_width(&srv).as_deref() == Some("44")).then_some(())
    });
    let divider = v.wait_cell(0, 44, T, |c| c.ch == '│');
    assert_eq!(
        divider.ch, '│',
        "the topbar divider did not follow the drag to column 44: {divider:?}"
    );
    let border = v.wait_cell(2, 44, T, |c| is_box(c.ch));
    assert!(
        is_box(border.ch),
        "the groups' left border did not follow the topbar drag to column 44: {border:?}"
    );
    wait_cols(&srv, &sup, 96, "the supervisor's PTY after the topbar drag");
}

/// Hidden there is no sidebar line under row 0: the divider cell is `Outside`, so a drag there must not widen
/// the sidebar or bring it back (plan §20.2, completion ⑥).
#[test]
fn hidden_topbar_divider_drag_does_nothing() {
    let (srv, _sup, _w1, mut v) = sidebar_shown();
    v.input(&prefixed(b"s"));
    let hidden = v.wait_no_text("~/.cmux-fleet-fmx-demo", T);
    let toggle = v.cell(0, 1);
    assert_eq!(
        toggle.ch, '◫',
        "the sidebar is not hidden before the drag: {toggle:?}\n{hidden}"
    );
    v.input(&sgr_drag((15, 0), (40, 0), 0));
    v.wait_quiet(Duration::from_millis(400), T);
    assert_eq!(
        kv_sidebar_width(&srv),
        None,
        "dragging the hidden topbar's (15,0) wrote kv sidebar_width: {:?}",
        kv_sidebar_width(&srv)
    );
    let screen = v.text();
    assert!(
        !screen.contains("~/.cmux-fleet-fmx-demo"),
        "dragging the hidden topbar's (15,0) showed the sidebar:\n{screen}"
    );
}

/// The horizontal separator moved down with the topbar, so a drag released on row 36 keeps `v_ratio` 60; with the
/// old `ratio_from_y(rows − 1, row)` it would come out 61 (plan §20.4-6, §20.6).
#[test]
fn hsep_drag_with_the_topbar_keeps_the_ratio() {
    let (srv, _sup, _w1, mut v) = sidebar_shown();
    v.input(&sgr_drag((50, 36), (50, 36), 0));
    v.wait_quiet(Duration::from_millis(300), T);
    let l = srv.layout();
    assert_eq!(
        l["v_ratio"],
        json!(60),
        "dragging the hsep at row 36 below the topbar changed v_ratio: {l}"
    );
    assert_eq!(
        l["h_ratio"],
        json!(50),
        "dragging the hsep at row 36 changed h_ratio: {l}"
    );
}

// ------------------------------ S2b: the topbar ↺ restores (plan §13.6-2 / §13.6-9) ------------------------------

const CLOSED_SIDEBAR: &str = r#"{"v":1,"current":"fleet-20261030-010101-ab12cd34","fleets":[{"id":"fleet-20261030-010101-ab12cd34","title":"fleetmux-demo","state":"closed","line1":"已关闭 · 可恢复","line2":"~/.cmux-fleet-fmx-demo"}]}"#;

/// Clicking the topbar's `↺` restores the newest closed fleet: the registered command runs in a `resume` pane of
/// this server (plan §13.6-2 / §13.6-9).
#[test]
fn clicking_reopen_restores_the_newest_closed_fleet() {
    let srv = Server::start();
    srv.ok("kv.set", json!({ "key": "sidebar", "value": CLOSED_SIDEBAR }));
    srv.ok(
        "kv.set",
        json!({ "key": "restorable", "value": json!({"v": 1, "runs": [{
            "id": "fleet-20261030-010101-ab12cd34", "title": "fleetmux-demo", "closed_at": 1790000000,
            "argv": ["sh", "-c", "echo RESUMED; sleep 5"], "cwd": "/tmp", "env": {}}]}).to_string() }),
    );
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("已关闭 · 可恢复", T);
    v.input(&sgr_click(7, 0, 0));
    let screen = v.wait_text("RESUMED", T);
    let panes = srv.panes();
    assert!(
        panes.iter().any(|p| p["title"] == json!("resume")),
        "clicking ↺ at (7,0) spawned no 'resume' pane in this server: {panes:?}\n{screen}"
    );
}


/// A42-LINUX-SIDEBAR-ALIGN-03: the Linux window reports the narrowest band its own header controls need as
/// `0x06` `sidebar_min`; the server keeps the **effective** sidebar at least that wide, so the grid's divider moves
/// with the header boundary, while kv `sidebar_width` (the user's request) is not overwritten. Removing the floor
/// gives the requested width back.
#[test]
fn a_window_sidebar_floor_widens_the_band_without_overwriting_the_request() {
    let srv = Server::start();
    let _ = sidebar_page(&srv);
    set_sidebar(&srv);
    srv.ok(
        "kv.set",
        json!({ "key": "sidebar_width", "value": "24" }),
    );
    let mut v = Viewer::attach(&srv, W, H);
    let border = v.wait_cell(0, 24, T, |c| is_box(c.ch));
    assert!(
        is_box(border.ch),
        "kv sidebar_width 24 did not put the content border at (0,24): {border:?}"
    );

    let mut gui = InsetViewer::attach(&srv, 0);
    gui.send_line(json!({ "inset": 0, "sidebar_min": 34 }));
    let widened = v.wait_cell(0, 34, T, |c| is_box(c.ch));
    assert!(
        is_box(widened.ch),
        "the window's sidebar_min 34 did not widen the band to column 34: {widened:?}\n{}",
        v.text()
    );
    assert_eq!(
        kv_sidebar_width(&srv).as_deref(),
        Some("24"),
        "the effective band overwrote the user's requested sidebar_width"
    );

    // Hit/drag use the same effective geometry: the drag line is at the effective 34, so a drag that starts
    // there resizes the requested width (24 -> 40). A press at the requested 24 is inside the content instead.
    v.input(&sgr_drag((34, 20), (40, 20), 0));
    wait_until("kv sidebar_width 40 after a drag on the effective edge", T, || {
        (kv_sidebar_width(&srv).as_deref() == Some("40")).then_some(())
    });
    let dragged = v.wait_cell(0, 40, T, |c| is_box(c.ch));
    assert!(
        is_box(dragged.ch),
        "the effective edge drag did not put the content border at (0,40): {dragged:?}\n{}",
        v.text()
    );

    // The user's requested width can go back to 24; while the window still reports floor 34 the effective band
    // stays 34, and removing the floor gives the requested 24 back. 34 is the measured floor here, not a lock.
    srv.ok("kv.set", json!({ "key": "sidebar_width", "value": "24" }));
    let still_floor = v.wait_cell(0, 34, T, |c| is_box(c.ch));
    assert!(
        is_box(still_floor.ch),
        "requested 24 with floor 34 did not keep the effective border at (0,34): {still_floor:?}"
    );
    gui.send_line(json!({ "inset": 0, "sidebar_min": 0 }));
    let back = v.wait_cell(0, 24, T, |c| is_box(c.ch));
    assert!(
        is_box(back.ch),
        "removing sidebar_min did not give the requested 24 columns back: {back:?}\n{}",
        v.text()
    );

    // M81: the same rule with a floor *above* the request — the case a page with narrower cells reaches (its
    // sidebar's own px width has to carry the toolbar and GTK's window buttons). A requested 34 is shown as 38
    // while the window reports 38, the drag line is the effective 38, and dropping the floor gives 34 back. 38 is
    // this fixture's floor, not a locked width.
    srv.ok("kv.set", json!({ "key": "sidebar_width", "value": "34" }));
    gui.send_line(json!({ "inset": 0, "sidebar_min": 38 }));
    let above = v.wait_cell(0, 38, T, |c| is_box(c.ch));
    assert!(
        is_box(above.ch),
        "requested 34 with floor 38 did not widen the band to 38: {above:?}\n{}",
        v.text()
    );
    v.input(&sgr_drag((38, 20), (45, 20), 0));
    wait_until("kv sidebar_width 45 after a drag on the effective 38", T, || {
        (kv_sidebar_width(&srv).as_deref() == Some("45")).then_some(())
    });
    srv.ok("kv.set", json!({ "key": "sidebar_width", "value": "34" }));
    let still_38 = v.wait_cell(0, 38, T, |c| is_box(c.ch));
    assert!(
        is_box(still_38.ch),
        "requested 34 with floor 38 did not stay at 38: {still_38:?}"
    );
    gui.send_line(json!({ "inset": 0, "sidebar_min": 0 }));
    let back_34 = v.wait_cell(0, 34, T, |c| is_box(c.ch));
    assert!(
        is_box(back_34.ch),
        "removing the 38-column floor did not give the requested 34 back: {back_34:?}\n{}",
        v.text()
    );

    // Collapse and expand (`^b s`, rev 12): the shown band goes away into the one-column rail, and because the
    // window keeps its floor while collapsed the expansion comes back to the effective 34 immediately — the
    // request (24) is never the band, not even for one round trip, and kv still holds the request.
    srv.ok("kv.set", json!({ "key": "sidebar_width", "value": "24" }));
    gui.send_line(json!({ "inset": 0, "sidebar_min": 34 }));
    let shown = v.wait_cell(0, 34, T, |c| is_box(c.ch));
    assert!(
        is_box(shown.ch),
        "the floor did not put the band at 34 before collapsing: {shown:?}\n{}",
        v.text()
    );
    v.input(&prefixed(b"s"));
    let screen = v.wait_no_text("~/.cmux-fleet-fmx-demo", T);
    assert_eq!(
        v.cell(0, 1).ch,
        '◫',
        "^b s did not leave the rail toggle at (0,1):\n{screen}"
    );
    v.input(&prefixed(b"s"));
    let expanded = v.wait_cell(0, 34, T, |c| is_box(c.ch));
    assert!(
        is_box(expanded.ch),
        "expanding did not come back to the effective 34: {expanded:?}\n{}",
        v.text()
    );
    assert_eq!(
        kv_sidebar_width(&srv).as_deref(),
        Some("24"),
        "collapse/expand overwrote the user's requested sidebar_width"
    );

    // M81 refinement: a floor the controls need *above* the widest drag width (a very small font makes the toolbar
    // wider than 60 columns) is honoured as it is — capping it would leave the header boundary and the grid's
    // divider on two different columns. When even that does not fit the content, the layout's own
    // SIDEBAR_MIN_CONTENT rule hides the sidebar, so there is one boundary, never two.
    srv.ok("kv.set", json!({ "key": "sidebar_width", "value": "60" }));
    gui.send_line(json!({ "inset": 0, "sidebar_min": 66 }));
    let above_widest = v.wait_cell(0, 66, T, |c| is_box(c.ch));
    assert!(
        is_box(above_widest.ch),
        "a floor of 66 columns was capped instead of honoured: {above_widest:?}\n{}",
        v.text()
    );
    assert_eq!(
        kv_sidebar_width(&srv).as_deref(),
        Some("60"),
        "the floor above the widest drag width overwrote the request"
    );
    gui.send_line(json!({ "inset": 0, "sidebar_min": 200 }));
    let screen = v.wait_no_text("~/.cmux-fleet-fmx-demo", T);
    assert_eq!(
        v.cell(0, 1).ch,
        '◫',
        "a floor that cannot fit the content did not hide the sidebar (one boundary, never two):\n{screen}"
    );

    // M81 RB-01/RB-02: the wire's own upper bound (`proto::view::SIDEBAR_MIN_MAX` = `u16::MAX`) is a legal line
    // too. The layout's content-minimum rule hides the band, and that comparison must not overflow
    // (`65535 + SIDEBAR_MIN_CONTENT` does not fit a `u16`): a debug server must not panic and a release one must
    // not wrap into showing a band wider than the viewer. Dropping the floor gives the request back.
    gui.send_line(json!({ "inset": 0, "sidebar_min": u16::MAX }));
    let screen = v.wait_no_text("~/.cmux-fleet-fmx-demo", T);
    assert_eq!(
        v.cell(0, 1).ch,
        '◫',
        "the wire-maximum floor did not hide the sidebar:\n{screen}"
    );
    gui.send_line(json!({ "inset": 0, "sidebar_min": 0 }));
    let back = v.wait_cell(0, 60, T, |c| is_box(c.ch));
    assert!(
        is_box(back.ch),
        "removing the wire-maximum floor did not give the requested 60 columns back: {back:?}\n{}",
        v.text()
    );
}

/// The row the sidebar's first card is drawn on, read from the screen instead of hardcoded: the first card row is
/// the layout's header band, and that band is platform geometry — macOS's in-grid topbar takes [`TOPBAR_ROWS`] rows,
/// Linux's native headerbar keeps its own separation row *on top of* the topbar row a forced macOS layout still
/// reserves (M86: this file used to assume row 2 and failed on Linux with `cell (2,24) never matched`).
fn first_card_row(v: &mut Viewer) -> u16 {
    let end = Instant::now() + T;
    loop {
        if let Some(row) = (0..H).find(|r| v.cell(*r, 1).ch == '●') {
            return row;
        }
        assert!(
            Instant::now() < end,
            "no sidebar card dot anywhere on the screen:\n{}",
            v.text()
        );
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// A42-LINUX-SIDEBAR-ALIGN-03 (M81, parent feedback): the window's floor is its native controls' capability, not a
/// function of the sidebar the server currently shows. With requested 24, floor 34 and a viewer exactly
/// 24 + `SIDEBAR_MIN_CONTENT` columns wide the server hides the sidebar (content minimum) and sends
/// `topbar.sidebar = 0`; if the window zeroed its floor on that, the server would re-show the 24-column request and
/// the two would oscillate between 24 and hidden. The message sequence has to settle on one fixed point.
///
/// The mirror is pinned off so the group sides are the same on both platforms; the floor is about the sidebar's
/// width, not about which side the groups are on. The row the assertions read is the first card row of the real
/// header geometry ([`first_card_row`]), not a constant: Linux keeps one more header row than macOS.
#[test]
fn a_hidden_sidebar_keeps_the_window_floor_and_settles() {
    let srv = Server::start_with_env(&[("FLEETMUX_LAYOUT_MIRROR", "0")]);
    let _ = sidebar_page(&srv);
    set_sidebar(&srv);
    srv.ok("kv.set", json!({ "key": "sidebar_width", "value": "24" }));
    // 84 = 24 + SIDEBAR_MIN_CONTENT: a 24-column sidebar fits, a 34-column one does not.
    let mut v = Viewer::attach(&srv, 84, H);
    let mut gui = InsetViewer::attach_sized(&srv, 0, 84, H);
    let card_row = first_card_row(&mut v);
    let shown = v.wait_cell(card_row, 24, T, |c| is_box(c.ch));
    assert!(
        is_box(shown.ch),
        "the 24-column request is not shown at 84 columns on the card row {card_row}: {shown:?}\n{}",
        v.text()
    );
    gui.send_line(json!({ "inset": 0, "sidebar_min": 34 }));
    let hidden = v.wait_cell(card_row, 0, T, |c| is_box(c.ch));
    assert!(
        is_box(hidden.ch),
        "the 34-column floor did not hide the sidebar at 84 columns on the card row {card_row}: {hidden:?}\n{}",
        v.text()
    );
    // The window re-reports the same floor (it does not depend on the shown band): the server must not flip back to
    // the 24-column request — that is the oscillation this pins.
    for step in 0..3 {
        gui.send_line(json!({ "inset": 0, "sidebar_min": 34 }));
        v.wait_quiet(Duration::from_millis(150), T);
        let screen = v.text();
        assert!(
            is_box(v.cell(card_row, 0).ch),
            "step {step}: the layout oscillated back to the 24-column request on the card row {card_row}:\n{screen}"
        );
    }
    // Dropping the floor (the window is gone) gives the request back: it was never overwritten.
    gui.send_line(json!({ "inset": 0, "sidebar_min": 0 }));
    let back = v.wait_cell(card_row, 24, T, |c| is_box(c.ch));
    assert!(
        is_box(back.ch),
        "removing the floor did not give the requested 24 columns back on the card row {card_row}: {back:?}\n{}",
        v.text()
    );
}

// ---------------------------------------------------------------- S1c: new bash pages (plan §14.2 / §14.5)

/// The sidebar's bash panes (`type "shell"`, plan §14.3). User 2026-11-09: each is a tab in the root page's `workers`
/// group (the fleet registers it); listed with `all` so a bash elsewhere would still be seen.
fn shell_panes(srv: &Server) -> Vec<serde_json::Value> {
    srv.panes_all()
        .into_iter()
        .filter(|p| p["type"] == json!("shell"))
        .collect()
}

/// User 2026-11-09: a bash pane is a tab in the root page's `workers` group, never a page of its own.
fn in_root_workers(srv: &Server, id: &str) {
    let p = shell_panes(srv)
        .into_iter()
        .find(|p| p["pane"] == json!(id))
        .unwrap_or_else(|| panic!("bash pane {id} is not listed: {:?}", srv.panes_all()));
    assert_eq!(p["page"], json!(""), "the bash pane {id} is not on the root page: {p}");
    assert_eq!(p["group"], json!("workers"), "the bash pane {id} is not in the workers group: {p}");
    let ids = srv.page_ids();
    assert!(!ids.iter().any(|p| p.starts_with("_bash-")), "a bash opened a page of its own: {ids:?}");
}

/// Whether bash pane `id` is shown: the root page is current, `id` is the selected tab of the focused `workers` group
/// and nothing is zoomed.
fn bash_shown(srv: &Server, id: &str) -> bool {
    let l = srv.layout();
    srv.current_page().is_empty()
        && l["focus"] == json!("workers")
        && l["selected"]["workers"] == json!(id)
        && l["zoom"] == Value::Null
}

/// Wait for the first bash page (`bash-1`, `type "shell"`) and return its pane id.
fn wait_shell_pane(srv: &Server) -> String {
    wait_until("one type shell pane titled bash-1", T, || {
        let sh = shell_panes(srv);
        (sh.len() == 1 && sh[0]["title"] == json!("bash-1")).then_some(())
    });
    shell_panes(srv)[0]["pane"]
        .as_str()
        .expect("a bash pane has an id")
        .to_string()
}

/// Two left presses on the sidebar's empty card area within `DOUBLE_CLICK` (300 ms) open a bash, which becomes the
/// selected tab of the focused `workers` group (plan §14.5-2; user 2026-11-09: in the main fleet's worker area).
#[test]
fn double_clicking_the_blank_sidebar_area_opens_a_bash_page() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);

    let mut dbl = sgr_click(5, 10, 0);
    dbl.extend(sgr_click(5, 10, 0));
    v.input(&dbl);
    let id = wait_shell_pane(&srv);
    v.wait_text("终端 · 运行中", T);
    in_root_workers(&srv, &id);
    wait_until("the new bash tab to be selected and focused", T, || {
        bash_shown(&srv, &id).then_some(())
    });
    // The bash card is the second card (row 2 + 4 = 6) and carries the blue bar; the fleet card gives it up.
    wait_until("the bash card at (6,5) to take the blue bar", T, || {
        (v.cell(6, 5).bg == Color::Indexed(4)).then_some(())
    });
    let screen = v.text();
    let fleet = v.cell(2, 5);
    assert_ne!(
        fleet.bg,
        Color::Indexed(4),
        "the fleet card at (2,5) is still selected while bash-1 is: {fleet:?}\n{screen}"
    );
}

/// One press on the blank card area is not a double click: no bash page, no card (plan §14.5-2).
#[test]
fn a_single_blank_click_opens_no_bash_page() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);

    v.input(&sgr_click(5, 10, 0));
    v.wait_quiet(Duration::from_millis(300), T);
    let panes = srv.panes();
    assert!(
        shell_panes(&srv).is_empty(),
        "one blank click opened a bash page: {panes:?}"
    );
    let screen = v.text();
    assert!(
        !screen.contains("bash-1"),
        "one blank click drew a bash card:\n{screen}"
    );
}

/// Two presses on a fleet card are not the blank area (plan §14.5-2: only `hit == Nothing` counts).
#[test]
fn a_double_click_on_a_card_opens_no_bash_page() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);

    let mut dbl = sgr_click(5, 3, 0);
    dbl.extend(sgr_click(5, 3, 0));
    v.input(&dbl);
    v.wait_quiet(Duration::from_millis(300), T);
    let panes = srv.panes();
    assert!(
        shell_panes(&srv).is_empty(),
        "a double click on the fleet card opened a bash page: {panes:?}"
    );
}

/// `^b c` opens a bash page too (plan §14.2: the tmux key), the same pane and card as `+`.
#[test]
fn prefix_c_opens_a_bash_page() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);

    v.input(&prefixed(b"c"));
    let id = wait_shell_pane(&srv);
    v.wait_text("bash-1", T);
    in_root_workers(&srv, &id);
    wait_until("^b c to show the bash tab", T, || bash_shown(&srv, &id).then_some(()));
}

/// User 2026-11-09 ("都在主fleet的worker区域有映射"): with a child fleet's page shown, `^b c` still puts the bash in the
/// root page's `workers` group, goes back to the root page and shows the new tab there; the child page stays.
#[test]
fn prefix_c_on_a_child_page_opens_the_bash_in_the_root_workers_group() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    srv.ok("page.open", json!({"page": "M0", "title": "M0"}));
    srv.ok("page.select", json!({"page": "M0"}));
    wait_until("the child page to be current", T, || (srv.current_page() == "M0").then_some(()));

    v.input(&prefixed(b"c"));
    let id = wait_shell_pane(&srv);
    in_root_workers(&srv, &id);
    wait_until("^b c on a child page to show the bash tab on the root page", T, || {
        bash_shown(&srv, &id).then_some(())
    });
    assert!(srv.page_ids().contains(&"M0".to_string()), "the child page went away");
    wait_until("the bash card at (6,5) to take the blue bar", T, || {
        (v.cell(6, 5).bg == Color::Indexed(4)).then_some(())
    });
}

/// The hidden topbar keeps the `+` at the same column (plan §14.5-1): it still opens a bash page.
#[test]
fn clicking_plus_while_hidden_opens_a_bash_page() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);

    v.input(&prefixed(b"s"));
    v.wait_no_text("~/.cmux-fleet-fmx-demo", T);
    v.input(&sgr_click(4, 0, 0));
    let id = wait_shell_pane(&srv);
    in_root_workers(&srv, &id);
}

/// A shell that exits takes its card — and its workers tab — with it (plan §14.2: pane gone → card gone).
#[test]
fn a_bash_card_goes_away_when_its_shell_exits() {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);

    v.input(&sgr_click(4, 0, 0));
    let id = wait_shell_pane(&srv);
    in_root_workers(&srv, &id);
    v.wait_text("bash-1", T);
    srv.ok("pane.kill", json!({ "pane": id }));
    wait_until("the bash tab to leave the workers group", T, || {
        (!srv.layout()["groups"]["workers"].as_array()?.contains(&json!(id))).then_some(())
    });
    assert_eq!(srv.current_page(), "", "the root page is no longer current");
    let screen = v.wait_no_text("bash-1", T);
    assert!(
        shell_panes(&srv).is_empty(),
        "the shell pane {id} survived pane.kill: {:?}\n{screen}",
        srv.panes()
    );
}

// ---- A26 rev3 (user requirement A42-LAYOUT-LINUX-MIRROR): on Linux the content is mirrored — workers left, the
// supervisor/lower stack in the middle, the fleet sidebar in the right band. The mouse follows the mirrored
// geometry: the sidebar's drag line is on its left edge and the toolbar stays on row 0's left.

/// The mirrored layout of `sidebar_shown` with the same two panes: `FLEETMUX_LAYOUT_MIRROR=1` forces the Linux
/// layout on this host.
fn mirrored_sidebar_shown() -> (Server, String, String, Viewer) {
    let srv = Server::start_with_env(&[("FLEETMUX_LAYOUT_MIRROR", "1"), ("FLEETMUX_NATIVE_HEADER", "1")]);
    let [sup, w1] = sidebar_page(&srv);
    let mut v = Viewer::attach_native(&srv, W, H);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    (srv, sup, w1, v)
}

/// Default Linux ordering uses the same real mouse/PTY path without relying on the mirror override.
#[cfg(target_os = "linux")]
#[test]
fn linux_default_sidebar_drag_and_header_key_keep_the_pane_order() {
    let srv = Server::start_with_env(&[("FLEETMUX_LAYOUT_MIRROR", "")]);
    let [sup, _worker] = sidebar_page(&srv);
    set_sidebar(&srv);
    let mut v = Viewer::attach_native(&srv, W, H);
    v.wait_text("fleetmux-demo", T);
    let (_, sx) = v.wait_find("SUP-BODY", T);
    let (_, wx) = v.wait_find("W1-BODY", T);
    assert!(34 < sx && sx < wx);
    v.input(&sgr_drag((34, 20), (44, 20), 0));
    wait_until("native sidebar drag saved", T,
        || (kv_sidebar_width(&srv).as_deref() == Some("44")).then_some(()));
    wait_cols(&srv, &sup, 96, "default Linux supervisor resized");
    assert!(is_box(v.wait_cell(20, 44, T, |c| is_box(c.ch)).ch));
    v.input(&sgr_click(1, 0, 0));
    v.wait_quiet(Duration::from_millis(100), T);
    assert!(v.text().contains("fleetmux-demo"), "blank header row is not a toggle");
    v.input(b"\x02s");
    v.wait_no_text("~/.cmux-fleet-fmx-demo", T);
    v.input(b"\x02s");
    v.wait_text("~/.cmux-fleet-fmx-demo", T);
    assert_eq!(kv_sidebar_width(&srv).as_deref(), Some("44"));
    assert!(v.wait_find("SUP-BODY", T).1 < v.wait_find("W1-BODY", T).1);
}

/// Sidebar dragging measures from the platform's sidebar edge, independently of the content mirror.
/// Widening to 44 columns saves kv `sidebar_width` and resizes the PTYs without changing their ordering.
#[test]
fn mirrored_sidebar_edge_drag_resizes_from_the_platform_edge() {
    let (srv, sup, _w1, mut v) = mirrored_sidebar_shown();
    let (from, to) = if cfg!(target_os = "linux") {
        (34, 44)
    } else {
        (206, 196)
    };
    v.input(&sgr_drag((from, 20), (to, 20), 0));
    wait_until(
        "kv sidebar_width 44 after dragging the platform sidebar edge",
        T,
        || (kv_sidebar_width(&srv).as_deref() == Some("44")).then_some(()),
    );
    wait_cols(
        &srv,
        &sup,
        96,
        "the supervisor's PTY at a 44-column mirrored sidebar",
    );
    // Linux: the left sidebar ends on column 44; legacy mirror: the right band starts on 196.
    let edge = if cfg!(target_os = "linux") { 44 } else { 195 };
    let border = v.wait_cell(20, edge, T, |c| is_box(c.ch));
    assert!(
        is_box(border.ch),
        "row 20, column {edge} is not the content border beside the sidebar: {border:?}"
    );
}

/// A26 rev5 (`CURRENT_REQUIREMENTS` rev 6 `utm_topbar`): the mirrored grid keeps **no** topbar row, so there is no
/// in-grid `◫` to click at (0,0) — the toolbar lives in the window's native headerbar, whose `◫` sends `^b s`. A
/// click on row 0 is content now (it must not hide the sidebar), and the headerbar's key still toggles it while the
/// content is mirrored.
#[test]
fn mirrored_grid_has_no_topbar_row_and_the_headerbar_key_toggles_the_sidebar() {
    let (srv, _sup, _w1, mut v) = mirrored_sidebar_shown();
    // Row 0 is the workers' tab row, not the toolbar: a press there is a pane/tab press, never `◫`.
    v.input(&sgr_click(1, 0, 0));
    std::thread::sleep(Duration::from_millis(300));
    let screen = v.text();
    assert!(
        screen.contains("~/.cmux-fleet-fmx-demo"),
        "a row-0 click hid the mirrored sidebar: row 0 is content, not the toolbar:\n{screen}"
    );
    assert!(
        !v.row_text(0).starts_with(" ◫"),
        "the mirrored grid drew a toolbar on row 0: {:?}",
        v.row_text(0)
    );
    // The headerbar's `◫` sends the prefix key the row-0 cell used to call.
    v.input(b"\x02s");
    let screen = v.wait_no_text("~/.cmux-fleet-fmx-demo", T);
    assert!(
        !screen.contains("~/.cmux-fleet-fmx-demo"),
        "^b s (the headerbar's ◫) did not hide the mirrored sidebar:\n{screen}"
    );
    let border = v.cell(20, 0);
    assert!(
        is_box(border.ch),
        "row 20, column 0 is not the workers' left border after hiding: {border:?}"
    );
    let _ = srv;
}

// ------------------------------ sidebar scroll (like cmux) ----------------------------

/// A viewer this tall fits four cards (rows 2 / 6 / 10 / 14); a fifth would cross the reserved bottom rows.
const SHORT: u16 = 24;

/// The title on a card row: columns 3..=30 of the sidebar, trimmed (the `×` sits at column 31).
fn card_title(v: &mut Viewer, row: u16) -> String {
    (3..31).map(|c| v.cell(row, c).ch).collect::<String>().replace('\0', " ").trim().to_string()
}

/// The sidebar's scrollbar column (the gap column `sb − 1` = 33) from row 2 to the row above the status line.
fn bar_column(v: &mut Viewer) -> String {
    (2..SHORT - 1).map(|r| v.cell(r, 33).ch).collect()
}

/// Wait until card row `row` shows `title`.
fn wait_card(v: &mut Viewer, row: u16, title: &str) {
    let end = Instant::now() + T;
    loop {
        if card_title(v, row) == title {
            return;
        }
        if Instant::now() >= end {
            panic!("card row {row} never showed {title:?}; screen:\n{}", v.text());
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// The fleet card and `n` bashes opened with `^b c` (each becomes the shown workers tab), in a short viewer.
fn short_sidebar_with_bash(n: usize) -> (Server, Viewer) {
    let srv = Server::start();
    sidebar_page(&srv);
    let mut v = Viewer::attach(&srv, W, SHORT);
    v.wait_text("W1-BODY", T);
    set_sidebar(&srv);
    v.wait_text("fleetmux-demo", T);
    for k in 1..=n {
        v.input(&prefixed(b"c"));
        wait_until(&format!("bash {k} to be open and shown"), T, || {
            let sh = shell_panes(&srv);
            let last = sh.iter().find(|p| p["title"] == json!(format!("bash-{k}")))?;
            let id = last["pane"].as_str()?.to_string();
            (sh.len() == k && bash_shown(&srv, &id)).then_some(())
        });
    }
    (srv, v)
}

/// The pane id of the bash pane titled `title`.
fn pane_titled(srv: &Server, title: &str) -> String {
    shell_panes(srv)
        .into_iter()
        .find(|p| p["title"] == json!(title))
        .and_then(|p| p["pane"].as_str().map(String::from))
        .unwrap_or_else(|| panic!("no bash pane titled {title}: {:?}", srv.panes_all()))
}

/// Like cmux: six cards in a sidebar that fits four scroll with the wheel, one card per step, stopping at both
/// ends; no `… 还有 N 个` line; a grey `┃` thumb in the gap column; a click picks the card drawn under it and a `×`
/// closes that card, after which the list is clamped so no card slot is left blank.
#[test]
fn the_sidebar_scrolls_with_the_wheel_and_clicks_hit_the_scrolled_cards() {
    let (srv, mut v) = short_sidebar_with_bash(5);
    // bash-5 (card 5 of 0..=5) is selected and scrolled into view: cards 2..=5 are drawn.
    wait_card(&mut v, 2, "bash-2");
    wait_card(&mut v, 14, "bash-5");
    let screen = v.text();
    assert!(!screen.contains("还有"), "an overflow line is drawn:\n{screen}");
    let bar = bar_column(&mut v);
    // Track rows 2..=16 (15 rows), thumb 15 × 4 / 6 = 10 rows, at the bottom (the last offset): rows 7..=16.
    assert_eq!(bar.matches('┃').count(), 10, "the thumb is 10 rows: {bar:?}\n{screen}");
    assert_eq!(v.cell(16, 33).ch, '┃', "the thumb ends on the last card's last row:\n{screen}");
    assert_ne!(v.cell(6, 33).ch, '┃', "the thumb starts at row 7 at the last offset:\n{screen}");

    v.input(&sgr(WHEEL_UP, 5, 10, true));
    wait_card(&mut v, 2, "bash-1");
    // Click the top card: it is card 1 of the merged list (bash-1), not card 0.
    v.input(&sgr_click(5, 2, 0));
    let b1 = pane_titled(&srv, "bash-1");
    wait_until("the click to pick bash-1", T, || bash_shown(&srv, &b1).then_some(()));

    for _ in 0..3 {
        v.input(&sgr(WHEEL_UP, 5, 10, true));
    }
    wait_card(&mut v, 2, "fleetmux-demo");
    assert_ne!(v.cell(16, 33).ch, '┃', "at the top the thumb leaves the track's bottom:\n{}", v.text());
    assert_eq!(v.cell(2, 33).ch, '┃', "at the top the thumb starts on row 2:\n{}", v.text());

    for _ in 0..10 {
        v.input(&sgr(WHEEL_DOWN, 33, 10, true)); // over the scrollbar column itself
    }
    wait_card(&mut v, 2, "bash-2");
    wait_card(&mut v, 14, "bash-5");

    // The `×` on the top card closes bash-2 (card 2), the card drawn there.
    let gone = shell_panes(&srv)
        .into_iter()
        .find(|p| p["title"] == json!("bash-2"))
        .and_then(|p| p["pane"].as_str().map(String::from))
        .expect("bash-2 is open");
    v.input(&sgr_click(31, 2, 0));
    wait_until("the × to close bash-2", T, || {
        (!shell_panes(&srv).iter().any(|p| p["pane"] == json!(gone))).then_some(())
    });
    assert!(bash_shown(&srv, &b1), "closing another card keeps bash-1 selected: {}", srv.layout());
    // Five cards, four fit: the list is clamped to cards 1..=4 — bash-1 at the top, no blank slot at the bottom.
    wait_card(&mut v, 2, "bash-1");
    wait_card(&mut v, 14, "bash-4");
}

/// The selected card always comes into view (cmux): a keyboard step onto a card above or below the view, and a new
/// bash, scroll the list just far enough; a window tall enough for every card has no scrollbar.
#[test]
fn the_selected_card_scrolls_into_view() {
    let (_srv, mut v) = short_sidebar_with_bash(5);
    wait_card(&mut v, 14, "bash-5");
    for _ in 0..5 {
        v.input(&sgr(WHEEL_UP, 5, 10, true));
    }
    wait_card(&mut v, 2, "fleetmux-demo");

    // `^b (` steps from bash-5 (card 5, out of view) to bash-4 (card 4): the list scrolls to show it last.
    v.input(&prefixed(b"("));
    wait_card(&mut v, 14, "bash-4");
    wait_card(&mut v, 2, "bash-1");

    for _ in 0..5 {
        v.input(&sgr(WHEEL_UP, 5, 10, true));
    }
    wait_card(&mut v, 2, "fleetmux-demo");
    v.input(&prefixed(b"c"));
    wait_card(&mut v, 14, "bash-6");
    wait_card(&mut v, 2, "bash-3");

    v.resize(W, 40);
    wait_card(&mut v, 2, "fleetmux-demo");
    wait_card(&mut v, 26, "bash-6");
    let col: String = (2..39).map(|r| v.cell(r, 33).ch).collect();
    assert!(!col.contains('┃'), "every card fits: no scrollbar; column 33 is {col:?}");
}
