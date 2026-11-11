"""pi extension extensions/session-board.ts: delivery decision + event wiring against a fake `pi`.

The extension has no runtime imports from pi packages, so plain `node --experimental-strip-types`
loads it. The real pi runtime (sendMessage queueing) is not exercised here."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / 'extensions/session-board.ts'

PROBE = r'''
const mod = await import(process.argv[2]);
const out = {};
const d = mod.deliveryOptions;
out.decide = {
  fleetIdle: d({ fleet: true, idle: true, phase: "timer" }),
  fleetBusy: d({ fleet: true, idle: false, phase: "tool" }),
  idle: d({ fleet: false, idle: true, phase: "timer" }),
  busy: d({ fleet: false, idle: false, phase: "tool" }),
  input: d({ fleet: false, idle: true, phase: "input" }),
};
out.fleet = {
  v2: mod.isFleetManaged({ FLEETMUX_PI_SLOT: "worker-1" }),
  bridge: mod.isFleetManaged({ FLEETMUX_PI_BRIDGE: "[]" }),
  v1: mod.isFleetManaged({ FLEETMUX_PI_CALLBACK: "[]" }),
  plain: mod.isFleetManaged({ HOME: "/h" }),
};

async function session(env) {
  for (const k of ["PI_SESSION_BOARD", "FLEETMUX_PI_SLOT", "FLEETMUX_PI_BRIDGE", "FLEETMUX_PI_CALLBACK"]) delete process.env[k];
  Object.assign(process.env, env, { PI_SESSION_BOARD_SCRIPT: "/x/session_board.py", PI_SESSION_BOARD_INTERVAL_MS: "40" });
  const handlers = {}, sent = [], execs = [];
  let reply = "", idle = false;
  const pi = {
    on: (name, h) => { handlers[name] = h; },
    exec: async (cmd, args) => { execs.push([cmd, ...args]); return { stdout: reply, stderr: "", code: 0 }; },
    sendMessage: (msg, opts) => sent.push([msg.content, opts ?? null]),
    getSessionName: () => "bash-3",
  };
  mod.default(pi);
  if (!handlers.session_start) return { registered: false };
  const ctx = { cwd: "/w", hasUI: false, isIdle: () => idle, sessionManager: { getSessionId: () => "sid1" } };
  await handlers.session_start({ reason: "startup" }, ctx);
  await handlers.session_start({ reason: "reload" }, ctx);          // no second timer
  reply = "warn-busy";
  await handlers.tool_result({ toolName: "edit", input: { path: "a.py" }, isError: false }, ctx);
  idle = true; reply = "warn-idle";
  await handlers.tool_result({ toolName: "bash", input: { command: "git commit -m x" } }, ctx);
  reply = "";
  await handlers.tool_result({ toolName: "read", input: { path: "a.py" } }, ctx);
  reply = "warn-input";
  await handlers.input({ text: "goal here", source: "interactive" }, ctx);
  reply = "warn-timer";
  await new Promise((r) => setTimeout(r, 150));
  await handlers.session_shutdown({ reason: "quit" }, ctx);
  const payloads = execs.map((e) => JSON.parse(e.at(-1)));
  return { registered: true, sent, cmd: execs[0].slice(0, 4), payloads };
}
out.plain = await session({});
out.managed = await session({ FLEETMUX_PI_SLOT: "worker-1" });
out.off = await session({ PI_SESSION_BOARD: "0" });
console.log(JSON.stringify(out));
'''


def test_delivery_decision_and_wiring(tmp_path):
    node = shutil.which('node') or pytest.skip('Node required')
    shutil.copy(EXT, tmp_path / 'session-board.ts')
    probe = tmp_path / 'probe.mjs'
    probe.write_text(PROBE)
    run = subprocess.run([node, '--experimental-strip-types', str(probe), str(tmp_path / 'session-board.ts')],
                         capture_output=True, text=True, timeout=30, cwd=tmp_path)  # hangs if a timer survives shutdown
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout.strip().splitlines()[-1])

    assert got['decide'] == {
        'fleetIdle': {'deliverAs': 'nextTurn'}, 'fleetBusy': {'deliverAs': 'nextTurn'},
        'idle': {'deliverAs': 'steer', 'triggerTurn': True}, 'busy': {'deliverAs': 'steer'},
        'input': {'deliverAs': 'nextTurn'},
    }
    assert got['fleet'] == {'v2': True, 'bridge': True, 'v1': True, 'plain': False}

    plain = got['plain']
    assert plain['cmd'] == ['python3', '/x/session_board.py', 'hook', 'pi']
    events = [p['event'] for p in plain['payloads']]
    assert events[:2] == ['start', 'start'] and events[-1] == 'end'
    assert 'check' in events                                     # the 30 s timer (40 ms here) ran
    first = plain['payloads'][0]
    assert first['session_id'] == 'sid1' and first['cwd'] == '/w' and first['name'] == 'bash-3'
    assert isinstance(first['pid'], int)
    tools = [p for p in plain['payloads'] if p['event'] == 'tool']
    assert tools[0]['path'] == 'a.py' and tools[1]['command'] == 'git commit -m x'
    assert len(tools) == 2                                       # read is not recorded
    assert any(p['event'] == 'prompt' and p['prompt'] == 'goal here' for p in plain['payloads'])
    sent = {text: opts for text, opts in plain['sent']}
    assert sent['warn-busy'] == {'deliverAs': 'steer'}
    assert sent['warn-idle'] == {'deliverAs': 'steer', 'triggerTurn': True}
    assert sent['warn-input'] == {'deliverAs': 'nextTurn'}
    assert sent['warn-timer'] == {'deliverAs': 'steer', 'triggerTurn': True}

    managed = got['managed']
    assert managed['sent'] and all(opts == {'deliverAs': 'nextTurn'} for _, opts in managed['sent'])

    assert got['off'] == {'registered': False}
