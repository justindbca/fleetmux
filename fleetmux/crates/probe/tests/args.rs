//! ft-probe command line (brief-m03 §3.6, README "Command line"). The probe is a binary-only package, so the
//! test includes `src/main.rs` as a module and calls its `parse_args`.
#[path = "../src/main.rs"]
#[allow(dead_code)]
mod probe;

use probe::{MouseMode, Opts, Parsed, parse_args};

fn parse(args: &[&str]) -> Result<Parsed, String> {
    parse_args(args.iter().map(|s| s.to_string()))
}

fn opts(args: &[&str]) -> Opts {
    match parse(args) {
        Ok(Parsed::Run(o)) => o,
        other => panic!("{args:?} → {other:?}"),
    }
}

#[test]
fn new_flags_parse_and_default() {
    let o = opts(&[]);
    assert_eq!(
        (o.mouse_mode, o.sgr, o.flood, o.lines, o.wide),
        (MouseMode::Drag, true, None, 0, false),
        "defaults: {o:?}"
    );
    assert!(o.mouse && o.paste, "old defaults changed: {o:?}");

    let o = opts(&["--mouse-mode", "motion", "--x10", "--flood", "500", "--lines", "40", "--wide"]);
    assert_eq!(
        (o.mouse_mode, o.sgr, o.flood, o.lines, o.wide),
        (MouseMode::Motion, false, Some(500), 40, true),
        "{o:?}"
    );
    assert_eq!(opts(&["--mouse-mode", "click"]).mouse_mode, MouseMode::Click);
    assert_eq!(opts(&["--mouse-mode", "drag"]).mouse_mode, MouseMode::Drag);

    assert!(parse(&["--mouse-mode", "x"]).is_err());
    assert!(parse(&["--mouse-mode"]).is_err());
    assert!(parse(&["--flood"]).is_err());
    assert!(parse(&["--flood", "fast"]).is_err());
    assert!(parse(&["--lines", "-1"]).is_err());
}
