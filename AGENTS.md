# fleetmux contributor instructions

Before changing this project, read `docs/design.md` completely. It is the confirmed design baseline; README files are usage summaries, and historical test reports do not override it.

All fleetmux (formerly cmux-fleet), Fleet agents, Pi fleet adapter, fleet skills, examples, tests, documentation, and related Codex/Pi startup `.command` launchers belong in this repository. Keep launchers under `launchers/`. Projects that use the fleet may only call it or keep thin compatibility wrappers, usage notes, and reports; do not implement fleet framework or launcher changes there.

- Extend the existing supervisor/worker/daemon architecture; do not introduce a second dispatcher or replace the task/service protocols without an explicit user decision.
- Preserve slot/generation identities: `supervisor-1` -> `supervisor-1.1`, `worker-1` -> `worker-1.1`. Reuse the existing label in the CLI session, UI, routing and history.
- Keep one main page: supervisor top-left, worker tabs on the right, history/control tabs together at bottom-left with history selected.
- Claude workers must run through the official Claude Code CLI, not a Pi proxy using Claude subscription credentials.
- Preserve private Redis isolation, delivery/generation validation, handoff and acceptance checks. Test operator input through the supervisor TUI, not only the request CLI.
- Update the design baseline and regression tests when the user explicitly changes a design constraint. Distinguish code changes, local validation, independent review, merge and deployment; one does not imply the others.
- Run `python -m pytest -q tests` for code changes. Do not restart or replace an existing production fleet as an incidental test.
- Generate request JSON with `json.dump()` / `json.dumps()` (or an equivalent serializer); never hand-build JSON containing long or multiline task descriptions. Let the serializer escape newlines, tabs, quotes, and other control characters. Before submitting a request, validate the actual file with `python3 -m json.tool <request.json> >/dev/null`; submit only if validation succeeds. On a JSON parsing error, fix the source/generator and revalidate; do not disable strict parsing or blindly resubmit.
