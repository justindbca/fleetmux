//! `fleetmux scenario upgrade --from <release> --to <release>`: the upgrade scenario. It builds a small fleet tree
//! on the `--from` release in an isolated world (own HOME, SHELL, TMUX_TMPDIR, private tmux, private Redis,
//! releases installed inside the world), with scripted agents in place of the model, then upgrades it with the
//! `--to` release's `fleetmux upgrade` and checks the result. When `--from` and `--to` resolve to one release
//! directory there is nothing to switch: the run passes after `prepare` without starting a tree.
//!
//! Output: one line per step, `scenario: <step>: ok|FAIL <detail>`, then one JSON line, always the last line of
//! stdout: `{"result": "pass"|"fail", "step", "reason", "from", "to", "duration_s", "report"}`. Exit codes: 0 pass,
//! 1 a step or check failed (timeout included), 2 usage error or incomplete environment (a missing program, an
//! unusable release; never a skip), 130 interrupted by a signal.
pub mod after;
pub mod args;
pub mod guard;
pub mod proc;
pub mod release;
pub mod steps;
pub mod tree;
pub mod world;

use std::collections::BTreeMap;
use std::os::unix::fs::PermissionsExt;
use std::panic::AssertUnwindSafe;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use guard::{Guard, Owner};
use release::Release;
use steps::{Failure, Steps};
use world::World;

/// Test seam: `<step>:<fail|panic|hang>` makes that step fail, panic or hang after it did its work (the clean-up
/// paths are tested with real processes this way).
pub const FAULT_ENV: &str = "FLEETMUX_SCENARIO_FAULT";
/// The reason of a run whose `--from` and `--to` are one release directory: it passes after `prepare`.
pub const SAME_RELEASE: &str = "from and to are the same release: nothing to switch";
/// `--stop-timeout` passed to `fleetmux upgrade`: the scenario's daemons stop in seconds; this bounds a stuck one.
pub const UPGRADE_STOP_TIMEOUT_S: u64 = 120;
/// The scenario's limit for the whole `fleetmux upgrade` call: the stop levels (3 x 120 s), verify (300 s) and a way
/// back to `--from` that repeats both, with room to spare.
pub const UPGRADE_LIMIT_S: u64 = 1800;
/// Seconds one scripted turn takes.
pub const TURN_S: &str = "2";
/// How long the main thread has to clean up itself after a signal or the deadline before the watcher does it.
const GRACE: Duration = Duration::from_secs(30);

static FINAL_PRINTED: AtomicBool = AtomicBool::new(false);

/// `fleetmux scenario ...`
pub fn run(argv: &[String]) -> ExitCode {
    match argv.first().map(String::as_str) {
        Some("upgrade") => ExitCode::from(upgrade(&argv[1..]) as u8),
        Some("-h" | "--help") => {
            println!("{}", args::USAGE);
            ExitCode::SUCCESS
        }
        _ => {
            eprintln!("fleetmux scenario: unknown scenario\n{}", args::USAGE);
            ExitCode::from(2)
        }
    }
}

/// The final line, printed once (the main thread and the watcher thread may both reach it).
fn final_line(v: &Value) {
    if !FINAL_PRINTED.swap(true, Ordering::SeqCst) {
        println!("{v}");
    }
}

#[derive(Debug, Default)]
struct Shared {
    step: String,
    from: String,
    to: String,
    report: String,
}

fn upgrade(argv: &[String]) -> i32 {
    let started = Instant::now();
    let a = match args::parse(argv) {
        Ok(a) => a,
        Err(msg) => {
            if msg.is_empty() {
                println!("{}", args::USAGE);
                return 0;
            }
            eprintln!("fleetmux scenario upgrade: {msg}\n{}", args::USAGE);
            final_line(&json!({"result": "fail", "step": "usage", "reason": msg, "from": "", "to": "",
                               "duration_s": 0, "report": ""}));
            return 2;
        }
    };
    let base: BTreeMap<String, String> = std::env::vars_os()
        .map(|(k, v)| (k.to_string_lossy().into_owned(), v.to_string_lossy().into_owned()))
        .collect();
    let guard = Guard::new();
    let owner = Owner(Arc::clone(&guard));
    guard::catch_signals();
    let deadline = started + Duration::from_secs(a.timeout_s);
    let shared = Arc::new(Mutex::new(Shared {
        step: steps::STEPS[0].into(),
        from: a.from.clone(),
        to: a.to.clone(),
        report: String::new(),
    }));
    {
        let shared = Arc::clone(&shared);
        let finish: Arc<dyn Fn(i32, String) + Send + Sync> = Arc::new(move |code, why| {
            let s = shared.lock().unwrap_or_else(|e| e.into_inner());
            println!("scenario: {}: FAIL {why}", s.step);
            final_line(&json!({"result": "fail", "step": s.step, "reason": why, "from": s.from, "to": s.to,
                               "duration_s": started.elapsed().as_secs(), "report": s.report}));
            std::process::exit(code);
        });
        guard::watch(Arc::clone(&guard), deadline, GRACE, finish);
    }
    let mut ctx = Ctx::new(a, base, Arc::clone(&guard), deadline, Arc::clone(&shared));
    let mut lines: Vec<String> = Vec::new();
    let res = std::panic::catch_unwind(AssertUnwindSafe(|| {
        steps::run_all(&mut ctx, &mut |l| {
            println!("{l}");
            lines.push(l.to_string());
        })
    }));
    let mut note = None;
    let failure = match res {
        Ok(o) => {
            note = o.note;
            o.failure
        }
        Err(p) => {
            let msg = p
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| p.downcast_ref::<&str>().map(|s| s.to_string()))
                .unwrap_or_else(|| "panic".into());
            let step = shared.lock().unwrap_or_else(|e| e.into_inner()).step.clone();
            let line = format!("scenario: {step}: FAIL panic: {msg}");
            println!("{line}");
            lines.push(line);
            let left = guard.cleanup();
            let line = if left.is_empty() {
                "scenario: cleanup: ok after the panic".to_string()
            } else {
                format!("scenario: cleanup: FAIL left running: {}", left.join("; "))
            };
            println!("{line}");
            lines.push(line);
            Some(Failure::new(&step, format!("panic: {msg}")))
        }
    };
    if !guard.cleaned() {
        guard.cleanup();
    }
    let code = match &failure {
        None => 0,
        Some(_) if guard::signalled().is_some() => 130,
        Some(f) if f.step == "prepare" && ctx.env_error => 2,
        Some(_) => 1,
    };
    let report = ctx.write_report(&lines, failure.as_ref(), started);
    shared.lock().unwrap_or_else(|e| e.into_inner()).report = report.clone();
    let (from, to) = ctx.labels();
    let (step, reason) = failure.map(|f| (f.step, f.reason)).unwrap_or_else(|| (String::new(), note.unwrap_or_default()));
    final_line(&json!({"result": if code == 0 {"pass"} else {"fail"}, "step": step, "reason": reason,
                       "from": from, "to": to, "duration_s": started.elapsed().as_secs(), "report": report}));
    drop(owner);
    code
}

