//! Layout model contract (plan.md §3.3; design.md §2.5, §3.1, §3.2, §3.4). Red in phase 0 (`todo!()`),
//! green from M0.

use fleetmux_layout::{Group, Layout, LayoutError, PaneId, PaneType, Pos, Rect};

fn id(s: &str) -> PaneId {
    s.to_string()
}

fn ids(v: &[&str]) -> Vec<PaneId> {
    v.iter().map(|s| s.to_string()).collect()
}

fn near(actual: u16, expected: u16, slack: u16) -> bool {
    actual.abs_diff(expected) <= slack
}

/// Workers group `[worker-1, worker-2, worker-3]`.
fn three_workers() -> Layout {
    let mut l = Layout::new();
    for w in ["worker-1", "worker-2", "worker-3"] {
        assert_eq!(l.insert(id(w), PaneType::Worker), Group::Workers);
    }
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1", "worker-2", "worker-3"]).as_slice()
    );
    l
}

/// A filled run: supervisor + advisor, history + control + one tab, three workers.
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
fn ratios_clamp_to_10_90() {
    let mut l = Layout::new();
    assert_eq!(l.ratios(), (50, 60));
    l.set_ratios(5, 95);
    assert_eq!(l.ratios(), (10, 90));
    l.set_ratios(0, 255);
    assert_eq!(l.ratios(), (10, 90));
    l.set_ratios(91, 9);
    assert_eq!(l.ratios(), (90, 10));
    l.set_ratios(10, 90);
    assert_eq!(l.ratios(), (10, 90));
    l.set_ratios(30, 70);
    assert_eq!(l.ratios(), (30, 70));
}

#[test]
fn reset_restores_50_50_and_60_40() {
    let mut l = full_run();
    let before = l.rects(240, 60);
    l.set_ratios(30, 80);
    assert_eq!(l.ratios(), (30, 80));
    assert_ne!(l.rects(240, 60), before);
    l.reset_ratios();
    assert_eq!(l.ratios(), (50, 60));
    assert_eq!(l.rects(240, 60), before);
}

#[test]
fn zoom_then_unzoom_restores_rects() {
    let mut l = full_run();
    l.set_ratios(40, 70);
    let before = l.rects(200, 50);
    assert_eq!(before.zoom, None);
    assert_eq!(l.zoom(), None);

    l.toggle_zoom(id("worker-2"));
    assert_eq!(l.zoom(), Some(&id("worker-2")));
    let zoomed = l.rects(200, 50);
    assert!(zoomed.zoom.is_some());
    assert_ne!(zoomed, before);

    l.toggle_zoom(id("worker-2"));
    assert_eq!(l.zoom(), None);
    assert_eq!(l.rects(200, 50), before);
    assert_eq!(l.ratios(), (40, 70), "zoom must not touch the ratios");
}

#[test]
fn insert_by_type_table() {
    // design.md §3.4, one assertion block per row.
    let mut l = Layout::new();

    // worker → Workers, last.
    assert_eq!(l.insert(id("worker-1"), PaneType::Worker), Group::Workers);
    assert_eq!(l.insert(id("worker-2"), PaneType::Worker), Group::Workers);
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1", "worker-2"]).as_slice()
    );

    // supervisor → Supervisor, first.
    assert_eq!(
        l.insert(id("supervisor-1"), PaneType::Supervisor),
        Group::Supervisor
    );
    // advisor → Supervisor, last.
    assert_eq!(
        l.insert(id("advisor-1"), PaneType::Advisor),
        Group::Supervisor
    );
    assert_eq!(
        l.group(Group::Supervisor),
        ids(&["supervisor-1", "advisor-1"]).as_slice()
    );
    assert_eq!(
        l.insert(id("supervisor-1.1"), PaneType::Supervisor),
        Group::Supervisor
    );
    assert_eq!(
        l.group(Group::Supervisor),
        ids(&["supervisor-1.1", "supervisor-1", "advisor-1"]).as_slice()
    );

    // tab → Lower, last.
    assert_eq!(l.insert(id("tab-a"), PaneType::Tab), Group::Lower);
    // daemon without a history pane → Lower, first.
    assert_eq!(l.insert(id("control"), PaneType::Daemon), Group::Lower);
    assert_eq!(l.group(Group::Lower), ids(&["control", "tab-a"]).as_slice());
    // history → Lower, first.
    assert_eq!(l.insert(id("history"), PaneType::History), Group::Lower);
    assert_eq!(
        l.group(Group::Lower),
        ids(&["history", "control", "tab-a"]).as_slice()
    );
    // daemon with a history pane → right after history.
    assert_eq!(l.insert(id("control-2"), PaneType::Daemon), Group::Lower);
    assert_eq!(
        l.group(Group::Lower),
        ids(&["history", "control-2", "control", "tab-a"]).as_slice()
    );
    assert_eq!(l.insert(id("tab-b"), PaneType::Tab), Group::Lower);
    assert_eq!(
        l.group(Group::Lower),
        ids(&["history", "control-2", "control", "tab-a", "tab-b"]).as_slice()
    );

    // placeholder → like worker.
    assert_eq!(
        l.insert(id("placeholder-1"), PaneType::Placeholder),
        Group::Workers
    );
    // unknown → Workers, last.
    assert_eq!(l.insert(id("mystery"), PaneType::Unknown), Group::Workers);
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1", "worker-2", "placeholder-1", "mystery"]).as_slice()
    );

    assert_eq!(l.pane_type("history"), Some(PaneType::History));
    assert_eq!(l.pane_type("mystery"), Some(PaneType::Unknown));
    assert_eq!(l.pane_type("nope"), None);
}

