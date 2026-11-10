//! Navigation allow-list (PROTOCOL.md §15.2, design.md §3.5.7). Red in W0 (`todo!()`), green in W1.
use std::path::Path;

use fleetmux_web::policy::{Decision, allow, is_fleet_jump};

const RUN: &str = "/r/u/.cmux-fleet/runs/fleet-1"; // never touched: the allow-list is lexical
const PAGE: &str = "/tmp/fleetmux-web/abc-42/page";

fn decide(url: &str) -> Decision {
    allow(url, Path::new(RUN), Path::new(PAGE))
}

#[test]
#[ignore = "M2.5: web-window"]
fn policy_allows_file_under_two_dirs_only() {
    assert_eq!(decide("about:blank"), Decision::Allow);
    assert_eq!(
        decide(&format!("file://{RUN}/history.html")),
        Decision::Allow
    );
    assert_eq!(
        decide(&format!("file://{RUN}/history.html?x=1#top")),
        Decision::Allow
    );
    assert_eq!(
        decide(&format!("file://{PAGE}/history.html")),
        Decision::Allow
    );
    // Outside both directories, a sibling whose name only starts like the run dir, and a `..` escape: refused.
    assert_eq!(decide("file:///etc/passwd"), Decision::Deny);
    assert_eq!(
        decide(&format!("file://{RUN}-other/history.html")),
        Decision::Deny
    );
    assert_eq!(
        decide(&format!("file://{RUN}/../fleet-2/history.html")),
        Decision::Deny
    );
    assert_eq!(decide("javascript:alert(1)"), Decision::Deny);
    assert_eq!(decide("data:text/html,x"), Decision::Deny);
}

#[test]
#[ignore = "M2.5: web-window"]
fn policy_recognises_fleet_jump() {
    let jump = format!("file://{RUN}/history.html?fleet-jump=k7q2x-ab12&rel=child&to=fleet-2");
    assert!(is_fleet_jump(&jump));
    assert!(!is_fleet_jump(&format!("file://{RUN}/history.html?x=1")));
    assert!(!is_fleet_jump(&format!("file://{RUN}/fleet-jump.html")));
    // Recognised only: until M4 it loads like any other allowed URL (design.md §3.5.8).
    assert_eq!(decide(&jump), Decision::Allow);
}

#[test]
#[ignore = "M2.5: web-window"]
fn policy_sends_http_to_system_opener() {
    assert_eq!(
        decide("https://github.com/cmux-fleet/fleetmux"),
        Decision::External
    );
    assert_eq!(decide("http://127.0.0.1:8080/"), Decision::External);
    assert_eq!(decide("HTTPS://example.com/"), Decision::External);
}
