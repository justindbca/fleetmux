#!/usr/bin/env bash
# Smoke for the transport v2 Claude supervisor adapter (route 2.0b; A3).
#
#   scripts/smoke_claude_supervisor.sh                 offline: pair gate + adapter regression tests
#   scripts/smoke_claude_supervisor.sh --live pi       real cmux: Claude supervisor + 2 Pi workers (task)
#   scripts/smoke_claude_supervisor.sh --live claude   real cmux: Claude supervisor + 2 Claude workers (task)
#
# --live must run inside a cmux terminal and uses real models (Claude subscription; Pi needs
# PI_MODEL=provider/model). It uses an isolated FLEETMUX_HOME / FLEETMUX_STATE under
# $SMOKE_DIR, requires the --lifecycle start flag (feature D) for these unverified pairs, and
# always runs `stop` at exit. Passing it is the evidence for flipping fleet.yaml#cli_pairs to verified
# (a human edits status/verified_by). Env: SUPERVISOR_MODEL (default opus), WORKER_MODEL
# (default sonnet), PI_MODEL, SMOKE_TIMEOUT seconds (default 900), CLAUDE / PI binaries.
set -euo pipefail
export DISABLE_AUTOUPDATER=1  # every Claude Code this smoke starts never self-updates (10/14 01:36 broken shared install)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
cd "$ROOT"

offline() {
  "$PY" - <<'EOF'
from fleetmux import cli_pairs
for worker, mode in (('pi', 'task'), ('pi', 'service'), ('claude', 'task')):
    cfg = dict(supervisor_cli='claude', worker_cli=worker, mode=mode)
    row = cli_pairs.load_pairs().lookup('claude', worker, mode)
    assert row and row['adapter_missing'] == [], (worker, mode, row)
    if row['status'] == 'verified':   # flipped by a real smoke; verified_by names the run
        assert row.get('verified_by') and cli_pairs.pair_problem(cfg) is None, row
    else:
        assert cli_pairs.pair_problem(cfg), 'unverified pair must need smoke'
    assert cli_pairs.pair_problem(dict(cfg, lifecycle='smoke')) is None
    print('gate ok: claude+%s/%s status=%s verified_by=%s' % (worker, mode, row['status'], row.get('verified_by')))
EOF
  "$PY" -m pytest -q tests/test_claude_supervisor.py tests/test_cli_pairs.py
}

live() {
  local worker="$1"
  [[ "$worker" == pi || "$worker" == claude ]] || { echo "usage: $0 --live pi|claude" >&2; exit 2; }
  "$PY" -m fleetmux.fleet start --help | grep -q -- '--lifecycle' || {
    echo "BLOCKED: this branch has no 'start --lifecycle' (feature D); unverified pairs start only with --lifecycle smoke" >&2
    exit 3; }
  SMOKE_DIR="${SMOKE_DIR:-$(mktemp -d /tmp/cf-claude-sup.XXXXXX)}"
  unset FLEETMUX_HOME FLEETMUX_STATE FLEETMUX_CONFIG  # §50: the new names would win over the isolated ones
  export FLEETMUX_HOME="$SMOKE_DIR/home" FLEETMUX_STATE="$SMOKE_DIR/state"
  mkdir -p "$FLEETMUX_HOME" "$FLEETMUX_STATE"
  local args=(start --transport-version 2 --allow-development-v2 --lifecycle smoke --mode task --workers 2
              --supervisor-cli claude --supervisor-model "${SUPERVISOR_MODEL:-opus}" --worker-cli "$worker"
              --runs-dir "$SMOKE_DIR/runs" --redis-runtime-dir "$SMOKE_DIR/redis" --name claude-sup-smoke)
  [[ -n "${CLAUDE:-}" ]] && args+=(--claude "$CLAUDE")
  if [[ "$worker" == pi ]]; then
    [[ -n "${PI_MODEL:-}" ]] || { echo "PI_MODEL=provider/model is required for --live pi" >&2; exit 2; }
    args+=(--pi-model "$PI_MODEL" --allow-unsandboxed-pi)
    [[ -n "${PI:-}" ]] && args+=(--pi "$PI")
  else
    args+=(--worker-model "${WORKER_MODEL:-sonnet}")
  fi
  trap '"$PY" -m fleetmux.fleet stop --config "$FLEETMUX_HOME/latest.json" >/dev/null 2>&1 || true' EXIT
  "$PY" -m fleetmux.fleet "${args[@]}"
  local run
  run="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["run"])' "$FLEETMUX_HOME/latest.json")"
  echo "run: $run"
  # READY gate: supervisor + 2 workers idle (after READY) before the task goes in.
  local deadline=$((SECONDS + 300))
  until "$PY" -m fleetmux.fleet status --config "$FLEETMUX_HOME/latest.json" 2>/dev/null \
      | "$PY" -c 'import json,sys; s=json.load(sys.stdin).get("slots") or {}; sys.exit(0 if len(s) == 3 and all(v.get("state")=="idle" for v in s.values()) else 1)'; do
    (( SECONDS < deadline )) || { echo "FAIL: not all slots READY within 300s" >&2; exit 1; }
    sleep 5
  done
  echo "all slots READY"
  "$PY" -m fleetmux.fleet task - <<'EOF'   # reads FLEETMUX_HOME/latest.json
{"goal": "Have worker-1 write worker-1/ok.txt containing exactly OK, then finish.", "hours": 0.25,
 "artifacts": [{"path": "worker-1/ok.txt", "contains": ["OK"]}]}
EOF
  deadline=$((SECONDS + ${SMOKE_TIMEOUT:-900}))
  until [[ -s "$run/result.json" ]]; do
    (( SECONDS < deadline )) || { echo "FAIL: no result.json within ${SMOKE_TIMEOUT:-900}s" >&2; exit 1; }
    sleep 5
  done
  cat "$run/result.json"
  "$PY" -c 'import json,sys; r=json.load(open(sys.argv[1])); sys.exit(0 if r.get("status")=="completed" else 1)' "$run/result.json" \
    || { echo "FAIL: task did not complete" >&2; exit 1; }
  if find "$run" \( -name .inbox -o -name 'msg-*.md' \) | grep -q .; then
    echo "FAIL: message files exist (v2 must not use the file mailbox)" >&2; exit 1
  fi
  echo "PASS: claude+$worker task smoke completed; record the run id and date in fleet.yaml#cli_pairs verified_by"
}

case "${1:-}" in
  ""|--offline) offline ;;
  --live) live "${2:-}" ;;
  *) echo "usage: $0 [--offline | --live pi|claude]" >&2; exit 2 ;;
esac
