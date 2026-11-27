//! The fixed step order of `fleetmux scenario upgrade` and the loop that runs it.
//!
//! The order lives in [`STEPS`] and nowhere else: [`run_all`] walks it from the first entry, stops at the first
//! failing step and then always runs `cleanup`. Every step prints one line `scenario: <step>: ok|FAIL <detail>`;
//! the steps after the first failure print nothing (they did not run). A step that does not apply returns a detail
//! starting with `n/a`; it prints `scenario: <step>: n/a <why>` and does not fail the run. A run with nothing left
//! to do after a passed step ([`Steps::nothing_to_do`]) skips to `cleanup` and passes.

/// Every step, in the order it runs. `cleanup` is last and runs on every path.
pub const STEPS: &[&str] = &[
    "prepare",
    "world",
    "start_from",
    "subfleet_up",
    "applet_up",
    "submit_jobs",
    "progress_before",
    "upgrade",
    "check_daemons",
    "check_interpreter",
    "check_jobs",
    "check_supervisor",
    "check_subfleet",
    "check_applet",
    "check_window",
    "progress_after",
    "supervisor_holds",
    "cleanup",
];

/// The last step; it runs whatever happened before.
pub const CLEANUP: &str = "cleanup";

/// What one step returns: `Ok(detail)` or `Err(Failure)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Failure {
    /// The step name reported (a step may refine it, e.g. `upgrade:stop_root`).
    pub step: String,
    pub reason: String,
    /// The later steps still run (see [`Failure::soft`]).
    pub soft: bool,
}

impl Failure {
    pub fn new(step: &str, reason: impl Into<String>) -> Failure {
        Failure {
            step: step.to_string(),
            reason: reason.into(),
            soft: false,
        }
    }

    /// A failure after which the later steps still run (their results are evidence); the run still fails.
    pub fn soft(step: &str, reason: impl Into<String>) -> Failure {
        Failure {
            soft: true,
            ..Failure::new(step, reason)
        }
    }
}

/// The implementation of the steps. `run` is called with names from [`STEPS`] only, in that order.
pub trait Steps {
    fn run(&mut self, step: &str) -> Result<String, Failure>;
    /// Called before each step: `Some(reason)` stops the run (timeout, signal) as a failure of that step.
    fn interrupted(&self) -> Option<String> {
        None
    }
    /// The step being run, published for the signal and timeout paths.
    fn entering(&mut self, _step: &str) {}
    /// Asked after each passed step: `Some(why)` means the run has nothing left to do; the remaining steps are
    /// skipped, `cleanup` still runs and the run passes with `why` as its note.
    fn nothing_to_do(&self) -> Option<String> {
        None
    }
}

/// The outcome of a whole run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Outcome {
    /// `None` = every step passed.
    pub failure: Option<Failure>,
    /// The steps that ran, in order (cleanup included).
    pub ran: Vec<String>,
    /// Why a passed run stopped early (see [`Steps::nothing_to_do`]).
    pub note: Option<String>,
}

impl Outcome {
    pub fn passed(&self) -> bool {
        self.failure.is_none()
    }
}

