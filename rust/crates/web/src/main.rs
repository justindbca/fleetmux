//! `fleetmux-web` binary (PROTOCOL.md §15.1): only dispatches to the library.
use std::process::ExitCode;

fn main() -> ExitCode {
    fleetmux_web::window::main(&std::env::args().skip(1).collect::<Vec<_>>())
}