#[test]
fn move_before_after_end() {
    let mut l = three_workers();
    l.insert(id("history"), PaneType::History);

    l.move_to(id("worker-3"), Group::Workers, Pos::Before(id("worker-1")))
        .unwrap();
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-3", "worker-1", "worker-2"]).as_slice()
    );

    l.move_to(id("worker-3"), Group::Workers, Pos::After(id("worker-1")))
        .unwrap();
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1", "worker-3", "worker-2"]).as_slice()
    );

    l.move_to(id("worker-3"), Group::Workers, Pos::End).unwrap();
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1", "worker-2", "worker-3"]).as_slice()
    );

    // Across groups.
    l.move_to(id("worker-1"), Group::Lower, Pos::After(id("history")))
        .unwrap();
    assert_eq!(
        l.group(Group::Lower),
        ids(&["history", "worker-1"]).as_slice()
    );
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-2", "worker-3"]).as_slice()
    );
    l.move_to(id("worker-1"), Group::Workers, Pos::Before(id("worker-2")))
        .unwrap();
    assert_eq!(l.group(Group::Lower), ids(&["history"]).as_slice());
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1", "worker-2", "worker-3"]).as_slice()
    );

    // Errors leave the layout untouched.
    let snapshot = l.clone();
    assert_eq!(
        l.move_to(id("ghost"), Group::Workers, Pos::End),
        Err(LayoutError::NoSuchPane(id("ghost")))
    );
    assert_eq!(
        l.move_to(id("worker-1"), Group::Workers, Pos::Before(id("history"))),
        Err(LayoutError::AnchorNotInGroup(id("history")))
    );
    assert_eq!(
        l.move_to(id("worker-1"), Group::Lower, Pos::After(id("ghost"))),
        Err(LayoutError::AnchorNotInGroup(id("ghost")))
    );
    assert_eq!(l, snapshot);
}

#[test]
fn remove_selected_transfers_to_the_one_inserted_before_it() {
    let mut l = three_workers();
    assert!(l.select(id("worker-2")));
    assert_eq!(l.selected(Group::Workers), Some(&id("worker-2")));

    // Successor spawned (lands last), then tabbed in front of the old one.
    assert_eq!(l.insert(id("worker-2.1"), PaneType::Worker), Group::Workers);
    l.move_to(
        id("worker-2.1"),
        Group::Workers,
        Pos::Before(id("worker-2")),
    )
    .unwrap();
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1", "worker-2.1", "worker-2", "worker-3"]).as_slice()
    );
    assert_eq!(
        l.selected(Group::Workers),
        Some(&id("worker-2")),
        "moving a tab in must not steal the selection"
    );

    assert_eq!(l.remove(id("worker-2")), Some(id("worker-2.1")));
    assert_eq!(l.selected(Group::Workers), Some(&id("worker-2.1")));
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1", "worker-2.1", "worker-3"]).as_slice()
    );
}

#[test]
fn remove_selected_without_predecessor_transfers_to_left_neighbour() {
    let mut l = three_workers();
    assert!(l.select(id("worker-3")));
    assert_eq!(l.remove(id("worker-3")), Some(id("worker-2")));
    assert_eq!(l.selected(Group::Workers), Some(&id("worker-2")));
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1", "worker-2"]).as_slice()
    );

    // Middle pane, nothing inserted before it.
    let mut l = three_workers();
    assert!(l.select(id("worker-2")));
    assert_eq!(l.remove(id("worker-2")), Some(id("worker-1")));
    assert_eq!(l.selected(Group::Workers), Some(&id("worker-1")));

    // Removing a pane that is not selected keeps the selection.
    let mut l = three_workers();
    assert!(l.select(id("worker-1")));
    assert_eq!(l.remove(id("worker-3")), Some(id("worker-1")));
    assert_eq!(l.selected(Group::Workers), Some(&id("worker-1")));

    // Unknown pane: nothing happens.
    assert_eq!(l.remove(id("ghost")), None);
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1", "worker-2"]).as_slice()
    );
}

