#!/usr/bin/env python3
"""上线验收（只读，沙盒内，cwd = ~/cmux-fleet）：Claude worker 的 --settings 文件带 bypass 同意。

macmini 板块轮动 2026-10-30..11-06：开 advisor 的 Claude worker 带 --setting-sources project 起，读不到
~/.claude/settings.json 里的 skipDangerousModePermissionPrompt，停在 Bypass Permissions 确认框直到启动超时。
本脚本用现场 cmux_fleet 生成监工 / worker / advised worker 三种槽的启动参数（打桩到 execvp 为止，不连 Redis、
不起 Claude、只写本次临时目录），断言每个带 --dangerously-skip-permissions 的槽，其 --settings 文件里
skipDangerousModePermissionPrompt 为 true。
Claude Code 版本（>= 2.1.280）沙盒里读不到（真实 HOME 整棵禁读），不在这里查，入队前已另行核对。
协议：最后一行 JSON {"result": "pass|fail|defer", "reason": ...}，退出码 0/1/2 与之一致。"""
import json
import os
import sys
import tempfile
from pathlib import Path


def done(result, reason):
    print(json.dumps({"result": result, "reason": reason}, ensure_ascii=False))
    sys.exit({"pass": 0, "fail": 1, "defer": 2}[result])


try:
    from cmux_fleet import fleet
except Exception as e:  # noqa: BLE001 — any import failure is a failed rollout
    done("fail", "现场 cmux_fleet 导入失败：%r" % e)


def launch_argv(work, slot, extra):
    """fleet.actor 跑到 execvp 为止，返回它要 exec 的 argv。"""
    cfg = dict(run=str(work / "run"), workers=["worker-1"], supervisor_cli="claude", worker_cli="claude",
               claude="/nonexistent/claude", python=sys.executable, **extra)
    path = work / "config.json"
    fleet.save(path, cfg)
    fleet.agent_cwd(cfg, slot).mkdir(parents=True, exist_ok=True)

    class R:
        def hgetall(self, key):
            return {"generation": "0", "state": "starting", "launch_ready": "1", "label": slot}
    seen = []

    def execvp(prog, argv):
        seen.append(argv)
        raise SystemExit(0)
    fleet.connect = lambda c: R()
    fleet.trust_claude_dir = lambda cwd: None  # 不碰 ~/.claude.json
    fleet.os.chdir = lambda p: None
    fleet.os.execvp = execvp
    try:
        fleet.actor(path, slot, "prompt", 0)
    except SystemExit:
        pass
    if not seen:
        raise RuntimeError("actor 没走到 execvp")
    return seen[0]


cases = [("supervisor", {}), ("worker-1", {}), ("worker-1", {"advisor": True})]
miss = []
for i, (slot, extra) in enumerate(cases):
    label = slot + (" + advisor" if extra else "")
    work = Path(tempfile.mkdtemp(prefix="bypass-check-%d-" % i))
    try:
        argv = launch_argv(work, slot, extra)
        if "--dangerously-skip-permissions" not in argv:
            miss.append(label + " 没带 --dangerously-skip-permissions")
            continue
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
    except Exception as e:  # noqa: BLE001
        miss.append("%s 生成启动参数失败：%r" % (label, e))
        continue
    if settings.get("skipDangerousModePermissionPrompt") is not True:
        miss.append(label + " 的 --settings 文件缺 skipDangerousModePermissionPrompt: true")
    if extra.get("advisor") and ("--setting-sources" not in argv
                                 or argv[argv.index("--setting-sources") + 1:][:1] != ["project"]):
        miss.append(label + " 没带 --setting-sources project（验收没走到出事的那种启动）")
if miss:
    done("fail", "；".join(miss))
done("pass", "监工 / worker / advised worker 三种槽的 --settings 文件都带 skipDangerousModePermissionPrompt: true")
