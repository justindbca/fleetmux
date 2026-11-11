//! M4 fleet pages (design-1 `m4-design.md` v0.7 §4.2 control-protocol increments, §4.3 R1–R14, §5.3 jumps, §5.4
//! read-only pages; test list §8.1). Phase-A **red tests**: they talk to the server only through the control socket
//! (JSON) and raw view connections (bytes), never through a Rust symbol M4 has not written, so this bin compiles on
//! `a88859ab` and every test fails by behaviour (`unknown_cmd` for `page.*`, no `pages` / `page` echo, a pane landing
//! in the root page, …). All are `#[ignore = "m4"]`; the `m4` gate runs them with `--include-ignored`.
//!
//! Evidence is never deleted: servers run under `$FM4_TEST_ROOT/<pid>-<seq>/` through `m4keep::Kept`
//! (`tests/common/mod.rs` is used unchanged; its `TempDir` cleanup is not reused).
mod common;
mod m4keep;

use common::*;
use m4keep::*;

const SLEEP: &[&str] = &["sleep", "1000"];

// ------------------------------------------------------------------ hello / server.json (§4.2)

#[test]
#[ignore = "m4"]
fn hello_with_a_page_binds_the_connection_and_echoes_it() {
    let srv = Kept::start("hello_with_a_page_binds_the_connection_and_echoes_it");
    open(&srv, "M0", "M0");
    let mut c = srv.client();
    let r = c.ok("hello", json!({"page": "M0"}));
    assert_eq!(
        r["pages"],
        json!(1),
        "hello{{page}} reply lacks pages:1: {r} {}",
        srv.at()
    );
    assert_eq!(
        r["page"],
        json!("M0"),
        "hello{{page}} does not echo the page: {r} {}",
        srv.at()
    );
    let root = srv.ok("hello", json!({}));
    assert_eq!(root["pages"], json!(1), "{root} {}", srv.at());
    assert_eq!(
        root["page"],
        json!(""),
        "unbound hello must say page:\"\": {root} {}",
        srv.at()
    );
}

#[test]
#[ignore = "m4"]
fn a_second_hello_with_another_page_is_bad_request() {
    let srv = Kept::start("a_second_hello_with_another_page_is_bad_request");
    open(&srv, "M0", "M0");
    open(&srv, "M1", "M1");
    let mut c = bind(&srv, "M0");
    expect_code(&mut c, &srv, "hello", json!({"page": "M1"}), "bad_request");
    // The connection stays bound to M0.
    let r = c.ok("hello", json!({}));
    assert_eq!(r["page"], json!("M0"), "{r} {}", srv.at());
}

#[test]
#[ignore = "m4"]
fn server_json_reports_pages() {
    let srv = Kept::start("server_json_reports_pages");
    let path = srv.runtime.join("server.json");
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    let v: Value = serde_json::from_str(&text).expect("server.json is JSON");
    assert_eq!(
        v["pages"],
        json!(1),
        "server.json must carry \"pages\": 1 (design §4.2): {text} {}",
        srv.at()
    );
}

// ------------------------------------------------------------------ page.open / list / close (§4.2)

#[test]
#[ignore = "m4"]
fn page_open_list_close_roundtrip() {
    let srv = Kept::start("page_open_list_close_roundtrip");
    open(&srv, "M0", "child M0");
    let list = page_list(&srv);
    assert_eq!(list.len(), 2, "root + M0 expected: {list:?} {}", srv.at());
    assert_eq!(list[0]["page"], json!(""), "root page first: {list:?}");
    let m0 = page_entry(&srv, "M0");
    assert_eq!(
        (m0["state"].as_str(), m0["title"].as_str()),
        (Some("live"), Some("child M0")),
        "{m0}"
    );
    assert_eq!(m0["panes"], json!(0), "{m0}");
    assert_eq!(
        list[0]["current"],
        json!(true),
        "root is current at start: {list:?}"
    );

    srv.ok("page.close", json!({"page": "M0"}));
    let m0 = page_entry(&srv, "M0");
    assert_eq!(
        m0["state"],
        json!("reclaimed"),
        "page.close keeps the page as reclaimed: {m0} {}",
        srv.at()
    );

    srv.ok("page.close", json!({"page": "M0", "keep": false}));
    let list = page_list(&srv);
    assert_eq!(
        list.len(),
        1,
        "keep:false removes the page: {list:?} {}",
        srv.at()
    );
}

#[test]
#[ignore = "m4"]
fn page_close_with_panes_is_not_empty() {
    let srv = Kept::start("page_close_with_panes_is_not_empty");
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    let w = spawn_id_on(&srv, &mut c, "fx-w", "worker");
    let mut root = srv.client();
    expect_code(
        &mut root,
        &srv,
        "page.close",
        json!({"page": "M0"}),
        "not_empty",
    );
    assert_eq!(
        page_entry(&srv, "M0")["state"],
        json!("live"),
        "{}",
        srv.at()
    );
    assert!(
        ids(&list_on(&mut c, json!({}))).contains(&w),
        "{}",
        srv.at()
    );
}