#[test]
fn rotation_sequence_spawn_tab_into_kill_keeps_slot() {
    // design.md §3.2, step by step.
    let mut l = three_workers();
    assert!(l.select(id("worker-1")));

    // spawn new generation: lands last by type.
    assert_eq!(l.insert(id("worker-1.1"), PaneType::Worker), Group::Workers);
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1", "worker-2", "worker-3", "worker-1.1"]).as_slice()
    );

    // tab_into(new, old): in front of the old one.
    l.move_to(
        id("worker-1.1"),
        Group::Workers,
        Pos::Before(id("worker-1")),
    )
    .unwrap();
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1.1", "worker-1", "worker-2", "worker-3"]).as_slice()
    );

    // kill_pane(old): old slot kept, selection handed to the successor.
    assert_eq!(l.remove(id("worker-1")), Some(id("worker-1.1")));
    assert_eq!(
        l.group(Group::Workers),
        ids(&["worker-1.1", "worker-2", "worker-3"]).as_slice()
    );
    assert_eq!(l.selected(Group::Workers), Some(&id("worker-1.1")));
}

#[test]
fn supervisor_rotation_keeps_advisor_position() {
    let mut l = Layout::new();
    l.insert(id("supervisor-1"), PaneType::Supervisor);
    l.insert(id("advisor-1"), PaneType::Advisor);
    assert!(l.select(id("supervisor-1")));

    // supervisor-1 → supervisor-1.1 while the supervisor is shown.
    assert_eq!(
        l.insert(id("supervisor-1.1"), PaneType::Supervisor),
        Group::Supervisor
    );
    l.move_to(
        id("supervisor-1.1"),
        Group::Supervisor,
        Pos::Before(id("supervisor-1")),
    )
    .unwrap();
    assert_eq!(
        l.group(Group::Supervisor),
        ids(&["supervisor-1.1", "supervisor-1", "advisor-1"]).as_slice()
    );
    assert_eq!(l.remove(id("supervisor-1")), Some(id("supervisor-1.1")));
    assert_eq!(
        l.group(Group::Supervisor),
        ids(&["supervisor-1.1", "advisor-1"]).as_slice()
    );
    assert_eq!(l.selected(Group::Supervisor), Some(&id("supervisor-1.1")));

    // supervisor-1.1 → supervisor-1.2 while the advisor is shown: advisor keeps slot and selection.
    assert!(l.select(id("advisor-1")));
    assert_eq!(
        l.insert(id("supervisor-1.2"), PaneType::Supervisor),
        Group::Supervisor
    );
    l.move_to(
        id("supervisor-1.2"),
        Group::Supervisor,
        Pos::Before(id("supervisor-1.1")),
    )
    .unwrap();
    assert_eq!(l.remove(id("supervisor-1.1")), Some(id("advisor-1")));
    assert_eq!(
        l.group(Group::Supervisor),
        ids(&["supervisor-1.2", "advisor-1"]).as_slice()
    );
    assert_eq!(l.selected(Group::Supervisor), Some(&id("advisor-1")));
}

#[test]
fn rects_at_240x60_match_design_estimates() {
    let l = full_run();
    let r = l.rects(240, 60);

    // design.md §2.5: supervisor ≈118×34, lower ≈118×22, workers ≈119×57 (±2).
    assert!(
        near(r.supervisor.w, 118, 2) && near(r.supervisor.h, 34, 2),
        "supervisor {:?}",
        r.supervisor
    );
    assert!(
        near(r.lower.w, 118, 2) && near(r.lower.h, 22, 2),
        "lower {:?}",
        r.lower
    );
    assert!(
        near(r.workers.w, 119, 2) && near(r.workers.h, 57, 2),
        "workers {:?}",
        r.workers
    );

    // Left column: supervisor above lower; workers to the right of both.
    assert!(r.supervisor.y + r.supervisor.h <= r.lower.y);
    assert!(r.supervisor.x + r.supervisor.w <= r.workers.x);
    assert!(r.lower.x + r.lower.w <= r.workers.x);
    assert!(r.workers.x + r.workers.w <= 240);
    assert!(r.lower.y + r.lower.h <= 59 && r.workers.y + r.workers.h <= 59);

    assert_eq!(
        r.status,
        Rect {
            x: 0,
            y: 59,
            w: 240,
            h: 1
        }
    );
    assert_eq!(r.zoom, None);
}

#[test]
fn zoomed_rect_is_whole_content_area_with_status_line() {
    let mut l = full_run();
    l.toggle_zoom(id("worker-1"));

    let r = l.rects(240, 60);
    assert_eq!(
        r.zoom,
        Some(Rect {
            x: 0,
            y: 0,
            w: 240,
            h: 59
        })
    );
    assert_eq!(
        r.status,
        Rect {
            x: 0,
            y: 59,
            w: 240,
            h: 1
        }
    );

    let r = l.rects(80, 24);
    assert_eq!(
        r.zoom,
        Some(Rect {
            x: 0,
            y: 0,
            w: 80,
            h: 23
        })
    );
    assert_eq!(
        r.status,
        Rect {
            x: 0,
            y: 23,
            w: 80,
            h: 1
        }
    );
}