/// The run's state across steps.
struct Ctx {
    args: args::Args,
    base: BTreeMap<String, String>,
    guard: Arc<Guard>,
    deadline: Instant,
    shared: Arc<Mutex<Shared>>,
    fault: Option<(String, String)>,
    /// A `prepare` failure caused by the environment (exit 2).
    env_error: bool,
    /// `--from` and `--to` resolved to one release directory: nothing to switch, the run passes after `prepare`.
    same_release: bool,
    tools: BTreeMap<String, PathBuf>,
    world: Option<World>,
    log: PathBuf,
    from: Option<Release>,
    to: Option<Release>,
    env: BTreeMap<String, String>,
    cfg: Option<PathBuf>,
    repos: Vec<PathBuf>,
    jobs: Vec<String>,
    observed: serde_json::Map<String, Value>,
    /// The environment of the `--to` CLI after the upgrade (`<to>/bin` first, `FLEETMUX_BIN` of `--to`).
    env_to: BTreeMap<String, String>,
    /// What the scenario saw just before the upgrade.
    before: Option<Before>,
    /// When the upgrade began (seconds since the epoch).
    upgrade_at: i64,
    /// Job A's committed steps when `fleetmux upgrade` returned: the baseline of `progress_after`.
    steps_a_at_upgrade_end: Option<u32>,
}

/// The tree just before the upgrade, as the scenario observed it.
#[derive(Debug, Clone, Default)]
struct Before {
    jobs: Vec<(String, String)>,
    supervisor: Option<String>,
    applet_pid: i64,
    steps_a: u32,
    /// The fleets running before: `(key, run dir)`, the root first.
    nodes: Vec<(String, PathBuf)>,
}

fn epoch() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Does a daemon hold `<run>/daemon.lock`? (the check `releases.daemon_running` makes: a non-blocking flock)
pub fn lock_held(run: &Path) -> bool {
    use std::os::unix::io::AsRawFd;
    let Ok(f) = std::fs::OpenOptions::new().create(true).append(true).open(run.join("daemon.lock")) else {
        return false;
    };
    let r = unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if r == 0 {
        unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_UN) };
        return false;
    }
    std::io::Error::last_os_error().raw_os_error() == Some(libc::EWOULDBLOCK)
}

type StepResult = Result<String, Failure>;

impl Ctx {
    fn new(
        args: args::Args,
        base: BTreeMap<String, String>,
        guard: Arc<Guard>,
        deadline: Instant,
        shared: Arc<Mutex<Shared>>,
    ) -> Ctx {
        let fault = base
            .get(FAULT_ENV)
            .and_then(|f| f.split_once(':'))
            .map(|(s, k)| (s.to_string(), k.to_string()));
        Ctx {
            args,
            base,
            guard,
            deadline,
            shared,
            fault,
            env_error: false,
            same_release: false,
            tools: BTreeMap::new(),
            world: None,
            log: std::env::temp_dir().join("fleetmux-scenario.log"),
            from: None,
            to: None,
            env: BTreeMap::new(),
            cfg: None,
            repos: Vec::new(),
            jobs: Vec::new(),
            observed: serde_json::Map::new(),
            env_to: BTreeMap::new(),
            before: None,
            upgrade_at: 0,
            steps_a_at_upgrade_end: None,
        }
    }

    fn stop(&self) -> Option<String> {
        guard::stop_reason(self.deadline, self.args.timeout_s)
    }

    fn world(&self) -> &World {
        self.world.as_ref().expect("the world exists after prepare")
    }

    fn labels(&self) -> (String, String) {
        (
            self.from.as_ref().map(Release::label).unwrap_or_else(|| self.args.from.clone()),
            self.to.as_ref().map(Release::label).unwrap_or_else(|| self.args.to.clone()),
        )
    }

    /// Run a command of the world (the fleet environment), bounded by `limit`.
    fn cmd(&self, argv: Vec<String>, stdin: Option<&str>, limit: Duration) -> Result<proc::Done, String> {
        let stop = || self.stop();
        proc::run(&self.guard, &argv, &self.env, &self.world().root, stdin, limit, &self.log, &stop)
    }

    /// `<from>/bin/fleetmux <args>`: the `--from` release's own CLI (the real binary forwards to its Python CLI).
    fn fleet(&self, args: &[&str], stdin: Option<&str>, limit: Duration) -> Result<proc::Done, String> {
        let from = self.from.as_ref().expect("from is resolved in prepare");
        let mut argv = vec![from.fleetmux().display().to_string()];
        argv.extend(args.iter().map(|s| s.to_string()));
        self.cmd(argv, stdin, limit)
    }

    /// A JSON document from a fleet command's stdout (the whole of it, or its last JSON line).
    fn fleet_json(&self, args: &[&str]) -> Result<Value, String> {
        let done = self.fleet(args, None, Duration::from_secs(60))?;
        parse_json(&done.stdout).ok_or_else(|| {
            format!("`fleetmux {}` gave no JSON (exit {:?}): {}", args.join(" "), done.code, done.tail(3))
        })
    }

    fn cfg_str(&self) -> String {
        self.cfg.as_ref().map(|p| p.display().to_string()).unwrap_or_default()
    }

    fn status(&self) -> Result<Value, String> {
        let cfg = self.cfg_str();
        self.fleet_json(&["status", "--config", &cfg])
    }

    /// Poll `f` once a second until it returns `Ok`, `limit` passes or the run has to stop.
    fn poll<T>(&self, limit: Duration, mut f: impl FnMut(&Ctx) -> Result<T, String>) -> Result<T, String> {
        let until = Instant::now() + limit;
        loop {
            let last = match f(self) {
                Ok(v) => return Ok(v),
                Err(e) => e,
            };
            if let Some(why) = self.stop() {
                return Err(format!("{why}; last state: {last}"));
            }
            if Instant::now() >= until {
                return Err(format!("not reached within {} s; last state: {last}", limit.as_secs()));
            }
            std::thread::sleep(Duration::from_secs(1));
        }
    }

    fn committed_steps(&self, repo: &Path) -> u32 {
        let git = self.tools.get("git").map(|p| p.display().to_string()).unwrap_or("git".into());
        let done = self.cmd(
            vec![git, "-C".into(), repo.display().to_string(), "rev-list".into(), "--count".into(), "HEAD".into()],
            None,
            Duration::from_secs(30),
        );
        done.ok()
            .and_then(|d| d.stdout.trim().parse::<u32>().ok())
            .map(|n| n.saturating_sub(1)) // the scenario's own first commit is not a step
            .unwrap_or(0)
    }

