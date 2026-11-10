//! Performance budgets (plan.md §6 `perf` package, design §7.3 M3, brief-m03 §3.4). Always `#[ignore]`: the
//! orchestrating session runs them by hand with `cargo test -p fleetmux --test perf -- --ignored` (release
//! numbers on the M-series Mac and the UTM VM). Uses the shared `common` harness (this file had no inline one).
mod common;
use common::*;

/// `ps -o %cpu=` of `pid`.
fn cpu_percent(pid: i32) -> f64 {
    let out = Command::new("ps")
        .args(["-o", "%cpu=", "-p", &pid.to_string()])
        .output()
        .expect("run ps");
    let text = String::from_utf8_lossy(&out.stdout);
    text.trim()
        .parse()
        .unwrap_or_else(|_| panic!("ps -o %cpu= for {pid}: {text:?}"))
}

fn stat(srv: &Server, key: &str) -> u64 {
    let s = srv.stats();
    s[key]
        .as_u64()
        .unwrap_or_else(|| panic!("server.stats without {key}: {s}"))
}

#[test]
#[ignore = "M3: perf, manual"]
fn eight_panes_flood_cpu_under_30pct() {
    let srv = Server::start();
    for k in 0..8 {
        start_probe_as(&srv, &format!("fx-worker-{k}"), "worker", &["--no-mouse", "--flood", "2000"]);
    }
    let v = Viewer::attach(&srv, W, H);
    std::thread::sleep(Duration::from_secs(1));
    let samples: Vec<f64> = (0..10)
        .map(|_| {
            std::thread::sleep(Duration::from_secs(1));
            cpu_percent(srv.pid())
        })
        .collect();
    let mean = samples.iter().sum::<f64>() / samples.len() as f64;
    assert!(mean < 30.0, "server CPU {mean:.1}% with 8 flooding panes (samples {samples:?})");
    drop(v);
}

#[test]
#[ignore = "M3: perf, manual"]
fn idle_cpu_is_zero_no_polling() {
    let srv = Server::start();
    sh(&srv, "fx-worker-1", "worker", "exec sleep 1000");
    let v = Viewer::attach(&srv, W, H);
    v.wait_quiet(Duration::from_millis(500), Duration::from_secs(10));
    std::thread::sleep(Duration::from_secs(1));
    let w0 = stat(&srv, "wakeups");
    std::thread::sleep(Duration::from_secs(5));
    let cpu = cpu_percent(srv.pid());
    let w1 = stat(&srv, "wakeups");
    assert!(cpu < 1.0, "idle server at {cpu}% CPU");
    assert_eq!(w1 - w0, 0, "event loop woke in 5 idle seconds (PROTOCOL.md §4.17: no polling)");
    drop(v);
}

#[test]
#[ignore = "M3: perf, manual"]
fn scrollback_10k_memory_under_50mb() {
    let srv = Server::start();
    let rss0 = stat(&srv, "rss_bytes");
    let id = sh(
        &srv,
        "fx-worker-1",
        "worker",
        "i=1; while [ $i -le 10000 ]; do printf 'LINE-%05d %0100d\\n' $i 0; i=$((i+1)); done; exec sleep 1000",
    );
    srv.wait_text(&id, "LINE-10000", Duration::from_secs(60));
    let rss1 = stat(&srv, "rss_bytes");
    let grew = rss1.saturating_sub(rss0);
    assert!(grew <= 50 << 20, "10,000 lines of scrollback grew the server by {} MiB", grew >> 20);
}