#[test]
fn layout_json_roundtrip() {
    let mut l = full_run();
    assert!(l.select(id("worker-2")));
    assert!(l.select(id("control")));
    l.focus_group(Group::Workers);
    l.set_ratios(35, 75);
    l.toggle_zoom(id("worker-2"));

    let text = l.to_json();
    let v: serde_json::Value = serde_json::from_str(&text).expect("layout.json is JSON");
    assert_eq!(v["h_ratio"], 35);
    assert_eq!(v["v_ratio"], 75);

    let back = Layout::from_json(&text).expect("roundtrip");
    // Pane type info is optional in layout.json (an extra key beside the seven fixed ones), so compare
    // the fixed fields; if types do come back, they must be the original ones.
    for g in Group::ALL {
        assert_eq!(back.group(g), l.group(g), "{g:?}");
        assert_eq!(back.selected(g), l.selected(g), "{g:?}");
        for p in l.group(g) {
            let ty = back.pane_type(p);
            assert!(ty.is_none() || ty == l.pane_type(p), "{p}: {ty:?}");
        }
    }
    assert_eq!(back.focus(), Group::Workers);
    assert_eq!(back.selected(Group::Workers), Some(&id("worker-2")));
    assert_eq!(back.selected(Group::Lower), Some(&id("control")));
    assert_eq!(back.zoom(), Some(&id("worker-2")));
    assert_eq!(back.ratios(), (35, 75));

    // A fresh layout roundtrips too.
    let empty = Layout::new();
    assert_eq!(
        Layout::from_json(&empty.to_json()).expect("empty roundtrip"),
        empty
    );
}

#[test]
fn from_json_rejects_bad_ratio() {
    let good = full_run().to_json();
    let base: serde_json::Value = serde_json::from_str(&good).expect("layout.json is JSON");
    assert!(Layout::from_json(&good).is_ok());

    for (key, value) in [
        ("h_ratio", 95),
        ("h_ratio", 5),
        ("v_ratio", 0),
        ("v_ratio", 200),
        ("h_ratio", -1),
    ] {
        let mut v = base.clone();
        v[key] = serde_json::json!(value);
        let err =
            Layout::from_json(&v.to_string()).expect_err("bad ratio must be rejected, not clamped");
        assert!(
            matches!(err, LayoutError::BadRatio { which, value: got } if which == key && got == value),
            "{key}={value}: {err:?}"
        );
    }

    assert!(matches!(
        Layout::from_json("not json"),
        Err(LayoutError::BadJson(_))
    ));
}

#[test]
fn group_names_match_protocol() {
    for (g, name) in [
        (Group::Supervisor, "supervisor"),
        (Group::Lower, "lower"),
        (Group::Workers, "workers"),
    ] {
        assert_eq!(g.name(), name);
        assert_eq!(Group::from_name(name), Some(g));
    }
    assert_eq!(Group::from_name("Workers"), None);
    assert_eq!(Group::from_name("left"), None);
    assert_eq!(Group::from_name(""), None);
}

/// layout.json (protocol-draft §8.3) = the §4.14 layout object plus `"version": 1`. The seven keys are
/// fixed and must carry exactly these values; extra keys (e.g. pane type info) are allowed beside them.
fn assert_layout_json(text: &str, object: &serde_json::Value) {
    assert!(!text.contains('\n'), "layout.json is one line: {text:?}");
    let parsed: serde_json::Value = serde_json::from_str(text).expect("layout.json is JSON");
    assert!(parsed.is_object(), "{parsed}");
    assert_eq!(parsed["version"], 1, "{parsed}");
    for key in ["h_ratio", "v_ratio", "zoom", "focus", "groups", "selected"] {
        assert!(parsed.get(key).is_some(), "missing {key}: {parsed}");
        assert_eq!(parsed[key], object[key], "{key}");
    }
}

#[test]
fn layout_json_is_protocol_layout_object_plus_version() {
    // protocol-draft §4.14 (layout object) and §8.3 (layout.json = that object + "version": 1).
    let empty = Layout::new();
    let empty_object = serde_json::json!({
        "h_ratio": 50, "v_ratio": 60, "zoom": null, "focus": "supervisor",
        "groups": {"supervisor": [], "lower": [], "workers": []},
        "selected": {"supervisor": null, "lower": null, "workers": null},
    });
    assert_eq!(empty.to_object(), empty_object);
    assert_layout_json(&empty.to_json(), &empty_object);

    let mut l = full_run();
    assert!(l.select(id("supervisor-1")));
    assert!(l.select(id("control")));
    l.focus_group(Group::Lower);
    l.set_ratios(65, 40);
    l.toggle_zoom(id("worker-3"));
    let object = serde_json::json!({
        "h_ratio": 65, "v_ratio": 40, "zoom": "worker-3", "focus": "lower",
        "groups": {
            "supervisor": ["supervisor-1", "advisor-1"],
            "lower": ["history", "control", "evermem-shell"],
            "workers": ["worker-1", "worker-2", "worker-3"],
        },
        "selected": {"supervisor": "supervisor-1", "lower": "control", "workers": null},
    });
    // The layout.get / layout.set reply body is exactly the layout object.
    assert_eq!(l.to_object(), object);
    assert_layout_json(&l.to_json(), &object);
}