/// Run [`STEPS`] in order with `steps`, printing one line per step through `out`.
pub fn run_all(steps: &mut dyn Steps, out: &mut dyn FnMut(&str)) -> Outcome {
    let mut failure: Option<Failure> = None;
    let mut note = None;
    let mut ran = Vec::new();
    for step in STEPS.iter().copied().filter(|s| *s != CLEANUP) {
        if let Some(why) = steps.interrupted() {
            let f = Failure::new(step, why);
            out(&format!("scenario: {}: FAIL {}", f.step, f.reason));
            failure.get_or_insert(f);
            break;
        }
        steps.entering(step);
        ran.push(step.to_string());
        match steps.run(step) {
            Ok(detail) if detail.starts_with("n/a") => out(&format!("scenario: {step}: {detail}")),
            Ok(detail) => out(&format!("scenario: {step}: ok {detail}").trim_end().to_string()),
            Err(f) => {
                out(&format!("scenario: {}: FAIL {}", f.step, f.reason));
                let soft = f.soft;
                failure.get_or_insert(f);
                if !soft {
                    break;
                }
                continue;
            }
        }
        if failure.is_none()
            && let Some(why) = steps.nothing_to_do()
        {
            note = Some(why);
            break;
        }
    }
    steps.entering(CLEANUP);
    ran.push(CLEANUP.to_string());
    match steps.run(CLEANUP) {
        Ok(detail) => out(&format!("scenario: {CLEANUP}: ok {detail}").trim_end().to_string()),
        Err(f) => {
            out(&format!("scenario: {}: FAIL {}", f.step, f.reason));
            failure.get_or_insert(f);
        }
    }
    Outcome { failure, ran, note }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Rec {
        ran: Vec<String>,
        fail_at: Option<&'static str>,
        stop_at: Option<&'static str>,
    }

    impl Steps for Rec {
        fn run(&mut self, step: &str) -> Result<String, Failure> {
            self.ran.push(step.to_string());
            if Some(step) == self.fail_at {
                return Err(Failure::new(step, "broken on purpose"));
            }
            Ok(format!("did {step}"))
        }
        fn interrupted(&self) -> Option<String> {
            match (self.stop_at, self.ran.last()) {
                (Some(s), Some(last)) if last == s => Some("timed out".into()),
                _ => None,
            }
        }
    }

    fn rec(fail_at: Option<&'static str>, stop_at: Option<&'static str>) -> Rec {
        Rec {
            ran: Vec::new(),
            fail_at,
            stop_at,
        }
    }

    #[test]
    fn order_is_fixed() {
        assert_eq!(STEPS.first(), Some(&"prepare"));
        assert_eq!(STEPS.last(), Some(&CLEANUP));
        let pos = |s: &str| STEPS.iter().position(|x| *x == s).unwrap();
        assert!(pos("world") < pos("start_from"));
        assert!(pos("start_from") < pos("subfleet_up"));
        assert!(pos("subfleet_up") < pos("applet_up"));
        assert!(pos("applet_up") < pos("submit_jobs"));
        assert!(pos("submit_jobs") < pos("progress_before"));
        assert!(pos("progress_before") < pos("upgrade"));
        assert!(pos("upgrade") < pos("check_daemons"));
        assert!(pos("check_window") < pos("progress_after"));
        let mut seen = std::collections::HashSet::new();
        assert!(STEPS.iter().all(|s| seen.insert(*s)), "a step name appears twice");
    }

    #[test]
    fn all_pass_runs_every_step_in_order() {
        let mut r = rec(None, None);
        let mut lines = Vec::new();
        let o = run_all(&mut r, &mut |l| lines.push(l.to_string()));
        assert!(o.passed());
        assert_eq!(r.ran, STEPS.iter().map(|s| s.to_string()).collect::<Vec<_>>());
        assert_eq!(o.ran, r.ran);
        assert_eq!(lines.len(), STEPS.len());
        for (line, step) in lines.iter().zip(STEPS) {
            assert!(line.starts_with(&format!("scenario: {step}: ok")), "{line}");
        }
    }

    #[test]
    fn a_failure_names_its_step_and_cleanup_still_runs() {
        let mut r = rec(Some("submit_jobs"), None);
        let mut lines = Vec::new();
        let o = run_all(&mut r, &mut |l| lines.push(l.to_string()));
        let f = o.failure.clone().unwrap();
        assert_eq!(f.step, "submit_jobs");
        assert_eq!(f.reason, "broken on purpose");
        let upto = STEPS.iter().position(|s| *s == "submit_jobs").unwrap();
        let mut want: Vec<String> = STEPS[..=upto].iter().map(|s| s.to_string()).collect();
        want.push(CLEANUP.into());
        assert_eq!(r.ran, want);
        assert!(lines.iter().any(|l| l.starts_with("scenario: submit_jobs: FAIL broken on purpose")));
        assert!(lines.last().unwrap().starts_with("scenario: cleanup: ok"));
        assert!(!lines.iter().any(|l| l.contains("progress_before")));
    }

    #[test]
    fn an_interruption_fails_the_next_step_and_cleans_up() {
        let mut r = rec(None, Some("start_from"));
        let mut lines = Vec::new();
        let o = run_all(&mut r, &mut |l| lines.push(l.to_string()));
        let f = o.failure.unwrap();
        assert_eq!(f.step, "subfleet_up");
        assert!(f.reason.contains("timed out"));
        assert_eq!(r.ran.last().map(String::as_str), Some(CLEANUP));
        assert!(!r.ran.iter().any(|s| s == "subfleet_up"));
    }

    #[test]
    fn a_soft_failure_fails_the_run_but_the_later_steps_still_run() {
        struct Soft(Vec<String>);
        impl Steps for Soft {
            fn run(&mut self, step: &str) -> Result<String, Failure> {
                self.0.push(step.into());
                match step {
                    "upgrade" => Err(Failure::soft(step, "verify failed; the new release runs")),
                    "check_jobs" => Err(Failure::new(step, "job lost")),
                    _ => Ok(String::new()),
                }
            }
        }
        let mut s = Soft(Vec::new());
        let mut lines = Vec::new();
        let o = run_all(&mut s, &mut |l| lines.push(l.to_string()));
        let f = o.failure.unwrap();
        assert_eq!(f.step, "upgrade", "the first failure is the run's failure");
        assert!(lines.iter().any(|l| l.starts_with("scenario: upgrade: FAIL verify failed")), "{lines:?}");
        // the checks after the soft failure ran, up to the next (hard) failure, then cleanup
        let upto = STEPS.iter().position(|x| *x == "check_jobs").unwrap();
        let mut want: Vec<String> = STEPS[..=upto].iter().map(|s| s.to_string()).collect();
        want.push(CLEANUP.into());
        assert_eq!(s.0, want);
        assert!(lines.iter().any(|l| l.starts_with("scenario: check_jobs: FAIL job lost")), "{lines:?}");
    }

    #[test]
    fn a_step_that_does_not_apply_prints_n_a_and_does_not_fail() {
        struct Na(Vec<String>);
        impl Steps for Na {
            fn run(&mut self, step: &str) -> Result<String, Failure> {
                self.0.push(step.into());
                Ok(if step == "check_window" { "n/a no desktop window in the isolated world".into() } else { String::new() })
            }
        }
        let mut n = Na(Vec::new());
        let mut lines = Vec::new();
        let o = run_all(&mut n, &mut |l| lines.push(l.to_string()));
        assert!(o.passed());
        assert!(lines.contains(&"scenario: check_window: n/a no desktop window in the isolated world".to_string()), "{lines:?}");
    }

    #[test]
    fn a_failing_cleanup_is_reported_when_nothing_failed_before() {
        let mut r = rec(Some(CLEANUP), None);
        let o = run_all(&mut r, &mut |_| {});
        assert_eq!(o.failure.unwrap().step, CLEANUP);
    }

    #[test]
    fn a_run_with_nothing_to_do_passes_after_its_step_and_still_cleans_up() {
        struct Same(Vec<String>);
        impl Steps for Same {
            fn run(&mut self, step: &str) -> Result<String, Failure> {
                self.0.push(step.into());
                Ok(String::new())
            }
            fn nothing_to_do(&self) -> Option<String> {
                (self.0.last().map(String::as_str) == Some("prepare"))
                    .then(|| "from and to are the same release: nothing to switch".to_string())
            }
        }
        let mut s = Same(Vec::new());
        let mut lines = Vec::new();
        let o = run_all(&mut s, &mut |l| lines.push(l.to_string()));
        assert!(o.passed(), "{o:?}");
        assert_eq!(o.note.as_deref(), Some("from and to are the same release: nothing to switch"));
        assert_eq!(s.0, vec!["prepare", CLEANUP]);
        assert_eq!(o.ran, s.0);
        assert!(!lines.iter().any(|l| l.starts_with("scenario: world")), "{lines:?}");
        assert!(lines.last().unwrap().starts_with("scenario: cleanup: ok"), "{lines:?}");
    }

    #[test]
    fn the_first_failure_wins_over_a_failing_cleanup() {
        struct Two(Vec<String>);
        impl Steps for Two {
            fn run(&mut self, step: &str) -> Result<String, Failure> {
                self.0.push(step.into());
                if step == "world" || step == CLEANUP {
                    return Err(Failure::new(step, "x"));
                }
                Ok(String::new())
            }
        }
        let mut t = Two(Vec::new());
        let o = run_all(&mut t, &mut |_| {});
        assert_eq!(o.failure.unwrap().step, "world");
        assert_eq!(t.0, vec!["prepare", "world", CLEANUP]);
    }
}