    // ------------------------------------------------------------------------------------------------- steps
    fn prepare(&mut self) -> StepResult {
        let f = |r: String| Failure::new("prepare", r);
        let redis_dir = self.base.get(world::REDIS_BIN_ENV).cloned().unwrap_or(world::REDIS_BIN_DIR.into());
        self.tools = world::find_tools(self.base.get("PATH").map(String::as_str), &redis_dir).map_err(|e| {
            self.env_error = true;
            f(e)
        })?;
        let w = World::create().map_err(|e| {
            self.env_error = true;
            f(format!("cannot create the world directory: {e}"))
        })?;
        self.guard.own_root(&w.root);
        if !self.args.keep {
            self.guard.remove_when_clean(&w.root);
        }
        self.guard.set_tools(self.tools.get("tmux").map(PathBuf::as_path), None);
        self.log = w.root.join("scenario.log");
        self.world = Some(w);
        // The installs (a git reference) use the caller's HOME (build caches, the Python finder) without the
        // caller's fleet: the scrubbed environment, with the Redis directory first on PATH.
        let mut env: BTreeMap<String, String> = self
            .base
            .iter()
            .filter(|(k, _)| !world::SCRUB.contains(&k.as_str()) && !world::SCRUB_PREFIX.iter().any(|p| k.starts_with(p)))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        if let Some(t) = self.base.get(release::CARGO_TARGET_ENV) {
            env.insert(release::CARGO_TARGET_ENV.into(), t.clone());
        }
        let path = self.base.get("PATH").cloned().unwrap_or_default();
        env.insert("PATH".into(), format!("{redis_dir}:{path}"));
        let repo = self.args.repo.clone().or_else(release::default_repo).unwrap_or_else(|| PathBuf::from("."));
        let w = self.world().clone();
        let stop = || self.stop();
        let inst = release::Installer {
            guard: &self.guard,
            git: &self.tools["git"],
            repo: &repo,
            work: &w.work,
            releases: &w.releases,
            env: &env,
            log: &self.log,
            stop: &stop,
        };
        let from = release::resolve(&self.args.from, &inst);
        let to = from.as_ref().ok().map(|_| release::resolve(&self.args.to, &inst));
        let from = from.map_err(|e| f(format!("--from: {e}")));
        let to = match to {
            Some(t) => t.map_err(|e| f(format!("--to: {e}"))),
            None => Err(f(String::new())),
        };
        if self.stop().is_some() {
            return Err(f(self.stop().unwrap_or_default()));
        }
        let from = from.inspect_err(|_| self.env_error = true)?;
        let to = to.inspect_err(|_| self.env_error = true)?;
        for part in ["fleetmux/demo_actor.py", "fleetmux/scenario_actor.py"] {
            if !to.dir.join(part).is_file() {
                self.env_error = true;
                return Err(f(format!(
                    "the --to release {} has no {part} (the scripted agents come from --to)",
                    to.dir.display()
                )));
            }
        }
        self.guard.set_tools(None, Some(&from.fleetmux()));
        let detail = format!(
            "tools ok; from {} at {}; to {} at {}; world {}",
            from.label(),
            from.dir.display(),
            to.label(),
            to.dir.display(),
            w.root.display()
        );
        let mut s = self.shared.lock().unwrap_or_else(|e| e.into_inner());
        s.from = from.label();
        s.to = to.label();
        drop(s);
        // rollout runs the scenario once per release that runs on the machine; a reinstall of the same release,
        // or a retry after a way back, gives the same directory twice: no tree is started for it
        self.same_release = from.dir == to.dir;
        if self.same_release {
            self.observed.insert("same_release".into(), json!(true));
        }
        let detail = if self.same_release { format!("{detail}; {SAME_RELEASE}") } else { detail };
        self.from = Some(from);
        self.to = Some(to);
        Ok(detail)
    }

    fn world_step(&mut self) -> StepResult {
        let f = |r: String| Failure::new("world", r);
        let io = |e: std::io::Error| Failure::new("world", e.to_string());
        let w = self.world().clone();
        let from = self.from.clone().expect("from");
        let to = self.to.clone().expect("to");
        w.write_tmux_wrapper(&self.tools["tmux"]).map_err(io)?;
        // the scripted actor (copied into the world, so every agent process names the world on its command line)
        let actor = w.work.join("actor");
        std::fs::create_dir(&actor).map_err(io)?;
        for name in ["demo_actor.py", "scenario_actor.py"] {
            std::fs::copy(to.dir.join("fleetmux").join(name), actor.join(name)).map_err(io)?;
        }
        let live = live_marker(&w.bin.join("live"));
        let live_s = live.as_ref().map(|l| l.display().to_string());
        let log_s = w.root.join("actor.jsonl").display().to_string();
        let mut settings = vec![("FLEETMUX_DEMO_ACTOR_LOG", log_s.as_str()), ("FLEETMUX_DEMO_TURN_S", TURN_S)];
        if let Some(l) = &live_s {
            settings.push(("FLEETMUX_DEMO_LIVE", l.as_str()));
        }
        let launcher = w.bin.join("claude");
        std::fs::write(&launcher, launcher_script(&to.python(), &actor.join("scenario_actor.py"), &settings))
            .map_err(io)?;
        std::fs::set_permissions(&launcher, std::fs::Permissions::from_mode(0o700)).map_err(io)?;
        // the named sub-fleet, the applet, the root roster
        std::fs::write(w.work.join("ops.yaml"), tree::OPS_ROSTER).map_err(io)?;
        std::fs::write(w.work.join("ops-supervisor.md"), tree::OPS_ROLE).map_err(io)?;
        let applet = w.work.join("applet_tick.py");
        std::fs::write(&applet, tree::APPLET_PY).map_err(io)?;
        let work = w.work.display().to_string();
        std::fs::write(w.work.join("roster.yaml"), tree::root_roster(&work, &applet.display().to_string()))
            .map_err(io)?;
        // the private fleet.yaml: both model chains pinned to the run's default model, no automatic release switch
        let private = private_fleet_yaml(&from.dir);
        std::fs::write(w.work.join("fleet.yaml"), &private).map_err(io)?;
        let dirs: Vec<PathBuf> = self.tools.values().filter_map(|p| p.parent().map(Path::to_path_buf)).collect();
        let mut env = w.env(&self.base, &from.bin_dir(), &dirs);
        env.insert("FLEETMUX_CONFIG".into(), w.work.join("fleet.yaml").display().to_string());
        env.insert("FLEETMUX_DEMO_ACTOR_LOG".into(), w.root.join("actor.jsonl").display().to_string());
        env.insert("FLEETMUX_DEMO_TURN_S".into(), TURN_S.into());
        if let Some(l) = &live {
            env.insert("FLEETMUX_DEMO_LIVE".into(), l.display().to_string());
        }
        let mut env_to = w.env(&self.base, &to.bin_dir(), &dirs);
        for k in ["FLEETMUX_CONFIG", "FLEETMUX_DEMO_ACTOR_LOG", "FLEETMUX_DEMO_TURN_S", "FLEETMUX_DEMO_LIVE"] {
            if let Some(v) = env.get(k) {
                env_to.insert(k.into(), v.clone());
            }
        }
        self.env = env;
        self.env_to = env_to;
        // one git repository per job: the running job's worker commits one step per turn in it
        let git = self.tools["git"].display().to_string();
        self.repos.clear();
        for name in ["job-a", "job-b"] {
            let repo = w.work.join(name);
            std::fs::create_dir(&repo).map_err(io)?;
            std::fs::write(repo.join("README"), format!("fleetmux scenario upgrade: {name}\n")).map_err(io)?;
            let r = repo.display().to_string();
            for args in [
                vec!["init", "-q", "-b", "main"],
                vec!["add", "README"],
                vec!["-c", "user.name=fleetmux scenario", "-c", "user.email=scenario@fleetmux.invalid", "commit", "-q", "-m", "scenario: start"],
            ] {
                let mut argv = vec![git.clone(), "-C".into(), r.clone()];
                argv.extend(args.iter().map(|s| s.to_string()));
                let d = self.cmd(argv, None, Duration::from_secs(30)).map_err(f)?;
                if !d.ok() {
                    return Err(f(format!("git in {r}: {}", d.tail(2))));
                }
            }
            self.repos.push(repo);
        }
        Ok(format!(
            "HOME/SHELL/TMUX_TMPDIR/FLEETMUX_* in {}; tmux -f /dev/null; actor from {}; live marker {}",
            w.root.display(),
            to.dir.display(),
            if live.is_some() { "on" } else { "off" }
        ))
    }