#[test]
fn from_json_reads_protocol_example_and_ignores_extra_keys() {
    // The protocol-draft §4.14 example plus "version": 1 and an unknown key.
    let text = r#"{"version":1,"h_ratio":50,"v_ratio":60,"zoom":null,"focus":"supervisor",
        "groups":{"supervisor":["k7q2-1"],"lower":["k7q2-2","k7q2-3"],"workers":["k7q2-12"]},
        "selected":{"supervisor":"k7q2-1","lower":"k7q2-2","workers":"k7q2-12"},
        "future_key":{"anything":true}}"#;
    let l = Layout::from_json(text).expect("protocol example parses");
    assert_eq!(l.ratios(), (50, 60));
    assert_eq!(l.zoom(), None);
    assert_eq!(l.focus(), Group::Supervisor);
    assert_eq!(l.group(Group::Supervisor), ids(&["k7q2-1"]).as_slice());
    assert_eq!(l.group(Group::Lower), ids(&["k7q2-2", "k7q2-3"]).as_slice());
    assert_eq!(l.group(Group::Workers), ids(&["k7q2-12"]).as_slice());
    assert_eq!(l.selected(Group::Lower), Some(&id("k7q2-2")));
    assert_eq!(l.selected(Group::Workers), Some(&id("k7q2-12")));
    assert_eq!(
        l.pane_type("k7q2-1"),
        None,
        "the file carries no pane type info"
    );

    // Wrong value types are BadJson, not a panic and not BadRatio.
    let base: serde_json::Value = serde_json::from_str(text).unwrap();
    for (key, bad) in [
        ("h_ratio", serde_json::json!("50")),
        ("focus", serde_json::json!("left")),
        ("groups", serde_json::json!([])),
    ] {
        let mut v = base.clone();
        v[key] = bad;
        assert!(
            matches!(
                Layout::from_json(&v.to_string()),
                Err(LayoutError::BadJson(_))
            ),
            "{key}"
        );
    }
}

// S1-① sidebar geometry (plan §4.1): the sidebar is a left band of `Layout::sidebar()` columns that every
// group is shifted right by, dropped when the content would be narrower than `SIDEBAR_MIN_CONTENT`, and never
// part of the layout JSON.

#[test]
fn sidebar_is_off_by_default_and_rects_keep_the_240x60_contract() {
    let l = Layout::new();
    assert_eq!(l.sidebar(), 0, "a new layout has no sidebar");
    assert_eq!(l.sidebar_cols(240), 0, "240 columns, no sidebar asked for");
    let r = l.rects(240, 60);
    assert_eq!(
        r.supervisor,
        Rect {
            x: 1,
            y: 1,
            w: 118,
            h: 34
        },
        "{r:?}"
    );
    assert_eq!(r.workers.x, 120, "{r:?}");
    assert_eq!(
        r.status,
        Rect {
            x: 0,
            y: 59,
            w: 240,
            h: 1
        },
        "{r:?}"
    );
}

#[test]
fn sidebar_shifts_every_group_right_and_keeps_the_status_row_full_width() {
    let mut with_sidebar = Layout::new();
    with_sidebar.set_sidebar(34);
    let a = with_sidebar.rects(240, 60);
    // 240 − 34 = 206 content columns: the same picture, moved right by 34.
    let b = Layout::new().rects(206, 60);
    assert_eq!(a.supervisor.x, b.supervisor.x + 34, "{a:?} vs {b:?}");
    assert_eq!(a.supervisor.w, b.supervisor.w, "{a:?} vs {b:?}");
    assert_eq!(a.supervisor.y, b.supervisor.y, "{a:?} vs {b:?}");
    assert_eq!(a.supervisor.h, b.supervisor.h, "{a:?} vs {b:?}");
    assert_eq!(a.lower.x, b.lower.x + 34, "{a:?} vs {b:?}");
    assert_eq!(a.lower.w, b.lower.w, "{a:?} vs {b:?}");
    assert_eq!(a.lower.y, b.lower.y, "{a:?} vs {b:?}");
    assert_eq!(a.lower.h, b.lower.h, "{a:?} vs {b:?}");
    assert_eq!(a.workers.x, b.workers.x + 34, "{a:?} vs {b:?}");
    assert_eq!(a.workers.w, b.workers.w, "{a:?} vs {b:?}");
    assert_eq!(a.workers.y, b.workers.y, "{a:?} vs {b:?}");
    assert_eq!(a.workers.h, b.workers.h, "{a:?} vs {b:?}");
    assert_eq!(
        a.status,
        Rect {
            x: 0,
            y: 59,
            w: 240,
            h: 1
        },
        "the status row keeps the full width: {a:?}"
    );
    assert_eq!(a.supervisor.w, 101, "supervisor PTY width: {a:?}");
    assert_eq!(a.workers.w, 102, "workers PTY width: {a:?}");
}

