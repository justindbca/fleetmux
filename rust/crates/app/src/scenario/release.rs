//! `--from` / `--to`: a release directory used as it is (read only), or a git reference installed into the
//! scenario's own world with the product installer (`scripts/install_release.sh` of that very commit, `--headless`,
//! `FLEETMUX_RELEASES=<world>/fleetmux-releases`, never `--activate`). The binary is built from the commit's own `rust/` tree with
//! `cargo build --release --locked -p fleetmux`; the build directory is `$FLEETMUX_SCENARIO_CARGO_TARGET`, else a
//! per-user cache directory in the system temp directory, so a second run only rebuilds the workspace crates.
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use super::guard::Guard;
use super::proc;

pub const CARGO_TARGET_ENV: &str = "FLEETMUX_SCENARIO_CARGO_TARGET";

/// A usable release.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Release {
    /// What the caller passed (`main`, a path, ...).
    pub spec: String,
    pub dir: PathBuf,
    /// The commit, when it is known (a git reference, or the release's `bin/installed.json`).
    pub sha: Option<String>,
}

impl Release {
    pub fn python(&self) -> PathBuf {
        self.dir.join(".venv/bin/python")
    }
    pub fn fleet_py(&self) -> PathBuf {
        self.dir.join("fleetmux/fleet.py")
    }
    pub fn bin_dir(&self) -> PathBuf {
        self.dir.join("bin")
    }
    pub fn fleetmux(&self) -> PathBuf {
        self.dir.join("bin/fleetmux")
    }
    pub fn label(&self) -> String {
        match &self.sha {
            Some(s) if !self.spec.contains(s.as_str()) => format!("{} ({})", self.spec, &s[..s.len().min(12)]),
            _ => self.spec.clone(),
        }
    }
}

/// The pieces a release directory must hold.
pub const PARTS: &[&str] = &[".venv/bin/python", "fleetmux/fleet.py", "bin/fleetmux"];

/// `Ok` when `dir` holds every part of [`PARTS`]; `Err` names the missing ones.
pub fn check_dir(dir: &Path) -> Result<(), String> {
    let missing: Vec<&str> = PARTS.iter().copied().filter(|p| !dir.join(p).exists()).collect();
    if missing.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "{} is not a fleetmux release directory: missing {}",
            dir.display(),
            missing.join(", ")
        ))
    }
}

/// Is `spec` meant as a directory (an absolute or relative path that exists, or contains a slash)?
pub fn is_dir_spec(spec: &str) -> bool {
    spec.starts_with('/') || spec.starts_with('.') || spec.starts_with('~') || Path::new(spec).is_dir()
}

/// The full sha of `reference` in `repo`.
pub fn rev_parse(git: &Path, repo: &Path, reference: &str) -> Result<String, String> {
    let out = Command::new(git)
        .arg("-C")
        .arg(repo)
        .args(["rev-parse", "--verify", "--quiet"])
        .arg(format!("{reference}^{{commit}}"))
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("cannot run git: {e}"))?;
    let sha = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if out.status.success() && sha.len() == 40 && sha.chars().all(|c| c.is_ascii_hexdigit()) {
        Ok(sha)
    } else {
        Err(format!("{reference:?} is not a commit in {}", repo.display()))
    }
}

/// The checkout this binary was built from (`<crate>/../../..`), when it is a git checkout.
pub fn default_repo() -> Option<PathBuf> {
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let p = std::fs::canonicalize(p).ok()?;
    p.join(".git").exists().then_some(p)
}

/// The cargo build directory for installs.
pub fn cargo_target(base: &BTreeMap<String, String>) -> PathBuf {
    if let Some(t) = base.get(CARGO_TARGET_ENV).filter(|t| !t.is_empty()) {
        return PathBuf::from(t);
    }
    let uid = unsafe { libc::getuid() };
    std::env::temp_dir().join(format!("fleetmux-scenario-target-{uid}"))
}

