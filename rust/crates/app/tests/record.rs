//! `--record` tests (brief-m03 §3.4, M3, package B20 `record-hook`; PROTOCOL.md §7). Stage A: red until B20
//! (and B6 for the server binary); ignored until M3.
#![allow(dead_code)]

use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

use fleetmux::server::record::Recorder;
use fleetmux_proto::record::{self, Entry, MAX_ENTRY};
use serde_json::{Value, json};

const BIN: &str = env!("CARGO_BIN_EXE_fleetmux");
const TIMEOUT: Duration = Duration::from_secs(10);

static SEQ: AtomicU32 = AtomicU32::new(0);

/// Short private temp directory (macOS `sockaddr_un` limit is 103 bytes), removed on drop.
struct TempDir(PathBuf);

impl TempDir {
    fn new() -> TempDir {
        let p = std::env::temp_dir().join(format!(
            "frc{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = std::fs::remove_dir_all(&p);
        mkdir(&p);
        TempDir(p.canonicalize().expect("canonical temp dir"))
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn mkdir(p: &Path) {
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(p)
        .unwrap_or_else(|e| panic!("mkdir {}: {e}", p.display()));
}

/// Every entry of a `.rec` file; panics on a truncated or malformed file.
fn entries(path: &Path) -> Vec<Entry> {
    let mut f = std::fs::File::open(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    let mut out = Vec::new();
    while let Some(e) =
        record::read(&mut f).unwrap_or_else(|e| panic!("{}: bad entry: {e}", path.display()))
    {
        out.push(e);
    }
    out
}

fn bytes_of(entries: &[Entry]) -> Vec<u8> {
    entries
        .iter()
        .flat_map(|e| e.bytes.iter().copied())
        .collect()
}

/// One control connection (PROTOCOL.md §1): a JSON request per line, a reply per line.
struct Client {
    reader: BufReader<UnixStream>,
    writer: UnixStream,
    next_id: u64,
}

impl Client {
    fn connect(sock: &Path) -> std::io::Result<Client> {
        let s = UnixStream::connect(sock)?;
        s.set_read_timeout(Some(TIMEOUT))?;
        Ok(Client {
            reader: BufReader::new(s.try_clone()?),
            writer: s,
            next_id: 1,
        })
    }

    fn request(&mut self, cmd: &str, mut req: Value) -> std::io::Result<Value> {
        req["id"] = json!(self.next_id);
        req["cmd"] = json!(cmd);
        self.next_id += 1;
        self.writer.write_all(format!("{req}\n").as_bytes())?;
        let mut line = String::new();
        self.reader.read_line(&mut line)?;
        serde_json::from_str(&line).map_err(|e| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, format!("{e}: {line:?}"))
        })
    }

    fn ok(&mut self, cmd: &str, req: Value) -> Value {
        let r = self
            .request(cmd, req.clone())
            .unwrap_or_else(|e| panic!("{cmd} {req}: {e}"));
        assert_eq!(r["ok"], json!(true), "{cmd} {req} failed: {r}");
        r
    }
}

/// `fleetmux server --runtime <tmp>/rt --session rec --record <tmp>/rec`; killed (with its panes) on drop.
struct Server {
    child: Child,
    runtime: PathBuf,
    pgids: Vec<i32>,
}

impl Server {
    fn start(tmp: &Path, rec: &Path) -> Server {
        let runtime = tmp.join("rt");
        mkdir(&runtime);
        let out = std::fs::File::create(tmp.join("server.out")).expect("server output file");
        let child = Command::new(BIN)
            .arg("server")
            .arg("--runtime")
            .arg(&runtime)
            .args(["--session", "rec", "--record"])
            .arg(rec)
            .stdin(Stdio::null())
            .stdout(Stdio::from(out.try_clone().expect("dup output file")))
            .stderr(Stdio::from(out))
            .spawn()
            .expect("spawn fleetmux server");
        let mut srv = Server {
            child,
            runtime,
            pgids: Vec::new(),
        };
        let end = Instant::now() + TIMEOUT;
        loop {
            if let Ok(mut c) = Client::connect(&srv.sock())
                && c.request("hello", json!({}))
                    .is_ok_and(|r| r["ok"] == json!(true))
            {
                break;
            }
            if let Ok(Some(status)) = srv.child.try_wait() {
                panic!(
                    "fleetmux server exited ({status}): {}",
                    std::fs::read_to_string(tmp.join("server.out")).unwrap_or_default()
                );
            }
            assert!(Instant::now() < end, "fleetmux server did not answer hello");
            std::thread::sleep(Duration::from_millis(50));
        }
        srv
    }

    fn sock(&self) -> PathBuf {
        self.runtime.join("ctl.sock")
    }

    fn client(&self) -> Client {
        Client::connect(&self.sock()).expect("connect to the server")
    }

    fn spawn(&mut self, title: &str, script: &str) -> String {
        let r = self.client().ok(
            "pane.spawn",
            json!({"argv": ["bash", "-c", script], "title": title, "type": "worker"}),
        );
        self.pgids.push(r["pgid"].as_i64().expect("pgid") as i32);
        r["pane"].as_str().expect("pane id").to_string()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.client_shutdown();
        for pgid in &self.pgids {
            unsafe {
                libc::kill(-pgid, libc::SIGKILL);
            }
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Server {
    fn client_shutdown(&self) -> std::io::Result<Value> {
        Client::connect(&self.sock())?.request("server.shutdown", json!({"force": true}))
    }
}

#[test]
#[ignore = "M3: record-hook"]
fn record_flag_writes_rec_per_pane_with_nondecreasing_offsets() {
    let tmp = TempDir::new();
    let rec = tmp.path().join("rec");
    mkdir(&rec);
    let mut srv = Server::start(tmp.path(), &rec);
    let script =
        |tag: &str| format!("for i in 1 2 3 4 5; do echo {tag}-$i; sleep 0.1; done; exec sleep 30");
    let a = srv.spawn("rec-a", &script("AAA"));
    let b = srv.spawn("rec-b", &script("BBB"));

    for (pane, tag) in [(&a, "AAA"), (&b, "BBB")] {
        let path = rec.join(format!("{pane}.rec"));
        let end = Instant::now() + TIMEOUT;
        loop {
            if path.exists() {
                let text = String::from_utf8_lossy(&bytes_of(&entries(&path))).into_owned();
                if text.contains(&format!("{tag}-5")) {
                    break;
                }
            }
            assert!(
                Instant::now() < end,
                "{} never recorded {tag}-5",
                path.display()
            );
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    let r = srv.client_shutdown().expect("server.shutdown");
    assert_eq!(r["ok"], json!(true), "server.shutdown: {r}");

    let mut names: Vec<String> = std::fs::read_dir(&rec)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    let mut want = vec![format!("{a}.rec"), format!("{b}.rec")];
    want.sort();
    assert_eq!(names, want, "exactly one .rec per pane");

    for (pane, tag, other) in [(&a, "AAA", "BBB"), (&b, "BBB", "AAA")] {
        let es = entries(&rec.join(format!("{pane}.rec")));
        assert!(!es.is_empty(), "{pane}.rec has no entries");
        for w in es.windows(2) {
            assert!(
                w[0].offset_us <= w[1].offset_us,
                "{pane}.rec offsets decrease: {} then {}",
                w[0].offset_us,
                w[1].offset_us
            );
        }
        assert!(
            es.iter()
                .all(|e| !e.bytes.is_empty() && e.bytes.len() <= MAX_ENTRY)
        );
        let text = String::from_utf8_lossy(&bytes_of(&es)).into_owned();
        let mut at = 0;
        for i in 1..=5 {
            let needle = format!("{tag}-{i}");
            let pos = text[at..]
                .find(&needle)
                .unwrap_or_else(|| panic!("{pane}.rec lacks {needle} in order: {text:?}"));
            at += pos + needle.len();
        }
        assert!(
            !text.contains(other),
            "{pane}.rec holds another pane's output: {text:?}"
        );
        let last = es.last().unwrap().offset_us;
        assert!(
            last >= 400_000,
            "{pane}.rec: five lines 0.1 s apart end ≥ 0.4 s after spawn, last offset {last} µs"
        );
    }
}

#[test]
#[ignore = "M3: record-hook"]
fn record_file_mode_0600_and_split_over_1mib() {
    let tmp = TempDir::new();
    let recorder = Recorder::new(tmp.path().to_path_buf());
    let mut file = recorder.open("k7q2-1").expect("open k7q2-1.rec");
    let big: Vec<u8> = (0..(5 * MAX_ENTRY / 2)).map(|i| (i % 251) as u8).collect();
    file.append(10, &big).expect("append 2.5 MiB");
    file.append(20, b"tail").expect("append tail");
    file.append(30, b"").expect("append nothing");
    drop(file);

    let path = tmp.path().join("k7q2-1.rec");
    let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode, 0o600, "{}: mode {mode:o}", path.display());

    let es = entries(&path);
    let lens: Vec<usize> = es.iter().map(|e| e.bytes.len()).collect();
    assert_eq!(
        lens,
        vec![MAX_ENTRY, MAX_ENTRY, MAX_ENTRY / 2, 4],
        "entry lengths"
    );
    let offsets: Vec<u64> = es.iter().map(|e| e.offset_us).collect();
    assert_eq!(
        offsets,
        vec![10, 10, 10, 20],
        "split entries keep the read's offset"
    );
    let mut want = big.clone();
    want.extend_from_slice(b"tail");
    assert!(
        bytes_of(&es) == want,
        "recorded bytes differ from what was appended"
    );
}