#[test]
fn sidebar_is_dropped_when_the_content_would_be_narrower_than_60() {
    let mut l = Layout::new();
    l.set_sidebar(34);
    assert_eq!(
        l.sidebar_cols(93),
        0,
        "93 columns leave 59 for the content, under SIDEBAR_MIN_CONTENT"
    );
    assert_eq!(l.sidebar_cols(94), 34, "94 columns leave exactly 60");
    assert_eq!(
        l.rects(93, 24),
        Layout::new().rects(93, 24),
        "a dropped sidebar changes no rect at 93x24"
    );
}

// A42-LINUX-SIDEBAR-ALIGN-03 (M81 RB-01/RB-02): the window's header floor arrives on the wire as a `u16`
// (`proto::view::SIDEBAR_MIN_MAX` = `u16::MAX`), so 65535 columns is a legal sidebar width. The
// content-minimum rule has to decide that case with room to spare: `sidebar + SIDEBAR_MIN_CONTENT` does not
// fit a `u16`, so a debug build must not panic and a release build must not wrap into drawing a band wider
// than the viewer. The rule itself is unchanged: shown while the content keeps SIDEBAR_MIN_CONTENT columns.
#[test]
fn a_wire_maximum_sidebar_is_compared_without_overflow() {
    let mut l = Layout::new();
    l.set_sidebar(u16::MAX);
    assert_eq!(
        l.sidebar_cols(u16::MAX),
        0,
        "65535 + SIDEBAR_MIN_CONTENT columns cannot fit a 65535-column viewer"
    );
    assert_eq!(
        l.rects(u16::MAX, 24),
        Layout::new().rects(u16::MAX, 24),
        "a sidebar that cannot fit the content changes no rect"
    );
    // An ordinary viewer too: with the old `u16` sum a release build wrapped `65535 + 60` to 59 and a
    // 100-column viewer would claim a 65535-column band. The content rule hides it at every width.
    assert_eq!(
        l.sidebar_cols(100),
        0,
        "a 100-column viewer cannot show a 65535-column band"
    );
    assert_eq!(
        l.rects(100, 24),
        Layout::new().rects(100, 24),
        "a wrapped band would change the 100-column rects"
    );
    // The widest sidebar that still leaves exactly SIDEBAR_MIN_CONTENT columns is shown, one column narrower
    // is hidden: the boundary is the same content rule, not an overflow artefact.
    let exact = u16::MAX - fleetmux_layout::SIDEBAR_MIN_CONTENT;
    l.set_sidebar(exact);
    assert_eq!(
        l.sidebar_cols(u16::MAX),
        exact,
        "the widest sidebar that leaves exactly SIDEBAR_MIN_CONTENT columns"
    );
    assert_eq!(
        l.sidebar_cols(u16::MAX - 1),
        0,
        "one column short of SIDEBAR_MIN_CONTENT hides the sidebar"
    );
    // The same band on the right (mirrored Linux content): the rects stay inside the viewer.
    l.set_mirror(true);
    assert_eq!(l.sidebar_cols(u16::MAX), exact, "mirrored: the same rule");
    assert_eq!(l.rects(u16::MAX, 24).status.w, u16::MAX);
    assert_eq!(
        l.sidebar_rect(u16::MAX, 24),
        Some(Rect {
            x: fleetmux_layout::SIDEBAR_MIN_CONTENT,
            y: 0,
            w: exact,
            h: 23
        }),
        "mirrored: the band is the right columns, after the content"
    );
}

#[test]
fn zoomed_rect_starts_after_the_sidebar() {
    let mut l = Layout::new();
    l.insert(id("supervisor-1"), PaneType::Supervisor);
    l.toggle_zoom(id("supervisor-1"));
    l.set_sidebar(34);
    let r = l.rects(240, 60);
    assert_eq!(
        r.zoom,
        Some(Rect {
            x: 34,
            y: 0,
            w: 206,
            h: 59
        }),
        "{r:?}"
    );
}

#[test]
fn sidebar_is_not_part_of_the_layout_json() {
    let mut l = Layout::new();
    l.set_sidebar(34);
    let object = l.to_object();
    let mut keys: Vec<&str> = object
        .as_object()
        .expect("the layout object is a JSON object")
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        vec![
            "focus", "groups", "h_ratio", "selected", "v_ratio", "zoom"
        ],
        "{object}"
    );
    let text = l.to_json();
    let back = Layout::from_json(&text).expect("the layout json round trips");
    assert_eq!(back.sidebar(), 0, "the sidebar is not restored: {text}");
}

// ---- S1f (plan §20.6): the topbar takes row 0, so every rect moves down one row ----

