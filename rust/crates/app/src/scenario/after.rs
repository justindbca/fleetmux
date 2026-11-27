//! The scenario's own checks after `fleetmux upgrade` (it does not rely on the upgrade's own verify).
//!
//! Each check is a pure function of what the scenario observed from the outside: `status` and `applet list` of the
//! `--to` CLI, the process table (`ps`), the run configurations, the daemon locks and `git log` of the job
//! repositories. The scenario polls each check until it passes or its limit runs out.
//!
//! Scope of the interpreter check: the daemons, every run config's `python`, the actor and
//! history processes started after the upgrade began, and the applets must be on `--to`. Agent sessions that were
//! already open before the upgrade keep their old hook lines until their next generation; their processes are
//! listed, not failed.
//! "Not worse": a state that was already bad before the upgrade (a held root supervisor) is reported as
//! such and does not fail the check.
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// One process of the world, from `ps -o pid=,etime=,command=`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Proc {
    pub pid: i32,
    /// Seconds since the epoch when it started (now minus its elapsed time).
    pub started: i64,
    pub argv: Vec<String>,
    /// `__PYVENV_LAUNCHER__` of its environment: on macOS a venv's python re-executes the framework's
    /// `Python.app`, so argv[0] names the base interpreter and only this names the venv's python.
    pub launcher: Option<String>,
}

/// The interpreter a Python process runs as: the venv launcher when there is one, else argv[0].
pub fn interpreter(p: &Proc) -> &str {
    p.launcher.as_deref().unwrap_or_else(|| p.argv.first().map(String::as_str).unwrap_or(""))
}

/// `__PYVENV_LAUNCHER__` from `ps -wwE -o command=` output (the command line followed by the environment).
pub fn launcher_in(ps_env: &str) -> Option<String> {
    ps_env
        .split_whitespace()
        .find_map(|w| w.strip_prefix("__PYVENV_LAUNCHER__="))
        .filter(|v| !v.is_empty())
        .map(str::to_string)
}

/// One fleet of the tree: the root (`key` = `root`) or a sub-fleet (its name or task id).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Node {
    pub key: String,
    pub run: PathBuf,
    /// A daemon holds `<run>/daemon.lock` now.
    pub locked: bool,
    /// The run config's `python` now.
    pub python: Option<String>,
}

/// `path` lies inside `dir` (whole path components; `/private/tmp` and `/tmp` are the same place).
pub fn under(path: &str, dir: &Path) -> bool {
    let norm = |p: &str| -> String {
        let p = p.trim_end_matches('/');
        match p.strip_prefix("/private/tmp") {
            Some(rest) if rest.is_empty() || rest.starts_with('/') => format!("/tmp{rest}"),
            _ => p.to_string(),
        }
    };
    let (p, d) = (norm(path), norm(&dir.display().to_string()));
    p == d || p.strip_prefix(&d).is_some_and(|rest| rest.starts_with('/'))
}

/// `[[dd-]hh:]mm:ss` (ps `etime`) in seconds.
fn etime(t: &str) -> Option<i64> {
    let (days, rest) = match t.split_once('-') {
        Some((d, r)) => (d.parse::<i64>().ok()?, r),
        None => (0, t),
    };
    let mut secs = 0i64;
    for part in rest.split(':') {
        secs = secs * 60 + part.parse::<i64>().ok()?;
    }
    Some(days * 86400 + secs)
}

/// Parse `ps -o pid=,etime=,command=` output; `now` is the current time in seconds since the epoch.
pub fn parse_ps(text: &str, now: i64) -> Vec<Proc> {
    text.lines()
        .filter_map(|line| {
            let mut it = line.split_whitespace();
            let pid = it.next()?.parse::<i32>().ok()?;
            let started = now - etime(it.next()?)?;
            let argv: Vec<String> = it.map(str::to_string).collect();
            (!argv.is_empty()).then_some(Proc { pid, started, argv, launcher: None })
        })
        .collect()
}

/// The subcommand of a `... fleet.py <sub> ...` process (`daemon`, `actor`, `history`, ...) and the index of
/// fleet.py in its argv.
fn fleet_sub(p: &Proc) -> Option<(usize, &str)> {
    let i = p.argv.iter().position(|a| a.ends_with("fleetmux/fleet.py"))?;
    Some((i, p.argv.get(i + 1)?.as_str()))
}

const WRAPPERS: &[&str] = &["caffeinate", "systemd-inhibit"];

fn is_wrapper(p: &Proc) -> bool {
    p.argv.first().is_some_and(|a| WRAPPERS.iter().any(|w| a == w || a.ends_with(&format!("/{w}"))))
}

