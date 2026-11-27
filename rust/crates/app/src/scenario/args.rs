//! Command line of `fleetmux scenario upgrade`.
//!
//! ```text
//! fleetmux scenario upgrade --from <release> --to <release> [--repo DIR] [--timeout S] [--keep]
//! ```
//!
//! `<release>` is either a release directory (it holds `.venv/bin/python`, `fleetmux/fleet.py` and `bin/fleetmux`)
//! or a git reference (`main`, `HEAD`, a sha) resolved in `--repo` and installed into the scenario's own directory.
use std::path::PathBuf;

/// Default whole-run budget in seconds.
pub const DEFAULT_TIMEOUT_S: u64 = 3600;

pub const USAGE: &str = "usage: fleetmux scenario upgrade --from <release> --to <release> [--repo DIR] \
[--timeout S] [--keep]\n  <release>: a release directory (.venv/bin/python, fleetmux/fleet.py, bin/fleetmux) or a git \
reference resolved in --repo (default: the checkout this binary was built from)";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Args {
    pub from: String,
    pub to: String,
    pub repo: Option<PathBuf>,
    pub timeout_s: u64,
    pub keep: bool,
}

/// Parse the arguments that follow `scenario upgrade`. `Err` is the message for stderr (exit 2); an empty `Err`
/// means `--help` was asked for.
pub fn parse(argv: &[String]) -> Result<Args, String> {
    let mut from = None;
    let mut to = None;
    let mut repo = None;
    let mut timeout_s = DEFAULT_TIMEOUT_S;
    let mut keep = false;
    let mut i = 0;
    while i < argv.len() {
        let arg = argv[i].as_str();
        i += 1;
        if arg == "-h" || arg == "--help" {
            return Err(String::new());
        }
        if arg == "--keep" {
            keep = true;
            continue;
        }
        let (name, inline) = match arg.split_once('=') {
            Some((n, v)) if n.starts_with("--") => (n, Some(v.to_string())),
            _ => (arg, None),
        };
        if !matches!(name, "--from" | "--to" | "--repo" | "--timeout") {
            return Err(format!("unknown argument {arg}"));
        }
        let value = match inline {
            Some(v) => v,
            None => {
                let v = argv.get(i).cloned().ok_or_else(|| format!("{name} needs a value"))?;
                i += 1;
                v
            }
        };
        if value.is_empty() || value.starts_with("--") {
            return Err(format!("{name} needs a value, got {value:?}"));
        }
        match name {
            "--from" => from = Some(value),
            "--to" => to = Some(value),
            "--repo" => repo = Some(PathBuf::from(value)),
            _ => {
                timeout_s = value
                    .parse::<u64>()
                    .ok()
                    .filter(|t| *t > 0)
                    .ok_or_else(|| format!("--timeout needs a whole number of seconds above 0, got {value:?}"))?;
            }
        }
    }
    Ok(Args {
        from: from.ok_or("--from <release> is required")?,
        to: to.ok_or("--to <release> is required")?,
        repo,
        timeout_s,
        keep,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn parses_all_options() {
        let a = parse(&s(&[
            "--from", "main", "--to", "/r/abc", "--repo", "/src", "--timeout", "90", "--keep",
        ]))
        .unwrap();
        assert_eq!(
            a,
            Args {
                from: "main".into(),
                to: "/r/abc".into(),
                repo: Some(PathBuf::from("/src")),
                timeout_s: 90,
                keep: true
            }
        );
    }

    #[test]
    fn accepts_inline_values_and_defaults() {
        let a = parse(&s(&["--from=main", "--to=HEAD"])).unwrap();
        assert_eq!(a.from, "main");
        assert_eq!(a.to, "HEAD");
        assert_eq!(a.repo, None);
        assert_eq!(a.timeout_s, DEFAULT_TIMEOUT_S);
        assert!(!a.keep);
    }

    #[test]
    fn rejects_missing_and_bad_values() {
        assert!(parse(&s(&["--to", "HEAD"])).unwrap_err().contains("--from"));
        assert!(parse(&s(&["--from", "main"])).unwrap_err().contains("--to"));
        assert!(parse(&s(&["--from", "--to", "HEAD"])).is_err());
        assert!(parse(&s(&["--from", "a", "--to", "b", "--timeout", "0"])).is_err());
        assert!(parse(&s(&["--from", "a", "--to", "b", "--timeout", "x"])).is_err());
        assert!(parse(&s(&["--from", "a", "--to", "b", "--bogus"])).unwrap_err().contains("--bogus"));
        assert!(parse(&s(&["--from", "a", "--to"])).is_err());
    }
}