#[test]
fn topbar_shifts_every_rect_down_one_row_and_keeps_the_status_row() {
    let mut plain = Layout::new();
    plain.set_sidebar(34);
    plain.insert(id("supervisor-1"), PaneType::Supervisor);
    plain.toggle_zoom(id("supervisor-1"));
    let mut with_topbar = Layout::new();
    with_topbar.set_sidebar(34);
    with_topbar.insert(id("supervisor-1"), PaneType::Supervisor);
    with_topbar.toggle_zoom(id("supervisor-1"));
    with_topbar.set_topbar(15);
    let a = with_topbar.rects(240, 60);
    // The invariant: with the header band (topbar row + its separation row, A26 rev7), the picture is the
    // no-header picture of two rows less, moved down two.
    let b = plain.rects(240, 58);
    for (name, top, below) in [
        ("supervisor", a.supervisor, b.supervisor),
        ("lower", a.lower, b.lower),
        ("workers", a.workers, b.workers),
    ] {
        assert_eq!(top.x, below.x, "{name} x: {top:?} vs {below:?}");
        assert_eq!(
            top.y,
            below.y + 2,
            "{name} moves down the header band's two rows: {top:?} vs {below:?}"
        );
        assert_eq!(top.w, below.w, "{name} w: {top:?} vs {below:?}");
        assert_eq!(top.h, below.h, "{name} h: {top:?} vs {below:?}");
    }
    let zoom_a = a.zoom.expect("no zoom set");
    let zoom_b = b.zoom.expect("no zoom set");
    assert_eq!(
        zoom_a,
        Rect {
            y: zoom_b.y + 2,
            ..zoom_b
        },
        "the zoomed pane also starts on the band's last row (2): {zoom_a:?} vs {zoom_b:?}"
    );
    assert_eq!(
        a.status,
        Rect {
            x: 0,
            y: 59,
            w: 240,
            h: 1
        },
        "the status row stays on the last row: {a:?}"
    );
    assert_eq!(
        a.supervisor,
        Rect {
            x: 35,
            y: 3,
            w: 101,
            h: 33
        },
        "240x60 sidebar 34 topbar: supervisor starts below the header band: {a:?}"
    );
    assert_eq!(
        a.workers,
        Rect {
            x: 137,
            y: 3,
            w: 102,
            h: 55
        },
        "240x60 sidebar 34 topbar: workers start below the header band: {a:?}"
    );
}

#[test]
fn topbar_left_is_the_drawn_sidebar_or_the_toolbar() {
    let mut l = Layout::new();
    l.set_topbar(15);
    l.set_sidebar(34);
    assert_eq!(
        l.topbar_left(240),
        34,
        "a shown sidebar 34 at 240 columns: the left part is the sidebar, 34 columns"
    );
    l.set_sidebar(0);
    assert_eq!(
        l.topbar_left(240),
        15,
        "hidden sidebar: the left part is the toolbar, 15 columns"
    );
    l.set_sidebar(34);
    assert_eq!(
        l.topbar_left(93),
        15,
        "at 93 columns the sidebar is dropped (93 - 34 < 60): the left part is the toolbar, 15 columns"
    );
    l.set_topbar(0);
    assert_eq!(l.topbar_left(240), 0, "no topbar: no left part");
}

#[test]
fn topbar_is_runtime_state_not_in_the_layout_json() {
    let mut l = Layout::new();
    l.set_sidebar(34);
    l.set_topbar(15);
    let object = l.to_object();
    let mut keys: Vec<&str> = object
        .as_object()
        .expect("the layout object is a JSON object")
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        vec![
            "focus", "groups", "h_ratio", "selected", "v_ratio", "zoom"
        ],
        "the topbar adds no layout object key: {object}"
    );
    let text = l.to_json();
    let back = Layout::from_json(&text).expect("the layout json round trips");
    assert_eq!(
        back.topbar(),
        0,
        "the topbar is runtime state, not restored from the layout json: {text}"
    );
}

// ---- A26 rev3 (user requirement A42-LAYOUT-LINUX-MIRROR, CURRENT_REQUIREMENTS rev 4 `content_layout`): the
// Linux content mirror. Workers take the left column, the supervisor/lower stack the middle, the fleet sidebar
// the right band; macOS keeps the left sidebar, the stack next to it and the workers on the right. It is runtime
// state (derived from the platform), so it is not written to `layout.json` and never restored from it.

#[test]
fn mirror_is_off_by_default_and_not_part_of_the_layout_json() {
    let l = Layout::new();
    assert!(!l.mirror(), "a new layout is not mirrored (the macOS layout)");
    let mut m = Layout::new();
    m.set_sidebar(34);
    m.set_mirror(true);
    let object = m.to_object();
    let mut keys: Vec<&str> = object
        .as_object()
        .expect("the layout object is a JSON object")
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        vec![
            "focus", "groups", "h_ratio", "selected", "v_ratio", "zoom"
        ],
        "the mirror adds no layout object key: {object}"
    );
    let text = m.to_json();
    let back = Layout::from_json(&text).expect("the layout json round trips");
    assert!(
        !back.mirror(),
        "the mirror is derived from the platform at boot, not restored: {text}"
    );
}

