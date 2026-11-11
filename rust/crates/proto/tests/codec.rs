//! Codec contract for fleetmux-proto (plan.md §3.1). Red in phase 0 (`todo!()`), green from M0.

use std::collections::BTreeSet;
use std::io::{Cursor, ErrorKind};

use fleetmux_proto::view::{self, Frame, FrameError};
use fleetmux_proto::{
    Command, ErrorCode, Group, MAX_LINE, Pos, Reply, WriteMode, b64, decode_line, encode_reply,
    known_commands, peer_allowed, record,
};
use serde_json::{Value, json};

const SCHEMA: &str = include_str!("../../../protocol/schema.json");

fn schema() -> Value {
    serde_json::from_str(SCHEMA).expect("schema.json is valid JSON")
}

fn line(v: &Value) -> Vec<u8> {
    let mut b = serde_json::to_vec(v).unwrap();
    b.push(b'\n');
    b
}

fn reply_json(bytes: &[u8]) -> Value {
    serde_json::from_slice(bytes).expect("reply is JSON")
}

#[test]
fn decode_each_command_from_schema_examples() {
    let s = schema();
    let cmds = s["commands"].as_object().unwrap();
    assert_eq!(cmds.len(), 22);
    for (name, c) in cmds {
        let examples = c["examples"].as_array().unwrap();
        assert!(!examples.is_empty(), "{name} has no example");
        for ex in examples {
            let req = &ex["request"];
            let got = decode_line(&line(req)).unwrap_or_else(|e| panic!("{name}: {e:?}"));
            assert_eq!(got.id, req["id"].as_u64().unwrap(), "{name}");
            assert_eq!(got.cmd.name(), name.as_str());
        }
    }
    // Spot-check that arguments land in the right fields.
    let ex = |n: &str| line(&s["commands"][n]["examples"][0]["request"]);
    match decode_line(&ex("pane.spawn")).unwrap().cmd {
        Command::PaneSpawn(sp) => {
            assert_eq!(sp.argv, ["bash", "-lc", "cd /w && exec claude"]);
            assert_eq!(sp.cwd.as_deref(), Some("/w"));
            assert_eq!(
                sp.env.get("FLEET_SLOT").map(String::as_str),
                Some("worker-1")
            );
            assert_eq!(sp.title, "fx-worker-1");
            assert_eq!(sp.ty, "worker");
            assert_eq!((sp.group, sp.anchor, sp.before), (None, None, false));
        }
        other => panic!("{other:?}"),
    }
    match decode_line(&ex("pane.write")).unwrap().cmd {
        Command::PaneWrite { pane, data, mode } => {
            assert_eq!(pane, "k7q2-12");
            assert_eq!(data, b"--help\n");
            assert_eq!(mode, WriteMode::Bracketed);
        }
        other => panic!("{other:?}"),
    }
    assert_eq!(
        decode_line(&ex("pane.move")).unwrap().cmd,
        Command::PaneMove {
            pane: "k7q2-13".into(),
            group: Group::Workers,
            pos: Pos::Before("k7q2-12".into())
        }
    );
    assert_eq!(
        decode_line(&ex("pane.kill")).unwrap().cmd,
        Command::PaneKill {
            pane: "k7q2-12".into(),
            timeout_ms: 5000
        }
    );
    // Defaults: no timeout_ms → 5000; no force → false; neither before nor after → End.
    let kill = decode_line(br#"{"id":1,"cmd":"pane.kill","pane":"k7q2-1"}"#).unwrap();
    assert_eq!(
        kill.cmd,
        Command::PaneKill {
            pane: "k7q2-1".into(),
            timeout_ms: 5000
        }
    );
    let sd = decode_line(br#"{"id":2,"cmd":"server.shutdown"}"#).unwrap();
    assert_eq!(sd.cmd, Command::ServerShutdown { force: false });
    let mv = decode_line(br#"{"id":3,"cmd":"pane.move","pane":"k7q2-1","group":"lower"}"#).unwrap();
    assert_eq!(
        mv.cmd,
        Command::PaneMove {
            pane: "k7q2-1".into(),
            group: Group::Lower,
            pos: Pos::End
        }
    );
    let ls = decode_line(br#"{"id":4,"cmd":"layout.set","zoom":null}"#).unwrap();
    assert_eq!(
        ls.cmd,
        Command::LayoutSet {
            h_ratio: None,
            v_ratio: None,
            zoom: Some(None)
        }
    );
}

#[test]
fn unknown_cmd_decodes_to_error_not_panic() {
    let e = decode_line(br#"{"id":5,"cmd":"pane.explode","pane":"k7q2-1"}"#).unwrap_err();
    assert_eq!(e.code, ErrorCode::UnknownCmd);
    assert_eq!(e.id, Some(5));
    let r = reply_json(&encode_reply(&e.into_reply()));
    assert_eq!(
        r,
        json!({"id": 5, "ok": false, "code": "unknown_cmd", "error": r["error"].clone()})
    );
    assert!(r["error"].as_str().unwrap().contains("pane.explode"));
    // Case matters, and a near-miss is still unknown.
    for bad in ["Hello", "pane.Spawn", "pane", "kv", ""] {
        let l = line(&json!({"id": 6, "cmd": bad}));
        assert_eq!(
            decode_line(&l).unwrap_err().code,
            ErrorCode::UnknownCmd,
            "{bad:?}"
        );
    }
}

#[test]
fn bad_json_and_missing_id_are_bad_request() {
    let no_id: &[&[u8]] = &[
        b"",
        b"not json\n",
        b"[1,2]\n",
        b"\"hello\"\n",
        br#"{"cmd":"hello"}"#,
        br#"{"id":"7","cmd":"hello"}"#,
        br#"{"id":-1,"cmd":"hello"}"#,
        br#"{"id":1.5,"cmd":"hello"}"#,
        br#"{"id":null,"cmd":"hello"}"#,
        b"{\"id\":1,\"cmd\":\"hello\"",
        b"\xff\xfe{\"id\":1}\n",
    ];
    for l in no_id {
        let e = decode_line(l).unwrap_err();
        assert_eq!(
            (e.code, e.id),
            (ErrorCode::BadRequest, None),
            "{:?}",
            String::from_utf8_lossy(l)
        );
        let r = reply_json(&encode_reply(&e.into_reply()));
        assert_eq!(r["id"], Value::Null);
        assert_eq!(r["ok"], json!(false));
        assert_eq!(r["code"], json!("bad_request"));
    }
    // id recovered, but the request is still malformed → bad_request carrying the id.
    let with_id: &[&[u8]] = &[
        br#"{"id":1}"#,
        br#"{"id":1,"cmd":42}"#,
        br#"{"id":1,"cmd":"pane.get"}"#,
        br#"{"id":1,"cmd":"pane.read","pane":"k7q2-1","lines":"ten"}"#,
        br#"{"id":1,"cmd":"pane.spawn","argv":[]}"#,
        br#"{"id":1,"cmd":"pane.spawn","argv":["true"],"before":false}"#,
        br#"{"id":1,"cmd":"pane.write","pane":"k7q2-1","data":"!!!","mode":"raw"}"#,
        br#"{"id":1,"cmd":"pane.move","pane":"k7q2-1","group":"workers","before":"k7q2-2","after":"k7q2-3"}"#,
        br#"{"id":1,"cmd":"pane.kill","pane":"k7q2-1","timeout_ms":600001}"#,
    ];
    for l in with_id {
        let e = decode_line(l).unwrap_err();
        assert_eq!(
            (e.code, e.id),
            (ErrorCode::BadRequest, Some(1)),
            "{}",
            String::from_utf8_lossy(l)
        );
    }
    // Oversize line: bad_request, id not trusted.
    let mut big = br#"{"id":1,"cmd":"kv.set","key":"k","value":""#.to_vec();
    big.resize(MAX_LINE + 1, b'a');
    big.extend_from_slice(b"\"}");
    let e = decode_line(&big).unwrap_err();
    assert_eq!((e.code, e.id), (ErrorCode::BadRequest, None));
    // Unknown extra keys are ignored, a trailing \r is JSON whitespace.
    let ok = decode_line(b"{\"id\":9,\"cmd\":\"hello\",\"future\":true}\r\n").unwrap();
    assert_eq!(ok.cmd, Command::Hello { protocol: None });
}

#[test]
fn encode_reply_is_one_line_no_embedded_newline() {
    let ok = Reply::Ok {
        id: 9,
        body: json!({"text": "line one\nline two\r\n\u{1b}[0m"}),
    };
    let err = Reply::Err {
        id: Some(3),
        code: ErrorCode::NoSuchPane,
        error: "no pane\nk7q2-12".into(),
    };
    let null_id = Reply::Err {
        id: None,
        code: ErrorCode::BadRequest,
        error: "not json".into(),
    };
    for r in [&ok, &err, &null_id] {
        let b = encode_reply(r);
        assert_eq!(b.last(), Some(&b'\n'), "{r:?}");
        assert_eq!(b.iter().filter(|&&c| c == b'\n').count(), 1, "{r:?}");
        assert!(b.len() - 1 <= MAX_LINE);
    }
    let v = reply_json(&encode_reply(&ok));
    assert_eq!(
        v,
        json!({"id": 9, "ok": true, "text": "line one\nline two\r\n\u{1b}[0m"})
    );
    let v = reply_json(&encode_reply(&err));
    assert_eq!(
        v,
        json!({"id": 3, "ok": false, "code": "no_such_pane", "error": "no pane\nk7q2-12"})
    );
    let v = reply_json(&encode_reply(&null_id));
    assert_eq!(
        v,
        json!({"id": null, "ok": false, "code": "bad_request", "error": "not json"})
    );
    // An empty body is just the envelope.
    let v = reply_json(&encode_reply(&Reply::Ok {
        id: 0,
        body: json!({}),
    }));
    assert_eq!(v, json!({"id": 0, "ok": true}));
    // Every error code has its snake_case wire form.
    let s = schema();
    let names: Vec<&str> = s["error_codes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    let ours: Vec<&str> = ErrorCode::ALL.iter().map(|c| c.as_str()).collect();
    assert_eq!(ours, names);
    for c in ErrorCode::ALL {
        assert_eq!(ErrorCode::parse(c.as_str()), Some(c));
    }
}

#[test]
fn base64_data_roundtrip_including_leading_dash() {
    // RFC 4648 §10 vectors plus the PROTOCOL.md example.
    for (raw, enc) in [
        (&b""[..], ""),
        (b"f", "Zg=="),
        (b"fo", "Zm8="),
        (b"foo", "Zm9v"),
        (b"foob", "Zm9vYg=="),
        (b"fooba", "Zm9vYmE="),
        (b"foobar", "Zm9vYmFy"),
        (b"--help\n", "LS1oZWxwCg=="),
    ] {
        assert_eq!(b64::encode(raw), enc);
        assert_eq!(b64::decode(enc).unwrap(), raw);
    }
    let all: Vec<u8> = (0..=255u8).collect();
    assert_eq!(b64::decode(&b64::encode(&all)).unwrap(), all);
    for bad in ["Zg", "Zg=", "Z===", "Zm9v!", "Zm 9v", "Zg==Zg==", "-_-_"] {
        assert!(b64::decode(bad).is_err(), "{bad:?}");
    }
    // Through the codec: a payload that starts with '-' (and holds NUL, ESC, paste markers) is plain data.
    let payload = b"-n --version\n\x00\x1b[200~x\x1b[201~";
    let l = line(
        &json!({"id": 4, "cmd": "pane.write", "pane": "k7q2-3", "data": b64::encode(payload), "mode": "raw"}),
    );
    assert_eq!(
        decode_line(&l).unwrap().cmd,
        Command::PaneWrite {
            pane: "k7q2-3".into(),
            data: payload.to_vec(),
            mode: WriteMode::Raw
        }
    );
    for (m, want) in [
        ("raw", WriteMode::Raw),
        ("bracketed", WriteMode::Bracketed),
        ("paste", WriteMode::Paste),
    ] {
        let l = line(
            &json!({"id": 1, "cmd": "pane.write", "pane": "k7q2-3", "data": "LQ==", "mode": m}),
        );
        match decode_line(&l).unwrap().cmd {
            Command::PaneWrite { data, mode, .. } => {
                assert_eq!((data, mode), (b"-".to_vec(), want))
            }
            other => panic!("{other:?}"),
        }
    }
}

#[test]
fn view_frame_roundtrip_all_types() {
    let frames = [
        Frame::Input(b"\x02z\x1b[<0;10;5M".to_vec()),
        Frame::Input(Vec::new()),
        Frame::Resize {
            cols: 240,
            rows: 60,
        },
        Frame::Detach,
        Frame::Screen(b"\x1b[?2026h\x1b[H hello \x1b[?2026l".to_vec()),
        Frame::Exit("server shutdown".into()),
        Frame::Exit(String::new()),
        Frame::ScrollTo(r#"{"pane":"k7q2-7","offset":57}"#.into()),
        Frame::Scroll(r#"{"panes":[]}"#.into()),
    ];
    let types = [
        view::T_INPUT,
        view::T_INPUT,
        view::T_RESIZE,
        view::T_DETACH,
        view::T_SCREEN,
        view::T_EXIT,
        view::T_EXIT,
        view::T_SCROLL_TO,
        view::T_SCROLL,
    ];
    let mut stream = Vec::new();
    for (f, t) in frames.iter().zip(types) {
        assert_eq!(f.type_byte(), t);
        let bytes = view::encode_frame(f).unwrap();
        assert_eq!(bytes[0], t);
        let len = u32::from_be_bytes(bytes[1..5].try_into().unwrap()) as usize;
        assert_eq!(bytes.len(), 5 + len);
        view::write_frame(&mut stream, f).unwrap();
    }
    // Resize payload is cols u16 BE then rows u16 BE.
    assert_eq!(
        view::encode_frame(&Frame::Resize {
            cols: 0x0102,
            rows: 0x0304
        })
        .unwrap(),
        [2, 0, 0, 0, 4, 1, 2, 3, 4]
    );
    assert_eq!(view::encode_frame(&Frame::Detach).unwrap(), [3, 0, 0, 0, 0]);
    let mut r = Cursor::new(stream.clone());
    for f in &frames {
        assert_eq!(view::read_frame(&mut r).unwrap().as_ref(), Some(f));
    }
    assert!(view::read_frame(&mut r).unwrap().is_none(), "clean EOF");
    // Incremental decoding: a partial frame asks for more, a whole one reports what it consumed.
    let first = view::encode_frame(&frames[0]).unwrap();
    assert!(view::decode_frame(&first[..3]).unwrap().is_none());
    assert!(
        view::decode_frame(&first[..first.len() - 1])
            .unwrap()
            .is_none()
    );
    assert_eq!(
        view::decode_frame(&stream).unwrap(),
        Some((frames[0].clone(), first.len()))
    );
    // EOF inside a frame is an error, not a clean end.
    let mut cut = Cursor::new(first[..first.len() - 1].to_vec());
    assert!(matches!(view::read_frame(&mut cut), Err(FrameError::Io(_))));
    // Handshake lines.
    let h = view::Hello {
        view: 1,
        cols: 120,
        rows: 40,
        term: "xterm-256color".into(),
        colorterm: "truecolor".into(),
    };
    let hb = h.encode();
    assert_eq!(hb.last(), Some(&b'\n'));
    assert_eq!(view::Hello::decode(&hb).unwrap(), h);
    let ok = view::HelloReply::Ok {
        epoch: "k7q2".into(),
        protocol: 1,
    };
    assert_eq!(
        reply_json(&ok.encode()),
        json!({"ok": true, "epoch": "k7q2", "protocol": 1})
    );
    assert_eq!(view::HelloReply::decode(&ok.encode()).unwrap(), ok);
    // A handshake that also carries `cmd` is invalid; a handshake line fed to the control decoder has no id.
    let both = line(
        &json!({"view": 1, "cols": 80, "rows": 24, "term": "", "colorterm": "", "cmd": "hello"}),
    );
    assert_eq!(
        view::Hello::decode(&both).unwrap_err().code,
        ErrorCode::BadRequest
    );
    let e = decode_line(&hb).unwrap_err();
    assert_eq!((e.code, e.id), (ErrorCode::BadRequest, None));
    let s = schema();
    let ex = &s["view"]["examples"][0];
    assert_eq!(view::Hello::decode(&line(&ex["hello"])).unwrap(), h);
    assert_eq!(
        view::HelloReply::decode(&line(&ex["hello_reply"])).unwrap(),
        ok
    );
}

#[test]
fn view_frame_rejects_oversize() {
    let max = view::MAX_FRAME;
    // Exactly MAX_FRAME is fine.
    let at = Frame::Screen(vec![b'x'; max]);
    let bytes = view::encode_frame(&at).unwrap();
    assert_eq!(view::read_frame(&mut Cursor::new(bytes)).unwrap(), Some(at));
    // One more is refused on write, and nothing is written.
    let over = Frame::Screen(vec![b'x'; max + 1]);
    let mut out = Vec::new();
    assert!(matches!(
        view::write_frame(&mut out, &over),
        Err(FrameError::Oversize { .. })
    ));
    assert!(out.is_empty());
    assert!(matches!(
        view::encode_frame(&Frame::Input(vec![0; max + 1])),
        Err(FrameError::Oversize { .. })
    ));
    // A header announcing more than MAX_FRAME is refused without reading the payload.
    for ty in [view::T_INPUT, view::T_SCREEN, view::T_EXIT] {
        let mut hdr = vec![ty];
        hdr.extend_from_slice(&((max as u32) + 1).to_be_bytes());
        let mut r = Cursor::new(hdr.clone());
        assert!(
            matches!(view::read_frame(&mut r), Err(FrameError::Oversize { len }) if len == max as u64 + 1)
        );
        assert_eq!(r.position(), 5, "payload must not be read");
        assert!(matches!(
            view::decode_frame(&hdr),
            Err(FrameError::Oversize { .. })
        ));
    }
    let mut huge = vec![view::T_SCREEN];
    huge.extend_from_slice(&u32::MAX.to_be_bytes());
    assert!(matches!(
        view::read_frame(&mut Cursor::new(huge)),
        Err(FrameError::Oversize { .. })
    ));
    // Lengths that do not fit the type, and unknown types.
    assert!(matches!(
        view::decode_frame(&[2, 0, 0, 0, 3, 0, 80, 0]),
        Err(FrameError::BadLength { ty: 2, len: 3 })
    ));
    assert!(matches!(
        view::decode_frame(&[2, 0, 0, 0, 4, 0, 0, 0, 24]),
        Err(FrameError::BadLength { ty: 2, .. })
    ));
    assert!(matches!(
        view::decode_frame(&[3, 0, 0, 0, 1, 0]),
        Err(FrameError::BadLength { ty: 3, len: 1 })
    ));
    assert!(matches!(
        view::decode_frame(&[0x7f, 0, 0, 0, 0]),
        Err(FrameError::UnknownType(0x7f))
    ));
    assert!(matches!(
        view::decode_frame(&[0x82, 0, 0, 0, 1, 0xff]),
        Err(FrameError::BadUtf8)
    ));
}

#[test]
fn record_entry_roundtrip() {
    let entries = [
        record::Entry {
            offset_us: 0,
            bytes: b"\x1b[?1049h".to_vec(),
        },
        record::Entry {
            offset_us: 1_500,
            bytes: "wide ⏺ ✻\r\n".as_bytes().to_vec(),
        },
        record::Entry {
            offset_us: u64::MAX,
            bytes: vec![0; record::MAX_ENTRY],
        },
    ];
    let mut file = Vec::new();
    for e in &entries {
        record::write(&mut file, e).unwrap();
    }
    // Layout: u64 BE offset, u32 BE length, bytes; no file header.
    assert_eq!(&file[..8], &0u64.to_be_bytes());
    assert_eq!(&file[8..12], &8u32.to_be_bytes());
    assert_eq!(&file[12..20], b"\x1b[?1049h");
    assert_eq!(
        file.len(),
        entries.iter().map(|e| 12 + e.bytes.len()).sum::<usize>()
    );
    let mut r = Cursor::new(file.clone());
    for e in &entries {
        assert_eq!(record::read(&mut r).unwrap().as_ref(), Some(e));
    }
    assert!(
        record::read(&mut r).unwrap().is_none(),
        "EOF on an entry boundary is the normal end"
    );
    // Truncated inside an entry (server killed mid-write).
    let first_len = 12 + entries[0].bytes.len();
    for cut in [3, 11, first_len + 5, first_len + 13] {
        let mut r = Cursor::new(file[..cut].to_vec());
        if cut > first_len {
            assert_eq!(record::read(&mut r).unwrap().as_ref(), Some(&entries[0]));
        }
        assert_eq!(
            record::read(&mut r).unwrap_err().kind(),
            ErrorKind::UnexpectedEof,
            "cut at {cut}"
        );
    }
    // Writer refuses what a reader would refuse.
    let mut sink = Vec::new();
    let empty = record::Entry {
        offset_us: 1,
        bytes: Vec::new(),
    };
    let big = record::Entry {
        offset_us: 1,
        bytes: vec![0; record::MAX_ENTRY + 1],
    };
    assert_eq!(
        record::write(&mut sink, &empty).unwrap_err().kind(),
        ErrorKind::InvalidInput
    );
    assert_eq!(
        record::write(&mut sink, &big).unwrap_err().kind(),
        ErrorKind::InvalidInput
    );
    assert!(sink.is_empty());
    let mut bad = 7u64.to_be_bytes().to_vec();
    bad.extend_from_slice(&((record::MAX_ENTRY as u32) + 1).to_be_bytes());
    assert_eq!(
        record::read(&mut Cursor::new(bad)).unwrap_err().kind(),
        ErrorKind::InvalidData
    );
}

#[test]
fn schema_lists_every_command_the_codec_knows() {
    let s = schema();
    assert_eq!(s["protocol"], json!(fleetmux_proto::PROTOCOL));
    assert_eq!(s["max_line_bytes"], json!(MAX_LINE));
    assert_eq!(s["view"]["max_frame_payload"], json!(view::MAX_FRAME));
    let in_schema: Vec<&str> = s["commands"]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    let in_codec = known_commands();
    assert_eq!(in_codec.len(), 22, "PROTOCOL.md §4 has 22 commands");
    assert_eq!(
        in_schema.iter().copied().collect::<BTreeSet<_>>(),
        in_codec.iter().copied().collect::<BTreeSet<_>>(),
        "schema.json and proto::known_commands() must list the same commands"
    );
    // Every name the codec knows decodes to a variant reporting that name (no stale entries either side).
    for (name, c) in s["commands"].as_object().unwrap() {
        let req = decode_line(&line(&c["examples"][0]["request"])).unwrap();
        assert_eq!(req.cmd.name(), name);
        for code in c["errors"].as_array().unwrap() {
            assert!(
                ErrorCode::parse(code.as_str().unwrap()).is_some(),
                "{name}: {code}"
            );
        }
    }
    let codes: BTreeSet<&str> = s["error_codes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(codes, ErrorCode::ALL.iter().map(|c| c.as_str()).collect());
    for g in [Group::Supervisor, Group::Lower, Group::Workers] {
        assert_eq!(Group::parse(g.as_str()), Some(g));
    }
    assert_eq!(Group::parse("left"), None);
    assert_eq!(WriteMode::parse("bracket"), None);
}

#[test]
fn peer_check_is_uid_equality() {
    assert!(peer_allowed(501, 501));
    assert!(!peer_allowed(501, 0));
    assert!(!peer_allowed(0, 501));
}

// ---- Stage A (brief-m03.md §3.4, A-R1 / A-R2 / P-2): appended, phase 0 tests above are unchanged. ----

#[test]
fn stats_reply_roundtrip_and_is_17th_known_command() {
    use fleetmux_proto::Stats;
    let s = schema();
    let ex = &s["commands"]["server.stats"]["examples"][0];
    let req = decode_line(&line(&ex["request"])).unwrap();
    assert_eq!((req.id, &req.cmd), (17, &Command::ServerStats));
    assert_eq!(req.cmd.name(), "server.stats");
    // The schema example's result decodes into Stats, and Stats goes back onto the wire unchanged.
    let mut body = ex["response"].clone();
    let obj = body.as_object_mut().unwrap();
    obj.remove("id");
    obj.remove("ok");
    let stats: Stats = serde_json::from_value(body.clone()).unwrap();
    assert_eq!(
        stats,
        Stats {
            panes: 2,
            viewers: 1,
            frames: 148,
            parse_bytes: 52344,
            wakeups: 391,
            rss_bytes: 18874368,
            scrollback_rows: 1200
        }
    );
    let wire = encode_reply(&Reply::Ok {
        id: 17,
        body: serde_json::to_value(&stats).unwrap(),
    });
    assert_eq!(reply_json(&wire), ex["response"]);
    // 22 commands: the codec's list holds server.stats once, 17th, after the 16 of phase 0 (web.* follow it, M2.5).
    let known = known_commands();
    assert_eq!(known.len(), 22);
    assert_eq!(known.get(16), Some(&"server.stats"));
    assert_eq!(known.iter().filter(|&&n| n == "server.stats").count(), 1);
}

#[test]
fn view_frame_open_roundtrip_and_rejects_bad_utf8() {
    assert_eq!(view::T_OPEN, 0x83);
    for f in [
        Frame::Open("/x/history.html".into()),
        Frame::Open(String::new()),
        Frame::Open("/tmp/é 漢/history.html".into()),
    ] {
        assert_eq!(f.type_byte(), view::T_OPEN);
        let bytes = view::encode_frame(&f).unwrap();
        let Frame::Open(path) = &f else {
            unreachable!()
        };
        assert_eq!(bytes[0], 0x83);
        assert_eq!(&bytes[1..5], &(path.len() as u32).to_be_bytes());
        assert_eq!(&bytes[5..], path.as_bytes());
        assert_eq!(
            view::read_frame(&mut Cursor::new(bytes.clone())).unwrap(),
            Some(f.clone())
        );
        assert_eq!(
            view::decode_frame(&bytes).unwrap(),
            Some((f.clone(), bytes.len()))
        );
    }
    assert!(matches!(
        view::decode_frame(&[0x83, 0, 0, 0, 1, 0xff]),
        Err(FrameError::BadUtf8)
    ));
    assert!(matches!(
        view::read_frame(&mut Cursor::new(vec![0x83, 0, 0, 0, 2, 0xc3, 0x28])),
        Err(FrameError::BadUtf8)
    ));
    assert!(matches!(
        view::encode_frame(&Frame::Open("x".repeat(view::MAX_FRAME + 1))),
        Err(FrameError::Oversize { .. })
    ));
}

#[test]
fn pane_info_optional_keys_default_when_absent() {
    use fleetmux_proto::PaneInfo;
    // The phase 0 pane.get example has neither key.
    let s = schema();
    let mut rec = s["commands"]["pane.get"]["examples"][0]["response"].clone();
    let obj = rec.as_object_mut().unwrap();
    obj.remove("id");
    obj.remove("ok");
    assert!(obj.get("unseen").is_none() && obj.get("scrollback").is_none());
    let old: PaneInfo = serde_json::from_value(rec.clone()).unwrap();
    assert_eq!((old.unseen, old.scrollback), (false, 0));
    assert_eq!(old.pane, "k7q2-12");
    // With the keys present they are read, and the server always writes them.
    let mut new = rec.clone();
    new["unseen"] = json!(true);
    new["scrollback"] = json!(4200);
    let info: PaneInfo = serde_json::from_value(new.clone()).unwrap();
    assert_eq!((info.unseen, info.scrollback), (true, 4200));
    assert_eq!(serde_json::to_value(&info).unwrap(), new);
    let wire = reply_json(&encode_reply(&Reply::Ok {
        id: 6,
        body: serde_json::to_value(&old).unwrap(),
    }));
    assert_eq!(
        (&wire["unseen"], &wire["scrollback"]),
        (&json!(false), &json!(0))
    );
    // The schema lists both as optional pane-record keys.
    let pr = &s["$defs"]["pane_record"];
    let required: Vec<&str> = pr["required"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    for k in ["unseen", "scrollback"] {
        assert!(pr["properties"].get(k).is_some(), "{k}");
        assert!(!required.contains(&k), "{k} must stay optional");
    }
}

// ---------------------------------------------------------------- M2.5 web frames (brief-m25 §3.1 W0; green in W2)

#[test]
#[ignore = "M2.5: web-relay"]
fn frame_0x84_roundtrip() {
    assert_eq!(view::T_WEB, 0x84);
    let line = r#"{"seq":7,"op":"goto","url":"file:///r/history.html?x=1"}"#;
    let f = Frame::Web(line.into());
    assert_eq!(f.type_byte(), 0x84);
    let bytes = view::encode_frame(&f).unwrap();
    assert_eq!(bytes[0], 0x84);
    assert_eq!(&bytes[1..5], &(line.len() as u32).to_be_bytes());
    assert_eq!(&bytes[5..], line.as_bytes());
    assert_eq!(
        view::read_frame(&mut Cursor::new(bytes.clone())).unwrap(),
        Some(f.clone())
    );
    assert_eq!(view::decode_frame(&bytes).unwrap(), Some((f, bytes.len())));
    let bad = [0x84, 0, 0, 0, 2, 0xff, 0xfe];
    assert!(matches!(
        view::read_frame(&mut Cursor::new(bad.to_vec())),
        Err(FrameError::BadUtf8)
    ));
}

#[test]
#[ignore = "M2.5: web-relay"]
fn frame_0x04_roundtrip() {
    assert_eq!(view::T_WEB_REPLY, 0x04);
    for line in [
        r#"{"seq":7,"ok":true,"url":"file:///r/history.html?x=1"}"#,
        r#"{"event":"window","state":"open"}"#,
        r#"{"op":"page","path":"/r/history.html","have":null}"#,
    ] {
        let f = Frame::WebReply(line.into());
        assert_eq!(f.type_byte(), 0x04);
        let bytes = view::encode_frame(&f).unwrap();
        assert_eq!((bytes[0], &bytes[5..]), (0x04, line.as_bytes()));
        assert_eq!(
            view::read_frame(&mut Cursor::new(bytes.clone())).unwrap(),
            Some(f.clone())
        );
        assert_eq!(view::decode_frame(&bytes).unwrap(), Some((f, bytes.len())));
    }
}

#[test]
#[ignore = "M2.5: web-relay"]
fn frame_0x84_cap_32mib() {
    assert_eq!(view::WEB_FRAME_MAX, 32 * 1024 * 1024);
    assert_eq!(view::max_payload(view::T_WEB), view::WEB_FRAME_MAX);
    // A 0x84 well over 1 MiB (a 5 MB history.html, design.md §3.5.3) and one at exactly 32 MiB pass.
    for len in [5 * 1024 * 1024, view::WEB_FRAME_MAX] {
        let f = Frame::Web("x".repeat(len));
        let bytes = view::encode_frame(&f).unwrap();
        assert_eq!(bytes.len(), 5 + len);
        assert_eq!(view::decode_frame(&bytes).unwrap(), Some((f, bytes.len())));
    }
    // One byte more is refused on write, and a header announcing it is refused before the payload is read.
    let over = Frame::Web("x".repeat(view::WEB_FRAME_MAX + 1));
    let mut out = Vec::new();
    assert!(matches!(
        view::write_frame(&mut out, &over),
        Err(FrameError::Oversize { .. })
    ));
    assert!(out.is_empty());
    let mut hdr = vec![0x84];
    hdr.extend_from_slice(&((view::WEB_FRAME_MAX + 1) as u32).to_be_bytes());
    assert!(matches!(
        view::read_frame(&mut Cursor::new(hdr)),
        Err(FrameError::Oversize { .. })
    ));
}

#[test]
#[ignore = "M2.5: web-relay"]
fn other_frames_keep_1mib_cap() {
    assert_eq!(view::MAX_FRAME, 1024 * 1024);
    for ty in [
        view::T_INPUT,
        view::T_SCREEN,
        view::T_EXIT,
        view::T_OPEN,
        view::T_WEB_REPLY,
    ] {
        assert_eq!(view::max_payload(ty), view::MAX_FRAME, "type {ty:#04x}");
    }
    // 0x04 (client → server) does not share 0x84's cap: over 1 MiB is refused like any other type.
    let over = Frame::WebReply("x".repeat(view::MAX_FRAME + 1));
    assert!(matches!(
        view::encode_frame(&over),
        Err(FrameError::Oversize { .. })
    ));
    let mut hdr = vec![0x04];
    hdr.extend_from_slice(&((view::MAX_FRAME + 1) as u32).to_be_bytes());
    assert!(matches!(
        view::read_frame(&mut Cursor::new(hdr)),
        Err(FrameError::Oversize { .. })
    ));
    let screen = Frame::Screen(vec![b'x'; view::MAX_FRAME + 1]);
    assert!(matches!(
        view::encode_frame(&screen),
        Err(FrameError::Oversize { .. })
    ));
}

// ---------------------------------------------------------------- M2.6 overlay frame (brief-m26 §3.1 W0; green in W2)

#[test]
#[ignore = "M2.6: W2"]
fn frame_0x85_roundtrip() {
    let line =
        r#"{"pane":"k7q2-3","rect":{"x":1,"y":36,"w":118,"h":22},"url":"file:///r/history.html"}"#;
    let f = Frame::Overlay(line.to_string());
    assert_eq!(f.type_byte(), 0x85);
    assert_eq!(view::T_OVERLAY, 0x85);
    let bytes = view::encode_frame(&f).unwrap();
    assert_eq!(bytes[0], 0x85);
    assert_eq!(&bytes[1..5], &(line.len() as u32).to_be_bytes());
    assert_eq!(&bytes[5..], line.as_bytes());
    assert_eq!(
        view::decode_frame(&bytes).unwrap(),
        Some((f.clone(), bytes.len()))
    );
    assert_eq!(
        view::read_frame(&mut Cursor::new(bytes.clone())).unwrap(),
        Some(f)
    );
    // A text frame like 0x83: 1 MiB cap, UTF-8 only.
    assert_eq!(view::max_payload(view::T_OVERLAY), view::MAX_FRAME);
    let over = Frame::Overlay("x".repeat(view::MAX_FRAME + 1));
    assert!(matches!(
        view::encode_frame(&over),
        Err(FrameError::Oversize { .. })
    ));
    let mut bad = vec![0x85];
    bad.extend_from_slice(&2u32.to_be_bytes());
    bad.extend_from_slice(&[0xff, 0xfe]);
    assert!(matches!(
        view::read_frame(&mut Cursor::new(bad)),
        Err(FrameError::BadUtf8)
    ));
}

#[test]
#[ignore = "M2.6: W2"]
fn hello_overlay_key_is_opt_in() {
    let base = json!({"view": 1, "cols": 80, "rows": 24, "term": "xterm-256color", "colorterm": "truecolor"});
    let with = |v: Value| {
        let mut h = base.clone();
        h["overlay"] = v;
        line(&h)
    };
    assert_eq!(view::wants_overlay(&line(&base)), Ok(false));
    assert_eq!(view::wants_overlay(&with(json!(0))), Ok(false));
    assert_eq!(view::wants_overlay(&with(json!(1))), Ok(true));
    // The handshake itself is unchanged: `Hello` ignores the key either way.
    assert_eq!(
        view::Hello::decode(&with(json!(1))).unwrap(),
        view::Hello::decode(&line(&base)).unwrap()
    );
}

#[test]
#[ignore = "M2.6: W2"]
fn hello_overlay_bad_value_is_bad_request() {
    let base = json!({"view": 1, "cols": 80, "rows": 24, "term": "xterm-256color", "colorterm": "truecolor"});
    // Supervisor ruling on W0 #1: only a missing key / integer 0 / integer 1 are valid; explicit null is not.
    for bad in [
        json!(null),
        json!(1.0),
        json!(2),
        json!(-1),
        json!(1.5),
        json!("1"),
        json!(true),
        json!([1]),
        json!({}),
    ] {
        let mut h = base.clone();
        h["overlay"] = bad.clone();
        let err = view::wants_overlay(&line(&h)).expect_err(&format!("overlay {bad}"));
        assert_eq!(err.code, ErrorCode::BadRequest, "overlay {bad}");
    }
    let err = view::wants_overlay(b"not json\n").expect_err("not a JSON object");
    assert_eq!(err.code, ErrorCode::BadRequest);
}

#[test]
#[ignore = "M2.6: W2"]
fn overlay_json_roundtrip_with_nulls() {
    let full = view::Overlay {
        pane: Some("k7q2-3".into()),
        rect: Some(view::CellRect {
            x: 1,
            y: 36,
            w: 118,
            h: 22,
        }),
        url: Some("file:///r/history.html?x=1#h".into()),
        topbar: Some(view::Topbar {
            title: "fleet-a".into(),
            status: "2 workers".into(),
            sidebar: 36,
            cols: 150,
        }),
    };
    let s = full.encode();
    assert!(!s.contains('\n'), "{s}");
    assert_eq!(
        serde_json::from_str::<Value>(&s).unwrap(),
        json!({"pane": "k7q2-3", "rect": {"x": 1, "y": 36, "w": 118, "h": 22},
               "url": "file:///r/history.html?x=1#h",
               "topbar": {"title": "fleet-a", "status": "2 workers", "sidebar": 36, "cols": 150}})
    );
    assert_eq!(view::Overlay::decode(&s).unwrap(), full);
    let none = view::Overlay::default();
    let s = none.encode();
    assert_eq!(
        serde_json::from_str::<Value>(&s).unwrap(),
        json!({"pane": null, "rect": null, "url": null, "topbar": null})
    );
    assert_eq!(view::Overlay::decode(&s).unwrap(), none);
    let hidden = view::Overlay {
        pane: Some("k7q2-3".into()),
        rect: None,
        url: Some("file:///r/history.html".into()),
        topbar: None,
    };
    assert_eq!(view::Overlay::decode(&hidden.encode()).unwrap(), hidden);
    for bad in [
        "",
        "[]",
        r#"{"pane":null,"rect":null}"#,
        r#"{"pane":1,"rect":null,"url":null}"#,
        r#"{"pane":null,"rect":{"x":1,"y":2,"w":3},"url":null}"#,
        r#"{"pane":null,"rect":{"x":1,"y":2,"w":3,"h":70000},"url":null}"#,
        r#"{"pane":null,"rect":null,"url":null,"topbar":"x"}"#,
        r#"{"pane":null,"rect":null,"url":null,"topbar":{"title":"a"}}"#,
        r#"{"pane":null,"rect":null,"url":null,"topbar":{"title":1,"status":"b"}}"#,
        r#"{"pane":null,"rect":null,"url":null,"topbar":{"title":"a","status":"b","sidebar":-1}}"#,
        r#"{"pane":null,"rect":null,"url":null,"topbar":{"title":"a","status":"b","cols":70000}}"#,
    ] {
        assert!(view::Overlay::decode(bad).is_err(), "{bad}");
    }
}

/// A26 rev7 (`CURRENT_REQUIREMENTS` rev 10 `top_container_alignment`): the `0x85` `topbar` object carries the
/// sidebar band geometry next to the title/status, and both geometry keys are optional, so a rev 6 payload (or an
/// older server) still decodes with 0 = "no geometry" and the headerbar keeps its fallback boundary.
#[test]
fn overlay_topbar_geometry_is_optional_and_roundtrips() {
    let with = view::Overlay {
        topbar: Some(view::Topbar {
            title: "fleet-a".into(),
            status: "2 workers".into(),
            sidebar: 36,
            cols: 150,
        }),
        ..view::Overlay::default()
    };
    let s = with.encode();
    assert_eq!(
        serde_json::from_str::<Value>(&s).unwrap()["topbar"],
        json!({"title": "fleet-a", "status": "2 workers", "sidebar": 36, "cols": 150})
    );
    assert_eq!(view::Overlay::decode(&s).unwrap(), with);
    let old = view::Overlay::decode(
        r#"{"pane":null,"rect":null,"url":null,"topbar":{"title":"a","status":"b"}}"#,
    )
    .expect("a rev 6 topbar object decodes");
    assert_eq!(
        old.topbar,
        Some(view::Topbar {
            title: "a".into(),
            status: "b".into(),
            sidebar: 0,
            cols: 0,
        }),
        "absent geometry keys mean 0, never an error"
    );
    assert_eq!(
        view::Overlay::decode(
            r#"{"pane":null,"rect":null,"url":null,"topbar":{"title":"a","status":"b","sidebar":0,"cols":0}}"#
        )
        .unwrap(),
        old
    );
}

// ---------------------------------------------------------------- M2.6 scroll frames (gui-scrollbar §5 ①)

#[test]
fn frame_0x86_and_0x05_roundtrip_and_reject_bad_utf8() {
    // 0x86 server → client: the scroll state, one JSON line, 1 MiB cap, UTF-8 only.
    let state = r#"{"panes":[{"pane":"k7q2-3","rect":{"x":1,"y":1,"w":118,"h":33},"offset":0,"total":412}]}"#;
    let f = Frame::Scroll(state.to_string());
    assert_eq!(f.type_byte(), 0x86);
    assert_eq!(view::T_SCROLL, 0x86);
    let bytes = view::encode_frame(&f).unwrap();
    assert_eq!(bytes[0], 0x86);
    assert_eq!(&bytes[1..5], &(state.len() as u32).to_be_bytes());
    assert_eq!(&bytes[5..], state.as_bytes());
    assert_eq!(
        view::decode_frame(&bytes).unwrap(),
        Some((f.clone(), bytes.len()))
    );
    assert_eq!(
        view::read_frame(&mut Cursor::new(bytes.clone())).unwrap(),
        Some(f)
    );
    assert_eq!(view::max_payload(view::T_SCROLL), view::MAX_FRAME);
    let over = Frame::Scroll("x".repeat(view::MAX_FRAME + 1));
    assert!(matches!(
        view::encode_frame(&over),
        Err(FrameError::Oversize { .. })
    ));
    let mut bad = vec![0x86];
    bad.extend_from_slice(&2u32.to_be_bytes());
    bad.extend_from_slice(&[0xff, 0xfe]);
    assert!(matches!(
        view::read_frame(&mut Cursor::new(bad)),
        Err(FrameError::BadUtf8)
    ));

    // 0x05 client → server: scroll this pane to this offset, same shape rules.
    let to = r#"{"pane":"k7q2-7","offset":57}"#;
    let f = Frame::ScrollTo(to.to_string());
    assert_eq!(f.type_byte(), 0x05);
    assert_eq!(view::T_SCROLL_TO, 0x05);
    let bytes = view::encode_frame(&f).unwrap();
    assert_eq!(bytes[0], 0x05);
    assert_eq!(&bytes[1..5], &(to.len() as u32).to_be_bytes());
    assert_eq!(&bytes[5..], to.as_bytes());
    assert_eq!(
        view::decode_frame(&bytes).unwrap(),
        Some((f.clone(), bytes.len()))
    );
    assert_eq!(
        view::read_frame(&mut Cursor::new(bytes.clone())).unwrap(),
        Some(f)
    );
    assert_eq!(view::max_payload(view::T_SCROLL_TO), view::MAX_FRAME);
    let over = Frame::ScrollTo("x".repeat(view::MAX_FRAME + 1));
    assert!(matches!(
        view::encode_frame(&over),
        Err(FrameError::Oversize { .. })
    ));
    let mut bad = vec![0x05];
    bad.extend_from_slice(&2u32.to_be_bytes());
    bad.extend_from_slice(&[0xff, 0xfe]);
    assert!(matches!(
        view::read_frame(&mut Cursor::new(bad)),
        Err(FrameError::BadUtf8)
    ));
    // Neither type has a length rule beyond the cap.
    let mut empty = vec![0x86];
    empty.extend_from_slice(&0u32.to_be_bytes());
    assert_eq!(
        view::read_frame(&mut Cursor::new(empty)).unwrap(),
        Some(Frame::Scroll(String::new()))
    );
}

#[test]
fn hello_scroll_key_is_opt_in() {
    let base = json!({"view": 1, "cols": 80, "rows": 24, "term": "xterm-256color", "colorterm": "truecolor"});
    let with = |v: Value| {
        let mut h = base.clone();
        h["scroll"] = v;
        line(&h)
    };
    assert_eq!(view::wants_scroll(&line(&base)), Ok(false));
    assert_eq!(view::wants_scroll(&with(json!(0))), Ok(false));
    assert_eq!(view::wants_scroll(&with(json!(1))), Ok(true));
    // Independent of `overlay`: declaring one does not imply the other.
    let mut overlay_only = base.clone();
    overlay_only["overlay"] = json!(1);
    assert_eq!(view::wants_scroll(&line(&overlay_only)), Ok(false));
    assert_eq!(view::wants_overlay(&with(json!(1))), Ok(false));
    let mut both = base.clone();
    both["overlay"] = json!(1);
    both["scroll"] = json!(1);
    assert_eq!(view::wants_overlay(&line(&both)), Ok(true));
    assert_eq!(view::wants_scroll(&line(&both)), Ok(true));
    // The handshake itself is unchanged: `Hello` ignores the key either way.
    assert_eq!(
        view::Hello::decode(&with(json!(1))).unwrap(),
        view::Hello::decode(&line(&base)).unwrap()
    );
}

#[test]
fn hello_scroll_bad_value_is_bad_request() {
    let base = json!({"view": 1, "cols": 80, "rows": 24, "term": "xterm-256color", "colorterm": "truecolor"});
    for bad in [
        json!(null),
        json!(1.0),
        json!(2),
        json!(-1),
        json!(1.5),
        json!("1"),
        json!(true),
        json!([1]),
        json!({}),
    ] {
        let mut h = base.clone();
        h["scroll"] = bad.clone();
        let err = view::wants_scroll(&line(&h)).expect_err(&format!("scroll {bad}"));
        assert_eq!(err.code, ErrorCode::BadRequest, "scroll {bad}");
    }
    let err = view::wants_scroll(b"not json\n").expect_err("not a JSON object");
    assert_eq!(err.code, ErrorCode::BadRequest);
}

#[test]
fn scroll_state_json_roundtrip() {
    let full = view::ScrollState {
        panes: vec![
            view::PaneScroll {
                pane: "k7q2-3".into(),
                rect: view::CellRect {
                    x: 1,
                    y: 1,
                    w: 118,
                    h: 33,
                },
                offset: 0,
                total: 412,
            },
            view::PaneScroll {
                pane: "k7q2-7".into(),
                rect: view::CellRect {
                    x: 121,
                    y: 1,
                    w: 118,
                    h: 57,
                },
                offset: 40,
                total: 1000,
            },
        ],
    };
    let s = full.encode();
    assert!(!s.contains('\n'), "{s}");
    assert_eq!(
        serde_json::from_str::<Value>(&s).unwrap(),
        json!({"panes": [
            {"pane": "k7q2-3", "rect": {"x": 1, "y": 1, "w": 118, "h": 33}, "offset": 0, "total": 412},
            {"pane": "k7q2-7", "rect": {"x": 121, "y": 1, "w": 118, "h": 57}, "offset": 40, "total": 1000}
        ]})
    );
    assert_eq!(view::ScrollState::decode(&s).unwrap(), full);
    // An empty state is legal (zoom on the history pane, or nothing visible).
    let empty = view::ScrollState::default();
    assert_eq!(
        serde_json::from_str::<Value>(&empty.encode()).unwrap(),
        json!({"panes": []})
    );
    assert_eq!(view::ScrollState::decode(&empty.encode()).unwrap(), empty);
    for bad in [
        "",
        "[]",
        "{}",
        r#"{"panes":null}"#,
        r#"{"panes":{}}"#,
        r#"{"panes":[{"rect":{"x":1,"y":2,"w":3,"h":4},"offset":0,"total":0}]}"#,
        r#"{"panes":[{"pane":1,"rect":{"x":1,"y":2,"w":3,"h":4},"offset":0,"total":0}]}"#,
        r#"{"panes":[{"pane":"p","rect":null,"offset":0,"total":0}]}"#,
        r#"{"panes":[{"pane":"p","rect":{"x":1,"y":2,"w":3},"offset":0,"total":0}]}"#,
        r#"{"panes":[{"pane":"p","rect":{"x":1,"y":2,"w":3,"h":70000},"offset":0,"total":0}]}"#,
        r#"{"panes":[{"pane":"p","rect":{"x":1,"y":2,"w":3,"h":4},"offset":-1,"total":0}]}"#,
        r#"{"panes":[{"pane":"p","rect":{"x":1,"y":2,"w":3,"h":4},"offset":0,"total":1.0}]}"#,
        r#"{"panes":[{"pane":"p","rect":{"x":1,"y":2,"w":3,"h":4},"offset":0}]}"#,
    ] {
        assert!(view::ScrollState::decode(bad).is_err(), "{bad}");
    }

    let to = view::ScrollTo {
        pane: "k7q2-7".into(),
        offset: 57,
    };
    let s = to.encode();
    assert!(!s.contains('\n'), "{s}");
    assert_eq!(
        serde_json::from_str::<Value>(&s).unwrap(),
        json!({"pane": "k7q2-7", "offset": 57})
    );
    assert_eq!(view::ScrollTo::decode(&s).unwrap(), to);
    for bad in [
        "",
        "[]",
        r#"{"pane":"p"}"#,
        r#"{"offset":1}"#,
        r#"{"pane":1,"offset":1}"#,
        r#"{"pane":"p","offset":-1}"#,
        r#"{"pane":"p","offset":1.5}"#,
        r#"{"pane":"p","offset":"1"}"#,
    ] {
        assert!(view::ScrollTo::decode(bad).is_err(), "{bad}");
    }
}

#[test]
fn wants_inset_reads_the_handshake_key() {
    // S1e (PROTOCOL.md §6.1, plan §17.3): the optional `inset` key, an integer 0-40, absent = 0 columns.
    let base = json!({"view": 1, "cols": 80, "rows": 24, "term": "xterm-256color", "colorterm": "truecolor"});
    let with = |v: Value| {
        let mut h = base.clone();
        h["inset"] = v;
        line(&h)
    };
    assert_eq!(
        view::wants_inset(&line(&base)),
        Ok(0),
        "a handshake without an inset key is 0 columns"
    );
    assert_eq!(view::wants_inset(&with(json!(0))), Ok(0), "inset 0");
    assert_eq!(view::wants_inset(&with(json!(10))), Ok(10), "inset 10");
    assert_eq!(
        view::wants_inset(&with(json!(40))),
        Ok(40),
        "inset 40 is the maximum"
    );
    // The handshake itself is unchanged: `Hello` ignores the key.
    assert_eq!(
        view::Hello::decode(&with(json!(10))).unwrap(),
        view::Hello::decode(&line(&base)).unwrap()
    );
    for bad in [
        json!(null),
        json!(-1),
        json!(41),
        json!(1.0),
        json!("10"),
        json!(true),
        json!([10]),
    ] {
        let err = view::wants_inset(&with(bad.clone())).expect_err(&format!("inset {bad}"));
        assert_eq!(
            err.code,
            ErrorCode::BadRequest,
            "inset {bad} must be bad_request"
        );
    }
    let err = view::wants_inset(b"not json\n").expect_err("not a JSON object");
    assert_eq!(err.code, ErrorCode::BadRequest);
}

#[test]
fn ok_line_carries_the_inset_flag() {
    // S1e (PROTOCOL.md §6.1, plan §17.3): a server that understands inset says `"inset":1` on its `ok` line, so
    // the page only sends `0x06` to servers that know it.
    let ok = view::HelloReply::Ok {
        epoch: String::from("e1"),
        protocol: 1,
    };
    let plain = ok.encode();
    assert!(
        !String::from_utf8(plain).unwrap().contains("inset"),
        "the plain ok line must not claim inset support"
    );
    let with = ok.encode_with_inset();
    let v: Value = serde_json::from_slice(&with).expect("the ok line is JSON");
    assert_eq!(v["ok"], json!(true), "the ok line: {v}");
    assert_eq!(
        v["inset"],
        json!(1),
        "the ok line claims inset support: {v}"
    );
    assert_eq!(
        view::HelloReply::decode(&with).unwrap(),
        ok,
        "an old client still decodes the ok line with the extra key"
    );
    let err = view::HelloReply::Err {
        code: ErrorCode::BadRequest,
        error: String::from("nope"),
    };
    assert_eq!(
        err.encode_with_inset(),
        err.encode(),
        "an error line carries no inset flag"
    );
}

// ---- M4 (design m4 §4.2, PROTOCOL.md §2 addendum / §4.23–§4.26): page commands and the optional page keys ----

#[test]
fn m4_page_commands_decode_with_their_defaults() {
    let d = |v: Value| decode_line(&line(&v)).unwrap_or_else(|e| panic!("{v}: {e:?}"));
    assert_eq!(
        d(json!({"id": 1, "cmd": "page.open", "page": "M0", "title": "M0 · build"})).cmd,
        Command::PageOpen {
            page: "M0".into(),
            title: Some("M0 · build".into()),
            reclaimed: false,
            history_url: None,
        }
    );
    assert_eq!(
        d(json!({"id": 2, "cmd": "page.open", "page": "studio-ops", "reclaimed": true,
                 "history_url": "http://127.0.0.1:8765/history.html?run=x"}))
        .cmd,
        Command::PageOpen {
            page: "studio-ops".into(),
            title: None,
            reclaimed: true,
            history_url: Some("http://127.0.0.1:8765/history.html?run=x".into()),
        }
    );
    assert_eq!(
        d(json!({"id": 3, "cmd": "page.close", "page": "M0.1"})).cmd,
        Command::PageClose { page: "M0.1".into(), keep: true },
        "keep defaults to true"
    );
    assert_eq!(
        d(json!({"id": 4, "cmd": "page.close", "page": "M0.1", "keep": false})).cmd,
        Command::PageClose { page: "M0.1".into(), keep: false }
    );
    assert_eq!(
        d(json!({"id": 5, "cmd": "page.select", "page": ""})).cmd,
        Command::PageSelect { page: String::new() },
        "page.select accepts the root page"
    );
    assert_eq!(
        d(json!({"id": 6, "cmd": "page.select", "page": "M3"})).cmd,
        Command::PageSelect { page: "M3".into() }
    );
    assert_eq!(d(json!({"id": 7, "cmd": "page.list"})).cmd, Command::PageList);
    for (name, v) in [
        ("page.open", json!({"id": 8, "cmd": "page.open", "page": "M0"})),
        ("page.close", json!({"id": 8, "cmd": "page.close", "page": "M0"})),
        ("page.select", json!({"id": 8, "cmd": "page.select", "page": "M0"})),
        ("page.list", json!({"id": 8, "cmd": "page.list"})),
    ] {
        assert_eq!(d(v).cmd.name(), name);
    }
}

#[test]
fn m4_page_ids_are_validated_and_the_root_cannot_be_opened() {
    assert_eq!(fleetmux_proto::MAX_PAGES, 64);
    for ok in ["M0", "M0.1", "studio-ops", "a_b", "Z9", &"x".repeat(64)] {
        assert!(fleetmux_proto::valid_page_id(ok), "{ok:?} is a page id");
    }
    for bad in ["", "M0 (M0-attempt-2)", "a/b", "a:b", "é", &"x".repeat(65)] {
        assert!(!fleetmux_proto::valid_page_id(bad), "{bad:?} is not a page id");
    }
    let bad_request = |v: Value| {
        let e = decode_line(&line(&v)).expect_err(&format!("{v} must be refused"));
        assert_eq!(e.code, ErrorCode::BadRequest, "{v}");
        assert_eq!(e.id, v["id"].as_u64(), "{v}");
    };
    for cmd in ["page.open", "page.close"] {
        bad_request(json!({"id": 11, "cmd": cmd, "page": ""}));
        bad_request(json!({"id": 12, "cmd": cmd}));
        bad_request(json!({"id": 13, "cmd": cmd, "page": "a b"}));
        bad_request(json!({"id": 14, "cmd": cmd, "page": "x".repeat(65)}));
        bad_request(json!({"id": 15, "cmd": cmd, "page": 7}));
    }
    bad_request(json!({"id": 16, "cmd": "page.select"}));
    bad_request(json!({"id": 17, "cmd": "page.select", "page": "a:b"}));
    bad_request(json!({"id": 18, "cmd": "page.open", "page": "M0", "reclaimed": "yes"}));
    bad_request(json!({"id": 19, "cmd": "page.close", "page": "M0", "keep": 1}));
    bad_request(json!({"id": 20, "cmd": "hello", "page": "a b"}));
    bad_request(json!({"id": 21, "cmd": "pane.list", "all": "true"}));
    // No new error code: unknown pages are `bad_request` too (decided by the server, design m4 §4.2).
    assert!(ErrorCode::BadRequest.as_str() == "bad_request");
}

#[test]
fn m4_page_and_all_keys_are_optional_and_absent_means_m3() {
    let d = |v: Value| decode_line(&line(&v)).unwrap_or_else(|e| panic!("{v}: {e:?}"));
    // hello: no page / null / "" = unbound (the root page), the same command as M3.
    let plain = d(json!({"id": 1, "cmd": "hello", "protocol": 1}));
    assert_eq!((plain.page.clone(), plain.all), (None, false));
    for same in [json!(null), json!("")] {
        let r = d(json!({"id": 1, "cmd": "hello", "protocol": 1, "page": same}));
        assert_eq!(r, plain, "hello page {same}");
    }
    let bound = d(json!({"id": 1, "cmd": "hello", "protocol": 1, "page": "M0"}));
    assert_eq!(bound.page.as_deref(), Some("M0"));
    assert_eq!(bound.cmd, plain.cmd, "the page key does not change the hello command");
    // pane.spawn: the optional target page.
    let spawn = json!({"id": 4, "cmd": "pane.spawn", "argv": ["sh"], "cwd": "/w", "title": "t", "type": "history"});
    let mut into = spawn.clone();
    into["page"] = json!("studio-ops");
    assert_eq!(d(spawn.clone()).page, None);
    assert_eq!(d(into.clone()).page.as_deref(), Some("studio-ops"));
    assert_eq!(d(into).cmd, d(spawn).cmd);
    // pane.list: all.
    assert!(!d(json!({"id": 5, "cmd": "pane.list"})).all);
    assert!(d(json!({"id": 5, "cmd": "pane.list", "all": true})).all);
    assert!(!d(json!({"id": 5, "cmd": "pane.list", "all": false})).all);
    // Other commands ignore both keys (unknown keys are ignored, PROTOCOL.md §3).
    let kv = d(json!({"id": 6, "cmd": "kv.get", "key": "run", "page": "M0", "all": true}));
    assert_eq!((kv.page, kv.all), (None, false));
}

#[test]
fn m4_page_commands_are_listed_apart_from_the_22_m3_commands() {
    assert_eq!(known_commands().len(), 22, "the M3 command list (and schema.json) is unchanged");
    assert_eq!(
        fleetmux_proto::page_commands(),
        &["page.open", "page.close", "page.select", "page.list"]
    );
    for p in fleetmux_proto::page_commands() {
        assert!(!known_commands().contains(p), "{p} is not an M3 command");
        let e = decode_line(&line(&json!({"id": 9, "cmd": p})));
        assert!(
            !matches!(e, Err(ref e) if e.code == ErrorCode::UnknownCmd),
            "{p} is decoded, not unknown_cmd"
        );
    }
}

// ---- M4 schema.json (supervisor [M29]): page commands listed apart, optional page keys, view `pages` ----

#[test]
fn m4_schema_lists_the_page_commands_apart_and_their_examples_decode() {
    let s = schema();
    let pages = s["page_commands"]
        .as_object()
        .expect("schema.json has a top-level page_commands object");
    let in_schema: BTreeSet<&str> = pages.keys().map(String::as_str).collect();
    let in_codec: BTreeSet<&str> = fleetmux_proto::page_commands().iter().copied().collect();
    assert_eq!(
        in_schema, in_codec,
        "schema.json page_commands and proto::page_commands() must list the same commands"
    );
    let common: Vec<&str> = s["common_errors"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c.as_str().unwrap())
        .collect();
    for (name, c) in pages {
        assert!(
            s["commands"].get(name).is_none(),
            "{name} must not be among the 22 M3 commands"
        );
        assert_eq!(c["request"]["properties"]["cmd"]["const"], json!(name), "{name}");
        let errors: Vec<&str> = c["errors"]
            .as_array()
            .unwrap_or_else(|| panic!("{name} has no errors list"))
            .iter()
            .map(|e| e.as_str().unwrap())
            .collect();
        for code in &errors {
            assert!(ErrorCode::parse(code).is_some(), "{name}: {code}");
        }
        let examples = c["examples"].as_array().unwrap();
        assert!(!examples.is_empty(), "{name} has no example");
        for ex in examples {
            let req = &ex["request"];
            let got = decode_line(&line(req)).unwrap_or_else(|e| panic!("{name}: {e:?}"));
            assert_eq!(got.id, req["id"].as_u64().unwrap(), "{name}");
            assert_eq!(got.cmd.name(), name.as_str());
            let resp = &ex["response"];
            assert_eq!(resp["id"], req["id"], "{name}");
            if resp["ok"] == json!(false) {
                let code = resp["code"].as_str().unwrap();
                assert!(
                    ErrorCode::parse(code).is_some()
                        && (errors.contains(&code) || common.contains(&code)),
                    "{name}: example error {code} is not listed"
                );
            } else {
                assert_eq!(resp["ok"], json!(true), "{name}");
            }
        }
    }
    // Spot-check that arguments land in the right fields.
    let ex = |n: &str| line(&s["page_commands"][n]["examples"][0]["request"]);
    assert_eq!(
        decode_line(&ex("page.open")).unwrap().cmd,
        Command::PageOpen {
            page: "M0".into(),
            title: Some("M0 build".into()),
            reclaimed: false,
            history_url: None,
        }
    );
    assert_eq!(
        decode_line(&ex("page.close")).unwrap().cmd,
        Command::PageClose { page: "M0".into(), keep: true }
    );
    assert_eq!(
        decode_line(&ex("page.select")).unwrap().cmd,
        Command::PageSelect { page: "M0".into() }
    );
    assert_eq!(decode_line(&ex("page.list")).unwrap().cmd, Command::PageList);
}

#[test]
fn m4_schema_page_keys_are_optional_and_the_m3_contract_is_unchanged() {
    let s = schema();
    assert_eq!(s["protocol"], json!(1));
    assert_eq!(s["commands"].as_object().unwrap().len(), 22);
    assert_eq!(s["error_codes"].as_array().unwrap().len(), 14);
    let optional = |at: &Value, key: &str, what: &str| {
        assert!(
            at["properties"].get(key).is_some(),
            "{what} lacks the optional key {key:?}"
        );
        assert!(
            !at["required"].as_array().unwrap().iter().any(|r| r == key),
            "{what}: {key:?} must stay optional"
        );
    };
    let c = &s["commands"];
    optional(&c["hello"]["request"], "page", "hello request");
    for k in ["pages", "page", "current"] {
        optional(&c["hello"]["response"], k, "hello response");
    }
    optional(&c["pane.spawn"]["request"], "page", "pane.spawn request");
    optional(&c["pane.list"]["request"], "all", "pane.list request");
    optional(&s["$defs"]["pane_record"], "page", "pane_record");
    assert_eq!(c["hello"]["response"]["properties"]["pages"]["const"], json!(1));
    // The codec reads the keys as the schema describes them.
    let req = |v: Value| decode_line(&line(&v)).unwrap();
    assert_eq!(
        req(json!({"id": 1, "cmd": "hello", "protocol": 1, "page": "M0"})).page.as_deref(),
        Some("M0")
    );
    assert!(req(json!({"id": 5, "cmd": "pane.list", "all": true})).all);
}

#[test]
fn m4_schema_view_hello_reply_may_declare_pages() {
    let s = schema();
    let ok = &s["view"]["hello_reply"]["oneOf"][0];
    assert_eq!(
        ok["properties"]["pages"]["const"],
        json!(1),
        "view ok line: optional pages:1"
    );
    assert!(!ok["required"].as_array().unwrap().iter().any(|r| r == "pages"));
    let mut reply = s["view"]["examples"][0]["hello_reply"].clone();
    let plain = view::HelloReply::decode(&line(&reply)).unwrap();
    reply["pages"] = ok["properties"]["pages"]["const"].clone();
    assert_eq!(
        view::HelloReply::decode(&line(&reply)).unwrap(),
        plain,
        "a client that does not know pages:1 reads the same reply"
    );
}
