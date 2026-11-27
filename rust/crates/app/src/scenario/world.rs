//! The scenario's isolated world: one fresh directory `/tmp/fmxu-XXXXXX` (0700; short, because the private Redis,
//! tmux and terminal servers put Unix sockets inside it) that holds HOME, FLEETMUX_HOME, FLEETMUX_STATE,
//! TMUX_TMPDIR, the Redis runtime root, the runs, the installed releases and the agent launcher. Every process
//! the scenario starts gets [`World::env`]: the caller's environment without anything that names the caller's fleet,
//! terminal, Claude / model session or Python setup, plus the world's own directories.
use std::collections::BTreeMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

/// Environment never passed on (prefixes and exact names), as `fleetmux demo` does.
pub const SCRUB_PREFIX: &[&str] = &[
    "FLEETMUX_",
    "CLAUDE",
    "ANTHROPIC_",
    "OPENAI_",
    "CODEX_",
    "CMUX_",
    "TMUX",
    "WEZTERM_",
];
pub const SCRUB: &[&str] = &["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "XPC_SERVICE_NAME"];
/// Where Homebrew keeps redis-server on macOS; first on PATH (it is keg-only on some machines).
pub const REDIS_BIN_DIR: &str = "/opt/homebrew/opt/redis/bin";
/// Test seam: another directory to put first instead of [`REDIS_BIN_DIR`].
pub const REDIS_BIN_ENV: &str = "FLEETMUX_SCENARIO_REDIS_BIN_DIR";

#[derive(Debug, Clone)]
pub struct World {
    pub root: PathBuf,
    pub home: PathBuf,
    pub fleet_home: PathBuf,
    pub state: PathBuf,
    pub tmux_tmpdir: PathBuf,
    pub redis_root: PathBuf,
    pub runs: PathBuf,
    pub bin: PathBuf,
    pub releases: PathBuf,
    pub work: PathBuf,
}

impl World {
    /// Create the directory tree (all 0700) under a new `mkdtemp` directory in `/tmp`.
    pub fn create() -> std::io::Result<World> {
        let root = mkdtemp("/tmp/fmxu-")?;
        let root = std::fs::canonicalize(&root).unwrap_or(root);
        let w = World {
            home: root.join("h"),
            fleet_home: root.join("fh"),
            state: root.join("s"),
            tmux_tmpdir: root.join("t"),
            redis_root: root.join("r"),
            runs: root.join("runs"),
            bin: root.join("bin"),
            releases: root.join("fleetmux-releases"),
            work: root.join("w"),
            root,
        };
        for d in [
            &w.home,
            &w.fleet_home,
            &w.state,
            &w.tmux_tmpdir,
            &w.redis_root,
            &w.runs,
            &w.bin,
            &w.releases,
            &w.work,
        ] {
            std::fs::create_dir(d)?;
            std::fs::set_permissions(d, std::fs::Permissions::from_mode(0o700))?;
        }
        Ok(w)
    }

    /// The environment of every fleet process the scenario starts. `release_bin` (the `bin` of the release the
    /// fleet runs) goes first on PATH after the world's own `bin` (the private tmux wrapper); `tools` are the
    /// directories of the programs found by [`find_tools`].
    pub fn env(
        &self,
        base: &BTreeMap<String, String>,
        release_bin: &Path,
        tools: &[PathBuf],
    ) -> BTreeMap<String, String> {
        let mut env: BTreeMap<String, String> = base
            .iter()
            .filter(|(k, _)| !SCRUB.contains(&k.as_str()) && !SCRUB_PREFIX.iter().any(|p| k.starts_with(p)))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        let mut path: Vec<String> = vec![
            self.bin.display().to_string(),
            release_bin.display().to_string(),
        ];
        for t in tools {
            let d = t.display().to_string();
            if !path.contains(&d) {
                path.push(d);
            }
        }
        if let Some(p) = base.get("PATH") {
            path.push(p.clone());
        }
        let s = |p: &Path| p.display().to_string();
        env.insert("PATH".into(), path.join(":"));
        env.insert("HOME".into(), s(&self.home));
        env.insert("SHELL".into(), "/bin/sh".into());
        env.insert("TMUX_TMPDIR".into(), s(&self.tmux_tmpdir));
        env.insert("FLEETMUX_HOME".into(), s(&self.fleet_home));
        env.insert("FLEETMUX_STATE".into(), s(&self.state));
        env.insert("FLEETMUX_RELEASES".into(), s(&self.releases));
        env.insert("FLEETMUX_DISCOVERY".into(), "off".into());
        env.insert("FLEETMUX_BIN".into(), s(&release_bin.join("fleetmux")));
        env.insert("FLEETMUX_ORPHAN_SWEEP_ROOT".into(), s(&self.redis_root));
        env.insert("PYTHONDONTWRITEBYTECODE".into(), "1".into());
        env.insert("GIT_CONFIG_GLOBAL".into(), "/dev/null".into());
        env.insert("GIT_CONFIG_NOSYSTEM".into(), "1".into());
        env
    }

    /// `<world>/bin/tmux`: the real tmux, always with `-f /dev/null` (no user or system configuration), so any
    /// tmux server the fleet starts in this world is a private one with its socket under TMUX_TMPDIR.
    pub fn write_tmux_wrapper(&self, tmux: &Path) -> std::io::Result<PathBuf> {
        let p = self.bin.join("tmux");
        std::fs::write(
            &p,
            format!(
                "#!/bin/sh\n# fleetmux scenario: private tmux (no configuration file)\nexec {} -f /dev/null \"$@\"\n",
                shell_quote(&tmux.display().to_string())
            ),
        )?;
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o700))?;
        Ok(p)
    }
}

