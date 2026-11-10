use fleetmux_proto::view::wants_native_header;

#[test]
fn native_header_is_an_explicit_optional_viewer_capability() {
    for line in [r#"{}"#, r#"{"native_header":0}"#] {
        assert!(!wants_native_header(line.as_bytes()).unwrap());
    }
    assert!(wants_native_header(br#"{"native_header":1}"#).unwrap());
    for line in [
        r#"{"native_header":null}"#, r#"{"native_header":true}"#,
        r#"{"native_header":2}"#, r#"{"native_header":1.0}"#,
        r#"{"native_header":"1"}"#,
    ] {
        assert!(wants_native_header(line.as_bytes()).is_err(), "{line}");
    }
}