    fn start_from(&mut self) -> StepResult {
        let f = |r: String| Failure::new("start_from", r);
        let w = self.world().clone();
        let s = |p: &Path| p.display().to_string();
        let roster = s(&w.work.join("roster.yaml"));
        let claude = s(&w.bin.join("claude"));
        let redis = s(&self.tools["redis-server"]);
        let (rr, fh, st, runs) = (s(&w.redis_root), s(&w.fleet_home), s(&w.state), s(&w.runs));
        let done = self
            .fleet(
                &[
                    "start", "--allow-development-v2", "--lifecycle", "service", "--mode", "service", "--backend",
                    "fleetmux", "--no-view", "--no-join", "--roster", &roster, "--supervisor-cli", "claude",
                    "--worker-cli", "claude", "--claude", &claude, "--redis-server", &redis, "--redis-runtime-dir",
                    &rr, "--fleet-home", &fh, "--cmux-state-dir", &st, "--runs-dir", &runs,
                ],
                None,
                Duration::from_secs(300),
            )
            .map_err(f)?;
        self.guard.scan();
        if !done.ok() {
            return Err(f(format!("start exited {:?}: {}", done.code, done.tail(4))));
        }
        let latest: Value = std::fs::read_to_string(w.fleet_home.join("latest.json"))
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .ok_or_else(|| f("start wrote no latest.json".into()))?;
        let cfg = latest
            .get("config")
            .and_then(Value::as_str)
            .ok_or_else(|| f("latest.json names no config".into()))?;
        self.cfg = Some(PathBuf::from(cfg));
        let status = self.status().map_err(f)?;
        self.observed.insert("status_after_start".into(), status);
        Ok(format!("root fleet started on --from: {cfg}"))
    }

    fn subfleet_up(&mut self) -> StepResult {
        let r = self.poll(Duration::from_secs(300), |c| tree::subfleet_up(&c.status()?, tree::SUBFLEET));
        self.guard.scan();
        r.map_err(|e| Failure::new("subfleet_up", e))
    }

    fn applet_up(&mut self) -> StepResult {
        let cfg = self.cfg_str();
        let pid = self
            .poll(Duration::from_secs(180), |c| tree::applet_up(&c.fleet_json(&["applet", "list", "--config", &cfg])?, tree::APPLET))
            .map_err(|e| Failure::new("applet_up", e))?;
        self.guard.own_pid(pid as i32);
        Ok(format!("applet {} up, pid {pid}", tree::APPLET))
    }

    fn submit_jobs(&mut self) -> StepResult {
        let f = |r: String| Failure::new("submit_jobs", r);
        let cfg = self.cfg_str();
        self.jobs.clear();
        for (name, repo) in ["a", "b"].iter().zip(self.repos.clone()) {
            let spec = tree::job_spec(name, &repo.display().to_string());
            let done = self.fleet(&["task", "--config", &cfg, "-"], Some(&spec), Duration::from_secs(60)).map_err(f)?;
            let v = parse_json(&done.stdout).ok_or_else(|| f(format!("task printed no JSON: {}", done.tail(3))))?;
            if v.get("status").and_then(Value::as_str) != Some("accepted") {
                return Err(f(format!("job {name} not accepted: {v}")));
            }
            let id = v.get("job_id").and_then(Value::as_str).unwrap_or("").to_string();
            self.jobs.push(id);
        }
        Ok(format!("submitted {}", self.jobs.join(", ")))
    }

    fn progress_before(&mut self) -> StepResult {
        let (a, b) = (self.repos[0].clone(), self.repos[1].clone());
        let cfg = self.cfg_str();
        let (sa, sb) = (a.display().to_string(), b.display().to_string());
        let res = self.poll(Duration::from_secs(600), |c| {
            let status = c.status()?;
            let applets = c.fleet_json(&["applet", "list", "--config", &cfg])?;
            let steps = c.committed_steps(&a);
            tree::assess(&status, &applets, &sa, &sb, steps).map(|ok| (ok, status, applets, steps))
        });
        let (ok, status, applets, steps) = res.map_err(|e| Failure::new("progress_before", e))?;
        let mut nodes = vec![("root".to_string(), self.cfg.as_ref().and_then(|c| c.parent()).map(Path::to_path_buf)
            .unwrap_or_default())];
        if let Some(subs) = status.get("subfleets").and_then(Value::as_object) {
            for (key, sf) in subs {
                let up = sf.get("state").and_then(Value::as_str) == Some("running")
                    && sf.get("daemon_alive").and_then(Value::as_bool) == Some(true);
                if let (true, Some(run)) = (up, sf.get("run").and_then(Value::as_str)) {
                    nodes.push((key.clone(), PathBuf::from(run)));
                }
            }
        }
        self.before = Some(Before {
            jobs: tree::jobs(&status).into_iter().map(|(id, st, _)| (id, st)).collect(),
            supervisor: supervisor_state(&status),
            applet_pid: tree::applet_up(&applets, tree::APPLET).unwrap_or(0),
            steps_a: steps,
            nodes,
        });
        self.observed.insert("status_before_upgrade".into(), status);
        self.observed.insert("applets_before_upgrade".into(), applets);
        self.observed.insert("job_a_steps_before_upgrade".into(), json!(steps));
        self.guard.scan();
        Ok(ok)
    }

    // ------------------------------------------------------------------------------------- after the upgrade
    fn before(&self) -> Before {
        self.before.clone().unwrap_or_default()
    }

    /// `<to>/bin/fleetmux <args>` with the `--to` environment, as JSON.
    fn to_json(&self, args: &[&str]) -> Result<Value, String> {
        let to = self.to.as_ref().expect("to is resolved in prepare");
        let mut argv = vec![to.fleetmux().display().to_string()];
        argv.extend(args.iter().map(|s| s.to_string()));
        let stop = || self.stop();
        let done = proc::run(&self.guard, &argv, &self.env_to, &self.world().root, None, Duration::from_secs(60),
                             &self.log, &stop)?;
        parse_json(&done.stdout).ok_or_else(|| {
            format!("`fleetmux {}` of --to gave no JSON (exit {:?}): {}", args.join(" "), done.code, done.tail(3))
        })
    }