/// The daemon processes of `run`: `... fleet.py daemon <run>/config.json ...` (a keep-awake wrapper is not one).
pub fn daemons<'a>(procs: &'a [Proc], run: &Path) -> Vec<&'a Proc> {
    let cfg = run.join("config.json");
    procs
        .iter()
        .filter(|p| !is_wrapper(p))
        .filter(|p| match fleet_sub(p) {
            // the interpreter itself runs fleet.py (argv[1]); a shell or tmux carrying the command line is not one
            Some((1, "daemon")) => p.argv.get(3).is_some_and(|c| under(c, &cfg)),
            _ => false,
        })
        .collect()
}

/// `check_daemons`: every fleet that ran before holds its lock again, and its daemon runs `--to`'s fleet.py.
pub fn check_daemons(nodes: &[Node], procs: &[Proc], to: &Path) -> Result<String, String> {
    let mut ok = Vec::new();
    let mut bad = Vec::new();
    for n in nodes {
        if !n.locked {
            bad.push(format!("{}: no daemon holds {}/daemon.lock", n.key, n.run.display()));
            continue;
        }
        let ds = daemons(procs, &n.run);
        if ds.is_empty() {
            bad.push(format!("{}: the lock is held but no daemon process of {} was found", n.key, n.run.display()));
            continue;
        }
        let mut fine = true;
        for d in &ds {
            let script = fleet_sub(d).map(|(i, _)| d.argv[i].as_str()).unwrap_or("?");
            if !under(script, to) {
                bad.push(format!("{}: daemon {} runs {script}", n.key, d.pid));
                fine = false;
            }
        }
        if fine {
            let pids: Vec<String> = ds.iter().map(|d| d.pid.to_string()).collect();
            ok.push(format!("{} daemon {} on --to", n.key, pids.join(",")));
        }
    }
    if bad.is_empty() {
        Ok(ok.join("; "))
    } else {
        Err(bad.join("; "))
    }
}

/// `check_interpreter`: run configs, daemons, actor / history processes started at or after `since`, and the
/// applets (`applets`: name and pid) are on `--to`'s interpreter.
pub fn check_interpreter(
    nodes: &[Node],
    procs: &[Proc],
    applets: &[(String, i32)],
    to: &Path,
    since: i64,
) -> Result<String, String> {
    let venv = to.join(".venv");
    let mut bad = Vec::new();
    let mut checked = 0;
    for n in nodes {
        match &n.python {
            Some(py) if under(py, &venv) => checked += 1,
            other => bad.push(format!("{} config python {}", n.key, other.as_deref().unwrap_or("(none)"))),
        }
        for d in daemons(procs, &n.run) {
            checked += 1;
            if !under(interpreter(d), &venv) {
                bad.push(format!("{} daemon {} on {}", n.key, d.pid, interpreter(d)));
            }
        }
    }
    let mut older = 0;
    for p in procs.iter().filter(|p| !is_wrapper(p)) {
        let Some((i, sub)) = fleet_sub(p) else { continue };
        if sub != "actor" && sub != "history" {
            continue;
        }
        if p.started < since {
            older += 1; // a session opened before the upgrade keeps its hook lines until its next generation
            continue;
        }
        checked += 1;
        // the interpreter itself, or a wrapper (`bash -lc`, tmux) carrying the launch command: the word before fleet.py
        let interp = if i == 1 { interpreter(p) } else { p.argv[i - 1].trim_matches(|c| c == '"' || c == '\'') };
        if !under(interp, &venv) || !under(&p.argv[i], to) {
            bad.push(format!("{sub} {} on {interp} {}", p.pid, p.argv[i]));
        }
    }
    for (name, pid) in applets {
        match procs.iter().find(|p| p.pid == *pid) {
            None => bad.push(format!("applet {name} (pid {pid}) is not running")),
            Some(p) if !under(interpreter(p), &venv) => bad.push(format!("applet {name} {pid} on {}", interpreter(p))),
            Some(_) => checked += 1,
        }
    }
    if bad.is_empty() {
        Ok(format!(
            "{checked} config(s) / process(es) on {}; {older} agent process(es) from before the upgrade not checked (a session opened before the upgrade keeps its hook lines until its next generation)",
            venv.display()
        ))
    } else {
        Err(format!("not on {}: {}", venv.display(), bad.join("; ")))
    }
}

/// `check_jobs`: every job open before is back with the same id; running stays running, queued stays queued (or
/// has started); `results` are the published job results (`job-results/<id>.json` status) by id.
pub fn check_jobs(
    before: &[(String, String)],
    after: &[(String, String)],
    results: &BTreeMap<String, String>,
) -> Result<String, String> {
    let mut ok = Vec::new();
    let mut bad = Vec::new();
    for (id, was) in before {
        if !matches!(was.as_str(), "running" | "queued") {
            continue;
        }
        if let Some(r) = results.get(id).filter(|r| *r != "ok") {
            bad.push(format!("{id} published {r}"));
            continue;
        }
        let now = after.iter().find(|(i, _)| i == id).map(|(_, s)| s.as_str());
        match (was.as_str(), now) {
            (_, None) => bad.push(format!("{id} missing (was {was})")),
            ("running", Some("running")) => ok.push(format!("{id} running")),
            ("queued", Some(s @ ("queued" | "starting" | "running"))) => ok.push(format!("{id} {s}")),
            (_, Some(s)) => bad.push(format!("{id} is {s}, was {was}")),
        }
    }
    if bad.is_empty() {
        Ok(if ok.is_empty() { "no open job before the upgrade".into() } else { ok.join("; ") })
    } else {
        Err(bad.join("; "))
    }
}