#[test]
#[ignore = "m4"]
fn closing_the_current_page_for_good_returns_to_the_root() {
    let srv = Kept::start("closing_the_current_page_for_good_returns_to_the_root");
    open(&srv, "M0", "M0");
    srv.ok("page.select", json!({"page": "M0"}));
    wait_current(&srv, "M0");
    srv.ok("page.close", json!({"page": "M0", "keep": false}));
    wait_current(&srv, "");
}

#[test]
#[ignore = "m4"]
fn page_open_rejects_bad_ids_and_the_root() {
    let srv = Kept::start("page_open_rejects_bad_ids_and_the_root");
    let mut c = srv.client();
    let long65 = "a".repeat(65);
    for bad in ["", "a b", "M0 (M0-attempt-2)", "x/y", long65.as_str()] {
        expect_code(
            &mut c,
            &srv,
            "page.open",
            json!({"page": bad, "title": "t"}),
            "bad_request",
        );
    }
    // 64 pages may be opened (a 64-character id is valid), the 65th page.open is refused (R14).
    let long64 = "b".repeat(64);
    open(&srv, &long64, "long");
    for i in 1..64 {
        open(&srv, &format!("M{i}"), "t");
    }
    expect_code(
        &mut c,
        &srv,
        "page.open",
        json!({"page": "M64", "title": "t"}),
        "bad_request",
    );
}

#[test]
#[ignore = "m4"]
fn page_open_reclaimed_sets_the_page_history_url() {
    let srv = Kept::start("page_open_reclaimed_sets_the_page_history_url");
    let url = history_page(&srv, "sub/M0");
    open_reclaimed(&srv, "M0", "memx", &url);
    assert_eq!(
        page_entry(&srv, "M0")["state"],
        json!("reclaimed"),
        "{}",
        srv.at()
    );
    let mut c = bind(&srv, "M0");
    assert_eq!(
        c.ok("kv.get", json!({"key": "history_url"}))["value"],
        json!(url),
        "{}",
        srv.at()
    );
    assert_eq!(
        srv.ok("kv.get", json!({"key": "history_url"}))["value"],
        Value::Null,
        "the child page's history_url leaked into the root page {}",
        srv.at()
    );
}

// ------------------------------------------------------------------ pane.spawn / pane.list per page

#[test]
#[ignore = "m4"]
fn pane_spawn_on_a_page_connection_lands_in_that_page_only() {
    let srv = Kept::start("pane_spawn_on_a_page_connection_lands_in_that_page_only");
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    let w = spawn_id_on(&srv, &mut c, "fx-child-w", "worker");
    assert!(
        !ids(&srv.panes()).contains(&w),
        "root pane.list lists a child-page pane {}",
        srv.at()
    );
    let mine = list_on(&mut c, json!({}));
    let rec = mine
        .iter()
        .find(|p| p["pane"] == json!(w))
        .unwrap_or_else(|| panic!("{mine:?} {}", srv.at()));
    assert_eq!(rec["page"], json!("M0"), "{rec}");
    assert!(
        !all_layout_ids(&srv.layout()).contains(&w),
        "child pane in the root layout {}",
        srv.at()
    );
}

#[test]
#[ignore = "m4"]
fn pane_spawn_with_an_explicit_page_lands_there() {
    let srv = Kept::start("pane_spawn_with_an_explicit_page_lands_there");
    open(&srv, "M0", "M0");
    let mut root = srv.client();
    let h = spawn_on(
        &srv,
        &mut root,
        "history",
        "history",
        SLEEP,
        json!({"page": "M0"}),
    )["pane"]
        .as_str()
        .unwrap()
        .to_string();
    assert!(
        !ids(&srv.panes()).contains(&h),
        "pane.spawn{{page:M0}} landed in the root page {}",
        srv.at()
    );
    assert_eq!(srv.pane(&h)["page"], json!("M0"), "{}", srv.at());
    let mut c = bind(&srv, "M0");
    assert!(
        ids(&list_on(&mut c, json!({}))).contains(&h),
        "{}",
        srv.at()
    );
}

#[test]
#[ignore = "m4"]
fn pane_spawn_on_a_bound_connection_creates_the_page() {
    let srv = Kept::start("pane_spawn_on_a_bound_connection_creates_the_page");
    let mut c = bind(&srv, "M0"); // not opened yet
    let d = spawn_id_on(&srv, &mut c, "daemon", "daemon");
    let m0 = page_entry(&srv, "M0");
    assert_eq!(
        (m0["state"].as_str(), m0["title"].as_str()),
        (Some("live"), Some("M0")),
        "{m0} {}",
        srv.at()
    );
    open(&srv, "M0", "memx");
    let m0 = page_entry(&srv, "M0");
    assert_eq!(
        (m0["state"].as_str(), m0["title"].as_str()),
        (Some("live"), Some("memx")),
        "{m0}"
    );
    assert_eq!(
        ids(&list_on(&mut c, json!({}))),
        vec![d],
        "page.open must only rename {}",
        srv.at()
    );
}