/// The programs the scenario needs, found on `[redis dir] + PATH`; `Err` names every missing one.
pub fn find_tools(path: Option<&str>, redis_dir: &str) -> Result<BTreeMap<String, PathBuf>, String> {
    let mut dirs: Vec<PathBuf> = vec![PathBuf::from(redis_dir)];
    if let Some(p) = path {
        dirs.extend(std::env::split_paths(p));
    }
    let mut found = BTreeMap::new();
    let mut missing = Vec::new();
    for name in ["redis-server", "tmux", "git"] {
        match dirs.iter().map(|d| d.join(name)).find(|p| is_exe(p)) {
            Some(p) => {
                found.insert(name.to_string(), p);
            }
            None => missing.push(name),
        }
    }
    if missing.is_empty() {
        Ok(found)
    } else {
        Err(format!(
            "missing programs: {} (looked in {} and PATH); a missing program fails the scenario",
            missing.join(", "),
            redis_dir
        ))
    }
}

pub fn is_exe(p: &Path) -> bool {
    std::fs::metadata(p).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

pub fn shell_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

fn mkdtemp(prefix: &str) -> std::io::Result<PathBuf> {
    let mut buf = format!("{prefix}XXXXXX\0").into_bytes();
    let p = unsafe { libc::mkdtemp(buf.as_mut_ptr() as *mut libc::c_char) };
    if p.is_null() {
        return Err(std::io::Error::last_os_error());
    }
    buf.pop();
    let path = PathBuf::from(String::from_utf8(buf).map_err(std::io::Error::other)?);
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700))?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_world_is_private_and_short() {
        let w = World::create().unwrap();
        let mode = std::fs::metadata(&w.root).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o700);
        assert!(w.root.display().to_string().len() < 40, "{}", w.root.display());
        for d in [&w.home, &w.tmux_tmpdir, &w.redis_root, &w.releases] {
            assert!(d.starts_with(&w.root) && d.is_dir());
        }
        // named like a machine's ~/fleetmux-releases: the product knows a release by that parent directory
        // (releases.release_of / from_python), so a daemon of a release installed here reports its release
        assert_eq!(w.releases.file_name().unwrap(), "fleetmux-releases");
        std::fs::remove_dir_all(&w.root).unwrap();
    }

    #[test]
    fn env_scrubs_the_callers_fleet_and_points_everything_into_the_world() {
        let w = World::create().unwrap();
        let mut base = BTreeMap::new();
        for (k, v) in [
            ("PATH", "/usr/bin:/bin"),
            ("HOME", "/Users/someone"),
            ("SHELL", "/bin/zsh"),
            ("TMUX", "/tmp/tmux-1/default,1,0"),
            ("TMUX_PANE", "%1"),
            ("FLEETMUX_HOME", "/Users/someone/.fleetmux"),
            ("FLEETMUX_PYTHON_CLI", "/x"),
            ("CLAUDECODE", "1"),
            ("ANTHROPIC_API_KEY", "k"),
            ("PYTHONPATH", "/x"),
            ("LANG", "en_US.UTF-8"),
        ] {
            base.insert(k.to_string(), v.to_string());
        }
        let env = w.env(&base, Path::new("/rel/bin"), &[PathBuf::from("/opt/redis/bin")]);
        assert_eq!(env["HOME"], w.home.display().to_string());
        assert_eq!(env["SHELL"], "/bin/sh");
        assert_eq!(env["TMUX_TMPDIR"], w.tmux_tmpdir.display().to_string());
        assert_eq!(env["FLEETMUX_HOME"], w.fleet_home.display().to_string());
        assert_eq!(env["FLEETMUX_RELEASES"], w.releases.display().to_string());
        assert_eq!(env["FLEETMUX_ORPHAN_SWEEP_ROOT"], w.redis_root.display().to_string());
        assert_eq!(env["FLEETMUX_BIN"], "/rel/bin/fleetmux");
        assert!(!env.contains_key("TMUX") && !env.contains_key("TMUX_PANE"));
        assert!(!env.contains_key("FLEETMUX_PYTHON_CLI") && !env.contains_key("CLAUDECODE"));
        assert!(!env.contains_key("ANTHROPIC_API_KEY") && !env.contains_key("PYTHONPATH"));
        assert_eq!(env["LANG"], "en_US.UTF-8");
        let path: Vec<&str> = env["PATH"].split(':').collect();
        assert_eq!(path[0], w.bin.display().to_string());
        assert_eq!(path[1], "/rel/bin");
        assert_eq!(path[2], "/opt/redis/bin");
        std::fs::remove_dir_all(&w.root).unwrap();
    }

    #[test]
    fn the_tmux_wrapper_always_passes_dev_null_as_config() {
        let w = World::create().unwrap();
        let p = w.write_tmux_wrapper(Path::new("/opt/homebrew/bin/tmux")).unwrap();
        let text = std::fs::read_to_string(&p).unwrap();
        assert!(text.contains("exec '/opt/homebrew/bin/tmux' -f /dev/null \"$@\""), "{text}");
        assert!(is_exe(&p));
        std::fs::remove_dir_all(&w.root).unwrap();
    }

    #[test]
    fn a_missing_program_is_named() {
        let empty = World::create().unwrap();
        let err = find_tools(Some(&empty.bin.display().to_string()), &empty.home.display().to_string())
            .unwrap_err();
        assert!(err.contains("redis-server") && err.contains("tmux") && err.contains("git"), "{err}");
        std::fs::remove_dir_all(&empty.root).unwrap();
    }
}