/// `check_supervisor`: the root supervisor is not held, unless it already was before the upgrade (not worse than before).
pub fn check_supervisor(before: Option<&str>, after: Option<&str>) -> Result<String, String> {
    match (before, after) {
        (_, None) => Err("no supervisor slot in status".into()),
        (Some("held"), Some("held")) => Ok("held, as it was before the upgrade (not worse than before)".into()),
        (_, Some("held")) => Err(format!("the root supervisor is held (before the upgrade: {})", before.unwrap_or("?"))),
        (_, Some(s)) => Ok(format!("root supervisor {s}")),
    }
}

/// `check_applet`: the applet is up again as a new process (not the copy from before).
pub fn check_applet(before_pid: i64, after: &serde_json::Value, name: &str) -> Result<String, String> {
    let pid = super::tree::applet_up(after, name)?;
    if pid == before_pid {
        return Err(format!("applet {name} pid {pid} is the copy from before the upgrade"));
    }
    Ok(format!("applet {name} up, pid {pid} (was {before_pid})"))
}

/// The step numbers of a job repository, oldest first, from `git log --reverse --format=%s` subjects
/// (`scenario: step <n>`; other subjects are not steps).
pub fn step_numbers(subjects: &str) -> Vec<u32> {
    subjects
        .lines()
        .filter_map(|l| l.trim().strip_prefix("scenario: step ")?.trim().parse().ok())
        .collect()
}

/// `progress_after` for the running job: the steps are exactly 1..=n with nothing repeated, and n is above the
/// count before the upgrade.
pub fn check_steps(steps: &[u32], before: u32) -> Result<String, String> {
    let mut seen = std::collections::BTreeSet::new();
    for s in steps {
        if !seen.insert(*s) {
            return Err(format!("step {s} twice: a completed step was redone ({steps:?})"));
        }
    }
    for (i, s) in steps.iter().enumerate() {
        let want = i as u32 + 1;
        if *s != want {
            return Err(format!("step {want} is missing: the numbers do not go on ({steps:?})"));
        }
    }
    let n = steps.len() as u32;
    if n <= before {
        return Err(format!("no new step after the upgrade: {n} step(s), {before} before"));
    }
    Ok(format!("steps 1..{n} with none repeated, {} new after the upgrade", n - before))
}

/// Job A's progress on `--to`: steps 1..n with none repeated, and at least one step committed after `fleetmux
/// upgrade` returned (`at_upgrade_end` steps then). A step committed before the switch ended is not progress on the
/// new release, even when it came after `progress_before` (`before_upgrade`).
pub fn check_progress(steps: &[u32], before_upgrade: u32, at_upgrade_end: u32) -> Result<String, String> {
    check_steps(steps, 0)?;
    let n = steps.len() as u32;
    if n <= at_upgrade_end {
        return Err(format!(
            "no new step since the upgrade ended: {n} step(s), {at_upgrade_end} when it ended, {before_upgrade} before it"
        ));
    }
    Ok(format!(
        "steps 1..{n} with none repeated, {} new since the upgrade ended ({at_upgrade_end} then, {before_upgrade} before it)",
        n - at_upgrade_end
    ))
}

/// Sub-fleet supervisors the root supervisor was told were held after `since` (epoch seconds), from the scripted
/// actor's log: one entry per sub-fleet `{subfleet, reason, held_at, notices, heal_at}`.
pub fn supervisor_holds(actor_log: &str, since: f64) -> Vec<serde_json::Value> {
    use serde_json::{json, Value};
    let mut out: Vec<Value> = Vec::new();
    for line in actor_log.lines() {
        let Ok(v) = serde_json::from_str::<Value>(line) else { continue };
        let at = v.get("at").and_then(Value::as_f64).unwrap_or(0.0);
        if at < since || v.get("role").and_then(Value::as_str) != Some("supervisor") {
            continue;
        }
        match v.get("event").and_then(Value::as_str) {
            Some("turn.start") => {
                let text = v.get("text").and_then(Value::as_str).unwrap_or("");
                if !(text.starts_with("[sub-fleet supervisor held]") || text.starts_with("[sub-fleet stuck]")
                    || text.starts_with("[sub-fleet held]"))
                {
                    continue;
                }
                let Some(name) = text.split_once("] sub-fleet ").and_then(|(_, r)| r.split_whitespace().next()) else {
                    continue;
                };
                if let Some(e) = out.iter_mut().find(|e| e["subfleet"] == name) {
                    e["notices"] = json!(e["notices"].as_u64().unwrap_or(0) + 1);
                    continue;
                }
                let first = text.lines().next().unwrap_or("");
                let reason = match first.split_once("is held: ") {
                    Some((_, r)) => r.split(" with no progress").next().unwrap_or(r),
                    None => first,
                };
                let reason: String = reason.chars().take(240).collect();
                out.push(json!({"subfleet": name, "reason": reason, "held_at": at, "notices": 1,
                                "heal_at": Value::Null}));
            }
            Some("turn.reply") => {
                let healed = v.get("reply").and_then(Value::as_str)
                    .and_then(|r| serde_json::from_str::<Value>(r).ok())
                    .and_then(|r| r.get("heal").and_then(Value::as_array).map(|h| !h.is_empty()))
                    .unwrap_or(false);
                if healed {
                    for e in out.iter_mut().filter(|e| e["heal_at"].is_null()) {
                        e["heal_at"] = json!(at);
                    }
                }
            }
            _ => {}
        }
    }
    out
}