    fn status_to(&self) -> Result<Value, String> {
        let cfg = self.cfg_str();
        self.to_json(&["status", "--config", &cfg])
    }

    fn applets_to(&self) -> Result<Value, String> {
        let cfg = self.cfg_str();
        self.to_json(&["applet", "list", "--config", &cfg])
    }

    /// This user's processes that name the world.
    fn procs(&self) -> Vec<after::Proc> {
        let uid = unsafe { libc::getuid() };
        let out = std::process::Command::new("ps")
            .args(["-ww", "-U", &uid.to_string(), "-o", "pid=,etime=,command="])
            .stdin(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .output();
        let text = out.map(|o| String::from_utf8_lossy(&o.stdout).into_owned()).unwrap_or_default();
        let root = self.world().root.clone();
        let mut procs: Vec<after::Proc> = after::parse_ps(&text, epoch())
            .into_iter()
            .filter(|p| p.argv.iter().any(|a| after::under(a, &root)))
            .collect();
        // a venv python on macOS shows the framework's Python.app as argv[0]: its venv is in its environment
        for p in procs.iter_mut().filter(|p| !p.argv[0].contains("/.venv/")) {
            let env = std::process::Command::new("ps")
                .args(["-wwE", "-o", "command=", "-p", &p.pid.to_string()])
                .stdin(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .output();
            p.launcher = env.ok().and_then(|o| after::launcher_in(&String::from_utf8_lossy(&o.stdout)));
        }
        procs
    }

    /// The fleets that ran before the upgrade, as they are now.
    fn nodes(&self) -> Vec<after::Node> {
        self.before()
            .nodes
            .iter()
            .map(|(key, run)| after::Node {
                key: key.clone(),
                run: run.clone(),
                locked: lock_held(run),
                python: std::fs::read_to_string(run.join("config.json"))
                    .ok()
                    .and_then(|t| serde_json::from_str::<Value>(&t).ok())
                    .and_then(|c| c.get("python").and_then(Value::as_str).map(str::to_string)),
            })
            .collect()
    }

    fn check(&mut self, step: &str, limit: u64, f: impl FnMut(&Ctx) -> Result<String, String>) -> StepResult {
        let r = self.poll(Duration::from_secs(limit), f);
        self.guard.scan();
        // what the tree looked like when the check ended, for the report
        let seen = self.status_to().unwrap_or_else(|e| json!({"error": e}));
        self.observed.insert(format!("status_after_{step}"), seen);
        r.map_err(|e| Failure::new(step, e))
    }

    fn check_daemons(&mut self) -> StepResult {
        let to = self.to.clone().expect("to").dir;
        self.check("check_daemons", 300, |c| after::check_daemons(&c.nodes(), &c.procs(), &to))
    }

    fn check_interpreter(&mut self) -> StepResult {
        let to = self.to.clone().expect("to").dir;
        let since = self.upgrade_at;
        self.check("check_interpreter", 300, |c| {
            let pid = tree::applet_up(&c.applets_to()?, tree::APPLET)?;
            after::check_interpreter(&c.nodes(), &c.procs(), &[(tree::APPLET.to_string(), pid as i32)], &to, since)
        })
    }

    fn check_jobs(&mut self) -> StepResult {
        let before = self.before().jobs;
        let run = self.before().nodes.first().map(|n| n.1.clone()).unwrap_or_default();
        self.check("check_jobs", 300, |c| {
            let st = c.status_to()?;
            let after: Vec<(String, String)> = tree::jobs(&st).into_iter().map(|(id, s, _)| (id, s)).collect();
            let mut results = BTreeMap::new();
            for (id, _) in &before {
                let r = std::fs::read_to_string(run.join("job-results").join(format!("{id}.json")))
                    .ok()
                    .and_then(|t| serde_json::from_str::<Value>(&t).ok())
                    .and_then(|v| v.get("status").and_then(Value::as_str).map(str::to_string));
                if let Some(r) = r {
                    results.insert(id.clone(), r);
                }
            }
            after::check_jobs(&before, &after, &results)
        })
    }

    fn check_supervisor(&mut self) -> StepResult {
        let before = self.before().supervisor;
        self.check("check_supervisor", 120, |c| {
            after::check_supervisor(before.as_deref(), supervisor_state(&c.status_to()?).as_deref())
        })
    }

    fn check_subfleet(&mut self) -> StepResult {
        self.check("check_subfleet", 300, |c| tree::subfleet_up(&c.status_to()?, tree::SUBFLEET))
    }

    fn check_applet(&mut self) -> StepResult {
        let pid = self.before().applet_pid;
        self.check("check_applet", 180, |c| after::check_applet(pid, &c.applets_to()?, tree::APPLET))
    }

    fn steps_of(&self, repo: &Path) -> Vec<u32> {
        let git = self.tools.get("git").map(|p| p.display().to_string()).unwrap_or("git".into());
        let argv = vec![git, "-C".into(), repo.display().to_string(), "log".into(), "--reverse".into(),
                        "--format=%s".into()];
        self.cmd(argv, None, Duration::from_secs(30)).map(|d| after::step_numbers(&d.stdout)).unwrap_or_default()
    }

    /// The running job goes on after the upgrade (new steps, none redone); then the scenario releases it and the
    /// queued job starts and commits.
    fn progress_after(&mut self) -> StepResult {
        // soft: supervisor_holds still records what the tree's supervisors went through; the run stays failed
        let f = |e: String| Failure::soft("progress_after", e);
        let (a, b) = (self.repos[0].clone(), self.repos[1].clone());
        let before = self.before().steps_a;
        let end = self.steps_a_at_upgrade_end.unwrap_or(before);
        let first = self
            .poll(Duration::from_secs(600), |c| after::check_progress(&c.steps_of(&a), before, end))
            .map_err(f)?;
        std::fs::write(a.join("SCENARIO-RELEASE"), "released by fleetmux scenario upgrade\n")
            .map_err(|e| f(format!("cannot release job A: {e}")))?;
        let id_b = self.jobs.get(1).cloned().unwrap_or_default();
        let started = self
            .poll(Duration::from_secs(900), |c| {
                let st = c.status_to()?;
                let state = tree::jobs(&st).into_iter().find(|(id, _, _)| *id == id_b).map(|(_, s, _)| s);
                let steps = c.steps_of(&b);
                match (state.as_deref(), steps.len()) {
                    (Some("running"), n) if n > 0 => after::check_steps(&steps, 0).map(|_| format!("{id_b} running, {n} step(s)")),
                    (s, n) => Err(format!("{id_b} {}, {n} step(s) in {}", s.unwrap_or("not listed"), b.display())),
                }
            })
            .map_err(f)?;
        // job A, released, must end ok: a job that ends blocked did not go on, whatever steps it committed
        let id_a = self.jobs.first().cloned().unwrap_or_default();
        let results = PathBuf::from(self.cfg_str()).with_file_name("job-results").join(format!("{id_a}.json"));
        let ended = self.poll(Duration::from_secs(300), |_| {
            let v = std::fs::read_to_string(&results).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok());
            match after::job_result(v.as_ref(), &id_a) {
                Err(e) if v.is_some() => Ok(Err(e)),  // published and not ok: final, no need to wait
                other => other.map(Ok),
            }
        });
        if let Some(v) = std::fs::read_to_string(&results).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()) {
            self.observed.insert("job_a_result".into(), v);
        }
        let ended = ended.map_err(f)?.map_err(|e| f(format!("job A after its release: {e}")))?;
        let a_final = self.steps_of(&a);
        let again = after::check_progress(&a_final, before, end).map_err(|e| f(format!("job A after its release: {e}")))?;
        self.observed.insert("job_a_steps_after".into(), json!(a_final));
        self.observed.insert("job_b_steps_after".into(), json!(self.steps_of(&b)));
        Ok(format!("job A: {first}; released; job B: {started}; job A at the end: {again}; {ended}"))
    }

