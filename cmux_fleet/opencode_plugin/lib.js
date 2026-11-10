// Pure helpers of the cmux-fleet opencode plugin. Kept out of index.js: the opencode v2 loader
// treats named function exports of the plugin entry as v1 plugins (LoadError).

export function payload(ev) { return ev?.data ?? ev?.properties ?? {} }

/** Model lock: same rule as opencode_adapter.model_problem (strict cost: finite number, exactly 0). */
export function modelProblem(locked, info) {
  const [provider, ...rest] = String(locked).split("/"); const id = rest.join("/")
  if (!provider || !id) return "model_lock: no locked model configured"
  if (!info || typeof info !== "object") return "model_lock: no assistant message to check"
  const m = (info.model && typeof info.model === "object") ? info.model : {}
  const gotProvider = m.providerID ?? info.providerID
  const gotId = m.id ?? m.modelID ?? info.modelID
  if (gotProvider !== provider || gotId !== id) return `model_lock: expected ${provider}/${id}, got ${gotProvider}/${gotId}`
  const cost = info.cost
  if (typeof cost !== "number" || !Number.isFinite(cost) || cost !== 0) return `cost_nonzero: expected cost 0, got ${JSON.stringify(cost)}`
  return null
}

/** Same classes and order as opencode_adapter.classify_error (text) / classify_failure (error object). */
export function classifyError(text, status) {
  const t = String(text || "")
  if (/freetiererror|free tier can only be used/i.test(t)) return "free_tier_rejected"
  if (status === 429 || /rate[ -]?limit|too many requests|ratelimit/i.test(t)) return "rate_limited"
  if (/provider\.no-route|model unavailable|model not found|unknown model|no such model/i.test(t)) return "model_unavailable"
  if (t.startsWith("model_lock:") || t.startsWith("cost_nonzero:")) return "model_lock"
  if (/\baborted\b|\binterrupted\b|AbortError/i.test(t)) return "aborted"
  return "other"
}
/** execution.failed data.error = {type, message, status, response:{body}} (measured: provider.auth 403 FreeTierError, provider.no-route). */
export function classifyFailure(error, outcome) {
  if (!error || typeof error !== "object") {
    const text = String(error || outcome || "failed")
    return { cls: classifyError(text), text }
  }
  const text = [error.type, error.status, error.message, error.response?.body].filter((x) => x !== undefined && x !== null && x !== "").join(" | ")
  return { cls: classifyError(text, error.status), text: text || String(outcome || "failed") }
}

/** The assistant messages that answer one user message, from ctx.session.context(): the list
 *  [{id, type:"user"|"assistant"|"idle"|..., model, cost, tokens, content}] (measured). Starts after the
 *  user message whose id is the bound inbox id; stops at the next user or idle entry. null when not found. */
export function turnMessages(list, inboxId) {
  const items = Array.isArray(list) ? list : Array.isArray(list?.messages) ? list.messages : Array.isArray(list?.data) ? list.data : null
  if (!items) return null
  const at = items.findIndex((m) => m?.id === inboxId && (m.type ?? m.role) === "user")
  if (at < 0) return null
  const assistants = []
  for (const m of items.slice(at + 1)) {
    const kind = m?.type ?? m?.role
    if (kind === "user" || kind === "idle") break
    if (kind === "assistant") assistants.push({ ...m, text: (m.content ?? []).filter((c) => c?.type === "text").map((c) => c.text || "").join("") })
  }
  return { assistants }
}

/** ctx.model.list() returns {location, data: Model.Info[]} with {id, modelID, providerID, status, enabled, ...}
 *  (measured, opencode 2.0.29). true / false when readable, null when the shape is unknown. Disabled = absent. */
export function catalogHas(list, providerID, modelID) {
  const items = Array.isArray(list) ? list : Array.isArray(list?.data) ? list.data : null
  if (!items) return null
  return items.some((m) => m?.providerID === providerID && (m?.id ?? m?.modelID) === modelID && m?.enabled !== false)
}

// Mirrors watchdog.DELETE_MARKERS (moves / renames count as deletes). Strict: unreadable = delete.
const DELETE_WORDS = ["rm", "rmdir", "unlink", "del", "delete", "remove", "rimraf", "shred", "trash", "purge", "wipe",
  "erase", "truncate", "drop table", "drop database", "drop schema", "git clean", "git reset --hard", "git push --force",
  "git push -f", "git branch -d", "git checkout --", "git restore", "dd if=", "> /dev/null", ">/dev/null", "mv", "move",
  "rename", "move-item", "rename-item", "remove-item", "删除", "清空", "移除", "清理", "移动", "改名", "重命名"]
export const DELETE_WORD_LIST = DELETE_WORDS
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
const DELETE_RE = DELETE_WORDS.map((w) => {
  const body = /^[a-z]+$/.test(w) ? (w.endsWith("e") && w.length > 2 ? esc(w.slice(0, -1)) + "(?:e|es|ed|ing)" : esc(w) + "(?:s|es|ed|ing|d)?") : esc(w)
  const head = /^[a-z0-9_]/.test(w) ? "(?<![a-z0-9_])" : ""
  const tail = /[a-z0-9_]$/.test(w) ? "(?![a-z0-9_])" : ""
  return new RegExp(head + body + tail)
})
export function permissionText(req) {
  if (!req || typeof req !== "object") return ""
  // opencode 2.0.29 Permission.Request = {id, sessionID, action, resources[], metadata?, message?} (measured); older names kept.
  const parts = ["action", "message", "permission", "type", "title", "command"].map((k) => String(req[k] ?? ""))
  for (const k of ["resources", "patterns", "pattern"]) { const v = req[k]; Array.isArray(v) ? parts.push(...v.map(String)) : parts.push(String(v ?? "")) }
  if (req.metadata && typeof req.metadata === "object") for (const v of Object.values(req.metadata)) if (["string", "number"].includes(typeof v)) parts.push(String(v))
  return parts.filter(Boolean).join("\n")
}
export function permissionClass(req) {
  const t = permissionText(req).toLowerCase()
  if (!t.trim()) return "delete"
  return DELETE_RE.some((re) => re.test(t)) ? "delete" : "other"
}

/** Model lock over one execution's steps: Map(assistantMessageID -> {model (step.started), cost (step.ended), ended}).
 *  Empty map, a step without step.ended, another model, or a cost that is not a finite 0 -> problems. */
export function stepProblems(locked, steps) {
  if (!(steps instanceof Map) || steps.size === 0) return ["model_lock: no assistant step in this execution"]
  const out = []
  for (const [id, s] of steps) {
    if (!s.ended) { out.push(`model_lock: step ${id} has no step.ended`); continue }
    const p = modelProblem(locked, { model: s.model, cost: s.cost })
    if (p) out.push(p + ` (step ${id})`)
  }
  return out
}

/** Context fill = opencode's own measure: input + output + reasoning + cache.read + cache.write of ONE step
 *  (the last step.ended / last assistant message). Never pass session.usage.updated tokens (cumulative). */
export function usageOf(tokens) {
  if (!tokens || typeof tokens !== "object") return null
  const n = (v) => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : 0)
  return Math.round(n(tokens.input) + n(tokens.output) + n(tokens.reasoning) + n(tokens.cache?.read) + n(tokens.cache?.write))
}
