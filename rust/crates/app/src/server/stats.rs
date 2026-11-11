//! `server.stats` counters (PROTOCOL.md §4.17). Signatures from stage A (brief-m03 §3.3); bodies from B21 (stats).
//! Other modules only add `fetch_add` calls on [`Counters`]: `parse_bytes` / `wakeups` in `boot.rs` (B6),
//! `frames` in `view::render_all` (B7). This file only reads them.

use std::sync::atomic::{AtomicU64, Ordering};

use super::State;

/// Cumulative counters, all starting at 0 with the server. `wakeups` is bumped once per event-loop wake-up that
/// does work other than answering `server.stats`.
#[derive(Debug, Default)]
pub struct Counters {
    pub frames: AtomicU64,
    pub parse_bytes: AtomicU64,
    pub wakeups: AtomicU64,
}

/// The `server.stats` result for the current state: pane and viewer counts, the three counters, this process's
/// RSS and the sum of every pane's scrollback rows.
pub fn snapshot(state: &State) -> fleetmux_proto::Stats {
    let panes = state.panes.list();
    let scrollback_rows = panes
        .iter()
        .map(|p| {
            state
                .panes
                .term(&p.pane)
                .map_or(u64::from(p.scrollback), |t| t.scrollback_len() as u64)
        })
        .sum();
    let c = &state.counters;
    fleetmux_proto::Stats {
        panes: panes.len() as u32,
        viewers: state.viewers.len() as u32,
        frames: c.frames.load(Ordering::Relaxed),
        parse_bytes: c.parse_bytes.load(Ordering::Relaxed),
        wakeups: c.wakeups.load(Ordering::Relaxed),
        rss_bytes: rss_bytes(),
        scrollback_rows,
    }
}

/// Resident set size of this process in bytes (macOS `proc_pidinfo` / `task_info`, Linux `/proc/self/statm`).
/// 0 when the platform gives no answer.
pub fn rss_bytes() -> u64 {
    platform_rss().unwrap_or(0)
}

/// macOS: `proc_pidinfo(PROC_PIDTASKINFO)` → `proc_taskinfo.pti_resident_size` (bytes).
#[cfg(target_os = "macos")]
fn platform_rss() -> Option<u64> {
    let mut info: libc::proc_taskinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of::<libc::proc_taskinfo>() as libc::c_int;
    // SAFETY: `info` is a writable buffer of exactly `size` bytes for the PROC_PIDTASKINFO flavor.
    let n = unsafe {
        libc::proc_pidinfo(
            std::process::id() as libc::c_int,
            libc::PROC_PIDTASKINFO,
            0,
            (&raw mut info).cast(),
            size,
        )
    };
    (n == size).then_some(info.pti_resident_size)
}

/// Linux: the second field of `/proc/self/statm` (resident pages) × the page size.
#[cfg(target_os = "linux")]
fn platform_rss() -> Option<u64> {
    let text = std::fs::read_to_string("/proc/self/statm").ok()?;
    // SAFETY: sysconf has no preconditions.
    let page = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
    rss_from_statm(&text, u64::try_from(page).ok()?)
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn platform_rss() -> Option<u64> {
    None
}

/// Resident bytes from a `/proc/<pid>/statm` line (`size resident shared text lib data dt`, in pages).
#[cfg_attr(not(any(target_os = "linux", test)), allow(dead_code))]
fn rss_from_statm(statm: &str, page_size: u64) -> Option<u64> {
    let resident: u64 = statm.split_whitespace().nth(1)?.parse().ok()?;
    resident.checked_mul(page_size)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn statm_resident_pages_times_page_size() {
        assert_eq!(
            rss_from_statm("5732 1203 812 186 0 642 0\n", 4096),
            Some(1203 * 4096)
        );
        assert_eq!(rss_from_statm("5732", 4096), None);
        assert_eq!(rss_from_statm("", 4096), None);
        assert_eq!(rss_from_statm("1 x 3", 4096), None);
    }

    #[cfg(any(target_os = "macos", target_os = "linux"))]
    #[test]
    fn rss_of_this_process_is_plausible_and_grows_with_touched_memory() {
        let before = rss_bytes();
        // A test binary is at least a few hundred KiB resident and well under 4 GiB.
        assert!(before > 256 << 10 && before < 4 << 30, "{before}");
        // Touch 64 MiB: resident memory must grow by most of it.
        let mut block = vec![0u8; 64 << 20];
        for i in (0..block.len()).step_by(4096) {
            block[i] = 1;
        }
        let after = rss_bytes();
        std::hint::black_box(&block);
        assert!(after >= before + (48 << 20), "{before} → {after}");
    }
}
