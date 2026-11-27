//! The fleet tree the scenario builds on the `--from` release, and the checks of its state.
//!
//! Tree: a service root (`--lifecycle service`) with two root workers (`worker-1` for the jobs, `ops-1` for the
//! named sub-fleet's supervisor), a named autostart sub-fleet `ops` with one worker, one applet
//! `applet-tick-1` (the SDK's own loop, a heartbeat every 2 s), and `limits.job_hot_add_max: 0`, so with
//! `worker-1` taken by the first job the second job stays queued. The agents are the scripted actor
//! (`fleetmux/scenario_actor.py`, on top of `fleetmux/demo_actor.py`); a job's worker commits one step per turn in
//! the job's own git repository. Everything is read through the product's public outputs: `status` (JSON) and
//! `applet list` (JSON), plus `git rev-list` in the job repository.
use serde_json::Value;

pub const SUBFLEET: &str = "ops";
pub const APPLET: &str = "applet-tick-1";
/// Marks a scenario job goal; the scripted actor answers such a job (demo_actor's own mark is not used).
pub const JOB_MARK: &str = "[fleetmux scenario job]";

/// The root roster (YAML; JSON flow style is valid YAML). Paths are absolute.
pub fn root_roster(work: &str, applet_py: &str) -> String {
    format!(
        "# fleetmux scenario upgrade: root roster\n\
         workers:\n  - name: worker-1\n  - name: ops-1\n\
         limits: {{job_hot_add_max: 0}}\n\
         subfleets:\n  - {{name: {SUBFLEET}, supervisor_slot: ops-1, supervisor_role: \"{work}/ops-supervisor.md\", \
         roster: \"{work}/ops.yaml\", autostart: true}}\n\
         applets:\n  - {{name: {APPLET}, command: [\"{{python}}\", \"{applet_py}\"], cwd: \"{work}\", heartbeat_s: 2, \
         stale_after_s: 20}}\n"
    )
}

/// The named sub-fleet's roster.
pub const OPS_ROSTER: &str = "# fleetmux scenario upgrade: the named sub-fleet\nworkers:\n  - name: opsw-1\n";

/// The named sub-fleet's supervisor role.
pub const OPS_ROLE: &str = "# Supervisor (service mode): scenario sub-fleet `ops`\n\n\
Scripted for `fleetmux scenario upgrade`: there is no work; answer every turn with the service decision \
{\"status\": \"RUNNING\", \"dispatch\": [], \"skip\": []}.\n";

/// The applet: the SDK's main loop, nothing else (each round is a heartbeat).
pub const APPLET_PY: &str = "\"\"\"fleetmux scenario upgrade: an applet that only keeps its heartbeat.\"\"\"\n\
import sys\n\nfrom fleetmux.applet import Applet\n\n\
sys.exit(Applet.from_env().run(lambda a: 1) or 0)\n";

/// The job spec (`fleetmux task --config <root> -`): one worker, the repository named in the goal.
pub fn job_spec(name: &str, repo: &str) -> String {
    serde_json::json!({
        "goal": format!("{JOB_MARK} {name}: commit one step per turn in repo={repo} until {repo}/SCENARIO-RELEASE exists."),
        "hours": 2,
        "workers": 1,
        "name": format!("scenario-{name}"),
    })
    .to_string()
}

/// The jobs of a `status` JSON, as `(id, state, goal)`.
pub fn jobs(status: &Value) -> Vec<(String, String, String)> {
    let s = |v: &Value, k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    status
        .get("jobs")
        .and_then(Value::as_array)
        .map(|a| a.iter().map(|j| (s(j, "id"), s(j, "state"), s(j, "goal"))).collect())
        .unwrap_or_default()
}

/// Is the named sub-fleet up: `subfleets.<name>.state == running` and its daemon alive.
pub fn subfleet_up(status: &Value, name: &str) -> Result<String, String> {
    let sf = status.get("subfleets").and_then(|s| s.get(name));
    let Some(sf) = sf else {
        let waiting = status.get("subfleet_waiting").map(|w| w.to_string()).unwrap_or_default();
        return Err(format!("sub-fleet {name} is not in status.subfleets yet {waiting}"));
    };
    let state = sf.get("state").and_then(Value::as_str).unwrap_or("");
    let alive = sf.get("daemon_alive").and_then(Value::as_bool).unwrap_or(false);
    if state == "running" && alive {
        Ok(format!("sub-fleet {name} running, daemon alive"))
    } else {
        Err(format!("sub-fleet {name}: state {state:?}, daemon_alive {alive}"))
    }
}

/// Is the applet up (`applet list` JSON: a list, or `{"applets": [...]}`): state `up` and a pid.
pub fn applet_up(list: &Value, name: &str) -> Result<i64, String> {
    let rows = list
        .as_array()
        .or_else(|| list.get("applets").and_then(Value::as_array))
        .cloned()
        .unwrap_or_default();
    let Some(row) = rows.iter().find(|r| r.get("name").and_then(Value::as_str) == Some(name)) else {
        return Err(format!("applet {name} is not listed"));
    };
    let state = row.get("state").and_then(Value::as_str).unwrap_or("");
    let pid = row
        .get("pid")
        .and_then(|p| p.as_i64().or_else(|| p.as_str().and_then(|s| s.parse().ok())))
        .unwrap_or(0);
    if state == "up" && pid > 1 {
        Ok(pid)
    } else {
        Err(format!("applet {name}: state {state:?}, pid {pid}"))
    }
}

