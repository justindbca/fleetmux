//! One command name (docs/design.md §50): `fleetmux` keeps its own subcommands and hands every other one to the
//! Python fleet CLI of the same release, by `exec` (argv, stdin / stdout / stderr and the exit code pass through
//! untouched). `gui --runtime D` stays here; `gui` without `--runtime` goes to Python, whose `gui` execs back the
//! `--runtime` form only (pinned in tests/test_fleetmux_command.py), so there is no loop.
//!
//! The Python CLI is found at, in order: `$FLEETMUX_PYTHON_CLI` (an executable, run as is), the release this
//! binary sits in (`<release>/bin/fleetmux` → `<release>/.venv/bin/python <release>/cmux_fleet/fleet.py`),
//! `~/fleetmux-current`, `~/cmux-fleet-current`. Nothing found → exit 127 with a message saying how to set it.
use std::fs;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};

const BIN: &str = env!("CARGO_BIN_EXE_fleetmux");
static SEQ: AtomicU32 = AtomicU32::new(0);

struct TempDir(PathBuf);

impl TempDir {
    fn new() -> TempDir {
        let p = std::env::temp_dir().join(format!(
            "fmxfwd{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = fs::remove_dir_all(&p);
        fs::create_dir_all(&p).unwrap();
        TempDir(p.canonicalize().unwrap())
    }
    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// A stand-in for the Python CLI: records its argv (one per line, `$0` first) in `$FAKE_ARGV`, copies stdin to
/// stdout after `out:`, writes one stderr line and exits 7.
const FAKE: &str = "#!/bin/sh\n\
    { printf '%s\\n' \"$0\"; for a in \"$@\"; do printf '%s\\n' \"$a\"; done; } > \"$FAKE_ARGV\"\n\
    printf 'out:'; cat\n\
    echo 'err-from-python' >&2\n\
    exit 7\n";

fn write_exe(path: &Path, text: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}

/// A release dir as install_release.sh lays it out: `.venv/bin/python` (the fake) and `cmux_fleet/fleet.py`.
fn make_release(root: &Path) -> PathBuf {
    write_exe(&root.join(".venv/bin/python"), FAKE);
    fs::create_dir_all(root.join("cmux_fleet")).unwrap();
    fs::write(root.join("cmux_fleet/fleet.py"), "# fake\n").unwrap();
    root.to_path_buf()
}

/// Run `bin args` with a clean lookup environment: HOME = `home`, no `$FLEETMUX_PYTHON_CLI` unless given.
fn run(
    bin: &Path,
    args: &[&str],
    home: &Path,
    cli: Option<&Path>,
    argv_file: &Path,
    stdin: &str,
) -> Output {
    let mut cmd = Command::new(bin);
    cmd.args(args)
        .env("HOME", home)
        .env("FAKE_ARGV", argv_file)
        .env_remove("FLEETMUX_PYTHON_CLI")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(c) = cli {
        cmd.env("FLEETMUX_PYTHON_CLI", c);
    }
    let mut child = cmd.spawn().expect("spawn fleetmux");
    child
        .stdin
        .take()
        .unwrap()
        .write_all(stdin.as_bytes())
        .unwrap();
    child.wait_with_output().unwrap()
}

fn argv_of(file: &Path) -> Vec<String> {
    fs::read_to_string(file)
        .unwrap_or_else(|e| panic!("the fake Python CLI did not run ({}): {e}", file.display()))
        .lines()
        .map(str::to_string)
        .collect()
}

#[test]
fn an_unknown_subcommand_is_handed_to_the_python_cli_with_argv_stdio_and_exit_code_intact() {
    let t = TempDir::new();
    let cli = t.path().join("fake-cli");
    write_exe(&cli, FAKE);
    let argv = t.path().join("argv");
    let out = run(
        Path::new(BIN),
        &["status", "--config", "a b/c.json", "", "--x=1"],
        t.path(),
        Some(&cli),
        &argv,
        "hello stdin",
    );
    assert_eq!(
        out.status.code(),
        Some(7),
        "exit code passes through: {out:?}"
    );
    assert_eq!(String::from_utf8_lossy(&out.stdout), "out:hello stdin");
    assert_eq!(String::from_utf8_lossy(&out.stderr), "err-from-python\n");
    assert_eq!(
        argv_of(&argv),
        vec![
            cli.display().to_string(),
            "status".into(),
            "--config".into(),
            "a b/c.json".into(),
            "".into(),
            "--x=1".into()
        ],
        "FLEETMUX_PYTHON_CLI is executed as is, with the arguments untouched"
    );
}

#[test]
fn gui_without_runtime_goes_to_python() {
    let t = TempDir::new();
    let cli = t.path().join("fake-cli");
    write_exe(&cli, FAKE);
    let argv = t.path().join("argv");
    let out = run(Path::new(BIN), &["gui"], t.path(), Some(&cli), &argv, "");
    assert_eq!(out.status.code(), Some(7), "{out:?}");
    assert_eq!(argv_of(&argv)[1..], ["gui".to_string()]);
}

#[test]
fn gui_with_runtime_stays_in_rust() {
    let t = TempDir::new();
    let cli = t.path().join("fake-cli");
    write_exe(&cli, FAKE);
    let argv = t.path().join("argv");
    let runtime = t.path().join("no-server-here");
    let out = run(
        Path::new(BIN),
        &["gui", "--runtime", runtime.to_str().unwrap()],
        t.path(),
        Some(&cli),
        &argv,
        "",
    );
    assert!(
        !argv.exists(),
        "gui --runtime must not reach Python: {out:?}"
    );
    assert_eq!(
        out.status.code(),
        Some(3),
        "Rust gui: cannot connect = exit 3: {out:?}"
    );
}

#[test]
fn the_release_this_binary_sits_in_is_found_first() {
    let t = TempDir::new();
    let rel = make_release(&t.path().join("rel"));
    fs::create_dir_all(rel.join("bin")).unwrap();
    let bin = rel.join("bin/fleetmux");
    fs::copy(BIN, &bin).unwrap();
    // a ~/fleetmux-current that must lose against the binary's own release
    make_release(&t.path().join("home/other"));
    std::os::unix::fs::symlink(
        t.path().join("home/other"),
        t.path().join("home/fleetmux-current"),
    )
    .unwrap();
    let argv = t.path().join("argv");
    let out = run(
        &bin,
        &["task", "-"],
        &t.path().join("home"),
        None,
        &argv,
        "{}",
    );
    assert_eq!(out.status.code(), Some(7), "{out:?}");
    let got = argv_of(&argv);
    assert_eq!(got[0], rel.join(".venv/bin/python").display().to_string());
    assert_eq!(
        got[1..],
        [
            rel.join("cmux_fleet/fleet.py").display().to_string(),
            "task".into(),
            "-".into()
        ]
    );
    assert_eq!(String::from_utf8_lossy(&out.stdout), "out:{}");
}

#[test]
fn home_fleetmux_current_then_the_old_cmux_fleet_current() {
    let t = TempDir::new();
    let home = t.path().join("home");
    let new = make_release(&t.path().join("new"));
    let old = make_release(&t.path().join("old"));
    std::os::unix::fs::symlink(&old, home_join(&home, "cmux-fleet-current")).unwrap();
    let argv = t.path().join("argv");
    // only the old name: it is used
    let out = run(Path::new(BIN), &["stop"], &home, None, &argv, "");
    assert_eq!(out.status.code(), Some(7), "{out:?}");
    assert_eq!(
        argv_of(&argv)[1],
        home.join("cmux-fleet-current/cmux_fleet/fleet.py")
            .display()
            .to_string()
    );
    // both: the new name wins
    std::os::unix::fs::symlink(&new, home.join("fleetmux-current")).unwrap();
    let out = run(Path::new(BIN), &["stop"], &home, None, &argv, "");
    assert_eq!(out.status.code(), Some(7), "{out:?}");
    assert_eq!(
        argv_of(&argv)[1],
        home.join("fleetmux-current/cmux_fleet/fleet.py")
            .display()
            .to_string()
    );
}

fn home_join(home: &Path, name: &str) -> PathBuf {
    fs::create_dir_all(home).unwrap();
    home.join(name)
}

#[test]
fn no_python_cli_anywhere_fails_and_says_how_to_set_it() {
    let t = TempDir::new();
    let home = t.path().join("home");
    fs::create_dir_all(&home).unwrap();
    let argv = t.path().join("argv");
    let out = run(Path::new(BIN), &["status"], &home, None, &argv, "");
    assert_eq!(out.status.code(), Some(127), "{out:?}");
    let err = String::from_utf8_lossy(&out.stderr);
    for needle in [
        "status",
        "FLEETMUX_PYTHON_CLI",
        "fleetmux-current",
        "cmux-fleet-current",
        "install_release.sh",
    ] {
        assert!(err.contains(needle), "missing {needle:?} in: {err}");
    }
}

#[test]
fn a_set_but_unusable_fleetmux_python_cli_is_an_error_not_a_fallback() {
    let t = TempDir::new();
    let home = t.path().join("home");
    make_release(&t.path().join("cur"));
    fs::create_dir_all(&home).unwrap();
    std::os::unix::fs::symlink(t.path().join("cur"), home.join("fleetmux-current")).unwrap();
    let argv = t.path().join("argv");
    let missing = t.path().join("nope");
    let out = run(
        Path::new(BIN),
        &["status"],
        &home,
        Some(&missing),
        &argv,
        "",
    );
    assert_eq!(out.status.code(), Some(127), "{out:?}");
    assert!(
        !argv.exists(),
        "an explicit FLEETMUX_PYTHON_CLI must not silently fall back"
    );
    assert!(
        String::from_utf8_lossy(&out.stderr).contains("FLEETMUX_PYTHON_CLI"),
        "{out:?}"
    );
}

#[test]
fn version_format_is_unchanged() {
    let out = Command::new(BIN).arg("--version").output().unwrap();
    assert!(out.status.success());
    let s = String::from_utf8_lossy(&out.stdout);
    let parts: Vec<&str> = s.trim_end().split(' ').collect();
    assert_eq!(parts.len(), 4, "{s:?}");
    assert_eq!(parts[0], "fleetmux");
    assert_eq!(parts[2], "(protocol");
    assert!(parts[3].ends_with(')'), "{s:?}");
}

#[test]
fn help_lists_our_subcommands_and_points_at_the_fleet_commands() {
    let out = Command::new(BIN).arg("--help").output().unwrap();
    assert_eq!(out.status.code(), Some(0), "{out:?}");
    let s = String::from_utf8_lossy(&out.stdout);
    assert!(s.starts_with("usage: fleetmux "), "{s}");
    for needle in ["server", "attach", "gui", "start", "status", "--help"] {
        assert!(s.contains(needle), "missing {needle:?} in: {s}");
    }
}

#[test]
fn no_arguments_is_still_a_usage_error() {
    let out = Command::new(BIN).output().unwrap();
    assert_eq!(out.status.code(), Some(2), "{out:?}");
    assert!(
        String::from_utf8_lossy(&out.stderr).contains("usage: fleetmux"),
        "{out:?}"
    );
}