#[test]
#[ignore = "m4"]
fn pane_list_on_a_page_that_does_not_exist_yet_is_empty() {
    let srv = Kept::start("pane_list_on_a_page_that_does_not_exist_yet_is_empty");
    srv.spawn_id("fx-root-w", "worker", SLEEP);
    let mut c = bind(&srv, "M7");
    assert_eq!(
        list_on(&mut c, json!({})),
        Vec::<Value>::new(),
        "{}",
        srv.at()
    );
    let l = c.ok("layout.get", json!({}));
    assert!(
        all_layout_ids(&l).is_empty(),
        "layout of a page not built yet must be empty: {l}"
    );
    assert_eq!(c.ok("kv.get", json!({"key": "run"}))["value"], Value::Null);
    // The connection is still usable.
    assert_eq!(list_on(&mut c, json!({})), Vec::<Value>::new());
}

#[test]
#[ignore = "m4"]
fn pane_spawn_with_an_explicit_unknown_page_is_bad_request() {
    let srv = Kept::start("pane_spawn_with_an_explicit_unknown_page_is_bad_request");
    let mut c = srv.client();
    let r = expect_code(
        &mut c,
        &srv,
        "pane.spawn",
        json!({"argv": SLEEP, "type": "worker", "page": "nope"}),
        "bad_request",
    );
    assert!(
        r["error"].as_str().unwrap_or("").contains("no such page"),
        "{r}"
    );
    let r = expect_code(
        &mut c,
        &srv,
        "page.select",
        json!({"page": "nope"}),
        "bad_request",
    );
    assert!(
        r["error"].as_str().unwrap_or("").contains("no such page"),
        "{r}"
    );
}

#[test]
#[ignore = "m4"]
fn pane_list_all_lists_every_page() {
    let srv = Kept::start("pane_list_all_lists_every_page");
    let a = srv.spawn_id("fx-root-w", "worker", SLEEP);
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    let b = spawn_id_on(&srv, &mut c, "fx-child-w", "worker");
    let mut root = srv.client();
    let all = list_on(&mut root, json!({"all": true}));
    let page_of = |id: &str| {
        all.iter()
            .find(|p| p["pane"] == json!(id))
            .map(|p| p["page"].clone())
    };
    assert_eq!(page_of(&a), Some(json!("")), "{all:?} {}", srv.at());
    assert_eq!(page_of(&b), Some(json!("M0")), "{all:?} {}", srv.at());
    assert!(
        !ids(&list_on(&mut root, json!({}))).contains(&b),
        "{}",
        srv.at()
    );
}

#[test]
#[ignore = "m4"]
fn pane_get_in_a_background_page_reports_its_group() {
    let srv = Kept::start("pane_get_in_a_background_page_reports_its_group");
    let rw = srv.spawn_id("fx-root-w", "worker", SLEEP);
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    let _sup = spawn_id_on(&srv, &mut c, "fx-sup", "supervisor");
    let _w1 = spawn_id_on(&srv, &mut c, "fx-w1", "worker");
    let w2 = spawn_id_on(&srv, &mut c, "fx-w2", "worker");
    let g = srv.pane(&w2);
    assert_eq!(
        (g["page"].as_str(), g["group"].as_str(), g["index"].as_u64()),
        (Some("M0"), Some("workers"), Some(1)),
        "group/index must come from the pane's own page: {g} {}",
        srv.at()
    );
    assert_eq!(srv.pane(&rw)["index"], json!(0), "{}", srv.at());
}

// ------------------------------------------------------------------ pane.move across pages (§5.2)

#[test]
#[ignore = "m4"]
fn pane_move_pulls_a_pane_from_another_page_and_keeps_its_pid() {
    let srv = Kept::start("pane_move_pulls_a_pane_from_another_page_and_keeps_its_pid");
    let rw = srv.spawn_id("fx-worker-1", "worker", SLEEP);
    let pid = srv.pane(&rw)["pid"].clone();
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    let sup = spawn_id_on(&srv, &mut c, "fx-peer-sup", "supervisor");
    c.ok(
        "pane.move",
        json!({"pane": rw, "group": "supervisor", "before": sup}),
    );
    let g = srv.pane(&rw);
    assert_eq!(
        g["page"],
        json!("M0"),
        "moved pane is not in the child page: {g} {}",
        srv.at()
    );
    assert_eq!(g["pid"], pid, "the move changed the pane's process: {g}");
    assert!(
        !all_layout_ids(&srv.layout()).contains(&rw),
        "still in the root layout {}",
        srv.at()
    );
    let cl = c.ok("layout.get", json!({}));
    assert_eq!(
        group_of(&cl, "supervisor"),
        vec![rw.clone(), sup.clone()],
        "{cl}"
    );
    assert!(
        pid_alive(pid.as_i64().unwrap() as i32),
        "process gone after the move"
    );
}

#[test]
#[ignore = "m4"]
fn pane_move_pulls_a_pane_of_the_same_group_from_another_page() {
    let srv = Kept::start("pane_move_pulls_a_pane_of_the_same_group_from_another_page");
    let rw = srv.spawn_id("fx-worker-1", "worker", SLEEP);
    let pid = srv.pane(&rw)["pid"].clone();
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    let peer = spawn_id_on(&srv, &mut c, "fx-peer", "worker");
    c.ok(
        "pane.move",
        json!({"pane": rw, "group": "workers", "before": peer}),
    );
    let g = srv.pane(&rw);
    assert_eq!(
        (g["page"].clone(), g["pid"].clone()),
        (json!("M0"), pid),
        "{g} {}",
        srv.at()
    );
    let cl = c.ok("layout.get", json!({}));
    assert_eq!(
        group_of(&cl, "workers"),
        vec![rw.clone(), peer],
        "{cl} {}",
        srv.at()
    );
    assert!(
        !group_of(&srv.layout(), "workers").contains(&rw),
        "{}",
        srv.at()
    );
}

