//! fleetmux binary: `--version` / `server` / `attach` / `reap` / `web` / `gui` (PROTOCOL.md §9; `web` added by
//! M2.5 W0, `gui` by M2.6 W0). Final form (brief-m03 A-R3):
//! it only dispatches; every subcommand lives in the library. Every other subcommand, and `gui` without
//! `--runtime`, is a fleet command: `forward` execs the Python fleet CLI with it (docs/design.md §50).
use std::process::ExitCode;

const USAGE: &str =
    "usage: fleetmux --version | server | attach | reap | web | gui --runtime D | <fleet command>";
const FLEET_HINT: &str = "fleet commands (start, task, request, status, stop, resume, pause, results, logs, \
operator, subfleet, advisor, night, agents, plan, peer, chrome, gui, ...) run the Python fleet CLI: \
fleetmux <command> --help";

fn main() -> ExitCode {
    let raw: Vec<std::ffi::OsString> = std::env::args_os().skip(1).collect();
    let first = raw.first().map(|a| a.to_str().unwrap_or(""));
    let args = || -> Vec<String> { std::env::args().skip(1).collect() };
    match first {
        Some("--version") => {
            println!(
                "fleetmux {} (protocol {})",
                env!("CARGO_PKG_VERSION"),
                fleetmux_proto::PROTOCOL
            );
            ExitCode::SUCCESS
        }
        Some("--help") | Some("-h") => {
            println!("{USAGE}\n{FLEET_HINT}");
            ExitCode::SUCCESS
        }
        Some("server") => fleetmux::server::run(&args()[1..]),
        Some("attach") => fleetmux::attach::run(&args()[1..]),
        Some("reap") => fleetmux::server::reap::run(&args()[1..]),
        Some("web") => fleetmux::server::boot::web(&args()[1..]),
        Some("gui") if raw[1..].iter().any(|a| a == "--runtime") => {
            fleetmux::attach::gui::run(&args()[1..])
        }
        Some(_) => fleetmux::forward::run(&raw),
        None => {
            eprintln!("{USAGE}\n{FLEET_HINT}");
            ExitCode::from(2)
        }
    }
}