    /// The black-box call of the `--to` release's `fleetmux upgrade`: its exit code and last JSON line only.
    fn upgrade(&mut self) -> StepResult {
        let to = self.to.clone().expect("to");
        let cfg = self.cfg_str();
        let argv = vec![
            to.fleetmux().display().to_string(),
            "upgrade".into(),
            "--to".into(),
            to.dir.display().to_string(),
            "--config".into(),
            cfg,
            "--stop-timeout".into(),
            UPGRADE_STOP_TIMEOUT_S.to_string(),
        ];
        self.upgrade_at = epoch();
        let done = self
            .cmd(argv, None, Duration::from_secs(UPGRADE_LIMIT_S))
            .map_err(|e| Failure::new("upgrade", e))?;
        self.guard.scan();
        // the baseline of progress_after: a step committed before the switch ended is not progress on --to
        if let Some(a) = self.repos.first().cloned() {
            let n = self.steps_of(&a).len() as u32;
            self.steps_a_at_upgrade_end = Some(n);
            self.observed.insert("job_a_steps_at_upgrade_end".into(), json!(n));
        }
        self.observed.insert("upgrade_ended_at".into(), json!(epoch()));
        self.observed.insert("upgrade_output".into(), json!(done.stdout.lines().collect::<Vec<_>>()));
        if let Some(v) = parse_json(&done.stdout) {
            self.observed.insert("upgrade_result".into(), v);
        }
        match upgrade_outcome(done.code, &done.stdout) {
            Ok(_) => Ok(format!("upgraded to {}", to.label())),
            // exit 1: the upgrade's own check failed and the new release runs; the run fails, and the scenario's
            // own checks still run, so the report shows what the tree looks like from the outside
            Err(e) if checks_follow(done.code, &done.stdout) => Err(Failure::soft(
                "upgrade",
                format!("{e}; the scenario's own checks follow (the run stays failed)"),
            )),
            Err(e) => Err(Failure::new("upgrade", format!("{e}; output: {}", done.tail(4)))),
        }
    }

    /// Sub-fleet supervisors reported held after the upgrade began, and the heal the root supervisor sent
    /// (recorded, never a failure).
    fn supervisor_holds(&mut self) -> StepResult {
        let Some(w) = &self.world else { return Ok("n/a no world".into()) };
        let log = std::fs::read_to_string(w.root.join("actor.jsonl")).unwrap_or_default();
        let since = self.upgrade_at as f64;
        let holds = after::supervisor_holds(&log, since);
        let line = after::holds_line(&holds, since);
        self.observed.insert("supervisor_holds".into(), json!(holds));
        Ok(line.strip_prefix("ok ").unwrap_or(&line).to_string())
    }

    fn cleanup_step(&mut self) -> StepResult {
        let left = self.guard.cleanup();
        if !left.is_empty() {
            return Err(Failure::new("cleanup", format!("left running: {}", left.join("; "))));
        }
        Ok(match &self.world {
            Some(w) if self.args.keep => format!("processes stopped; kept {}", w.root.display()),
            Some(w) => format!("processes stopped; removed {}", w.root.display()),
            None => "nothing was started".into(),
        })
    }

    fn write_report(&self, lines: &[String], failure: Option<&Failure>, started: Instant) -> String {
        let path = match &self.world {
            Some(w) if self.args.keep => w.root.join("report.json"),
            _ => std::env::temp_dir().join(format!(
                "fleetmux-scenario-{}-{}.json",
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0),
                std::process::id()
            )),
        };
        let (from, to) = self.labels();
        let report = json!({
            "from": from, "to": to,
            "from_dir": self.from.as_ref().map(|r| r.dir.display().to_string()),
            "to_dir": self.to.as_ref().map(|r| r.dir.display().to_string()),
            "world": self.world.as_ref().map(|w| w.root.display().to_string()),
            "kept": self.args.keep,
            "jobs": self.jobs,
            "steps": lines,
            "failure": failure.map(|f| json!({"step": f.step, "reason": f.reason})),
            "observed": Value::Object(self.observed.clone()),
            "duration_s": started.elapsed().as_secs(),
        });
        match std::fs::write(&path, serde_json::to_string_pretty(&report).unwrap_or_default()) {
            Ok(()) => path.display().to_string(),
            Err(_) => String::new(),
        }
    }

    fn inject(&self, step: &str) -> Result<(), Failure> {
        let Some((s, kind)) = &self.fault else { return Ok(()) };
        if s != step {
            return Ok(());
        }
        match kind.as_str() {
            "fail" => Err(Failure::new(step, "injected failure (FLEETMUX_SCENARIO_FAULT)")),
            "panic" => panic!("injected panic (FLEETMUX_SCENARIO_FAULT) in {step}"),
            "hang" => loop {
                if let Some(why) = self.stop() {
                    return Err(Failure::new(step, why));
                }
                std::thread::sleep(Duration::from_millis(100));
            },
            _ => Ok(()),
        }
    }
}

impl Steps for Ctx {
    fn run(&mut self, step: &str) -> Result<String, Failure> {
        let out = match step {
            "prepare" => self.prepare(),
            "world" => self.world_step(),
            "start_from" => self.start_from(),
            "subfleet_up" => self.subfleet_up(),
            "applet_up" => self.applet_up(),
            "submit_jobs" => self.submit_jobs(),
            "progress_before" => self.progress_before(),
            "upgrade" => self.upgrade(),
            "check_daemons" => self.check_daemons(),
            "check_interpreter" => self.check_interpreter(),
            "check_jobs" => self.check_jobs(),
            "check_supervisor" => self.check_supervisor(),
            "check_subfleet" => self.check_subfleet(),
            "check_applet" => self.check_applet(),
            "check_window" => Ok(CHECK_WINDOW_NA.to_string()),
            "progress_after" => self.progress_after(),
            "supervisor_holds" => self.supervisor_holds(),
            "cleanup" => return self.cleanup_step(),
            other => Err(Failure::new(other, "unknown step")),
        }?;
        self.inject(step)?;
        Ok(out)
    }