#[test]
#[ignore = "m4"]
fn pane_move_with_an_anchor_outside_the_connection_page_is_bad_request() {
    let srv = Kept::start("pane_move_with_an_anchor_outside_the_connection_page_is_bad_request");
    let ra = srv.spawn_id("fx-root-a", "worker", SLEEP);
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    let cw = spawn_id_on(&srv, &mut c, "fx-child", "worker");
    expect_code(
        &mut c,
        &srv,
        "pane.move",
        json!({"pane": cw, "group": "workers", "before": ra}),
        "bad_request",
    );
    assert_eq!(srv.pane(&cw)["page"], json!("M0"), "{}", srv.at());
    assert_eq!(
        group_of(&c.ok("layout.get", json!({})), "workers"),
        vec![cw]
    );
}

// ------------------------------------------------------------------ current page, viewers, sizes (§4.3 R2/R5/R6)

#[test]
#[ignore = "m4"]
fn page_select_switches_what_every_viewer_sees() {
    let srv = Kept::start("page_select_switches_what_every_viewer_sees");
    sh(
        &srv,
        "fx-root-sup",
        "supervisor",
        "echo ROOT-BODY; exec sleep 1000",
    );
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    sh_on(&srv, &mut c, "fx-child-sup", "supervisor", "CHILD-BODY");
    let mut v1 = Viewer::attach(&srv, W, H);
    let mut v2 = Viewer::attach(&srv, W, H);
    for v in [&mut v1, &mut v2] {
        v.wait_text("ROOT-BODY", T);
    }
    srv.ok("page.select", json!({"page": "M0"}));
    for v in [&mut v1, &mut v2] {
        v.wait_text("CHILD-BODY", T);
        v.wait_no_text("ROOT-BODY", T);
    }
    srv.ok("page.select", json!({"page": ""}));
    for v in [&mut v1, &mut v2] {
        v.wait_text("ROOT-BODY", T);
        v.wait_no_text("CHILD-BODY", T);
    }
}

#[test]
#[ignore = "m4"]
fn pane_focus_never_changes_the_current_page() {
    let srv = Kept::start("pane_focus_never_changes_the_current_page");
    srv.spawn_id("fx-root-sup", "supervisor", SLEEP);
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    let _w1 = spawn_id_on(&srv, &mut c, "fx-w1", "worker");
    let w2 = spawn_id_on(&srv, &mut c, "fx-w2", "worker");
    let root_focus = srv.layout()["focus"].clone();
    srv.ok("pane.focus", json!({"pane": w2}));
    assert_eq!(
        current(&srv),
        "",
        "pane.focus switched the current page {}",
        srv.at()
    );
    let cl = c.ok("layout.get", json!({}));
    assert_eq!(
        (cl["selected"]["workers"].clone(), cl["focus"].clone()),
        (json!(w2), json!("workers")),
        "{cl}"
    );
    assert_eq!(
        srv.layout()["focus"],
        root_focus,
        "root focus moved {}",
        srv.at()
    );
}

#[test]
#[ignore = "m4"]
fn background_page_ptys_follow_the_shared_size() {
    let srv = Kept::start("background_page_ptys_follow_the_shared_size");
    srv.spawn_id("fx-root-sup", "supervisor", SLEEP);
    let rw = srv.spawn_id("fx-root-w", "worker", SLEEP);
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    let cw = spawn_id_on(&srv, &mut c, "fx-child-w", "worker");
    assert_eq!(srv.pane(&cw)["page"], json!("M0"), "{}", srv.at());
    let mut v = Viewer::attach(&srv, W, H);
    wait_until("both lone workers sized alike", T, || {
        (pane_size(&srv, &cw) == pane_size(&srv, &rw)).then_some(())
    });
    let before = pane_size(&srv, &cw);
    v.resize(200, 50);
    wait_until("background-page worker resized with the viewer", T, || {
        let (a, b) = (pane_size(&srv, &cw), pane_size(&srv, &rw));
        (a != before && a == b).then_some(())
    });
}

#[test]
#[ignore = "m4"]
fn layout_is_per_page_and_ratios_are_shared() {
    let srv = Kept::start("layout_is_per_page_and_ratios_are_shared");
    let rs = srv.spawn_id("fx-root-sup", "supervisor", SLEEP);
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    let cs = spawn_id_on(&srv, &mut c, "fx-child-sup", "supervisor");
    c.ok("layout.set", json!({"h_ratio": 70}));
    let rl = srv.layout();
    let cl = c.ok("layout.get", json!({}));
    assert_eq!(
        rl["h_ratio"],
        json!(70),
        "ratios are shared across pages: {rl} {}",
        srv.at()
    );
    assert_eq!(group_of(&rl, "supervisor"), vec![rs.clone()], "{rl}");
    assert_eq!(group_of(&cl, "supervisor"), vec![cs.clone()], "{cl}");
    let path = srv.runtime.join("layout.json");
    wait_until(
        "layout.json with h_ratio 70 and only root groups",
        T,
        || {
            let v: Value = serde_json::from_str(&std::fs::read_to_string(&path).ok()?).ok()?;
            (v["h_ratio"] == json!(70) && group_of(&v, "supervisor") == vec![rs.clone()])
                .then_some(())
        },
    );
}