/// The step line for [`supervisor_holds`] (never a failure: a held sub-fleet supervisor is recorded, not judged).
pub fn holds_line(holds: &[serde_json::Value], since: f64) -> String {
    if holds.is_empty() {
        return "ok no sub-fleet supervisor was reported held after the upgrade began".into();
    }
    let parts: Vec<String> = holds
        .iter()
        .map(|h| {
            let held = h["held_at"].as_f64().unwrap_or(since);
            let heal = match h["heal_at"].as_f64() {
                Some(t) => format!("heal sent {:.0} s later", t - held),
                None => "no heal sent".into(),
            };
            format!("{} held {:.0} s after the upgrade began ({}); {}; {} notice(s)",
                    h["subfleet"].as_str().unwrap_or("?"), held - since, h["reason"].as_str().unwrap_or(""), heal,
                    h["notices"].as_u64().unwrap_or(0))
        })
        .collect();
    format!("ok {} (recorded, not a failure)", parts.join("; "))
}

/// Job A's end after the scenario released it: its `<run>/job-results/<id>.json` must say `ok` (a job that ends
/// blocked or failed did not go on after the upgrade, whatever steps it committed).
pub fn job_result(result: Option<&serde_json::Value>, id: &str) -> Result<String, String> {
    let Some(v) = result else {
        return Err(format!("{id}: no result published yet"));
    };
    let status = v.get("status").and_then(serde_json::Value::as_str).unwrap_or("?");
    if status == "ok" {
        return Ok(format!("{id} ended ok"));
    }
    let why = v.pointer("/result/reason").and_then(serde_json::Value::as_str).unwrap_or("");
    Err(format!("{id} ended {status}: {why}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const TO: &str = "/private/tmp/fmxu-ab12cd/rel/2222";
    const FROM: &str = "/private/tmp/fmxu-ab12cd/rel/1111";
    const RUN: &str = "/private/tmp/fmxu-ab12cd/runs/fleet-1";

    fn p(pid: i32, started: i64, cmd: &str) -> Proc {
        Proc { pid, started, argv: cmd.split_whitespace().map(str::to_string).collect(), launcher: None }
    }

    const FRAMEWORK: &str = "/opt/homebrew/Cellar/python@3.14/3.14.4_1/Frameworks/Python.framework/Versions/3.14/\
Resources/Python.app/Contents/MacOS/Python";

    #[test]
    fn a_macos_venv_python_is_known_by_its_launcher() {
        let env = format!("{FRAMEWORK} {TO}/fleetmux/fleet.py daemon {RUN}/config.json PATH=/usr/bin \
__PYVENV_LAUNCHER__={TO}/.venv/bin/python3.14 HOME=/h");
        assert_eq!(launcher_in(&env).as_deref(), Some(format!("{TO}/.venv/bin/python3.14").as_str()));
        assert_eq!(launcher_in("python x.py HOME=/h"), None);
        let mut d = p(2, 100, &format!("{FRAMEWORK} {TO}/fleetmux/fleet.py daemon {RUN}/config.json"));
        assert_eq!(interpreter(&d), FRAMEWORK);
        d.launcher = launcher_in(&env);
        assert!(interpreter(&d).ends_with("/rel/2222/.venv/bin/python3.14"));
        // the interpreter check accepts it on --to, and names a framework process launched from --from
        let nodes = vec![node("root", RUN, true, &format!("{TO}/.venv/bin/python"))];
        let mut applet = p(7, 500, &format!("{FRAMEWORK} /w/applet_tick.py"));
        applet.launcher = Some(format!("{TO}/.venv/bin/python"));
        let apps = vec![("applet-tick-1".to_string(), 7)];
        assert!(check_interpreter(&nodes, &[d.clone(), applet.clone()], &apps, Path::new(TO), 400).is_ok());
        let mut old = d.clone();
        old.launcher = Some(format!("{FROM}/.venv/bin/python3.14"));
        let e = check_interpreter(&nodes, &[old, applet.clone()], &apps, Path::new(TO), 400).unwrap_err();
        assert!(e.contains("daemon 2") && e.contains("1111"), "{e}");
        // without a launcher a framework python is not taken for --to's venv
        let bare = p(2, 100, &format!("{FRAMEWORK} {TO}/fleetmux/fleet.py daemon {RUN}/config.json"));
        assert!(check_interpreter(&nodes, &[bare, applet], &apps, Path::new(TO), 400).is_err());
    }

    fn node(key: &str, run: &str, locked: bool, python: &str) -> Node {
        Node { key: key.into(), run: PathBuf::from(run), locked, python: Some(python.into()) }
    }

    fn daemon(pid: i32, rel: &str, run: &str) -> Proc {
        p(pid, 100, &format!("{rel}/.venv/bin/python3.14 {rel}/fleetmux/fleet.py daemon {run}/config.json --resume"))
    }

    #[test]
    fn under_matches_whole_components_and_both_tmp_spellings() {
        assert!(under("/private/tmp/fmxu-ab12cd/rel/2222/.venv/bin/python", Path::new(TO)));
        assert!(under("/tmp/fmxu-ab12cd/rel/2222/fleetmux/fleet.py", Path::new(TO)));
        assert!(under(TO, Path::new("/tmp/fmxu-ab12cd/rel/2222")));
        assert!(!under("/private/tmp/fmxu-ab12cd/rel/22220/x", Path::new(TO)));
        assert!(!under("/private/tmp/fmxu-ab12cd/rel/1111/.venv/bin/python", Path::new(TO)));
    }

    #[test]
    fn ps_lines_give_pid_start_and_argv() {
        let text = "  101     01:05 /bin/sleep 600\n 202 2-03:04:05 python x.py daemon c.json\n 303 00:07 a\n";
        let ps = parse_ps(text, 1_000_000);
        assert_eq!(ps.len(), 3);
        assert_eq!(ps[0], p(101, 1_000_000 - 65, "/bin/sleep 600"));
        assert_eq!(ps[1].started, 1_000_000 - (2 * 86400 + 3 * 3600 + 4 * 60 + 5));
        assert_eq!(ps[1].argv, vec!["python", "x.py", "daemon", "c.json"]);
        assert_eq!(ps[2].pid, 303);
    }

    #[test]
    fn daemons_of_a_run_skip_the_keep_awake_wrapper_and_other_runs() {
        let procs = vec![
            p(1, 1, &format!("caffeinate -i {TO}/.venv/bin/python {TO}/fleetmux/fleet.py daemon {RUN}/config.json")),
            daemon(2, TO, RUN),
            daemon(3, TO, &format!("{RUN}/sub/ops")),
            p(4, 1, &format!("{TO}/.venv/bin/python {TO}/fleetmux/fleet.py actor {RUN}/config.json worker-1")),
        ];
        let d: Vec<i32> = daemons(&procs, Path::new(RUN)).iter().map(|p| p.pid).collect();
        assert_eq!(d, vec![2]);
        let d: Vec<i32> = daemons(&procs, &Path::new(RUN).join("sub/ops")).iter().map(|p| p.pid).collect();
        assert_eq!(d, vec![3]);
    }

    #[test]
    fn daemons_must_hold_their_lock_and_run_the_to_release() {
        let ops = format!("{RUN}/sub/ops");
        let nodes = vec![node("root", RUN, true, ""), node("ops", &ops, true, "")];
        let good = vec![daemon(2, TO, RUN), daemon(3, TO, &ops)];
        let ok = check_daemons(&nodes, &good, Path::new(TO)).unwrap();
        assert!(ok.contains("root") && ok.contains("ops"), "{ok}");
        let e = check_daemons(&nodes, &[daemon(2, TO, RUN), daemon(3, FROM, &ops)], Path::new(TO)).unwrap_err();
        assert!(e.contains("ops") && e.contains("1111"), "{e}");
        let unlocked = vec![node("root", RUN, true, ""), node("ops", &ops, false, "")];
        let e = check_daemons(&unlocked, &good, Path::new(TO)).unwrap_err();
        assert!(e.contains("ops") && e.contains("lock"), "{e}");
        let e = check_daemons(&nodes, &[daemon(2, TO, RUN)], Path::new(TO)).unwrap_err();
        assert!(e.contains("ops") && e.contains("no daemon process"), "{e}");
    }

    /// Words that cite a planning decision (its keyword, or a letter-number id such as the N, D and M series)
    /// instead of naming a behaviour: none may appear in what a step prints, since that text reaches release
    /// reports and notices. The words are built at run time so that this file does not carry them either.
    fn internal_ids(text: &str) -> Vec<String> {
        let keyword = ["rul", "ing"].concat();
        let task = format!("M{}", 28);
        text.split(|c: char| !c.is_ascii_alphanumeric())
            .filter(|w| {
                let b = w.as_bytes();
                w.eq_ignore_ascii_case(&keyword)
                    || *w == task
                    || (b.len() == 2 && b[0] == b'N' && (b'1'..=b'5').contains(&b[1]))
                    || ((2..=3).contains(&b.len()) && b[0] == b'D' && b[1..].iter().all(u8::is_ascii_digit))
            })
            .map(str::to_string)
            .collect()
    }

    #[test]
    fn step_lines_name_behaviour_not_internal_ids() {
        let py_to = format!("{TO}/.venv/bin/python");
        let nodes = vec![node("root", RUN, true, &py_to)];
        let actor_old = p(6, 50, &format!("{FROM}/.venv/bin/python {FROM}/fleetmux/fleet.py actor {RUN}/config.json s"));
        let applet = p(7, 500, &format!("{TO}/.venv/bin/python /w/applet_tick.py"));
        let procs = vec![daemon(2, TO, RUN), actor_old, applet];
        let apps = vec![("applet-tick-1".to_string(), 7)];
        let held = serde_json::json!({"subfleet": "M0", "reason": "stale", "held_at": 110.0, "notices": 1, "heal_at": 120.0});
        let lines = [
            check_interpreter(&nodes, &procs, &apps, Path::new(TO), 400).unwrap(),
            check_supervisor(Some("held"), Some("held")).unwrap(),
            holds_line(&[held], 100.0),
            crate::scenario::CHECK_WINDOW_NA.to_string(),
        ];
        for l in &lines {
            assert!(internal_ids(l).is_empty(), "{:?} in {l}", internal_ids(l));
        }
        let sample = format!("a ({}{} N{}) and D{} / M{}", "rul", "ing", 2, 11, 28);
        assert_eq!(internal_ids(&sample).len(), 4, "{sample}");
    }

    #[test]
    fn the_interpreter_check_covers_configs_daemons_new_agents_and_applets() {
        let py_to = format!("{TO}/.venv/bin/python");
        let py_from = format!("{FROM}/.venv/bin/python");
        let nodes = vec![node("root", RUN, true, &py_to)];
        let actor_new = p(5, 500, &format!("{TO}/.venv/bin/python {TO}/fleetmux/fleet.py actor {RUN}/config.json w"));
        let actor_old = p(6, 50, &format!("{FROM}/.venv/bin/python {FROM}/fleetmux/fleet.py actor {RUN}/config.json s"));
        let applet = p(7, 500, &format!("{TO}/.venv/bin/python /w/applet_tick.py"));
        let procs = vec![daemon(2, TO, RUN), actor_new.clone(), actor_old.clone(), applet.clone()];
        let apps = vec![("applet-tick-1".to_string(), 7)];
        let ok = check_interpreter(&nodes, &procs, &apps, Path::new(TO), 400).unwrap();
        // an agent session opened before the upgrade is listed, not failed
        assert!(ok.contains("1 agent process(es) from before the upgrade"), "{ok}");
        // a config still on --from
        let bad = vec![node("root", RUN, true, &py_from)];
        let e = check_interpreter(&bad, &procs, &apps, Path::new(TO), 400).unwrap_err();
        assert!(e.contains("root config python"), "{e}");
        // an actor started after the upgrade began on --from
        let late_old = p(8, 450, &format!("{FROM}/.venv/bin/python {FROM}/fleetmux/fleet.py actor {RUN}/config.json w"));
        let e = check_interpreter(&nodes, &[daemon(2, TO, RUN), late_old, applet.clone()], &apps, Path::new(TO), 400)
            .unwrap_err();
        assert!(e.contains("actor") && e.contains("8"), "{e}");
        // a history process counts like an actor
        let hist = p(9, 450, &format!("{FROM}/.venv/bin/python {FROM}/fleetmux/fleet.py history {RUN}/config.json"));
        let e = check_interpreter(&nodes, &[daemon(2, TO, RUN), hist, applet.clone()], &apps, Path::new(TO), 400)
            .unwrap_err();
        assert!(e.contains("history"), "{e}");
        // an applet on the old interpreter, or one that is not running
        let old_applet = p(7, 500, &format!("{FROM}/.venv/bin/python /w/applet_tick.py"));
        let e = check_interpreter(&nodes, &[daemon(2, TO, RUN), old_applet], &apps, Path::new(TO), 400).unwrap_err();
        assert!(e.contains("applet applet-tick-1"), "{e}");
        let e = check_interpreter(&nodes, &[daemon(2, TO, RUN)], &apps, Path::new(TO), 400).unwrap_err();
        assert!(e.contains("applet applet-tick-1") && e.contains("not running"), "{e}");
        // a daemon on the old interpreter
        let d_old = p(2, 1, &format!("{FROM}/.venv/bin/python {TO}/fleetmux/fleet.py daemon {RUN}/config.json"));
        let e = check_interpreter(&nodes, &[d_old, applet], &apps, Path::new(TO), 400).unwrap_err();
        assert!(e.contains("daemon 2"), "{e}");
    }

    #[test]
    fn a_wrapper_is_checked_by_the_launch_command_it_carries() {
        // `bash -lc "<python> <fleet.py> actor ..."` and `tmux new-session ... <python> <fleet.py> actor ...` carry an
        // actor's launch command; its interpreter is the word before fleet.py, not the wrapper's argv[0]
        let nodes = vec![node("root", RUN, true, &format!("{TO}/.venv/bin/python"))];
        let mut applet = p(7, 500, &format!("{FRAMEWORK} /w/applet_tick.py"));
        applet.launcher = Some(format!("{TO}/.venv/bin/python"));
        let apps = vec![("applet-tick-1".to_string(), 7)];
        let mut d = p(2, 100, &format!("{FRAMEWORK} {TO}/fleetmux/fleet.py daemon {RUN}/config.json"));
        d.launcher = Some(format!("{TO}/.venv/bin/python3.14"));
        let bash_to = p(11, 500, &format!("bash -lc {TO}/.venv/bin/python {TO}/fleetmux/fleet.py actor {RUN}/config.json w"));
        let tmux_to = p(12, 500, &format!("/opt/homebrew/bin/tmux -S /s new-session -d {TO}/.venv/bin/python {TO}/fleetmux/fleet.py actor {RUN}/config.json s"));
        let ok = check_interpreter(&nodes, &[d.clone(), applet.clone(), bash_to, tmux_to], &apps, Path::new(TO), 400)
            .unwrap();
        assert!(ok.contains("on "), "{ok}");
        let bash_from = p(13, 500, &format!("bash -lc {FROM}/.venv/bin/python {FROM}/fleetmux/fleet.py actor {RUN}/config.json w"));
        let e = check_interpreter(&nodes, &[d, applet, bash_from], &apps, Path::new(TO), 400).unwrap_err();
        assert!(e.contains("actor 13") && e.contains("1111"), "{e}");
        // a wrapper is never taken for a daemon
        let wrapped = p(14, 1, &format!("bash -lc {TO}/.venv/bin/python {TO}/fleetmux/fleet.py daemon {RUN}/config.json"));
        assert!(daemons(&[wrapped], Path::new(RUN)).is_empty());
    }

    fn rows(v: &[(&str, &str)]) -> Vec<(String, String)> {
        v.iter().map(|(a, b)| (a.to_string(), b.to_string())).collect()
    }

    #[test]
    fn jobs_come_back_with_the_same_ids_and_states() {
        let before = rows(&[("job-a", "running"), ("job-b", "queued")]);
        let none = BTreeMap::new();
        let ok = check_jobs(&before, &rows(&[("job-a", "running"), ("job-b", "queued")]), &none).unwrap();
        assert!(ok.contains("job-a running") && ok.contains("job-b queued"), "{ok}");
        // the queued job may have started meanwhile
        assert!(check_jobs(&before, &rows(&[("job-a", "running"), ("job-b", "running")]), &none).is_ok());
        // still on its way back: not yet
        let e = check_jobs(&before, &rows(&[("job-a", "suspended"), ("job-b", "queued")]), &none).unwrap_err();
        assert!(e.contains("job-a") && e.contains("suspended"), "{e}");
        // lost
        let e = check_jobs(&before, &rows(&[("job-a", "running")]), &none).unwrap_err();
        assert!(e.contains("job-b") && e.contains("missing"), "{e}");
        // published failed (e.g. not_carried / cancelled)
        let mut failed = BTreeMap::new();
        failed.insert("job-a".to_string(), "failed".to_string());
        let e = check_jobs(&before, &rows(&[("job-b", "queued")]), &failed).unwrap_err();
        assert!(e.contains("job-a") && e.contains("failed"), "{e}");
        // a running job must not fall back to queued
        let e = check_jobs(&before, &rows(&[("job-a", "queued"), ("job-b", "queued")]), &none).unwrap_err();
        assert!(e.contains("job-a") && e.contains("queued"), "{e}");
        // nothing open before: nothing to check
        assert!(check_jobs(&[], &[], &none).is_ok());
    }

    #[test]
    fn the_supervisor_check_is_not_worse_than_before() {
        assert!(check_supervisor(Some("idle"), Some("busy")).is_ok());
        assert!(check_supervisor(Some("idle"), Some("held")).unwrap_err().contains("held"));
        let ok = check_supervisor(Some("held"), Some("held")).unwrap();
        assert!(ok.contains("before the upgrade"), "{ok}");
        assert!(check_supervisor(Some("idle"), None).unwrap_err().contains("no supervisor"));
    }

    #[test]
    fn the_applet_must_be_a_new_process() {
        let list = |state: &str, pid: &str| json!({"status": "ok", "applets": [{"name": "a1", "state": state, "pid": pid}]});
        assert!(check_applet(100, &list("up", "200"), "a1").unwrap().contains("pid 200"));
        assert!(check_applet(100, &list("up", "100"), "a1").unwrap_err().contains("copy from before"));
        assert!(check_applet(100, &list("down", "0"), "a1").is_err());
        assert!(check_applet(100, &json!({"applets": []}), "a1").unwrap_err().contains("not listed"));
    }

    #[test]
    fn steps_go_on_without_repeating_one() {
        let s = step_numbers("scenario: start\nscenario: step 1\nscenario: step 2\nother\nscenario: step 3\n");
        assert_eq!(s, vec![1, 2, 3]);
        assert!(check_steps(&[1, 2, 3], 2).unwrap().contains("3"));
        assert!(check_steps(&[1, 2], 2).unwrap_err().contains("no new step"));
        assert!(check_steps(&[1, 2, 2, 3], 2).unwrap_err().contains("step 2 twice"));
        assert!(check_steps(&[1, 3, 4], 2).unwrap_err().contains("step 2 is missing"));
        assert!(check_steps(&[], 0).unwrap_err().contains("no new step"));
    }
    #[test]
    fn a_step_committed_before_the_upgrade_ended_is_not_progress() {
        // s2run5: step 2 was committed between progress_before (1 step) and the stop; job A never moved on --to
        let e = check_progress(&[1, 2], 1, 2).unwrap_err();
        assert!(e.contains("no new step since the upgrade ended"), "{e}");
        assert!(check_progress(&[1, 2, 3], 1, 2).unwrap().contains("1 new since the upgrade ended"));
        assert!(check_progress(&[1, 2], 1, 1).is_ok());
        assert!(check_progress(&[1, 2, 2, 3], 1, 2).unwrap_err().contains("twice"));
        assert!(check_progress(&[1, 3, 4], 1, 2).unwrap_err().contains("missing"));
    }

    #[test]
    fn held_sub_fleet_supervisors_and_the_heal_are_read_from_the_actor_log() {
        let held = "[sub-fleet supervisor held] sub-fleet M0 (/w/runs/r/sub/M0): its supervisor supervisor-1 is held: \
held (user_script:49: stale_identity:generation script: 600bef, on @user_script:49.; execution stop unconfirmed) \
with no progress, and the program could not prove its turn stopped.\nTo fix it, add this to your next decision \
(copy it as it is): \"heal\": [{\"subfleet\": \"M0\", \"reopen\": \"stay\"}]";
        let stuck = "[sub-fleet stuck] sub-fleet M0 (/w/runs/r/sub/M0): its supervisor is held (seen for 604 s).";
        let line = |v: serde_json::Value| v.to_string();
        let log = [
            line(json!({"event": "turn.start", "at": 50.0, "role": "supervisor", "kind": "block", "text": held})),
            line(json!({"event": "turn.start", "at": 100.0, "role": "worker-1", "kind": "block", "text": held})),
            line(json!({"event": "turn.start", "at": 400.0, "role": "supervisor", "kind": "block", "text": held})),
            line(json!({"event": "turn.reply", "at": 402.0, "role": "supervisor",
                        "reply": "{\"status\": \"RUNNING\", \"dispatch\": [], \"skip\": [], \"heal\": [{\"subfleet\": \"M0\", \"reopen\": \"stay\"}]}"})),
            "not json".to_string(),
            line(json!({"event": "turn.start", "at": 760.0, "role": "supervisor", "kind": "block", "text": stuck})),
        ]
        .join("\n");
        // before the upgrade (at 50) and other roles do not count
        let h = supervisor_holds(&log, 300.0);
        assert_eq!(h.len(), 1, "{h:?}");
        assert_eq!(h[0]["subfleet"], "M0");
        assert!(h[0]["reason"].as_str().unwrap().contains("stale_identity:generation"), "{h:?}");
        assert_eq!(h[0]["held_at"], 400.0);
        assert_eq!(h[0]["heal_at"], 402.0);
        assert_eq!(h[0]["notices"], 2);
        let text = holds_line(&h, 300.0);
        assert!(text.contains("M0 held 100 s after the upgrade began") && text.contains("heal sent 2 s later"), "{text}");
        assert!(text.contains("not a failure"), "{text}");
        assert!(supervisor_holds("", 0.0).is_empty());
        assert!(holds_line(&[], 0.0).starts_with("ok no sub-fleet supervisor was reported held"), "{}", holds_line(&[], 0.0));
    }

    #[test]
    fn a_released_job_must_end_ok() {
        // s2run7: job A ended blocked after its release, and progress_after passed anyway
        let blocked = json!({"status": "blocked", "result": {"reason": "supervisor claimed completion three times \
without passing verification: no artifacts were declared, nothing to verify"}});
        let e = job_result(Some(&blocked), "job-a1").unwrap_err();
        assert!(e.contains("job-a1 ended blocked") && e.contains("no artifacts were declared"), "{e}");
        assert!(job_result(None, "job-a1").unwrap_err().contains("no result published yet"));
        assert_eq!(job_result(Some(&json!({"status": "ok"})), "job-a1").unwrap(), "job-a1 ended ok");
    }

}