    fn interrupted(&self) -> Option<String> {
        self.stop()
    }

    fn nothing_to_do(&self) -> Option<String> {
        self.same_release.then(|| SAME_RELEASE.to_string())
    }

    fn entering(&mut self, step: &str) {
        self.shared.lock().unwrap_or_else(|e| e.into_inner()).step = step.to_string();
    }
}

/// The result of `fleetmux upgrade`: `Ok(last JSON line)` only for exit 0 with `result == "pass"`; otherwise the
/// failure reason, with the JSON's `step` copied as an opaque string.
pub fn upgrade_outcome(code: Option<i32>, stdout: &str) -> Result<Value, String> {
    let last = stdout.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("");
    let Some(v) = serde_json::from_str::<Value>(last).ok().filter(Value::is_object) else {
        return Err(format!("`fleetmux upgrade` gave no result line (exit {code:?}): {}", last.trim()));
    };
    let result = v.get("result").and_then(Value::as_str).unwrap_or("");
    if code == Some(0) && result == "pass" {
        return Ok(v);
    }
    let text = |k: &str| match v.get(k) {
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
        None => String::new(),
    };
    let mut back = match v.get("rolled_back") {
        Some(Value::Bool(b)) => format!(", rolled_back {b}"),
        _ => String::new(),
    };
    // where a failed way back stopped: "verify" = --from is up and only its check failed, another step = the tree
    // is down or mixed; "" (the way back worked) and null (there was none) add nothing
    if let Some(Value::String(at)) = v.get("rollback_step")
        && !at.is_empty()
    {
        back.push_str(&format!(", rollback_step {at:?}"));
    }
    Err(format!(
        "`fleetmux upgrade` exit {}, result {result:?}{back}, step {:?}: {}",
        code.map(|c| c.to_string()).unwrap_or_else(|| "none (killed)".into()),
        text("step"),
        text("reason")
    ))
}

/// `<world>/bin/claude`: the scripted actor, with its settings exported by the launcher itself (a pane that a
/// restarted terminal server opens does not inherit the scenario's environment).
pub fn launcher_script(python: &Path, actor: &Path, env: &[(&str, &str)]) -> String {
    let mut out = String::from("#!/bin/sh\n# fleetmux scenario: the scripted actor, not Claude Code; no model is called\n");
    for (k, v) in env {
        out.push_str(&format!("export {k}={}\n", world::shell_quote(v)));
    }
    out.push_str(&format!(
        "exec {} -I {} \"$@\"\n",
        world::shell_quote(&python.display().to_string()),
        world::shell_quote(&actor.display().to_string())
    ));
    out
}

/// `check_window` in the isolated world: reported, never failed.
pub const CHECK_WINDOW_NA: &str = "n/a the scenario runs its tree with --no-view in an isolated world, so there is \
no desktop window (CGWindowList) to check; `fleetmux upgrade` checks the window on a real machine";

/// The root supervisor slot's state in a `status` JSON.
fn supervisor_state(status: &Value) -> Option<String> {
    status.pointer("/slots/supervisor/state").and_then(Value::as_str).map(str::to_string)
}

/// After a failed upgrade, do the scenario's own checks still follow? Only for exit 1 with no `rolled_back` in the
/// result (the upgrade's own check failed and the new release runs); a switch that went back to `--from`, or whose
/// way back failed, leaves nothing on `--to` to check.
pub fn checks_follow(code: Option<i32>, stdout: &str) -> bool {
    let last = stdout.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("");
    let Some(v) = serde_json::from_str::<Value>(last).ok().filter(Value::is_object) else {
        return false;
    };
    code == Some(1) && v.get("rolled_back").is_none_or(Value::is_null)
}

/// The whole of `text` as JSON, else its last line that is a JSON object.
pub fn parse_json(text: &str) -> Option<Value> {
    if let Ok(v) = serde_json::from_str::<Value>(text.trim())
        && (v.is_object() || v.is_array())
    {
        return Some(v);
    }
    text.lines()
        .rev()
        .filter_map(|l| serde_json::from_str::<Value>(l.trim()).ok())
        .find(Value::is_object)
}

/// `<dir>/claude`: a link to `sleep` that runs under that name, or None (as `fleetmux demo` makes it: the
/// scripted agent runs it on its pane, so the daemon's check for an official CLI on the pane sees a `claude`).
fn live_marker(dir: &Path) -> Option<PathBuf> {
    std::fs::create_dir(dir).ok()?;
    let _ = std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700));
    let link = dir.join("claude");
    for target in ["/bin/sleep", "/usr/bin/gnusleep"] {
        if !Path::new(target).exists() {
            continue;
        }
        let _ = std::fs::remove_file(&link);
        if std::os::unix::fs::symlink(target, &link).is_ok()
            && std::process::Command::new(&link)
                .arg("0")
                .status()
                .is_ok_and(|s| s.success())
        {
            return Some(link);
        }
    }
    let _ = std::fs::remove_file(&link);
    None
}

