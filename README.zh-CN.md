# fleetmux

[![tests](https://github.com/justindbca/fleetmux/actions/workflows/tests.yml/badge.svg)](https://github.com/justindbca/fleetmux/actions/workflows/tests.yml)

[English](README.md) · **中文** · [Français](README.fr.md) · [Español](README.es.md)

**像管理容器一样管理 AI 团队。** 跑的是一支团队，而不是一个助理：fleetmux 把一个监工（supervisor）agent 和 N 个 worker agent 作为看得见的终端会话跑起来。
监工只判断、只派活；worker 只执行；任务什么时候算完成，由 daemon 判定，而不是由模型说了算。上下文快满的会话会写一份交接单，
换一个新会话接着干，所以一个任务可以跨很多个模型会话连续跑几个小时。同一个 fleet 里可以混用官方 **Claude Code**、官方
**Codex CLI**、**Pi**（Codex 模型与本地模型）和 **opencode**，任意「监工 × worker」组合均可，前提是该组合已通过真跑冒烟
（`fleetmux/config/fleet.yaml#cli_pairs`）。

**日常在用。** 2026 年 10 月起，它在三台 Mac 上以常驻服务 fleet 和定时任务 fleet 支撑一套实盘交易运作：运维团队负责其他团队的
起停，另有盯盘交易团队、对价格与指标触发作答的策略复盘团队、定时研究团队。每个团队都有 AI 组长和 AI 组员；盯盘交易与复盘两个
团队是运维团队的子 fleet。

## 项目经理，而不是秘书

个人助理类 agent 已经能派子 agent、跑定时任务、在模型之间故障切换，但子 agent 的结果仍交回模型自己判断，长任务靠上下文压缩撑着，
用哪个模型取决于调用方或配置写了什么。fleetmux 补上的是项目经理做的事：

1. **交付由程序验收。** 产物是否存在、是否包含必需内容、测试是否通过，由 daemon 判定，不采信模型的自述。
2. **会话可以无限接续。** 上下文接近上限时，会话写交接单，由新会话接替；卡死的会话原位重启；daemon 与监工互相看护。
3. **按能力矩阵派工。** 每次派工前，顾问依据能力矩阵（领域 × 任务类型 × 模型，每格附证据）选定模型与推理强度，所选结果须通过
   该矩阵的校验；撞上用量墙时，该槽位沿其角色的模型链换到下一个可用模型，或者暂停。
4. **全程可见。** 每个 agent 都是一个看得见的终端会话，随时可以旁观、随时可以介入。
5. **官方 CLI，你自己的登录。** 每个 agent 都在厂商自己的 CLI 里、以你本人的登录运行；不抽取凭证、不伪装客户端、不经代理转发
   API 调用。
6. **每个任务一份预算。** 预算卡限定调用次数、轮数、小时数和每个账号的额度；撞墙表决定是降一档模型，还是暂停运行、留待之后续跑。
7. **团队套团队。** 一个 worker 可以成为它自己子 fleet 的组长，因此一个常驻 fleet 能同时跑几条业务线，每条线按自己的时钟起停。

这件事为什么重要，是从我们自己的环境里学到的：我们跑过的一个助理网关，聊天通道断了几个月，外面没有任何东西发现，因为外面
根本没有东西在检查。在 fleet 里，每一次交付都由 daemon 检查。

作者 Justin Wang（多伦多），AI 系统工程师：[LinkedIn](https://www.linkedin.com/in/justinwangto) · [X](https://x.com/alaboluo)。
承接自由职业项目：AI 环境从零搭建、agent 运行框架调优、大模型能力挖掘、多 agent 协作编排。欢迎在 X 上私信联系。

[设计基线](docs/design.md)

**要点**

- **字面验收。** 只有声明的产物存在且包含必需内容，任务才结束；误报的 `COMPLETED` 会被计数，并会让任务判为阻塞。
- **上下文换代。** 上下文窗口用到可配置的比例时，会话写交接单，由接班者（`worker-1` → `worker-1.1`）在同一个标签里继续。
- **合盖续跑。** 合上笔记本，换个地方，打开，连上网，run 接着跑。daemon 分得清「机器睡了」和「连接断了」；启动时撞上网络错误会按
  退避间隔重试；网络没通之前不重启任何会话。
- **每个 run 一个私有 Redis**（只走 Unix socket，不开 TCP），上面跑一条有界的消息总线，每次投递都校验代数，退役会话的迟到回调
  永远不会生效。
- **看门与刹车。** 每个槽位有五态卡死检测；每个任务一张预算卡（调用次数、轮数、小时数、每个账号的额度），外加一张撞墙表：撞墙的
  槽位沿模型链往下换，或者可续跑地暂停。账号写在你私有配置文件的 `accounts` 块里，绝不进代码。
- **只有一个配置文件。** 所有表（CLI 组合、生命周期模式、撞墙表、看门对话框、模型卡、配置档、账号）都是
  `fleetmux/config/fleet.yaml` 里的一个块；同格式的私有 `~/.fleetmux/fleet.yaml` 覆盖运维方允许改的块。
- **官方 CLI，你自己的登录。** Claude Code 一律以官方 `claude` 交互界面、用你本人的登录运行，Pi 永远拿不到 Claude 凭证；Codex
  模型在官方 Codex CLI 里运行，或经 Pi 自己的 OpenAI 登录运行。fleetmux 不抽取或转发凭证、不伪装客户端、不经代理转发 API 调用。
- **子 fleet。** 一个 worker 可以成为它自己子 fleet 的监工。
- **2,000+ 个测试**（内存 Redis、假 cmux、合成会话记录；关键处用真 Redis）。

```
 cmux workspace "Fleet agents" (one page)
 ┌───────────────────────────────┬────────────────────────┐
 │ supervisor-1.2                │ [worker-1.1] [worker-2] │
 │ supervisor TUI (selected)     │ worker tabs            │
 │                               │                        │
 ├───────────────────────────────┤ selected worker's TUI  │
 │ [history] [daemon]            │                        │
 │ retired screens + handoffs    │                        │
 └───────────────────────────────┴────────────────────────┘
 daemon: the fleet daemon, private Redis socket, routing, verification and watchdog.
```

左侧是监工的终端界面（当前选中）；右侧是 worker 标签，下方显示所选 worker 的界面；左下的 `history` / `daemon` 标签存放退役会话的
屏幕与交接单，以及 fleet daemon（私有 Redis socket、路由、核验与看门）。

## 应用场景

每次 `start` 都要指定 `--lifecycle`，它规定「完成」的含义以及运行如何收尾（`fleetmux/config/fleet.yaml#lifecycle_modes`）。
下面是它面向的几类场景，其中定时报告、常驻服务和功能开发三类已在日常运行。

- **只允许使用本地模型的运维场景。** 有些环境只允许调用部署在自己机器上的模型，而本地模型很不擅长协作：记不住谁在做什么、没做完就
  报完成、小上下文窗口容易溢出、回合进行到一半就卡住。其中一部分是模型的问题，一部分是外层运行框架的问题。fleetmux 把协作从模型
  手里拿走，交给 daemon：监工只决定下一步，worker 只执行这一步；回合何时结束、结果是否存在且内容合格（文件按字面核对，服务回复按
  `reply.required` 校验，三次误报 `COMPLETED` 即判任务阻塞）、会话何时换新会话接续（窗口与比例按模型分别配置）、卡死的会话何时
  重启，都由 daemon 决定。监工和 worker 可以全部经 Pi 接本地模型（`--supervisor-cli pi --worker-cli pi --pi-model <provider>/<model>`）；
  `--max-busy` 把并发请求控制在推理服务的内存上限以内；fleet 的私有 Redis 只监听 Unix socket，不开任何端口。
- **交易盯盘：把策略交给 AI，触发时复盘。** 价格与指标由你自己的程序盯着，模型不用整天轮询。价格触及某个档位、或某个指标参数被越过
  时，程序发出一条请求，例如 `{"to": "exit-1", "payload": {strategy, position, trigger, data}, "reply": {"required":
  ["verdict", "param", "evidence"]}}`，由负责该角色的 worker（每个角色一份提示词，经 `--roles` 提供；工具与 skill 经 `--worker-kit`
  载入）做一次完整复盘：读持仓和行情数据、重新检验策略的前提，用 JSON 作答。指定了 `to` 的请求不经监工回合；同时到达的多个触发按
  `--max-busy` 排队；回复字段先由 daemon 校验，你的程序再去读（`fleetmux results --drain`）。请求没有全局时限：过期的请求只占住
  它自己的槽位；卡死的 Claude Code worker 会恢复它自己的会话，并重新收到同一条请求。
  fleet 不下单，也不认交易日历：结论怎么用由你的程序决定，下单走它自己的校验，开收盘前后由它对 fleet 执行
  `pause` / `resume`。
- **一个中等强度模型带一群免费弱模型做日常活。** 翻译、起草、摘要、整理，难点主要在量大。中等强度的监工（经 Pi 或 Codex）把活切成
  小块，一个章节、一批文档、一个主题，并声明任务必须产出的文件、每份的长度和必含内容。worker 经 Pi 用免费模型做这些小块（名单每行
  一个模型，`pi_model:`）。弱模型每次只看到一小块：派工项上的 `"fresh": true` 在同一槽位起一个干净的会话，上一块不会渗进下一块；
  较长的一块在小窗口溢出前就换新会话接续。弱模型做错的地方由程序把关，而不是采信它的说法：所有声明的产物都存在、长度够、含必需
  文字，任务才结束；三次误报 `COMPLETED` 即判阻塞。免费接口不稳且有上限：限流和用量墙都走撞墙表，槽位沿模型链往下换，或可续跑地
  暂停；`--max-busy` 守住免费档的并发上限；fleet 运行中改名单 YAML 即可增减 worker。要做质量把关，给名单里某一行配一个更强的
  `pi_model:`，让它当复核者。
- **定时研究报告**（`scheduled`）。调用方自己的定时器在工作日的固定时点启动 fleet（开启 `clock_gate` 时，fleet 在休市日会自己拒绝
  启动）：一个监工加两个 Claude Code worker，研究用的 skill 和网络搜索 MCP 经 `--worker-kit` 载入。任务目标里声明报告文件及其
  必含章节；daemon 按字面验收，给这次运行设硬性时限，结束时关闭工作区。
- **常驻判定服务**（`service`）。另一个程序用 `fleetmux request` 发 JSON 请求；指定了 `to` 的请求直接交给该角色的 worker
  （`--worker-names exit-1,reentry-1,...`，每个角色一份提示词，经 `--roles` 提供）。daemon 按 `reply.required` / `reply.allowed`
  校验每条回复，调用方在非工作时段用 `pause` / `resume` 把 fleet 挂起。
  详见英文版[服务模式](README.md#service-mode-resident-fleet)。
- **功能开发与独立复核**（`dev_task`）。监工拆分功能，worker 实现，另设复核 worker 评审；复核至多三轮，剩下的 P0 / P1 交给你，
  而不是开第四轮。只有交付闸的 `test_cmd` 通过、代码合并并交给发布队列，任务才算完成。接口契约（`"contracts": true`）在开工前
  冻结共用接口。
- **多阶段项目**（`program`）。每个阶段作为一个独立的子 `dev_task` fleet 运行；台账记录各阶段，最后一个阶段合并并复核后，项目结束。
- **复核直到通过**（`review_loop`）。复核 → 修改 → 复核，至多三轮，之后出报告。
- **单个 worker 撑不下的任务**（开发中）。worker 的工作流持续忙碌超过可配置的时长时，会被要求提出拆分方案；监工批准（并选定模型）
  后，该 worker 交棒，接班者成为一个子 fleet 的监工，在父 fleet 的 worker 区实时显示。worker 也可以主动提出拆分。详见英文版
  [子 fleet](README.md#sub-fleets-a-worker-becomes-a-sub-fleet-supervisor-development)与 `docs/design.md` §44。
- **一个常驻运维 fleet 管几条业务线**（开发中）。主监工以 service 模式运行，每条业务线一个 worker；这些 worker 各自监管名单里声明的
  一个具名子 fleet，外部程序可以按它自己的时钟启停这些子 fleet（`docs/design.md` §29）。
- **日间与夜间模型。** `start --roster day.yaml --night-roster night.yaml` 加 `fleetmux night on|off`，在空闲边界把配置档有差别的
  worker 换成夜间配置档（例如免费或本地模型），槽位名不变，并告诉监工哪些已做完、哪些还没做（完整的日 → 夜 → 日运行尚未做端到端
  验收）。`start --resume` 用同样的方式把中断的运行接回来。
- **在哪都能跑，在哪都能看。** 在 Linux 上由 `fleetmux` 后端取代 cmux；`fleetmux attach`（也可经 `ssh -t`）或 `fleetmux gui` 能从另一个
  终端看实时页面，手机上的终端应用也行。

聊天通道（Telegram、飞书、微信、邮件、Slack）是监工的第二块键盘，结果和「等人处理」的事项推回同一个对话：`fleetmux channel`，见 [contracts §8](docs/contracts.md)。

## 安装

只有一个命令：`fleetmux <subcommand>`（如 `fleetmux start`、`fleetmux status`、`fleetmux channel`）。二进制自己跑终端子命令（`server`、
`attach`、`reap`、`web`、`gui --runtime D`），其余 fleet 子命令转交同一发版的 Python 命令行（docs/design.md §50）。用项目旧名装好的机器
用 `fleetmux home migrate` 把数据一次搬过来（docs/design.md §50.1）。

### 一键安装（Linux 与 macOS）

1. **装系统包。**

   ```bash
   sudo apt install redis-server tmux git     # Debian / Ubuntu
   brew install redis tmux git                # macOS
   ```

2. **运行安装脚本。** 只需要 `curl` 和 `tar`。它把最新发版装到 `~/fleetmux-releases/v<版本>`，用 uv 取得 Python 3.14，下载预编译程序（校验 sha256），把 `fleetmux` 链接到 `~/.local/bin`，最后跑依赖检查。不需要 root，不需要编译器。

   ```bash
   curl -fsSL https://raw.githubusercontent.com/justindbca/fleetmux/main/scripts/install.sh | bash
   ```

3. **装好并登录一个 agent 命令行**，例如 Claude Code：

   ```bash
   curl -fsSL https://claude.ai/install.sh | bash && claude
   ```

4. **检查。** `fleetmux start --help` 能列出选项即可。

要求：`redis-server` ≥ 7.0、`tmux` ≥ 3.4、`git` ≥ 2.30，加一个 agent 命令行（Claude Code、Codex、pi 或 opencode，只装名单里用到的；pi 另需 Node.js ≥ 22）。完整表格见英文版 [Requirements](README.md#requirements)。

### 从源码安装（开发用）

```bash
git clone https://github.com/justindbca/fleetmux && cd fleetmux
pip install -e .
```

给 agent 加一个 Codex 配置档。Codex ≥ 0.156 每个配置档是一个独立文件（见 [`codex.config.example.toml`](codex.config.example.toml)）：

```toml
# ~/.codex/fleet.config.toml   (selected with `codex --profile fleet`)
model = "gpt-5.5"
```

别的不用配：fleet 会给 agent 建一个独立的 Codex home，并替每个 agent 目录自动回答 Codex 的「信任此目录？」提示。

在目标机上按发版安装（`scripts/install_release.sh`）见英文版的
[Release install](README.md#release-install-on-a-target-machine-macos-or-linux)。

## 快速上手

打开一个 cmux 终端，配置好你的 Pi provider/model，并替换下面的 `local/my-model`。新启动要求 Redis v2，并需显式的开发许可。不带
`--supervisor-cli` / `--worker-cli` 时，`start` 取 `fleetmux/config/fleet.yaml#cli_pairs` 里的缺省组合：**Pi 监工跑
`openai-codex/gpt-6-astra`（thinking high），配官方 Claude Code worker**。该表列出所有支持的组合及其状态（见英文版
[Redis v2 development runtime](README.md#redis-v2-development-runtime)），表里没有的组合直接拒绝。`start` 还必须带
`--lifecycle <mode>`（`service`、`dev_task`、`program`、`scheduled`、`review_loop`、`smoke`，见
`fleetmux/config/fleet.yaml#lifecycle_modes`）；`unverified` 组合只能以 `--lifecycle smoke` 启动。英文版里 Codex 专用的配置与
启动示例只作为 v1 旧版参考保留。

```bash
fleetmux start --workers 2 --supervisor-cli pi --worker-cli pi --lifecycle <mode> \
  --pi-model local/my-model --allow-unsandboxed-pi --allow-development-v2
echo '{"goal": "Survey the three most-cited papers on X and write a comparison",
       "hours": 4,
       "artifacts": [{"path": "worker-1/report.md", "min_chars": 800, "contains": ["Conclusion"]}]}' \
  | fleetmux task -                           # hand the goal to the supervisor
fleetmux status                               # task + per-agent state, context usage, inbox depth
fleetmux stop                                 # cancel; confirm runtime retirement before Redis shutdown
```

`fleetmux task -` 把目标交给监工；`fleetmux status` 显示任务、各 agent 的状态、上下文占用和待投递数；`fleetmux stop` 取消任务，
确认 runtime 已退役后才关闭 Redis。

## 完整参考

完整参考请看[英文 README](README.md)：依赖、发版安装、终端后端（cmux、WezTerm、fleetmux）与 `start` 选项、布局与历史页、服务模式、
worker 名单 YAML、交接窗口与比例、断点续跑、接口契约、子 fleet、Claude Code 与 Pi agent、Redis v2 开发接线、任务流转、Redis 键、
测试状态、模块地图、已知限制和开发说明。设计基线见 [docs/design.md](docs/design.md)。

许可：MIT
