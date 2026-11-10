//! Linux pane order matches macOS; native chrome remains independent.
use fleetmux_layout::{Group, HEADER_GAP, Hit, Layout, PaneType};

fn linux(sidebar: u16) -> Layout {
    let mut l = Layout::new();
    l.set_sidebar_left(true);
    l.set_native_header(true);
    l.set_header(HEADER_GAP);
    l.set_sidebar(sidebar);
    l.insert("s".into(), PaneType::Supervisor);
    l.insert("w".into(), PaneType::Worker);
    l.insert("h".into(), PaneType::History);
    l.select("h".into());
    l.select("s".into());
    l
}

#[test]
fn unified_linux_order_preserves_sizes_selection_and_persistence() {
    let l = linux(34);
    let mut old = l.clone();
    old.set_mirror(true);
    let (r, before) = (l.rects(240, 60), old.rects(240, 60));
    assert_eq!(l.sidebar_rect(240, 60).unwrap().x, 0);
    assert_eq!(r.supervisor.x, 35);
    assert_eq!(r.lower.x, r.supervisor.x);
    assert_eq!(r.workers.x, 137);
    for (now, previous) in [
        (r.supervisor, before.supervisor),
        (r.lower, before.lower),
        (r.workers, before.workers),
    ] {
        assert_eq!((now.y, now.w, now.h), (previous.y, previous.w, previous.h));
    }
    assert_eq!(l.to_object(), old.to_object());
    assert_eq!(l.selected(Group::Lower).map(String::as_str), Some("h"));
    assert!(l.native_header() && old.native_header());
    old.set_native_header(false);
    assert!(old.mirror(), "header mode must not change pane ordering");
    assert_eq!(l.to_object(), old.to_object(), "neither runtime flag is serialized");
}

#[test]
fn unified_linux_hits_and_ratio_drags_follow_the_middle_stack() {
    for width in [24, 34, 60] {
        let l = linux(width);
        let r = l.rects(240, 60);
        assert_eq!(l.hit_test(240, 60, 5, HEADER_GAP), Hit::Sidebar);
        assert_eq!(l.hit_test(240, 60, width, 10), Hit::SidebarEdge);
        assert_eq!(l.hit_test(240, 60, r.supervisor.x + 1, 10), Hit::Pane(Group::Supervisor));
        assert_eq!(l.hit_test(240, 60, r.workers.x + 1, 10), Hit::Pane(Group::Workers));
        assert_eq!(l.hit_test(240, 60, r.lower.x + 1, r.lower.y + 1), Hit::Pane(Group::Lower));
        let x = r.workers.x - 1;
        assert_eq!(l.hit_test(240, 60, x, 10), Hit::VSep);
        assert_eq!(l.ratio_from_x_in(240, x), 50);
        assert_eq!(l.hit_test(240, 60, r.supervisor.x + r.supervisor.w - 2, r.lower.y - 1), Hit::HSep);
    }
}

#[test]
fn terminal_header_uses_native_gap_without_moving_shared_panes() {
    for width in [0, 24, 34, 60] {
        let native = linux(width);
        let mut terminal = native.clone();
        terminal.set_header(0);
        terminal.set_topbar(15);
        assert_eq!(terminal.header_rows(), HEADER_GAP);
        assert_eq!(terminal.rects(240, 60), native.rects(240, 60));
        assert_eq!(terminal.hit_test(240, 60, 1, 0), Hit::Sidebar);
        assert_eq!(native.hit_test(240, 60, 1, 0), Hit::Outside);
    }
    let mut mac = Layout::new();
    mac.set_topbar(15);
    assert_eq!(mac.header_rows(), 2, "accepted macOS geometry is unchanged");
}

#[test]
fn unified_linux_collapse_resize_and_zoom_leave_header_gap_intact() {
    let mut l = linux(34);
    for width in [34, 0, 44] {
        l.set_sidebar(width);
        let r = l.rects(240, 60);
        assert!(r.supervisor.x < r.workers.x);
        assert_eq!(r.supervisor.y, HEADER_GAP + 1);
        assert_eq!(l.hit_test(240, 60, 80, 0), Hit::Outside);
        l.toggle_zoom("s".into());
        assert_eq!(l.rects(240, 60).zoom.unwrap().x, width);
        assert_eq!(l.rects(240, 60).zoom.unwrap().w, 240 - width);
        l.toggle_zoom("s".into());
        assert!(l.native_header());
        assert_eq!(l.topbar(), 0);
    }
    assert_eq!(l.sidebar_cols(40), 0);
}
