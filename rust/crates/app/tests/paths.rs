//! Runtime path / permission / pane id rules (plan.md §3.3; design.md §2.2, §2.4, §5). Red in phase 0
//! (`todo!()`), green from M0 (`server-core`).

// The app crate has no library target yet, so the module is compiled straight into this test.
// Once `src/lib.rs` exports `pub mod paths`, this becomes `use fleetmux::paths::*;`.
#[allow(dead_code)]
#[path = "../src/paths.rs"]
mod paths;

use paths::*;
use std::collections::HashSet;
use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const SAFE: &str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-";

/// Fresh directory under the system temp dir; removed by `Drop`.
struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> Self {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("fmx-{tag}-{}-{nanos}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        TempDir(dir)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::set_permissions(&self.0, fs::Permissions::from_mode(0o700));
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn mode(p: &Path) -> u32 {
    fs::metadata(p).unwrap().permissions().mode() & 0o777
}

#[test]
fn token_keeps_only_safe_chars_and_40_max() {
    assert_eq!(token("fleetmux"), "fleetmux");
    assert_eq!(token("Fleet_agents-2"), "Fleet_agents-2");
    // Unsafe chars are replaced by `_`, one per char (same rule as wezterm.py `_token`).
    assert_eq!(token("a b/c.d:e"), "a_b_c_d_e");
    assert_eq!(token("../etc"), "___etc");
    assert_eq!(token("é中"), "__");
    // 40 chars max.
    assert_eq!(token(&"x".repeat(60)), "x".repeat(40));
    assert_eq!(token(&"y".repeat(40)), "y".repeat(40));
    // Empty falls back to "fleet".
    assert_eq!(token(""), "fleet");

    let long = "run 2026/10/28 · fleet:agents ".repeat(4);
    let t = token(&long);
    assert_eq!(t.chars().count(), 40);
    assert!(t.chars().all(|c| SAFE.contains(c)), "{t:?}");
}

#[test]
fn same_state_and_session_give_same_runtime_dir() {
    let state = Path::new("/Users/someone/.fleetmux/state");
    let a = runtime_dir(state, "fleetmux");
    let b = runtime_dir(state, "fleetmux");
    assert_eq!(a, b);
    assert_eq!(a, state.join("fleetmux").join("fleetmux"));

    // The token, not the raw session name, names the directory.
    assert_eq!(
        runtime_dir(state, "my fleet"),
        state.join("fleetmux").join(token("my fleet"))
    );

    // Different session or different state dir → different runtime dir.
    assert_ne!(
        runtime_dir(state, "fleetmux"),
        runtime_dir(state, "other-fleet")
    );
    assert_ne!(
        runtime_dir(state, "fleetmux"),
        runtime_dir(Path::new("/var/tmp/state"), "fleetmux")
    );
}

#[test]
fn socket_over_limit_is_error_not_tmp_fallback() {
    // Short path: exactly `<runtime>/ctl.sock`.
    let short = Path::new("/var/tmp/fmx/fleetmux");
    assert_eq!(socket_path(short), Ok(short.join(SOCKET_NAME)));

    // `/` + name + `/ctl.sock` hits the limit exactly, then one byte over.
    let suffix = 1 + SOCKET_NAME.len();
    let at_limit = PathBuf::from(format!("/{}", "a".repeat(SOCKET_PATH_MAX - suffix - 1)));
    let sock = at_limit.join(SOCKET_NAME);
    assert_eq!(sock.as_os_str().len(), SOCKET_PATH_MAX);
    assert_eq!(socket_path(&at_limit), Ok(sock));

    let over = PathBuf::from(format!("/{}", "a".repeat(SOCKET_PATH_MAX - suffix)));
    assert_eq!(
        socket_path(&over),
        Err(PathError::TooLong {
            len: SOCKET_PATH_MAX + 1,
            max: SOCKET_PATH_MAX
        })
    );

    // A realistic deep state dir: error carrying the byte length, never a /tmp path.
    let deep = runtime_dir(
        Path::new(&format!(
            "/Users/someone/{}.fleetmux/state",
            "nested/".repeat(10)
        )),
        &"s".repeat(40),
    );
    let len = deep.join(SOCKET_NAME).as_os_str().len();
    assert!(len > SOCKET_PATH_MAX);
    assert_eq!(
        socket_path(&deep),
        Err(PathError::TooLong {
            len,
            max: SOCKET_PATH_MAX
        })
    );

    // Length counts bytes, not chars.
    let wide = PathBuf::from(format!("/{}", "é".repeat((SOCKET_PATH_MAX - suffix) / 2)));
    let wide_len = wide.join(SOCKET_NAME).as_os_str().len();
    assert!(wide_len > SOCKET_PATH_MAX);
    assert_eq!(
        socket_path(&wide),
        Err(PathError::TooLong {
            len: wide_len,
            max: SOCKET_PATH_MAX
        })
    );
}

#[test]
fn perms_fix_0775_to_0700() {
    let dir = TempDir::new("perms");
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o775)).unwrap();
    assert_eq!(mode(dir.path()), 0o775);

    let fixed = check_runtime_perms(dir.path()).expect("own dir is fixable");
    assert_eq!(
        fixed,
        Fixed::Chmod {
            from: 0o775,
            to: 0o700
        }
    );
    assert_eq!(mode(dir.path()), 0o700);

    // Second pass: nothing to do.
    assert_eq!(
        check_runtime_perms(dir.path()).expect("own dir"),
        Fixed::Unchanged
    );
    assert_eq!(mode(dir.path()), 0o700);

    // Other too-open shapes are fixed the same way.
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o707)).unwrap();
    assert_eq!(
        check_runtime_perms(dir.path()).expect("own dir"),
        Fixed::Chmod {
            from: 0o707,
            to: 0o700
        }
    );
    assert_eq!(mode(dir.path()), 0o700);

    // A plain file is not a runtime dir; a missing path is an I/O error.
    let file = dir.path().join("not-a-dir");
    fs::write(&file, b"").unwrap();
    assert!(matches!(check_runtime_perms(&file), Err(PermError::NotDir)));
    assert!(matches!(
        check_runtime_perms(&dir.path().join("missing")),
        Err(PermError::Io(_))
    ));
}