#[test]
fn mirror_puts_workers_left_the_stack_middle_and_the_sidebar_right() {
    let mut l = Layout::new();
    l.set_sidebar(34);
    l.set_mirror(true);
    let a = l.rects(240, 60);
    // The same 206 content columns as the macOS layout at 240 with a 34-column sidebar, mirrored left/right.
    let b = Layout::new().rects(206, 60);
    assert_eq!(
        a.workers.x,
        b.supervisor.x,
        "workers take the content's left column, where the stack sits unmirrored: {a:?} vs {b:?}"
    );
    assert_eq!(a.workers.w, b.workers.w, "{a:?} vs {b:?}");
    assert_eq!(a.workers.y, b.workers.y, "{a:?} vs {b:?}");
    assert_eq!(a.workers.h, b.workers.h, "{a:?} vs {b:?}");
    assert_eq!(a.supervisor.w, b.supervisor.w, "{a:?} vs {b:?}");
    assert_eq!(a.supervisor.y, b.supervisor.y, "{a:?} vs {b:?}");
    assert_eq!(a.supervisor.h, b.supervisor.h, "{a:?} vs {b:?}");
    assert_eq!(a.lower.w, b.lower.w, "{a:?} vs {b:?}");
    assert_eq!(a.lower.y, b.lower.y, "{a:?} vs {b:?}");
    assert_eq!(a.lower.h, b.lower.h, "{a:?} vs {b:?}");
    // The stack keeps its 50% width, measured from the right: 101 columns at 104..=204, the separator on 103.
    assert_eq!(a.supervisor.w, 101, "supervisor PTY width: {a:?}");
    assert_eq!(a.supervisor.x, 104, "the stack sits in the middle: {a:?}");
    assert_eq!(a.lower.x, 104, "history/daemon under the supervisor: {a:?}");
    assert_eq!(a.workers.w, 102, "workers PTY width: {a:?}");
    assert_eq!(
        l.sidebar_rect(240, 60),
        Some(Rect {
            x: 206,
            y: 0,
            w: 34,
            h: 59
        }),
        "the fleet sidebar is the right band: {a:?}"
    );
    assert_eq!(
        a.status,
        Rect {
            x: 0,
            y: 59,
            w: 240,
            h: 1
        },
        "the status row keeps the full width: {a:?}"
    );
}

#[test]
fn mirror_keeps_the_zoom_rect_over_the_whole_content_area() {
    let mut l = Layout::new();
    l.insert(id("supervisor-1"), PaneType::Supervisor);
    l.toggle_zoom(id("supervisor-1"));
    l.set_sidebar(34);
    l.set_mirror(true);
    assert_eq!(
        l.rects(240, 60).zoom,
        Some(Rect {
            x: 0,
            y: 0,
            w: 206,
            h: 59
        }),
        "a zoomed pane fills the content, left of the right-hand sidebar"
    );
}

#[test]
fn mirror_drops_the_sidebar_at_the_same_width_and_keeps_the_topbar_toolbar_left() {
    let mut l = Layout::new();
    l.set_sidebar(34);
    l.set_topbar(15);
    l.set_mirror(true);
    assert_eq!(l.sidebar_cols(93), 0, "the same 60-content-column rule");
    assert_eq!(
        l.rects(93, 24),
        {
            let mut plain = Layout::new();
            plain.set_topbar(15);
            plain.set_mirror(true);
            plain.rects(93, 24)
        },
        "a dropped sidebar changes no rect"
    );
    assert_eq!(
        l.topbar_left(240),
        15,
        "mirrored: row 0's left part is the toolbar, not the sidebar on the right"
    );
    l.set_sidebar(0);
    assert_eq!(l.topbar_left(240), 15, "hidden sidebar: the toolbar");
}

// ---- A26 rev3: the platform default lives in the crate (`for_platform`), `new` stays neutral for the tests.

#[test]
fn for_platform_is_the_platform_ordering_and_new_stays_neutral() {
    let platform = Layout::for_platform();
    assert!(!platform.mirror(), "both platforms default to workers right");
    let mut neutral = Layout::new();
    assert!(!neutral.mirror(), "new() is the macOS ordering everywhere");
    neutral.set_sidebar_left(cfg!(target_os = "linux"));
    neutral.set_native_header(cfg!(target_os = "linux"));
    if cfg!(target_os = "linux") {
        neutral.set_header(fleetmux_layout::HEADER_GAP);
    }
    assert_eq!(
        platform, neutral,
        "for_platform derives only runtime chrome from the platform"
    );
    let mut with_sidebar = Layout::for_platform();
    with_sidebar.set_sidebar(34);
    let band = with_sidebar.sidebar_rect(240, 60).expect("the band is shown");
    assert_eq!(band.x, 0, "both platforms: sidebar left");
    assert_eq!(with_sidebar.rects(240, 60).workers.x, 137, "workers right");
}