/// The state the scenario needs before the upgrade: the sub-fleet and the applet up, the job whose goal names
/// `repo_a` running with at least one committed step, the job naming `repo_b` queued. `Ok` is a one-line summary.
pub fn assess(
    status: &Value,
    applets: &Value,
    repo_a: &str,
    repo_b: &str,
    steps_a: u32,
) -> Result<String, String> {
    let mut ok = Vec::new();
    let mut bad = Vec::new();
    match subfleet_up(status, SUBFLEET) {
        Ok(m) => ok.push(m),
        Err(e) => bad.push(e),
    }
    match applet_up(applets, APPLET) {
        Ok(pid) => ok.push(format!("applet {APPLET} up, pid {pid}")),
        Err(e) => bad.push(e),
    }
    let rows = jobs(status);
    let find = |repo: &str| {
        let token = format!("repo={repo} ");
        rows.iter().find(|(_, _, goal)| goal.contains(&token)).cloned()
    };
    match find(repo_a) {
        None => bad.push(format!("no job for repo {repo_a}")),
        Some((id, state, _)) if state != "running" => bad.push(format!("{id} is {state:?}, not running")),
        Some((id, _, _)) if steps_a == 0 => bad.push(format!("{id} runs but has no step committed in {repo_a} yet")),
        Some((id, _, _)) => ok.push(format!("{id} running with {steps_a} committed step(s)")),
    }
    match find(repo_b) {
        None => bad.push(format!("no job for repo {repo_b}")),
        Some((id, state, _)) if state != "queued" => bad.push(format!("{id} is {state:?}, not queued")),
        Some((id, _, _)) => ok.push(format!("{id} queued")),
    }
    if bad.is_empty() {
        Ok(ok.join("; "))
    } else {
        Err(bad.join("; "))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn status(sub_state: &str, alive: bool, a: &str, b: &str) -> Value {
        json!({
            "subfleets": {"ops": {"state": sub_state, "daemon_alive": alive, "named": true}},
            "jobs": [
                {"id": "job-aaaa", "state": a, "goal": "[fleetmux scenario job] a: commit one step per turn in repo=/w/job-a until /w/job-a/SCENARIO-RELEASE exists."},
                {"id": "job-bbbb", "state": b, "goal": "[fleetmux scenario job] b: commit one step per turn in repo=/w/job-b until /w/job-b/SCENARIO-RELEASE exists."}
            ]
        })
    }

    fn applets(state: &str, pid: i64) -> Value {
        json!([{"name": "applet-tick-1", "state": state, "pid": pid}])
    }

    #[test]
    fn the_expected_tree_passes() {
        let ok = assess(&status("running", true, "running", "queued"), &applets("up", 4242), "/w/job-a", "/w/job-b", 2)
            .unwrap();
        assert!(ok.contains("job-aaaa running with 2 committed step(s)"), "{ok}");
        assert!(ok.contains("job-bbbb queued"), "{ok}");
        assert!(ok.contains("pid 4242"), "{ok}");
        // `applet list` prints {"status": "ok", "applets": [...]} with the pid as a string (a Redis hash field)
        let listed = json!({"status": "ok", "applets": [{"name": "applet-tick-1", "state": "up", "pid": "77"}]});
        assert!(assess(&status("running", true, "running", "queued"), &listed, "/w/job-a", "/w/job-b", 1).is_ok());
    }

    #[test]
    fn each_missing_piece_is_named() {
        let e = assess(&status("starting", false, "running", "queued"), &applets("up", 1), "/w/job-a", "/w/job-b", 1)
            .unwrap_err();
        assert!(e.contains("sub-fleet ops") && e.contains("applet applet-tick-1"), "{e}");
        let e = assess(&status("running", true, "queued", "queued"), &applets("up", 9), "/w/job-a", "/w/job-b", 0)
            .unwrap_err();
        assert!(e.contains("job-aaaa") && e.contains("not running"), "{e}");
        let e = assess(&status("running", true, "running", "running"), &applets("up", 9), "/w/job-a", "/w/job-b", 3)
            .unwrap_err();
        assert!(e.contains("job-bbbb") && e.contains("not queued"), "{e}");
        let e = assess(&status("running", true, "running", "queued"), &applets("up", 9), "/w/job-a", "/w/job-b", 0)
            .unwrap_err();
        assert!(e.contains("no step"), "{e}");
        let e = assess(&json!({"jobs": []}), &json!({"applets": []}), "/w/job-a", "/w/job-b", 0).unwrap_err();
        assert!(e.contains("no job for repo /w/job-a") && e.contains("no job for repo /w/job-b"), "{e}");
    }

    #[test]
    fn a_repo_prefix_does_not_match_another_job() {
        // /w/job-a must not match the goal of /w/job-ab
        let st = json!({"subfleets": {"ops": {"state": "running", "daemon_alive": true}},
            "jobs": [{"id": "x", "state": "running", "goal": "repo=/w/job-ab until"}]});
        assert!(assess(&st, &applets("up", 9), "/w/job-a", "/w/job-b", 1).unwrap_err().contains("no job for repo /w/job-a"));
    }

    #[test]
    fn the_roster_declares_the_subfleet_the_applet_and_no_hot_add() {
        let r = root_roster("/w", "/w/applet_tick.py");
        assert!(r.contains("name: worker-1") && r.contains("name: ops-1"));
        assert!(r.contains("job_hot_add_max: 0"));
        assert!(r.contains("name: ops, supervisor_slot: ops-1"));
        assert!(r.contains("autostart: true"));
        assert!(r.contains("name: applet-tick-1, command: [\"{python}\", \"/w/applet_tick.py\"]"));
        let spec: Value = serde_json::from_str(&job_spec("a", "/w/job-a")).unwrap();
        assert_eq!(spec["workers"], 1);
        assert!(spec["goal"].as_str().unwrap().starts_with(JOB_MARK));
        assert!(spec["goal"].as_str().unwrap().contains("repo=/w/job-a "));
    }
}