#[test]
#[ignore = "m4"]
fn kv_is_per_page_and_reserved_keys_are_root_only() {
    let srv = Kept::start("kv_is_per_page_and_reserved_keys_are_root_only");
    open(&srv, "M0", "M0");
    srv.ok(
        "kv.set",
        json!({"key": "history_url", "value": "file:///r/root/history.html"}),
    );
    let mut c = bind(&srv, "M0");
    c.ok(
        "kv.set",
        json!({"key": "history_url", "value": "file:///r/root/sub/M0/history.html"}),
    );
    assert_eq!(
        srv.ok("kv.get", json!({"key": "history_url"}))["value"],
        json!("file:///r/root/history.html"),
        "{}",
        srv.at()
    );
    assert_eq!(
        c.ok("kv.get", json!({"key": "history_url"}))["value"],
        json!("file:///r/root/sub/M0/history.html")
    );
    for key in ["sidebar", "stop_request", "restorable", "sidebar_width"] {
        expect_code(
            &mut c,
            &srv,
            "kv.set",
            json!({"key": key, "value": "x"}),
            "bad_request",
        );
        expect_code(&mut c, &srv, "kv.get", json!({"key": key}), "bad_request");
    }
    // The root connection is as in M3.
    srv.ok("kv.set", json!({"key": "sidebar_width", "value": "40"}));
    assert_eq!(
        srv.ok("kv.get", json!({"key": "sidebar_width"}))["value"],
        json!("40")
    );
    assert_eq!(
        srv.ok("kv.get", json!({"key": "never-set"}))["value"],
        Value::Null
    );
}

// ------------------------------------------------------------------ shutdown (§0 item 5, R12)

#[test]
#[ignore = "m4"]
fn server_shutdown_from_a_page_connection_is_refused() {
    let srv = Kept::start("server_shutdown_from_a_page_connection_is_refused");
    let rs = srv.spawn_id("fx-root-sup", "supervisor", SLEEP);
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    expect_code(
        &mut c,
        &srv,
        "server.shutdown",
        json!({"force": true}),
        "bad_request",
    );
    expect_code(&mut c, &srv, "server.shutdown", json!({}), "bad_request");
    assert!(
        srv.alive(),
        "a page connection shut the server down {}",
        srv.at()
    );
    assert!(srv.listed(&rs), "root pane gone {}", srv.at());
}

#[test]
#[ignore = "m4"]
fn shutdown_force_kills_every_page() {
    let srv = Kept::start("shutdown_force_kills_every_page");
    let ra = srv.spawn("fx-root-w", "worker", SLEEP, json!({}))["pid"]
        .as_i64()
        .unwrap() as i32;
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    let cb = spawn_on(&srv, &mut c, "fx-child-w", "worker", SLEEP, json!({}))["pid"]
        .as_i64()
        .unwrap() as i32;
    srv.ok("server.shutdown", json!({"force": true}));
    wait_until(
        "every page's process reaped",
        Duration::from_secs(10),
        || (!pid_alive(ra) && !pid_alive(cb)).then_some(()),
    );
}

#[test]
#[ignore = "m4"]
fn reaper_counts_panes_in_every_page() {
    let srv = Kept::start("reaper_counts_panes_in_every_page");
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    spawn_id_on(&srv, &mut c, "fx-child-w", "worker");
    // A history-only sidebar (no running card) makes the server reapable once it holds no pane (S2b).
    srv.ok(
        "kv.set",
        json!({"key": "sidebar", "value": sidebar_json("f-closed", &[("f-closed", "old", "closed", None)])}),
    );
    assert!(
        srv.panes().is_empty(),
        "root page should be empty here {}",
        srv.at()
    );
    std::thread::sleep(Duration::from_secs(13));
    assert!(
        srv.alive(),
        "the reaper ignored a pane in a child page and exited {}",
        srv.at()
    );
}

// ------------------------------------------------------------------ web.* per page (§4.2)

#[test]
#[ignore = "m4"]
fn web_url_from_a_non_current_page_is_no_window() {
    let srv = Kept::start("web_url_from_a_non_current_page_is_no_window");
    open(&srv, "M0", "M0");
    let mut wv = Raw::attach(&srv, json!({}));
    wv.window_open();
    let mut c = bind(&srv, "M0");
    expect_code(&mut c, &srv, "web.url", json!({}), "no_window");
    // The root page is current: its web.url is relayed as in M2.5.
    let pending = request_async(&srv, "web.url", json!({}));
    let cmd = wv
        .next_json(0x84, T, |v| v["op"] == json!("url"))
        .unwrap_or_else(|| panic!("root web.url not relayed {}", srv.at()));
    wv.send_json(json!({"seq": cmd["seq"], "ok": true, "url": "file:///r/root/history.html"}));
    let r = pending.join().unwrap();
    assert_eq!(r["url"], json!("file:///r/root/history.html"), "{r}");
}

