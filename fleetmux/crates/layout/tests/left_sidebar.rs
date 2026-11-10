//! Linux chrome moves independently of the existing terminal pane order.
use fleetmux_layout::{Group, HEADER_GAP, Hit, Layout, PaneType};

fn linux(sidebar: u16) -> Layout {
    let mut l = Layout::new();
    l.set_mirror(true);
    l.set_sidebar_left(true);
    l.set_header(HEADER_GAP);
    l.set_sidebar(sidebar);
    l.insert("s".into(), PaneType::Supervisor);
    l.insert("w".into(), PaneType::Worker);
    l.insert("h".into(), PaneType::History);
    l
}

#[test]
fn only_the_sidebar_moves_not_the_pane_order_or_sizes() {
    let left = linux(34);
    let mut old = left.clone();
    old.set_sidebar_left(false);
    let (a, b) = (left.rects(240, 60), old.rects(240, 60));
    assert_eq!(left.sidebar_rect(240, 60).unwrap().x, 0);
    assert!(a.workers.x < a.supervisor.x);
    assert_eq!(a.supervisor.x, a.lower.x);
    for (new, old) in [
        (a.supervisor, b.supervisor),
        (a.lower, b.lower),
        (a.workers, b.workers),
    ] {
        assert_eq!(
            (new.x, new.y, new.w, new.h),
            (old.x + 34, old.y, old.w, old.h)
        );
    }
    assert_eq!(a.status, b.status);
    assert_eq!(
        left.to_object(),
        old.to_object(),
        "no protocol or persistence change"
    );
}

#[test]
fn left_card_and_edge_hits_do_not_follow_the_content_mirror() {
    let l = linux(34);
    assert_eq!(l.hit_test(240, 60, 5, HEADER_GAP), Hit::Sidebar);
    for x in [33, 34] {
        assert_eq!(l.hit_test(240, 60, x, 10), Hit::SidebarEdge);
    }
    assert_eq!(l.hit_test(240, 60, 36, 10), Hit::Pane(Group::Workers));
    assert_eq!(l.hit_test(240, 60, 230, 10), Hit::Pane(Group::Supervisor));
    assert_eq!(l.content_x(240, 5), None);
    assert_eq!(l.content_x(240, 34), Some(0));
    assert_eq!(l.content_x(240, 239), Some(205));
    assert_eq!(l.content_x(240, 240), None);
}

#[test]
fn divider_drags_use_the_shifted_content_coordinates() {
    for width in [24, 34, 60] {
        let l = linux(width);
        let r = l.rects(240, 60);
        let x = r.supervisor.x - 1;
        assert_eq!(l.hit_test(240, 60, x, 10), Hit::VSep);
        assert_eq!(l.ratio_from_x_in(240, x), 50);
    }
}

#[test]
fn collapse_resize_zoom_and_header_gap_keep_the_existing_contract() {
    let mut l = linux(34);
    l.toggle_zoom("w".into());
    let r = l.rects(240, 60);
    assert_eq!(r.zoom.unwrap().x, 34);
    assert_eq!(r.zoom.unwrap().w, 206);
    assert_eq!(l.hit_test(240, 60, 5, 10), Hit::Sidebar);
    assert_eq!(l.hit_test(240, 60, 40, 0), Hit::Outside);
    l.set_sidebar(0);
    assert!(l.sidebar_rect(240, 60).is_none());
    assert_eq!(l.rects(240, 60).zoom.unwrap().x, 0);
    l.set_sidebar(34);
    assert_eq!(l.rects(240, 60).zoom.unwrap().x, 34);
    assert_eq!(l.sidebar_cols(40), 0);
    assert_eq!(l.rects(40, 60).zoom.unwrap().x, 0);
}