#[test]
fn perms_refuse_dir_owned_by_someone_else() {
    let mine = TempDir::new("owner");
    let me = fs::metadata(mine.path()).unwrap().uid();
    let root = Path::new("/");
    let root_meta = fs::metadata(root).unwrap();
    if me == root_meta.uid() {
        eprintln!("skip: running as the owner of / (uid {me}); no foreign-owned dir to test with");
        return;
    }
    let before = root_meta.permissions().mode();

    match check_runtime_perms(root) {
        Err(PermError::NotOwner { owner, me: got }) => {
            assert_eq!(owner, root_meta.uid());
            assert_eq!(got, me);
        }
        other => panic!("expected NotOwner for /, got {other:?}"),
    }
    assert_eq!(
        fs::metadata(root).unwrap().permissions().mode(),
        before,
        "a foreign dir is never chmod-ed"
    );
}

#[test]
fn pane_ids_differ_across_epochs() {
    assert_eq!(pane_id("k7q2", 12), "k7q2-12");
    assert_eq!(pane_id("k7q2", 0), "k7q2-0");

    let epochs: Vec<String> = (0..32).map(|_| new_epoch()).collect();
    for e in &epochs {
        assert_eq!(e.len(), 4, "{e:?}");
        assert!(
            e.chars()
                .all(|c| c.is_ascii_digit() || c.is_ascii_lowercase()),
            "{e:?}"
        );
    }
    let distinct: HashSet<&String> = epochs.iter().collect();
    assert!(distinct.len() >= 30, "epochs repeat too often: {epochs:?}");

    // Same sequence number in two generations never collides.
    let (a, b) = {
        let mut it = distinct.iter();
        (it.next().unwrap().as_str(), it.next().unwrap().as_str())
    };
    for seq in [0, 1, 3, 12, u32::MAX] {
        assert_ne!(pane_id(a, seq), pane_id(b, seq));
        assert!(pane_id(a, seq).starts_with(&format!("{a}-")));
    }
}