#[test]
#[ignore = "m4"]
fn web_goto_from_a_non_current_page_is_not_relayed() {
    let srv = Kept::start("web_goto_from_a_non_current_page_is_not_relayed");
    open(&srv, "M0", "M0");
    let mut wv = Raw::attach(&srv, json!({}));
    wv.window_open();
    let c = bind(&srv, "M0");
    let pending = request_async_on(
        c,
        "web.goto",
        json!({"url": "file:///r/root/sub/M0/history.html"}),
    );
    let relayed = wv.next_json(0x84, Duration::from_millis(800), |_| true);
    if let Some(cmd) = &relayed {
        wv.send_json(json!({"seq": cmd["seq"], "ok": true}));
    }
    let r = pending.join().unwrap();
    assert!(
        relayed.is_none(),
        "a background page's web.goto reached the window: {relayed:?} {}",
        srv.at()
    );
    assert_eq!(
        (r["ok"].clone(), r["windows"].clone()),
        (json!(true), json!(0)),
        "{r}"
    );
}

#[test]
#[ignore = "m4"]
fn overlay_url_follows_the_selected_page() {
    let srv = Kept::start("overlay_url_follows_the_selected_page");
    let root_url = history_page(&srv, "");
    let child_url = history_page(&srv, "sub/M0");
    srv.spawn_id("history", "history", SLEEP);
    srv.ok("kv.set", json!({"key": "history_url", "value": root_url}));
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    spawn_id_on(&srv, &mut c, "history", "history");
    c.ok("kv.set", json!({"key": "history_url", "value": child_url}));
    let mut gui = Raw::attach(&srv, json!({"overlay": 1}));
    gui.next_json(0x85, T, |o| o["url"] == json!(root_url))
        .unwrap_or_else(|| panic!("first 0x85 is not the root page's url {}", srv.at()));
    srv.ok("page.select", json!({"page": "M0"}));
    gui.next_json(0x85, T, |o| o["url"] == json!(child_url))
        .unwrap_or_else(|| panic!("0x85 url did not follow page.select {}", srv.at()));
}

#[test]
#[ignore = "m4"]
fn page_select_pushes_the_page_history_url_to_web_windows() {
    let srv = Kept::start("page_select_pushes_the_page_history_url_to_web_windows");
    let child_url = history_page(&srv, "sub/M0");
    open_reclaimed(&srv, "M0", "memx", &child_url);
    let mut wv = Raw::attach(&srv, json!({}));
    wv.window_open();
    let pending = request_async(&srv, "page.select", json!({"page": "M0"}));
    let cmd = wv.next_json(0x84, T, |v| v["op"] == json!("goto"));
    if let Some(cmd) = &cmd {
        wv.send_json(json!({"seq": cmd["seq"], "ok": true}));
    }
    let r = pending.join().unwrap();
    assert_eq!(r["ok"], json!(true), "page.select failed: {r} {}", srv.at());
    let cmd = cmd.unwrap_or_else(|| panic!("no 0x84 goto after page.select {}", srv.at()));
    assert_eq!(cmd["url"], json!(child_url), "{cmd}");
}

// ------------------------------------------------------------------ view handshake and jumps (§4.2, R13, §5.3)

#[test]
#[ignore = "m4"]
fn viewer_ok_line_declares_pages() {
    let srv = Kept::start("viewer_ok_line_declares_pages");
    let v = Raw::attach(&srv, json!({}));
    assert_eq!(
        v.reply["pages"],
        json!(1),
        "view ok line lacks \"pages\":1: {} {}",
        v.reply,
        srv.at()
    );
}

fn jump_url(from: &str, to: &str) -> String {
    format!("{from}?fleet-jump=k7q2x-ab12&rel=child&to={to}")
}

#[test]
#[ignore = "m4"]
fn jump_event_selects_the_page_whose_history_url_matches() {
    let srv = Kept::start("jump_event_selects_the_page_whose_history_url_matches");
    let root_url = history_page(&srv, "");
    let child_url = history_page(&srv, "sub/M0");
    srv.ok("kv.set", json!({"key": "history_url", "value": root_url}));
    open_reclaimed(&srv, "M0", "memx", &child_url);
    let mut wv = Raw::attach(&srv, json!({}));
    wv.window_open();
    wv.send_json(json!({"event": "jump", "url": jump_url(&root_url, &child_url)}));
    let cmd = wv.next_json(0x84, T, |v| v["op"] == json!("goto"));
    if let Some(cmd) = &cmd {
        wv.send_json(json!({"seq": cmd["seq"], "ok": true}));
    }
    wait_current(&srv, "M0");
    let cmd = cmd.unwrap_or_else(|| panic!("no 0x84 goto after the jump {}", srv.at()));
    assert_eq!(cmd["url"], json!(child_url), "{cmd}");
}

/// A reclaimed child page has no daemon and no pane; the jump back to the parent is done by the server alone
/// (design §0 item 6, §5.3: "不需要任何 daemon 活着").
#[test]
#[ignore = "m4"]
fn jump_from_a_reclaimed_page_back_to_the_root_needs_no_daemon() {
    let srv = Kept::start("jump_from_a_reclaimed_page_back_to_the_root_needs_no_daemon");
    let root_url = history_page(&srv, "");
    let child_url = history_page(&srv, "sub/M0");
    srv.ok("kv.set", json!({"key": "history_url", "value": root_url}));
    open_reclaimed(&srv, "M0", "memx", &child_url);
    assert_eq!(page_entry(&srv, "M0")["panes"], json!(0));
    srv.ok("page.select", json!({"page": "M0"}));
    wait_current(&srv, "M0");
    let mut wv = Raw::attach(&srv, json!({}));
    wv.window_open();
    wv.send_json(json!({"event": "jump", "url": jump_url(&child_url, &root_url).replace("rel=child", "rel=parent")}));
    wait_current(&srv, "");
}

