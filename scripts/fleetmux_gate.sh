#!/bin/bash
# fleetmux delivery gate. Usage: scripts/fleetmux_gate.sh p0|a|m0|m12|m25|m26|m3|m4 [--collect-only]
#   m4 --collect-only = phase A collect mode, NOT acceptance (brief-m4 §4, m4-design §10); m4 = full acceptance gate.
set -euo pipefail
USAGE='p0|a|m0|m12|m25|m26|m3|m4 [--collect-only]'
# Arguments are checked before anything is built: an unknown phase or argument used to exit 0 (m4-design §10).
[ $# -ge 1 ] || { echo "fleetmux_gate: missing phase (usage: $USAGE)" >&2; exit 2; }
phase=$1
case $phase in
  p0|a|m0|m12|m25|m26|m3|m4) ;;
  *) echo "fleetmux_gate: unknown phase '$phase' (usage: $USAGE)" >&2; exit 2 ;;
esac
collect_only=0
if [ $# -ge 2 ]; then
  if [ $# -eq 2 ] && [ "$phase" = m4 ] && [ "$2" = --collect-only ]; then
    collect_only=1
  else
    shift
    echo "fleetmux_gate: unexpected argument(s) for phase '$phase': $* (only 'm4 --collect-only' takes one)" >&2; exit 2
  fi
fi
COLLECT_BANNER='COLLECT_ONLY / NOT_ACCEPTANCE: fleetmux_gate m4 --collect-only (brief-m4 §4 phase A)'
if [ $collect_only = 1 ]; then echo "$COLLECT_BANNER"; fi
cd "$(dirname "$0")/.."
export PATH=$HOME/.cargo/bin:$PATH
export FLEETMUX_GATE=$phase
PY="${PYTHON:-python3.14} -m pytest -q -p no:cacheprovider"
WIRING="tests/test_backend_interface.py tests/test_fleetmux_fleet_wiring.py"
RED_PY="tests/test_fleetmux_backend.py tests/test_fleetmux_fresh_manager.py tests/test_history_view.py \
  tests/test_fleetmux_start_attach.py tests/test_install_fleetmux.py tests/test_fleetmux_default_switch.py"
COMMON_PY="$WIRING tests/test_backend_selection.py tests/test_check_deps.py tests/test_subfleet.py \
  tests/test_fleetmux_backend.py tests/test_fleetmux_fresh_manager.py tests/test_history_view.py tests/test_fleetmux_start_attach.py"
M12_BINS="--test mouse --test scroll --test views --test takeover --test paths --test attach_cli"
M3_BINS="$M12_BINS --test replay --test stats --test record --test web_relay --test attach_peer \
  --test overlay --test gui_client"   # web_relay: M2.5 (brief-m25 §3.7); attach_peer: task8 fix-m25-2; overlay / gui_client: M2.6 (brief-m26 §3.7)
TERM_M12_BINS="--test color"                              # fleetmux-term ignored M1 tests
TERM_M3_BINS="--test color --test parser_contract"        # + M3 wide-char / memory
LAYOUT_BINS="--test hit"                                  # fleetmux-layout ignored M1 tests
export FLEETMUX_REAL_BIN=$PWD/rust/target/debug/fleetmux
# M4 (brief-m4, m4-design v0.7 §8 / §10). Every file below is required: the m4 gate never skips a missing one.
M4_BINS="$M3_BINS --test pages"                           # crates/app/tests/pages.rs (#[ignore = "m4"])
M4_WEB_TEST=rust/crates/web/tests/m4_pages.rs         # runs inside the m3 `cargo test -p fleetmux-web -- --include-ignored`
M4_SHIM=rust/crates/probe/examples/fake_cli_shim.rs   # e2e fake CLI shim (ft-probe example, no Cargo change)
M3_PY="$COMMON_PY tests/test_install_fleetmux.py tests/test_fleetmux_default_switch.py tests/test_redact_recording.py"
# cmux backend sub-fleets do not regress, WezTerm still refuses (brief-m4 §3 item 14); test_subfleet.py is in COMMON_PY
NOREGRESS_PY="tests/test_subfleet_tmux.py tests/test_subfleet_named_tmux.py tests/test_named_subfleet_resume.py \
  tests/test_wezterm_fleet_wiring.py tests/test_tmux_view.py tests/test_escalate.py tests/test_service_escalate.py \
  tests/test_kit_fleet.py tests/test_lifecycle_close.py tests/test_fleetmux_sidebar.py tests/test_fleetmux_history.py"
M4_PY="tests/test_fleetmux_pages.py tests/test_fleetmux_tmux_view.py tests/test_subfleet_fleetmux.py"
M4_FAKE_KILL_PY="tests/test_fake_fleetmux_kill.py"   # fake server kill_group under a transient EPERM (fix-m4, bbdc18ca)
M4_DRAIN_PY="tests/test_m4_drain_s_contract.py"   # named sub-fleet down waits the parent's declared drain_s (brief §7.2, M43)
M4_TMUX_CLEANUP_PY="tests/test_m4_tmux_cleanup.py"   # fleet stop removes its own dead tmux socket, both backends (brief §7.1, M42)
M4_E2E="tests/test_m4_fake_cli.py tests/test_m4_e2e_hygiene.py tests/test_m4_e2e_studio_ops.py"   # real E2 (brief-m4 §5 item 1)
M4_REQUIRED="rust/crates/app/tests/pages.rs $M4_WEB_TEST $M4_SHIM tests/m4e $M4_PY $M4_E2E"
m4_require() {   # list every missing M4 test file, then fail
  local f missing=0
  for f in $M4_REQUIRED; do
    if [ ! -e "$f" ]; then echo "fleetmux_gate: missing M4 test file: $f" >&2; missing=$((missing + 1)); fi
  done
  if [ $missing -gt 0 ]; then echo "fleetmux_gate: $missing M4 test file(s) missing; the m4 gate does not skip them" >&2; exit 3; fi
}
py_no_skip() {   # pytest that may not skip anything: in M4 tests a missing dependency is a FAIL (m4-design §8.4 item 6)
  local skipped
  skipped=$( { $PY -rs "$@" 2>&1; } | tee /dev/stderr | awk '/^SKIPPED/ { n++ } END { print n + 0 }' )
  if [ "$skipped" != 0 ]; then echo "fleetmux_gate: $skipped SKIPPED summary line(s) in $* (the m4 gate allows no skip)" >&2; exit 1; fi
}
# --- m4 evidence roots (both m4 modes: supervisor [M29] lets collect mode create the same kind of kept root) ---
# deliver.test_cmd runs the gate with no extra environment, so the full m4 picks a kept root itself:
# M4E_ROOT defaults to $HOME/.m4e/fm4-integ (an explicit M4E_ROOT may override it, never a rotating temp dir);
# FM4_TEST_ROOT is always a new short directory under it. Nothing is reused or deleted.
M4_SOCK_MAX=103     # macOS sockaddr_un path limit (bytes), used for every M4 test socket
# Longest socket path below M4E_ROOT, in bytes including the leading '/', from the final fixtures (833d70c5 / d3f17df8):
#   e2e child tmux  /<6hex id>/runs/fleet-YYYYmmdd-HHMMSS-<8hex>/sub/mayfly/tmux.sock     = 1+6+6+30+21 = 64
#                   (run dir name and its uuid4().hex[:8] token: fleetmux/fleet.py:7532 / :7537; measured 92 B at 28 B)
#   Python          /<tag>-<8hex>/state/fleetmux/<session token>/ctl.sock, …/fleet-…/tmux.sock   = 53 measured
#   Rust            /r.XXXXXX/<pid>-<seq>/rt/ctl.sock = 9+24 (7-digit Linux pid); web /r.XXXXXX/w<pid>-<n>/w/web.sock <= 9+22
M4_SUFFIX_MAX=64    # the e2e child tmux socket is the longest, so M4E_ROOT may be at most 103 - 64 = 39 bytes
m4_is_temp() {      # rotating / cleaned temp locations (macOS periodic, tmpfs): evidence must not live there
  local t
  for t in /tmp /private/tmp /var/tmp /private/var/tmp /var/folders /private/var/folders /dev/shm /run/user ${TMPDIR:+"${TMPDIR%/}"}; do
    case $1 in "$t" | "$t"/*) return 0 ;; esac
  done
  return 1
}
m4_root_ok() {      # $1 = candidate M4E_ROOT; a temp location or a too-long path is exit 3
  local n
  if m4_is_temp "$1"; then
    echo "fleetmux_gate: M4E_ROOT '$1' is a rotating temp location; evidence must be kept (e.g. \$HOME/.m4e/fm4-integ)" >&2; exit 3
  fi
  n=$(printf %s "$1" | LC_ALL=C wc -c | tr -d ' ')
  if [ $((n + M4_SUFFIX_MAX)) -gt $M4_SOCK_MAX ]; then
    echo "fleetmux_gate: M4E_ROOT '$1' is $n bytes; socket paths need it <= $((M4_SOCK_MAX - M4_SUFFIX_MAX)) bytes" >&2; exit 3
  fi
}
m4_roots() {
  local root=${M4E_ROOT:-$HOME/.m4e/fm4-integ} real
  case $root in /*) ;; *) echo "fleetmux_gate: M4E_ROOT must be an absolute path, got '$root'" >&2; exit 3 ;; esac
  root=${root%/}
  m4_root_ok "$root"
  mkdir -p -m 700 "$root"
  real=$(cd "$root" && pwd -P)
  m4_root_ok "$real"   # again after symlinks are resolved
  if [ -n "${FM4_TEST_ROOT:-}" ]; then echo "fleetmux_gate m4: preset FM4_TEST_ROOT=$FM4_TEST_ROOT not used (every run gets a new one)"; fi
  FM4_TEST_ROOT=$(mktemp -d "$real/r.XXXXXX")
  export M4E_ROOT=$real FM4_TEST_ROOT
  echo "fleetmux_gate m4: evidence roots (kept, never deleted): M4E_ROOT=$M4E_ROOT FM4_TEST_ROOT=$FM4_TEST_ROOT"
}
# Every pytest call of m4 (full and --collect-only) gets its own --basetemp leaf inside FM4_TEST_ROOT ([M29]): the repo
# conftest's autouse tmp_path would otherwise land in pytest's rotating system temp dir, and pytest deletes an
# existing --basetemp, so a leaf that already exists is a failure. The leaf is never created, reused or cleaned here.
M4_PT_N=0
m4_pt_leaf() {      # sets M4_PT_LEAF; call it in this shell (not in $(...)) so the counter advances
  M4_PT_N=$((M4_PT_N + 1))
  M4_PT_LEAF=$FM4_TEST_ROOT/pt-$M4_PT_N
  if [ -e "$M4_PT_LEAF" ] || [ -L "$M4_PT_LEAF" ]; then
    echo "fleetmux_gate m4: pytest basetemp $M4_PT_LEAF already exists; pytest would delete it, so the gate stops" >&2; exit 3
  fi
  echo "fleetmux_gate m4: pytest basetemp (new, kept) $M4_PT_LEAF"
}
# --- end m4 evidence roots ---
if [ "$phase" = m4 ]; then   # before building: report missing files / dependencies first
  m4_require
  if [ $collect_only = 0 ]; then
    if ! command -v redis-server >/dev/null; then
      echo "fleetmux_gate: redis-server not on PATH (the m4 e2e needs it)" >&2; exit 3
    fi
  fi
  m4_roots   # both modes: collect mode's pytest calls need a kept basetemp too (it is still NOT acceptance)
fi
( cd rust && cargo --version ) | grep -q 1.99.1
( cd rust && cargo build --workspace --locked && cargo deny check licenses )
case $phase in
  p0|a)
    ( cd rust && cargo test --workspace --no-run )
    $PY --collect-only $RED_PY
    $PY $WIRING tests/test_backend_selection.py tests/test_check_deps.py tests/test_subfleet.py ;;   # 97 条在 e474887d 已绿，阶段 A 必须保持
  m0)
    ( cd rust && cargo test --workspace )
    $PY $COMMON_PY ;;
  m12)
    ( cd rust && cargo test --workspace && cargo test -p fleetmux $M12_BINS -- --include-ignored \
      && cargo test -p fleetmux-term $TERM_M12_BINS -- --include-ignored && cargo test -p fleetmux-layout $LAYOUT_BINS -- --include-ignored )
    $PY $COMMON_PY tests/test_install_fleetmux.py ;;
  m25)   # M2.5 web window (brief-m25 §3.7 item 2). pytest pins FLEETMUX_GATE=m3: RANK in tests/_fleetmux_milestone.py has no m25
    ( cd rust && cargo test -p fleetmux-web -- --include-ignored && cargo test -p fleetmux-proto --test codec -- --include-ignored \
      && cargo test -p fleetmux --test web_relay --test attach_cli --test attach_peer -- --include-ignored )
    FLEETMUX_GATE=m3 $PY tests/test_fleetmux_backend.py tests/test_check_deps.py tests/test_install_fleetmux.py ;;
  m26)   # M2.6 GUI viewer (brief-m26 §3.7). pytest pins FLEETMUX_GATE=m3: RANK in tests/_fleetmux_milestone.py has no m26
    ( cd rust && cargo test -p fleetmux-web -- --include-ignored && cargo test -p fleetmux-proto --test codec -- --include-ignored \
      && cargo test -p fleetmux --test overlay --test gui_client --test web_relay --test attach_cli --test attach_peer -- --include-ignored )
    FLEETMUX_GATE=m3 $PY tests/test_fleetmux_backend.py tests/test_fleetmux_start_attach.py tests/test_check_deps.py tests/test_install_fleetmux.py ;;
  m3)
    ( cd rust && cargo test --workspace && cargo test -p fleetmux $M3_BINS -- --include-ignored \
      && cargo test -p fleetmux-term $TERM_M3_BINS -- --include-ignored && cargo test -p fleetmux-layout $LAYOUT_BINS -- --include-ignored \
      && cargo test -p fleetmux-web -- --include-ignored && cargo test -p fleetmux-proto --test codec -- --include-ignored )
    $PY $COMMON_PY tests/test_install_fleetmux.py tests/test_fleetmux_default_switch.py tests/test_redact_recording.py ;;
  m4)    # pytest runs at FLEETMUX_GATE=m4 (RANK m4:4), never pinned to m3: that would skip every @milestone(4) test
    if [ $collect_only = 1 ]; then   # proves only: red tests collect, Rust compiles, the green phase A set stays green
      ( cd rust && cargo test --workspace --no-run && cargo build -p ft-probe --example fake_cli_shim --locked )
      m4_pt_leaf; $PY --basetemp="$M4_PT_LEAF" --collect-only $M4_PY $M4_E2E
      m4_pt_leaf; $PY --basetemp="$M4_PT_LEAF" $WIRING tests/test_backend_selection.py tests/test_check_deps.py tests/test_subfleet.py
      echo "$COLLECT_BANNER"
    else
      echo "fleetmux_gate m4: full acceptance gate; M4E_ROOT=$M4E_ROOT FM4_TEST_ROOT=$FM4_TEST_ROOT"
      # Supervisor [M37]: every Rust test of the full m4 runs one test at a time (RUST_TEST_THREADS=1, read by libtest
      # only: cargo builds stay parallel, pytest is outside this subshell). Tests that shell out to a --version stub
      # with a timeout (gui_client, app lib version_must_match_exactly) time out under parallel load. Nothing is
      # skipped or retried; the 7200 s deliver budget and the 5400 s report threshold are unchanged.
      ( cd rust && export RUST_TEST_THREADS=1 && cargo test --workspace && cargo test -p fleetmux $M4_BINS -- --include-ignored \
        && cargo test -p fleetmux-term $TERM_M3_BINS -- --include-ignored && cargo test -p fleetmux-layout $LAYOUT_BINS -- --include-ignored \
        && cargo test -p fleetmux-web -- --include-ignored && cargo test -p fleetmux-proto --test codec -- --include-ignored \
        && cargo build -p ft-probe --example fake_cli_shim --locked )
      m4_pt_leaf; $PY --basetemp="$M4_PT_LEAF" $M3_PY $NOREGRESS_PY
      m4_pt_leaf; py_no_skip --basetemp="$M4_PT_LEAF" $M4_PY $M4_FAKE_KILL_PY $M4_DRAIN_PY $M4_TMUX_CLEANUP_PY
      m4_pt_leaf; py_no_skip --basetemp="$M4_PT_LEAF" $M4_E2E
    fi ;;
  *) echo "fleetmux_gate: unknown phase '$phase'" >&2; exit 2 ;;
esac
