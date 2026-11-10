#!/usr/bin/env python3
"""上线验收（只读，沙盒内，cwd = ~/cmux-fleet）：cmux-fleet 批次集成版已到位。
协议：最后一行 JSON {"result": "pass|fail|defer", "reason": ...}，退出码 0/1/2 与之一致；只读目标根内文件。"""
import json
import sys
from pathlib import Path


def done(result, reason):
    print(json.dumps({"result": result, "reason": reason}, ensure_ascii=False))
    sys.exit({"pass": 0, "fail": 1, "defer": 2}[result])


need_files = ["cmux_fleet/fleet.py", "cmux_fleet/lifecycle.py", "cmux_fleet/config/fleet.yaml",
              "cmux_fleet/agents/supervisor.md"]
try:
    texts = {f: Path(f).read_text() for f in need_files}
except OSError as e:
    done("fail", "缺文件：%s" % e)
for f in [x for x in need_files if x.endswith(".py")]:
    try:
        compile(texts[f], f, "exec")
    except SyntaxError as e:
        done("fail", "语法错 %s：%s" % (f, e))
checks = [
    ("cmux_fleet/fleet.py", "--lifecycle"),
    ("cmux_fleet/fleet.py", "cli_change_ok"),
    ("cmux_fleet/fleet.py", "AskUserQuestion"),
    ("cmux_fleet/config/fleet.yaml", "hard_timeout_min: 150"),
    ("cmux_fleet/agents/supervisor.md", "Parallel by default"),
]
miss = ["%s 缺 %s" % (f, m) for f, m in checks if m not in texts[f]]
import yaml  # noqa: E402
walls_rules = "\n".join(str(v) for v in (yaml.safe_load(texts["cmux_fleet/config/fleet.yaml"])["walls"] or {}).values())
if "rate limited" in walls_rules.lower():
    miss.append("fleet.yaml#walls 仍含通用 rate limited 模式")
if miss:
    done("fail", "；".join(miss))
done("pass", "集成版文件齐、语法过、lifecycle / 夜间换 CLI / 禁弹框 / 150 分钟 / 并发派活 / 墙表收窄 均在位")
