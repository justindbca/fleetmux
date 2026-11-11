//! fleetmux app crate as a library, so `main.rs` stays a dispatcher and integration tests can reach the
//! internal seams (brief-m03 §3.3). Module ownership: `paths` / `server::{boot,socket,handlers,daemonize,stats}`
//! ctl-1; `server::{mod,pane,pty,reap,record}` server-1; `server::{view,mouse,scroll,views}`, `render`, `attach`
//! attach-1.
pub mod attach;
pub mod forward;
pub mod paths;
pub mod render;
pub mod server;