#[test]
#[ignore = "m4"]
fn jump_event_to_an_unknown_path_does_nothing() {
    let srv = Kept::start("jump_event_to_an_unknown_path_does_nothing");
    root_hello(&srv);
    let root_url = history_page(&srv, "");
    srv.ok("kv.set", json!({"key": "history_url", "value": root_url}));
    open_reclaimed(&srv, "M0", "memx", &history_page(&srv, "sub/M0"));
    let mut wv = Raw::attach(&srv, json!({}));
    wv.window_open();
    wv.send_json(
        json!({"event": "jump", "url": jump_url(&root_url, "file:///elsewhere/history.html")}),
    );
    std::thread::sleep(Duration::from_millis(500));
    assert_eq!(current(&srv), "", "{}", srv.at());
    // Still connected: a root web.goto still reaches this window.
    let pending = request_async(&srv, "web.goto", json!({"url": root_url}));
    let cmd = wv
        .next_json(0x84, T, |v| v["op"] == json!("goto"))
        .unwrap_or_else(|| panic!("window connection lost after an unknown jump {}", srv.at()));
    wv.send_json(json!({"seq": cmd["seq"], "ok": true}));
    assert_eq!(pending.join().unwrap()["ok"], json!(true));
}

// ------------------------------------------------------------------ read-only pages (§5.4, R7)

#[test]
#[ignore = "m4"]
fn history_pane_does_not_reopen_a_reclaimed_page() {
    let srv = Kept::start("history_pane_does_not_reopen_a_reclaimed_page");
    open_reclaimed(&srv, "M0", "memx", &history_page(&srv, "sub/M0"));
    let mut root = srv.client();
    spawn_on(
        &srv,
        &mut root,
        "history_view",
        "history",
        SLEEP,
        json!({"page": "M0"}),
    );
    assert_eq!(
        page_entry(&srv, "M0")["state"],
        json!("reclaimed"),
        "{}",
        srv.at()
    );
    spawn_on(
        &srv,
        &mut root,
        "fx-w",
        "worker",
        SLEEP,
        json!({"page": "M0"}),
    );
    assert_eq!(
        page_entry(&srv, "M0")["state"],
        json!("live"),
        "{}",
        srv.at()
    );
}

#[test]
#[ignore = "m4"]
fn reclaimed_page_draws_the_read_only_banner() {
    let srv = Kept::start("reclaimed_page_draws_the_read_only_banner");
    srv.spawn_id("fx-root-sup", "supervisor", SLEEP);
    open_reclaimed(&srv, "M0", "memx", &history_page(&srv, "sub/M0"));
    let mut v = Viewer::attach(&srv, W, H);
    srv.ok("page.select", json!({"page": "M0"}));
    v.wait_text("已收回 · 只读", T);
}

// ------------------------------------------------------------------ sidebar S3 (R8–R10)

/// Root supervisor printing ROOT-BODY, child page M0 (and M1 when `two`) each with a supervisor printing its body,
/// a sidebar whose child cards carry `page`, and an attached viewer showing it.
fn s3_world(name: &str, two: bool) -> (Kept, Viewer) {
    let srv = Kept::start(name);
    sh(
        &srv,
        "fx-root-sup",
        "supervisor",
        "echo ROOT-BODY; exec sleep 1000",
    );
    open(&srv, "M0", "child-m0");
    let mut c = bind(&srv, "M0");
    sh_on(&srv, &mut c, "fx-m0-sup", "supervisor", "M0-BODY");
    let mut cards = vec![
        ("f-root", "root-fleet", "running", Some("")),
        ("f-m0", "child-m0", "running", Some("M0")),
    ];
    if two {
        open(&srv, "M1", "child-m1");
        let mut c1 = bind(&srv, "M1");
        sh_on(&srv, &mut c1, "fx-m1-sup", "supervisor", "M1-BODY");
        cards.push(("f-m1", "child-m1", "running", Some("M1")));
    }
    let mut v = Viewer::attach(&srv, W, H);
    v.wait_text("ROOT-BODY", T);
    srv.ok(
        "kv.set",
        json!({"key": "sidebar", "value": sidebar_json("f-root", &cards)}),
    );
    v.wait_text("child-m0", T);
    (srv, v)
}

#[test]
#[ignore = "m4"]
fn sidebar_pick_selects_the_page_of_the_card() {
    let (srv, mut v) = s3_world("sidebar_pick_selects_the_page_of_the_card", false);
    let (row, col) = v.wait_find("child-m0", T);
    v.input(&sgr_click(col, row, 0));
    wait_current(&srv, "M0");
    v.wait_text("M0-BODY", T);
    let status = v.status_line();
    assert!(
        !status.contains("S3 开放"),
        "the S3 placeholder notice is still shown: {status:?} {}",
        srv.at()
    );
}

