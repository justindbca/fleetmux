// cmux-fleet TUI half of the bridge plugin (opencode v2 TUI plugin API: export default {id, setup(api)}).
// opencode 2.0.30 loads `<plugin dir>/tui.js` into the TUI for a local server plugin directory that has one
// (server: plugin entrypoints {server, tui} -> features.tui; TUI: loads the tui entrypoint, calls setup(api)).
// Job: show the session the server half (index.js) created and bound, by its id, so the tab is that session and
// typed lines go into it. --continue alone is not enough: the TUI lists the newest session ONCE at start and stays
// on the home screen when the plugin's session is not there yet (P1-01, 2.0.30 E2E C3/C4/R3/R4).
// The id comes from the server half's activity log `<session>.jsonl` (first line {bound: <session>}) in
// CMUX_FLEET_OC_SESSION_DIR (one per generation). Navigation only: no input, no prompt, no other process.
import { readdirSync, readFileSync } from "node:fs"
import { join } from "node:path"

const POLL_MS = 200
const WAIT_MS = 120000

/** The one session the server half bound in `dir`, or null (none yet, unreadable, or more than one). */
export function boundSession(dir) {
  let names = []
  try { names = readdirSync(dir) } catch { return null }
  const ids = []
  for (const name of names) {
    const m = /^(ses_[A-Za-z0-9]{6,64})\.jsonl$/.exec(name)
    if (!m) continue
    try { if (JSON.parse(readFileSync(join(dir, name), "utf8").split("\n", 1)[0]).bound === m[1]) ids.push(m[1]) } catch {}
  }
  return ids.length === 1 ? ids[0] : null
}

export default {
  id: "cmux-fleet.bridge.tui",
  setup(api) {
    const dir = process.env.CMUX_FLEET_OC_SESSION_DIR || ""
    if (!dir) return
    let timer = null, done = false
    const deadline = Date.now() + WAIT_MS
    const tick = () => {
      if (done) return
      const sid = boundSession(dir)
      if (!sid) { if (Date.now() < deadline) timer = setTimeout(tick, POLL_MS); return }
      done = true
      const route = api.ui.router.current()
      if (route?.type !== "session" || route.sessionID !== sid) api.ui.router.navigate({ type: "session", sessionID: sid })
    }
    tick()
    return () => { done = true; clearTimeout(timer) }
  },
}
