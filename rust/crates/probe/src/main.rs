//! ft-probe: test fixture (behaviour spec in crates/probe/README.md). This file keeps the command line
//! (`parse_args`, also used by `tests/args.rs`); `run` lives in the private modules below.
// Explicit paths: `tests/args.rs` includes this file with `#[path]`, and these resolve the same either way.
#[path = "app.rs"]
mod app;
#[path = "input.rs"]
mod input;
#[path = "log.rs"]
mod log;
#[path = "screen.rs"]
mod screen;
#[path = "sys.rs"]
mod sys;

use std::process::ExitCode;
use std::time::Duration;

const USAGE: &str = "usage: ft-probe [--no-mouse] [--no-paste] [--kitty-query] [--app-cursor] [--exit-after <ms>] \
     [--mouse-mode click|drag|motion] [--x10] [--flood <lines_per_s>] [--lines <n>] [--wide]";

/// Which mouse tracking mode to turn on (README "Command line"): `?1000` / `?1002` / `?1003`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum MouseMode {
    Click,
    #[default]
    Drag,
    Motion,
}

/// What the command line asked for (README "Command line").
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Opts {
    pub mouse: bool,
    pub paste: bool,
    pub kitty_query: bool,
    pub app_cursor: bool,
    pub exit_after: Option<Duration>,
    /// Tracking mode when mouse reporting is on (default `Drag`).
    pub mouse_mode: MouseMode,
    /// Turn on SGR encoding (`?1006`); `--x10` clears it.
    pub sgr: bool,
    /// `--flood <n>`: print `n` lines per second after start-up.
    pub flood: Option<u32>,
    /// `--lines <n>`: lines printed into scrollback before start-up (0 = none).
    pub lines: u32,
    /// `--wide`: draw the wide-character row.
    pub wide: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Parsed {
    Run(Opts),
    Help,
}

/// Parses the arguments after the program name. `Err` carries the message printed before the usage line.
pub fn parse_args<I: IntoIterator<Item = String>>(args: I) -> Result<Parsed, String> {
    let mut opts = Opts {
        mouse: true,
        paste: true,
        sgr: true,
        ..Opts::default()
    };
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--no-mouse" => opts.mouse = false,
            "--no-paste" => opts.paste = false,
            "--kitty-query" => opts.kitty_query = true,
            "--app-cursor" => opts.app_cursor = true,
            "--exit-after" => {
                let value = args
                    .next()
                    .ok_or("--exit-after needs a value in milliseconds")?;
                let ms: u64 = value
                    .parse()
                    .map_err(|_| format!("--exit-after: not a number of milliseconds: {value}"))?;
                opts.exit_after = Some(Duration::from_millis(ms));
            }
            "--mouse-mode" => {
                let value = args
                    .next()
                    .ok_or("--mouse-mode needs click, drag or motion")?;
                opts.mouse_mode = match value.as_str() {
                    "click" => MouseMode::Click,
                    "drag" => MouseMode::Drag,
                    "motion" => MouseMode::Motion,
                    _ => return Err(format!("--mouse-mode: not click, drag or motion: {value}")),
                };
            }
            "--x10" => opts.sgr = false,
            "--flood" => {
                let value = args.next().ok_or("--flood needs a number of lines per second")?;
                let n: u32 = value
                    .parse()
                    .ok()
                    .filter(|n| *n > 0)
                    .ok_or_else(|| format!("--flood: not a positive number of lines per second: {value}"))?;
                opts.flood = Some(n);
            }
            "--lines" => {
                let value = args.next().ok_or("--lines needs a number of lines")?;
                opts.lines = value
                    .parse()
                    .map_err(|_| format!("--lines: not a number of lines: {value}"))?;
            }
            "--wide" => opts.wide = true,
            "-h" | "--help" => return Ok(Parsed::Help),
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    Ok(Parsed::Run(opts))
}

/// Runs the probe until `q`, SIGTERM or `--exit-after`; returns the process exit code (README "Exit").
pub fn run(opts: &Opts, log_path: &std::path::Path) -> ExitCode {
    app::run(opts, log_path)
}

fn main() -> ExitCode {
    let opts = match parse_args(std::env::args().skip(1)) {
        Ok(Parsed::Run(opts)) => opts,
        Ok(Parsed::Help) => {
            println!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        Err(msg) => {
            eprintln!("ft-probe: {msg}\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let Some(log_path) = std::env::var_os("FT_PROBE_LOG") else {
        eprintln!("ft-probe: FT_PROBE_LOG is not set");
        return ExitCode::from(2);
    };
    run(&opts, std::path::Path::new(&log_path))
}