#[test]
#[ignore = "m4"]
fn sidebar_prev_next_walk_the_pages() {
    let (srv, mut v) = s3_world("sidebar_prev_next_walk_the_pages", true);
    v.input(&prefixed(b")"));
    wait_current(&srv, "M0");
    v.input(&prefixed(b")"));
    wait_current(&srv, "M1");
    v.input(&prefixed(b"("));
    wait_current(&srv, "M0");
    v.input(&prefixed(b"("));
    wait_current(&srv, "");
    let status = v.status_line();
    assert!(!status.contains("S3 开放"), "{status:?} {}", srv.at());
}

/// Columns of a sidebar card row that the S3 checks read: the sidebar's default width (`SIDEBAR_W`, 34).
const CARD_COLS: u16 = 34;

/// Where the sidebar card titled `title` starts: only rows below row 0 (the top bar, which also shows the current
/// fleet's name) and only the sidebar's columns, so the top bar is never taken for the root card.
fn wait_card(v: &mut Viewer, title: &str, timeout: Duration) -> (u16, u16) {
    let want: Vec<char> = title.chars().collect();
    let end = std::time::Instant::now() + timeout;
    loop {
        for row in 1..H {
            let cells: Vec<(u16, char)> = (0..CARD_COLS)
                .map(|c| (c, v.cell(row, c)))
                .filter(|(_, cell)| cell.width != 0)
                .map(|(c, cell)| (c, cell.ch))
                .collect();
            if let Some(i) = cells
                .windows(want.len().max(1))
                .position(|w| w.iter().map(|(_, ch)| *ch).eq(want.iter().copied()))
            {
                return (row, cells[i].0);
            }
        }
        if std::time::Instant::now() >= end {
            panic!("no sidebar card titled {title:?} within {timeout:?}; screen:\n{}", v.text());
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[test]
#[ignore = "m4"]
fn sidebar_highlight_and_topbar_follow_the_current_page_not_kv_current() {
    let (srv, mut v) = s3_world(
        "sidebar_highlight_and_topbar_follow_the_current_page_not_kv_current",
        false,
    );
    let (r_root, c_root) = wait_card(&mut v, "root-fleet", T);
    let (r_m0, c_m0) = wait_card(&mut v, "child-m0", T);
    // kv current = the root card: the root card is highlighted.
    let sel_bg = v.cell(r_root, c_root).bg;
    let other_bg = v.cell(r_m0, c_m0).bg;
    assert_ne!(
        sel_bg,
        other_bg,
        "precondition: highlighted and plain cards differ {}",
        srv.at()
    );
    srv.ok("page.select", json!({"page": "M0"}));
    wait_current(&srv, "M0");
    wait_until(
        "the highlight moves to the M0 card although kv current names the root",
        T,
        || (v.cell(r_m0, c_m0).bg == sel_bg && v.cell(r_root, c_root).bg == other_bg).then_some(()),
    );
    let top = v.row_text(0);
    assert!(
        top.contains("child-m0"),
        "topbar does not show the current page's fleet: {top:?} {}",
        srv.at()
    );
}

#[test]
#[ignore = "m4"]
fn child_cards_have_no_close_button() {
    let (srv, mut v) = s3_world("child_cards_have_no_close_button", false);
    let (r_root, _) = wait_card(&mut v, "root-fleet", T);
    let (r_m0, _) = wait_card(&mut v, "child-m0", T);
    let root: String = (0..CARD_COLS).map(|c| v.cell(r_root, c).ch).collect();
    assert!(
        root.contains('×'),
        "control: the root card has its ×: {root:?} {}",
        srv.at()
    );
    let child: String = (0..CARD_COLS).map(|c| v.cell(r_m0, c).ch).collect();
    assert!(
        !child.contains('×'),
        "a child card with `page` draws ×: {child:?} {}",
        srv.at()
    );
}

// ------------------------------------------------------------------ old viewers (§4.2 查看协议)

#[test]
#[ignore = "m4"]
fn old_viewer_sees_pages_without_any_new_frame() {
    let srv = Kept::start("old_viewer_sees_pages_without_any_new_frame");
    sh(
        &srv,
        "fx-root-sup",
        "supervisor",
        "echo ROOT-BODY; exec sleep 1000",
    );
    open(&srv, "M0", "M0");
    let mut c = bind(&srv, "M0");
    sh_on(&srv, &mut c, "fx-child-sup", "supervisor", "CHILD-BODY");
    let mut old = Raw::attach(&srv, json!({}));
    old.drain(Duration::from_millis(500));
    for (i, page) in ["M0", "", "M0", ""].iter().enumerate() {
        let before = old.count(0x81);
        srv.ok("page.select", json!({"page": page}));
        old.drain(Duration::from_millis(600));
        assert!(
            old.count(0x81) > before,
            "no redraw after switch {i} {}",
            srv.at()
        );
    }
    assert!(!old.closed, "old viewer disconnected {}", srv.at());
    let unknown: Vec<u8> = old
        .types()
        .into_iter()
        .filter(|t| ![0x81, 0x82, 0x83].contains(t))
        .collect();
    assert!(
        unknown.is_empty(),
        "old viewer got new frame types {unknown:02x?} {}",
        srv.at()
    );
}