/// What [`install`] runs, for the log.
pub struct Installer<'a> {
    pub guard: &'a Guard,
    pub git: &'a Path,
    pub repo: &'a Path,
    /// The scenario's work directory (source archives, extracted trees, binaries).
    pub work: &'a Path,
    /// `FLEETMUX_RELEASES` of the install: the world's own releases directory.
    pub releases: &'a Path,
    /// The environment for the build and the installer (the caller's, minus the scrubbed names).
    pub env: &'a BTreeMap<String, String>,
    pub log: &'a Path,
    /// Called between long operations: `Some(reason)` aborts.
    pub stop: &'a dyn Fn() -> Option<String>,
}

/// Resolve `spec` to a release: a directory is checked and used as it is; a git reference is installed under
/// `inst.releases/<sha>` (once per sha).
pub fn resolve(spec: &str, inst: &Installer) -> Result<Release, String> {
    if is_dir_spec(spec) {
        let home = std::env::var_os("HOME").map(PathBuf::from);
        return resolve_dir(spec, home.as_deref());
    }
    let sha = rev_parse(inst.git, inst.repo, spec)?;
    let dir = inst.releases.join(&sha);
    if !dir.exists() {
        install(&sha, inst)?;
    }
    check_dir(&dir)?;
    Ok(Release {
        spec: spec.to_string(),
        dir,
        sha: Some(sha),
    })
}

/// A release given as a directory: `~` is the caller's home, and a symbolic link (rollout passes
/// `~/fleetmux-releases/versions/<v>` -> `../<sha>`) is resolved before the directory is used.
pub fn resolve_dir(spec: &str, home: Option<&Path>) -> Result<Release, String> {
    let path = match (spec.strip_prefix("~/"), home) {
        (Some(rest), Some(h)) => h.join(rest),
        (None, Some(h)) if spec == "~" => h.to_path_buf(),
        _ => PathBuf::from(spec),
    };
    let dir = std::fs::canonicalize(&path).map_err(|e| format!("{spec}: {e}"))?;
    check_dir(&dir)?;
    let sha = installed_sha(&dir);
    Ok(Release {
        spec: spec.to_string(),
        dir,
        sha,
    })
}

fn installed_sha(dir: &Path) -> Option<String> {
    let text = std::fs::read_to_string(dir.join("bin/installed.json")).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    v.get("sha").and_then(|s| s.as_str()).map(str::to_string)
}

/// The cargo that honours `rust/rust-toolchain.toml`: rustup's proxy in `$CARGO_HOME/bin` or `~/.cargo/bin`
/// first (a distribution cargo on PATH ignores the pinned toolchain), then `cargo` on PATH.
pub fn find_cargo(env: &BTreeMap<String, String>) -> Option<PathBuf> {
    let mut cands = Vec::new();
    if let Some(h) = env.get("CARGO_HOME") {
        cands.push(Path::new(h).join("bin/cargo"));
    }
    if let Some(h) = env.get("HOME") {
        cands.push(Path::new(h).join(".cargo/bin/cargo"));
    }
    if let Some(p) = env.get("PATH") {
        cands.extend(std::env::split_paths(p).map(|d| d.join("cargo")));
    }
    cands.into_iter().find(|p| super::world::is_exe(p))
}

/// Install limit for one command (a cold release build of the workspace takes minutes).
const STEP_LIMIT: Duration = Duration::from_secs(1800);

/// The `tar` command that unpacks a `git archive` tarball of a release into `dest`. `-m` gives the files the time
/// of the unpacking instead of the commit time: the installs share one cargo target directory, and sources older
/// than the previous build (another release's) would be taken as already built.
pub fn extract_argv(tgz: &Path, dest: &Path) -> Vec<String> {
    vec!["tar".into(), "-xmzf".into(), tgz.display().to_string(), "-C".into(), dest.display().to_string()]
}