/// The private fleet.yaml of the world: the `--from` release's own `discovery:` block with both model chains
/// set to its default worker model (so no slot is rotated to another model), and `release_drift` off.
pub fn private_fleet_yaml(from: &Path) -> String {
    let model = std::fs::read_to_string(from.join("fleetmux/fleet.py"))
        .ok()
        .and_then(|t| {
            let i = t.find("'worker_model': '")? + "'worker_model': '".len();
            let rest = &t[i..];
            Some(rest[..rest.find('\'')?].to_string())
        })
        .filter(|m| !m.is_empty() && m.chars().all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c)))
        .unwrap_or_else(|| "sonnet".into());
    let shipped = std::fs::read_to_string(from.join("fleetmux/config/fleet.yaml")).unwrap_or_default();
    let mut out = String::from("# fleetmux scenario upgrade: private configuration of the isolated world\n");
    let mut inside = false;
    let mut seen = false;
    for line in shipped.lines() {
        let top = !line.starts_with([' ', '\t', '#']) && !line.trim().is_empty();
        if top {
            inside = line.starts_with("discovery:");
            seen |= inside;
        }
        if !inside {
            continue;
        }
        let t = line.trim_start();
        if line.starts_with("  ") && !line.starts_with("   ") && t.starts_with("supervisor:") {
            out.push_str(&format!("  supervisor: [{model}]\n"));
        } else if line.starts_with("  ") && !line.starts_with("   ") && t.starts_with("worker:") {
            out.push_str(&format!("  worker: [{model}]\n"));
        } else {
            out.push_str(line);
            out.push('\n');
        }
    }
    if !seen {
        out.push_str(&format!("discovery:\n  supervisor: [{model}]\n  worker: [{model}]\n"));
    }
    out.push_str("release_drift:\n  enabled: false\n");
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_private_config_pins_both_chains_and_turns_drift_off() {
        let d = std::env::temp_dir().join(format!("fmxpriv-{}", std::process::id()));
        std::fs::create_dir_all(d.join("fleetmux/config")).unwrap();
        std::fs::write(d.join("fleetmux/fleet.py"), "DEFAULTS = {'worker_model': 'sonnet', 'x': 1}\n").unwrap();
        std::fs::write(
            d.join("fleetmux/config/fleet.yaml"),
            "other:\n  supervisor: [keep]\ndiscovery:\n  # comment\n  supervisor: [astra, fable]\n  worker: [opus, sol]\n  mode: task\n  only_via: {claude: claude}\nnext:\n  worker: [x]\n",
        )
        .unwrap();
        let y = private_fleet_yaml(&d);
        assert!(y.contains("discovery:\n  # comment\n  supervisor: [sonnet]\n  worker: [sonnet]\n  mode: task\n"), "{y}");
        assert!(y.contains("only_via: {claude: claude}"));
        assert!(!y.contains("astra") && !y.contains("[keep]") && !y.contains("next:"), "{y}");
        assert!(y.ends_with("release_drift:\n  enabled: false\n"));
        std::fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn a_daemon_lock_is_seen_while_another_holder_has_it() {
        use std::os::unix::io::AsRawFd;
        let d = std::env::temp_dir().join(format!("fmxlock-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        assert!(!lock_held(&d), "nobody holds the lock yet");
        let f = std::fs::OpenOptions::new().create(true).append(true).open(d.join("daemon.lock")).unwrap();
        assert_eq!(unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) }, 0);
        assert!(lock_held(&d), "the lock is held by another open file");
        drop(f);
        // a child that another test forks meanwhile may hold a copy of the descriptor until its exec: poll briefly
        let until = Instant::now() + Duration::from_secs(2);
        while lock_held(&d) && Instant::now() < until {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(!lock_held(&d), "released with its holder");
        std::fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn the_launcher_carries_the_actor_settings_itself() {
        // a pane started by `fleetmux upgrade` need not inherit the scenario's environment: the launcher sets it
        let text = launcher_script(
            Path::new("/w/rel/2222/.venv/bin/python"),
            Path::new("/w/w/actor/scenario_actor.py"),
            &[("FLEETMUX_DEMO_LIVE", "/w/bin/live/claude"), ("FLEETMUX_DEMO_ACTOR_LOG", "/w/actor.jsonl"),
              ("FLEETMUX_DEMO_TURN_S", "2")],
        );
        assert!(text.starts_with("#!/bin/sh\n"), "{text}");
        assert!(text.contains("export FLEETMUX_DEMO_LIVE='/w/bin/live/claude'\n"), "{text}");
        assert!(text.contains("export FLEETMUX_DEMO_ACTOR_LOG='/w/actor.jsonl'\n"), "{text}");
        assert!(text.contains("export FLEETMUX_DEMO_TURN_S='2'\n"), "{text}");
        assert!(text.trim_end().ends_with("exec '/w/rel/2222/.venv/bin/python' -I '/w/w/actor/scenario_actor.py' \"$@\""), "{text}");
    }

    #[test]
    fn the_own_checks_follow_only_when_the_new_release_still_runs() {
        // an upgrade without a way back: exit 1 without rolled_back = a check failed and the new release runs
        let m28 = "{\"result\": \"fail\", \"step\": \"verify\", \"reason\": \"check failed: x\"}\n";
        assert!(checks_follow(Some(1), m28));
        // the switch went back to --from (or the way back failed too): checking --to makes no sense
        let back = "{\"result\": \"fail\", \"step\": \"verify\", \"reason\": \"r\", \"rolled_back\": true}\n";
        let half = "{\"result\": \"fail\", \"step\": \"verify\", \"reason\": \"r\", \"rolled_back\": false}\n";
        assert!(!checks_follow(Some(1), back));
        assert!(!checks_follow(Some(1), half));
        assert!(!checks_follow(Some(4), m28));
        assert!(!checks_follow(Some(1), "no json"));
        // the reason says where the tree is
        let e = upgrade_outcome(Some(1), back).unwrap_err();
        assert!(e.contains("rolled_back true"), "{e}");
    }

    #[test]
    fn a_failed_way_back_says_where_it_stopped() {
        let line = |back: &str, at: &str| {
            format!("{{\"result\": \"fail\", \"step\": \"verify\", \"reason\": \"r\", \"rolled_back\": {back}, \"rollback_step\": {at}}}\n")
        };
        // the way back stopped at start_new: the tree is down or mixed
        let e = upgrade_outcome(Some(1), &line("false", "\"start_new\"")).unwrap_err();
        assert!(e.contains("rolled_back false, rollback_step \"start_new\""), "{e}");
        // the way back brought --from up and only its check failed
        let e = upgrade_outcome(Some(1), &line("false", "\"verify\"")).unwrap_err();
        assert!(e.contains("rollback_step \"verify\""), "{e}");
        // the way back worked ("") or there was none (null): nothing more to say
        for (back, at) in [("true", "\"\""), ("true", "null")] {
            let e = upgrade_outcome(Some(1), &line(back, at)).unwrap_err();
            assert!(e.contains("rolled_back true") && !e.contains("rollback_step"), "{e}");
        }
    }

    #[test]
    fn only_exit_0_with_result_pass_is_a_passed_upgrade() {
        let pass = "upgrade: preflight: ok\n{\"result\": \"pass\", \"step\": \"\", \"reason\": \"\"}\n";
        assert_eq!(upgrade_outcome(Some(0), pass).unwrap()["result"], "pass");
        // exit 0 with a failing result line, or a passing line with a non-zero exit, is a failure
        let fail = "{\"result\": \"fail\", \"step\": \"x\", \"reason\": \"r\"}\n";
        assert!(upgrade_outcome(Some(0), fail).is_err());
        assert!(upgrade_outcome(Some(1), pass).is_err());
        assert!(upgrade_outcome(None, pass).is_err());
        // no JSON line at all
        let e = upgrade_outcome(Some(0), "usage: fleetmux ...\n").unwrap_err();
        assert!(e.contains("no result line"), "{e}");
        // the step is copied as it is, whatever its name (switch phases, rollback, ...)
        let odd = "upgrade: x\n{\"result\": \"fail\", \"step\": \"switch:rollback/started\", \"reason\": \"daemon did not come up; rolled back to 1111\"}\n";
        let e = upgrade_outcome(Some(4), odd).unwrap_err();
        assert!(e.contains("step \"switch:rollback/started\"") && e.contains("rolled back to 1111") && e.contains("exit 4"), "{e}");
    }

    #[test]
    fn json_is_the_whole_output_or_its_last_object_line() {
        assert_eq!(parse_json("{\n \"a\": 1\n}\n").unwrap()["a"], 1);
        assert_eq!(parse_json("noise\n{\"b\": 2}\nmore noise\n").unwrap()["b"], 2);
        assert!(parse_json("nothing here").is_none());
    }
}
