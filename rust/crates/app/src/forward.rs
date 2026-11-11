//! Fleet commands belong to the Python CLI (docs/design.md §50): every subcommand `main.rs` does not own — and
//! `gui` without `--runtime` — is handed to `fleet.py` of the same release by `exec`, so argv, stdin / stdout /
//! stderr and the exit code are the Python CLI's own.
//!
//! Lookup, first hit wins: `$FLEETMUX_PYTHON_CLI` (an executable, run as is; set but unusable is an error, never a
//! fallback), the release this binary sits in (`<release>/bin/fleetmux`), `~/fleetmux-current`. A release dir
//! counts when it holds `.venv/bin/python` and `fleetmux/fleet.py`.
use std::ffi::OsString;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

/// Optional override: the fleet CLI to exec (e.g. `<release>/.venv/bin/fleetmux-py`).
pub const CLI_ENV: &str = "FLEETMUX_PYTHON_CLI";
/// No Python CLI found (the shell's "command not found").
pub const NOT_FOUND: u8 = 127;

/// How to run the Python CLI: `program lead... <user args>`.
#[derive(Debug, PartialEq, Eq)]
pub struct Cli {
    pub program: PathBuf,
    pub lead: Vec<PathBuf>,
}

fn is_exe(p: &Path) -> bool {
    std::fs::metadata(p).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

/// `<rel>/.venv/bin/python <rel>/fleetmux/fleet.py` when `rel` is a release / checkout with its venv.
pub fn release_cli(rel: &Path) -> Option<Cli> {
    let python = rel.join(".venv/bin/python");
    let script = rel.join("fleetmux/fleet.py");
    (is_exe(&python) && script.is_file()).then(|| Cli {
        program: python,
        lead: vec![script],
    })
}

/// The release dirs this binary may sit in: `<exe>/../..`, as invoked and with links resolved.
fn own_releases(exe: Option<PathBuf>) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    let Some(exe) = exe else { return out };
    let canonical = std::fs::canonicalize(&exe).ok();
    for e in std::iter::once(exe).chain(canonical) {
        if let Some(rel) = e.parent().and_then(Path::parent)
            && !out.iter().any(|r| r == rel)
        {
            out.push(rel.to_path_buf());
        }
    }
    out
}

/// Find the Python CLI. `Err` = the message for stderr (exit [`NOT_FOUND`]).
pub fn locate(
    cmd: &str,
    env_cli: Option<OsString>,
    exe: Option<PathBuf>,
    home: Option<OsString>,
) -> Result<Cli, String> {
    if let Some(v) = env_cli.filter(|v| !v.is_empty()) {
        let p = PathBuf::from(&v);
        return if is_exe(&p) {
            Ok(Cli {
                program: p,
                lead: Vec::new(),
            })
        } else {
            Err(format!(
                "fleetmux: ${CLI_ENV}={} is not an executable file; point it at the fleet CLI \
                 (e.g. <release>/.venv/bin/fleetmux-py) or unset it",
                p.display()
            ))
        };
    }
    let mut looked = Vec::new();
    for rel in own_releases(exe) {
        if let Some(cli) = release_cli(&rel) {
            return Ok(cli);
        }
        looked.push(rel.display().to_string());
    }
    if let Some(home) = home.filter(|h| !h.is_empty()) {
        let rel = Path::new(&home).join("fleetmux-current");
        if let Some(cli) = release_cli(&rel) {
            return Ok(cli);
        }
        looked.push(rel.display().to_string());
    } else {
        looked.push("~/fleetmux-current ($HOME not set)".into());
    }
    Err(format!(
        "fleetmux: `{cmd}` is a fleet command, run by the Python fleet CLI, and none was found.\n\
         looked for .venv/bin/python + fleetmux/fleet.py in: {}\n\
         fix: set {CLI_ENV} to the fleet CLI (e.g. <release>/.venv/bin/fleetmux-py), or install a release \
         with scripts/install_release.sh <sha> <tarball> --activate (it maintains ~/fleetmux-current)",
        looked.join(", ")
    ))
}

/// Exec the Python CLI with `args` (all of them, the subcommand first). Returns only on failure.
pub fn run(args: &[OsString]) -> ExitCode {
    let cmd = args
        .first()
        .map(|a| a.to_string_lossy().into_owned())
        .unwrap_or_default();
    let cli = match locate(
        &cmd,
        std::env::var_os(CLI_ENV),
        std::env::current_exe().ok(),
        std::env::var_os("HOME"),
    ) {
        Ok(cli) => cli,
        Err(msg) => {
            eprintln!("{msg}");
            return ExitCode::from(NOT_FOUND);
        }
    };
    let err = Command::new(&cli.program).args(&cli.lead).args(args).exec();
    eprintln!("fleetmux: cannot run {}: {err}", cli.program.display());
    ExitCode::from(126)
}