fn install(sha: &str, inst: &Installer) -> Result<(), String> {
    let short = &sha[..12];
    let tgz = inst.work.join(format!("src-{short}.tgz"));
    let src = inst.work.join(format!("src-{short}"));
    let bins = inst.work.join(format!("bin-{short}"));
    let s = |p: &Path| p.display().to_string();
    let run = |what: &str, argv: Vec<String>, env: &BTreeMap<String, String>, cwd: &Path| -> Result<(), String> {
        let done = proc::run(inst.guard, &argv, env, cwd, None, STEP_LIMIT, inst.log, inst.stop)?;
        if done.ok() {
            Ok(())
        } else {
            Err(format!("{what} failed (exit {:?}): {}; log {}", done.code, done.tail(3), inst.log.display()))
        }
    };
    let git = s(inst.git);
    run(
        "git archive",
        vec![git, "-C".into(), s(inst.repo), "archive".into(), "--format=tar.gz".into(), "-o".into(), s(&tgz), sha.into()],
        inst.env,
        inst.work,
    )?;
    std::fs::create_dir_all(&src).map_err(|e| e.to_string())?;
    let unpacked = std::time::SystemTime::now();
    run("tar", extract_argv(&tgz, &src), inst.env, inst.work)?;
    let cargo = find_cargo(inst.env).ok_or("cargo not found (~/.cargo/bin or PATH): cannot build the release binary")?;
    let target = cargo_target(inst.env);
    let mut env = inst.env.clone();
    env.insert("CARGO_TARGET_DIR".into(), s(&target));
    if let Some(d) = cargo.parent() {
        let path = env.get("PATH").cloned().unwrap_or_default();
        env.insert("PATH".into(), format!("{}:{path}", d.display()));
    }
    run(
        "cargo build",
        vec![s(&cargo), "build".into(), "--release".into(), "--locked".into(), "-p".into(), "fleetmux".into()],
        &env,
        &src.join("rust"),
    )?;
    // the binary must come from this tree: one left over from another release's build is refused
    let built = target.join("release/fleetmux");
    let fresh = std::fs::metadata(&built).and_then(|m| m.modified()).map(|m| m >= unpacked).unwrap_or(false);
    if !fresh {
        return Err(format!("cargo build did not produce a new {} for {short}: refusing a stale binary", built.display()));
    }
    std::fs::create_dir_all(&bins).map_err(|e| e.to_string())?;
    std::fs::copy(&built, bins.join("fleetmux")).map_err(|e| format!("copy: {e}"))?;
    let sum = Command::new("shasum")
        .args(["-a", "256"])
        .arg(bins.join("fleetmux"))
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("shasum: {e}"))?;
    let hex = String::from_utf8_lossy(&sum.stdout)
        .split_whitespace()
        .next()
        .unwrap_or("")
        .to_string();
    std::fs::write(bins.join("SHA256SUMS"), format!("{hex}  fleetmux\n")).map_err(|e| e.to_string())?;
    std::fs::write(
        bins.join("manifest.json"),
        serde_json::json!({"source_sha": sha, "built_by": "fleetmux scenario upgrade"}).to_string(),
    )
    .map_err(|e| e.to_string())?;
    let mut env = inst.env.clone();
    env.insert("FLEETMUX_RELEASES".into(), s(inst.releases));
    run(
        "install_release.sh",
        vec![
            "/bin/bash".into(),
            s(&src.join("scripts/install_release.sh")),
            sha.into(),
            s(&tgz),
            "--bin-dir".into(),
            s(&bins),
            "--headless".into(),
            "--tool-sha".into(),
            sha.into(),
        ],
        &env,
        inst.work,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn tmp(tag: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("fmxrel-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    fn git() -> PathBuf {
        for d in ["/opt/homebrew/bin", "/usr/bin", "/usr/local/bin"] {
            let p = Path::new(d).join("git");
            if p.is_file() {
                return p;
            }
        }
        panic!("git is required by this test (a missing program is a failure, not a skip)");
    }

    #[test]
    fn a_release_dir_needs_python_fleet_py_and_the_binary() {
        let d = tmp("dir");
        let err = check_dir(&d).unwrap_err();
        for p in PARTS {
            assert!(err.contains(p), "{err}");
        }
        for p in PARTS {
            let f = d.join(p);
            std::fs::create_dir_all(f.parent().unwrap()).unwrap();
            std::fs::write(&f, "").unwrap();
            std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        assert!(check_dir(&d).is_ok());
        std::fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn paths_are_directories_and_names_are_references() {
        assert!(is_dir_spec("/x/y"));
        assert!(is_dir_spec("./rel"));
        assert!(!is_dir_spec("main"));
        assert!(!is_dir_spec("dde575b5"));
    }

    #[test]
    fn references_resolve_to_full_shas() {
        let d = tmp("git");
        let g = git();
        let ok = |args: &[&str]| {
            assert!(Command::new(&g).arg("-C").arg(&d).args(args).env("GIT_CONFIG_GLOBAL", "/dev/null")
                .stdout(Stdio::null()).status().unwrap().success());
        };
        ok(&["init", "-q", "-b", "main"]);
        ok(&["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "one"]);
        let sha = rev_parse(&g, &d, "main").unwrap();
        assert_eq!(sha.len(), 40);
        assert_eq!(rev_parse(&g, &d, &sha[..8]).unwrap(), sha);
        assert!(rev_parse(&g, &d, "no-such-branch").unwrap_err().contains("no-such-branch"));
        std::fs::remove_dir_all(&d).unwrap();
    }
    #[test]
    fn an_unpacked_release_is_newer_than_any_earlier_build() {
        // `git archive` stamps every file with the commit time. The installs share one cargo target directory, so
        // sources older than the previous build (the other release's) would be taken as built: --to would get
        // --from's binary. Unpacked files must carry the time of the unpacking instead.
        let d = tmp("mtime");
        let repo = d.join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        let g = git();
        let ok = |args: &[&str]| {
            assert!(Command::new(&g).arg("-C").arg(&repo).args(args).env("GIT_CONFIG_GLOBAL", "/dev/null")
                .env("GIT_COMMITTER_DATE", "2001-01-01T00:00:00Z").env("GIT_AUTHOR_DATE", "2001-01-01T00:00:00Z")
                .stdout(Stdio::null()).status().unwrap().success());
        };
        ok(&["init", "-q", "-b", "main"]);
        std::fs::write(repo.join("lib.rs"), "// x\n").unwrap();
        ok(&["add", "lib.rs"]);
        ok(&["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "old"]);
        let tgz = d.join("src.tgz");
        ok(&["archive", "--format=tar.gz", "-o", tgz.to_str().unwrap(), "HEAD"]);
        let dest = d.join("src");
        std::fs::create_dir_all(&dest).unwrap();
        let before = std::time::SystemTime::now() - Duration::from_secs(5);
        let argv = extract_argv(&tgz, &dest);
        assert!(Command::new(&argv[0]).args(&argv[1..]).status().unwrap().success(), "{argv:?}");
        let m = std::fs::metadata(dest.join("lib.rs")).unwrap().modified().unwrap();
        assert!(m >= before, "unpacked with the archive's time {m:?}, older than {before:?}");
        std::fs::remove_dir_all(&d).unwrap();
    }

    fn fake_release(dir: &Path, sha: &str) {
        for p in PARTS {
            let f = dir.join(p);
            std::fs::create_dir_all(f.parent().unwrap()).unwrap();
            std::fs::write(&f, "").unwrap();
            std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        std::fs::write(dir.join("bin/installed.json"), format!("{{\"sha\": \"{sha}\"}}")).unwrap();
    }

    #[test]
    fn a_release_given_by_a_versions_link_or_from_home_is_used_by_its_real_directory() {
        // the layout rollout passes: <releases>/versions/<v> -> ../<sha>
        let d = tmp("link");
        let root = d.join("fleetmux-releases");
        let sha = "1111111111111111111111111111111111111111";
        fake_release(&root.join(sha), sha);
        std::fs::create_dir_all(root.join("versions")).unwrap();
        std::os::unix::fs::symlink(format!("../{sha}"), root.join("versions/0.5.1")).unwrap();
        let real = std::fs::canonicalize(root.join(sha)).unwrap();
        let r = resolve_dir(&root.join("versions/0.5.1").display().to_string(), None).unwrap();
        assert_eq!(r.dir, real);
        assert_eq!(r.dir.parent().unwrap().file_name().unwrap(), "fleetmux-releases");
        assert_eq!(r.sha.as_deref(), Some(sha));
        // `~/...` is the given home, then the link is resolved the same way
        let r = resolve_dir("~/fleetmux-releases/versions/0.5.1", Some(&d)).unwrap();
        assert_eq!(r.dir, real);
        assert_eq!(r.spec, "~/fleetmux-releases/versions/0.5.1");
        // the running release by its own directory and the new one by its link name the same directory
        let from = resolve_dir(&root.join(sha).display().to_string(), None).unwrap();
        assert_eq!(from.dir, r.dir);
        std::fs::remove_dir_all(&d).unwrap();
    }

}
