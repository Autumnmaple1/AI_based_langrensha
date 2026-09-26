# 月下议事厅 · Mooncourt

一个面向社团比赛与 Agent 策略实验的 **9 人 AI 狼人杀框架**。九个 Agent 通过 WebSocket 接入同一位裁判，由服务端统一分配身份、推进阶段、校验动作和记录结果；主持人与观众可以在浏览器中观看对局，赛后查看回放。

Agent 可以使用普通 Python 策略，也可以接入大语言模型。运行本地演示不需要模型密钥。

## 从哪里开始

| 你想做什么 | 入口 |
| --- | --- |
| 先看一局完整对局 | [快速体验](#快速体验) |
| 编写自己的参赛 Agent | [开发 Agent](#开发-agent)、[详细开发指南](agents/README.md) |
| 拿到个人凭证后参加正式比赛 | [凭证与实际参赛流程](#凭证与实际参赛流程) |
| 启动裁判、组织比赛 | [组织一场比赛](#组织一场比赛) |
| 本地运行九个自己的 Agent | [本地九人测试](#本地同时运行九个自己的-agent) |
| 批量运行策略或模型实例 | [多实例与模型接入](#多实例与模型接入) |
| 了解消息字段、规则与管理 API | [协议文档](PROTOCOL.md) |

## 项目如何工作

```text
参赛者本机 / 多实例运行器
  Agent 策略 → Python SDK ── WebSocket /ws/agent ──┐
                                                 │
                                         裁判服务（aiohttp）
                                         ├─ 规则引擎与动作校验
                                         ├─ 身份隔离、截止时间、步骤控制
                                         └─ SQLite 对局记录
                                                 │
  浏览器看板 ← HTTP + WebSocket /ws/watch ──────────┘
  观众：公开信息；主持人：完整身份、比赛控制与回放
```

- **裁判是唯一权威状态。** Agent 只提交动作，不能自行改变身份、票数或阶段。
- **每个座位有独立视野。** SDK 提供该座位可见的消息历史；狼人额外获知队友，技能结果按身份私发，终局公开全部身份。
- **按步骤收集和结算。** 狼人刀票、白天投票会向本步参与者并行请求；收到全部有效动作或到达截止时间后结算。发言按顺序逐人进行，并非每步都等待九个人。
- **通信与策略分离。** SDK 负责认证、就绪、断线重连、历史同步、动作重传及暂停恢复；参赛者实现决策方法即可。
- **支持现场展示与复盘。** 看板提供手动步进、自动推进、历史对局与导出；导出的 JSON 可用回放校验器复核。

后端使用 Python 3.11+、`asyncio`、`aiohttp` 和 SQLite；前端是随服务提供的 HTML/CSS/JavaScript，无需前端构建，也无需单独安装数据库服务。

## 快速体验

以下命令均在项目根目录执行。复制文件和设置环境变量的示例使用 Windows PowerShell。

### 1. 安装依赖

建议使用虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
```

macOS/Linux 激活命令为 `source .venv/bin/activate`。仅运行项目可以安装 `python -m pip install -e .`；仓库也提供 `requirements.txt`，其中包含运行和测试依赖。

### 2. 启动演示

```powershell
python -m werewolf.demo
```

打开 [本地看板](http://127.0.0.1:8765)，使用 `runtime/demo/config.json` 中的 `admin_token` 登录主持人视图。

演示会启动真实裁判与九个无需模型的示例 Agent，并在就绪后自动开局。默认采用**手动步进**：点击“下一步”推进，或点击“切换自动”连续运行。对局结束后服务继续保留，方便查看记录；按 `Ctrl+C` 退出。

要自动跑完一局并退出：

```powershell
python -m werewolf.demo --step-mode auto --pace-ms 0 --agent-delay 0 --exit-after-game
```

演示配置与数据库分别保存在 `runtime/demo/config.json` 和 `runtime/demo/matches.sqlite3`。`--manual` 表示等待主持人点击开局，与控制逐步推进的 `--step-mode manual` 含义不同。

> demo 已占满九个席位。要用自己的代码进行九人自测，请直接按[本地九人测试](#本地同时运行九个自己的-agent)启动独立裁判和多实例运行器。

## 开发 Agent

### 1. 复制可运行模板

```powershell
Copy-Item -Recurse agents/template agents/my-team
python -m agents.check agents/my-team/my_agent.py
```

macOS/Linux 可用 `cp -r agents/template agents/my-team`。模板默认已覆盖全部请求类型，可先自检，再逐步替换其中的策略。

必须实现的方法是：

```python
async def act(self, observation, request) -> dict:
    ...
```

`observation` 包含 `game_id` 和当前座位可见的 `history`；动作被拒后重试时，还会提供 `last_error`。`request` 包含请求类型 `type`、自己的座位号 `player_id`、请求编号和 `content`。合法目标、技能可用性与截止时间 `deadline_at` 都在 `content` 中。

另外可以实现两个异步钩子：`on_game_start(observation)` 用来重置每局记忆，`on_game_end(result)` 用来记录结果。完整可运行实现见 [模板](agents/template/my_agent.py)。

| 请求类型 | 返回动作 | 关键限制 |
| --- | --- | --- |
| `werewolves_act` / `werewolves_revote` | `{"action": "kill_vote", "target": ...}` | 从 `legal_targets` 选目标；`0` 为空刀，允许弃票时可用 `None` |
| `witch_act` | `save` / `poison` / `pass` | 依据 `legal_actions`、`legal_save_targets`、`legal_poison_targets` |
| `seer_act` | `{"action": "inspect", "target": ...}` | 从 `legal_targets` 选择 |
| `hunter_act` | `shoot` / `pass` | 开枪目标来自 `legal_targets` |
| `speech` / `speech_dying` | `{"action": "speak", "text": "..."}` | 非空，长度不超过 `max_codepoints` |
| `vote` | `{"action": "vote", "target": ...}` | 从 `legal_targets` 选择；`0` 为弃票 |

`save`、`poison`、`shoot` 需要 `target`；放弃技能只返回 `{"action": "pass"}`。动作字典必须能严格转换为 JSON，不要添加解释字段或返回 `NaN` 等特殊值。

默认每次行动有 60 秒预算，以请求实际截止时间为准。非法动作可以在原截止时间前修正，重试不会重置计时；未及时提交有效动作时，裁判使用该请求类型的默认处理。调用模型时应先准备合法兜底，给网络提交预留时间，并避免同步 I/O 阻塞事件循环。

### 2. 自检并连接比赛

自检将 Agent 放进 14 个固定场景，并使用裁判的校验器检查动作。通过表示这些场景中的接口与动作合法，不代表策略强度或真实模型服务已验证。

```powershell
python -m agents.check agents/my-team/my_agent.py

# 正式参赛：先按下文把主办方发来的个人凭证放进 runtime/match/
python -m agents.run --agent agents/my-team/my_agent.py --credentials runtime/match/agent-03.json --server ws://主办方公网IP:8765/ws/agent
```

比赛时将地址与凭证路径换成主办方提供的值。凭证格式为 `{"agent_id": "agent-03", "token": "…"}`；Agent ID 不等于座位号，座位和身份每局重新随机分配。

默认一局结束后退出；加 `--keep-alive` 可在结束后自动准备下一局。文件有多个 Agent 类时可用 `--class` 指定，构造设置可通过 `--settings` 传入 JSON 文件。

可参考两个完整示例：

- [baseline_agent.py](agents/example/baseline_agent.py)：无需模型的启发式策略。
- [llm_agent.py](agents/example/llm_agent.py)：单个模型 Agent，失败时使用策略兜底；设置格式见 [settings.example.json](agents/example/settings.example.json)。

更多历史读取、模型接入、错误处理与提交说明见 [Agent 开发指南](agents/README.md)。

## 组织一场比赛

### 1. 初始化并启动裁判

```powershell
# 首次运行：生成主持人配置与九份个人凭证，不启动服务
python -m werewolf.server --init

# 本机运行
python -m werewolf.server

# 或供局域网参赛者连接
python -m werewolf.server --host 0.0.0.0 --port 8765
```

本机与局域网启动方式二选一。默认生成：

| 路径 | 用途 |
| --- | --- |
| `runtime/config.json` | 主持人令牌、全部 Agent 凭证和裁判配置，仅主办方保管 |
| `runtime/agent-01.json` 至 `agent-09.json` | 逐一分发给参赛者的个人凭证 |
| `runtime/matches.sqlite3` | 服务运行后的对局数据库 |

已有配置时跳过 `--init`，该命令拒绝覆盖现有配置。可用 `--config`、`--db` 指定其他配置和数据库路径。

### 2. 入席与开局

1. 告知参赛者 `ws://<服务器实际地址>:8765/ws/agent`，每人分发一份不同的凭证。
2. 参赛者启动 Agent；认证后 SDK 自动发送就绪。
3. 主持人打开 `http://<服务器实际地址>:8765`，使用 `admin_token` 登录。
4. 九个 Agent 就绪后，点击“开始对局”。初始配置采用手动步进，可点击“下一步”或切换自动。
5. 赛后在主持人菜单查看历史对局或导出记录。

未登录的看板仅展示公开视野。不要分发 `runtime/config.json`，也不要同时让两个客户端使用同一份个人凭证，后连接会替换旧连接。

客户端断线可通过 SDK 重连；**裁判进程重启不会续跑未完成对局**，恢复时会将其标记中止。远程部署、暂停/恢复、鉴权和管理 API 见 [PROTOCOL.md](PROTOCOL.md)。

## 凭证与实际参赛流程

先区分两种运行方式，**不要混用服务器地址和凭证**：

| 场景 | 裁判在哪 | 谁准备凭证 | 社员运行什么 |
| --- | --- | --- | --- |
| 正式比赛 | 主办方服务器 | 主办方给每人一份 | `agents.run` 启动自己的一个 Agent |
| 本地九人测试 | 自己的电脑 `127.0.0.1:8766` | 自己初始化生成九份 | `werewolf.server` + `multi_agent.run` |

### 1. 主办方分发，社员保存

主办方从**比赛服务器**生成的 `agent-01.json` 至 `agent-09.json` 中，每人单独分配一份。它包含 `agent_id` 和 `token`，不是模型 API 密钥，也不是角色或座位号。不要编辑其中的值、共用凭证或把全部凭证发给每个人。

| 文件 | 谁使用 | 如何使用 |
| --- | --- | --- |
| 服务器 `runtime/config.json` | 主办方 | 裁判读取；其中 `admin_token` 用于看板登录，不传给 Agent |
| 主办方发来的 `agent-XX.json` | 对应社员 | 保存在本机 `runtime/match/`，通过 `--credentials` 传入 |
| `my_settings.json` | 使用模型的社员 | 模型地址、模型名等，通过 `--settings` 传入；密钥放环境变量 |
| 本地 `runtime/local/agent-XX.json` | 自测者 | 自己的本地裁判生成，只用于本地九人测试 |

主办方下载并解压凭证包后，把 `agent-01.json` 发给第一位社员，`agent-02.json` 发给第二位，依此类推。记录分配关系；压缩包包含全部凭证，只由主办方保存。社员把收到的文件原样复制到下面的位置，不需要手动填写 token，也不需要把它写进 Python 代码：

```text
项目根目录/
  agents/my-team/my_agent.py       自己的策略代码
  runtime/match/agent-03.json      自己收到的正式比赛凭证（以 03 为例）
  my_settings.json                可选的模型设置
```

### 2. 自检并连接正式比赛

例如收到 `agent-03.json` 后，在项目根目录执行（将来源路径改成实际下载位置）：

```powershell
New-Item -ItemType Directory -Force runtime/match
Copy-Item "$env:USERPROFILE/Downloads/agent-03.json" runtime/match/agent-03.json
python -m agents.check agents/my-team/my_agent.py
python -m agents.run --agent agents/my-team/my_agent.py --credentials runtime/match/agent-03.json --server ws://主办方公网IP:8765/ws/agent --keep-alive
```

把 `主办方公网IP` 替换为主办方公布的地址；如果主办方提供 `wss://` 地址，使用完整地址。直接 IP 部署的 HTTP/WS 是明文连接。`127.0.0.1` 永远指当前电脑，连接云端比赛时不能填它。

如果自己的 Agent 需要模型设置，在上述运行命令末尾追加 `--settings my_settings.json`，并在同一终端设置它所需的模型密钥环境变量。个人凭证用于登录裁判，模型密钥用于调用模型，两者不能替代。

### 3. 等待主持人开局

启动后保持进程运行，SDK 自动认证和就绪；九位社员到齐后主办方开局。`--keep-alive` 表示结束后继续就绪，不表示自动开局。只想打一局则去掉它。自检不需要比赛凭证；社员也不需要下载主办方的 `config.json`。

观看比赛时打开主办方提供的看板地址，例如 `http://主办方公网IP:8765`。社员不用输入自己的 token 登录网页；网页上的“主持人登录”使用的是主办方的 `admin_token`。

**本地生成的凭证不能用于云端比赛。** 即使文件名和 Agent ID 相同，token 也不同。所有凭证保存在被 Git 忽略的 `runtime/` 下，不提交仓库。

## 本地同时运行九个自己的 Agent

`multi_agent.run` 可以加载社员的 Python 文件和类。以下流程运行的是**你的同一份代码的九个独立对象**，分别使用九份凭证、九份可见历史和各自的随机种子；不是启动九个内置对手。对象在同一 Python 进程中异步运行，不是操作系统进程隔离，不要用模块全局变量或同一日志文件共享游戏状态。

### 1. 准备自己的代码并自检

以下命令在项目根目录执行；已有自己的代码时不要再次复制模板：

```powershell
Copy-Item -Recurse agents/template agents/my-team
python -m agents.check agents/my-team/my_agent.py
```

### 2. 终端 A：初始化本地凭证并启动裁判

使用单独的 `runtime/local/` 和端口 `8766`，避免与 demo 或比赛配置混用。已有本地配置时自动跳过初始化：

```powershell
if (-not (Test-Path runtime/local/config.json)) {
    python -m werewolf.server --init --config runtime/local/config.json
}
python -m werewolf.server --config runtime/local/config.json --db runtime/local/matches.sqlite3 --host 127.0.0.1 --port 8766
```

保持终端 A 运行。初始化后，`runtime/local/` 下会同时有 `config.json` 和 `agent-01.json` 至 `agent-09.json`。九份个人凭证由多实例运行器读取，`config.json` 则供本地裁判及你登录本地看板使用，不需要逐一分发。

### 3. 终端 B：配置自己的 Agent

另开 PowerShell，进入同一个项目根目录并激活安装了项目的 Python 环境。首次复制配置：

```powershell
Copy-Item multi_agent/config.example.json multi_agent/config.local.json
```

编辑 `multi_agent/config.local.json` 顶层的 `agent`，将示例模板路径改成自己的文件：

```json
{
  "server": "ws://127.0.0.1:8766/ws/agent",
  "agent": "../agents/my-team/my_agent.py",
  "class": "MyAgent",
  "output_dir": "runs",
  "defaults": {},
  "agents": [
    {"credentials": "../runtime/local/agent-01.json", "seed": 1},
    {"credentials": "../runtime/local/agent-02.json", "seed": 2},
    {"credentials": "../runtime/local/agent-03.json", "seed": 3},
    {"credentials": "../runtime/local/agent-04.json", "seed": 4},
    {"credentials": "../runtime/local/agent-05.json", "seed": 5},
    {"credentials": "../runtime/local/agent-06.json", "seed": 6},
    {"credentials": "../runtime/local/agent-07.json", "seed": 7},
    {"credentials": "../runtime/local/agent-08.json", "seed": 8},
    {"credentials": "../runtime/local/agent-09.json", "seed": 9}
  ]
}
```

`class` 填代码里的实际类名，也可省略让加载器选择文件中第一个带 `act` 的类。**Agent 和凭证的相对路径都相对于配置文件所在目录**，所以这里需要 `../`。不必复制九份 Python 文件。

### 4. 一条命令启动九个实例

先在终端 B 读取并复制本地主持人令牌，再启动九个实例：

```powershell
(Get-Content runtime/local/config.json -Raw | ConvertFrom-Json).admin_token
python -m multi_agent.run --config multi_agent/config.local.json --once
```

打开 [本地九人测试看板](http://127.0.0.1:8766)。至此只需要两个终端：A 跑裁判，B 跑九个 Agent，不必手动打开九个窗口。

登录主持人，确认九个实例就绪，点击“开始对局”，然后“下一步”或“切换自动”。`--once` 在各实例完成一局后退出；去掉它可连续测试，每局仍由主持人开局。停止时先在终端 B 按 `Ctrl+C`，再停止终端 A。

成功的表现是：终端 B 显示 `Starting 9 agents`，看板显示九个实例已就绪，开局后出现发言、投票和最终结算。仅看到进程启动不代表比赛已经开始。

### 5. 查看结果或测试不同策略

每次启动在 `multi_agent/runs/<本次编号>/` 产生九份 JSONL 日志和 `summary.json`。日志包含提交动作、耗时及 SDK 事件；摘要包含完成局数、最终结果、SDK 错误和决策次数。动作被记录不等于裁判已接受，确认接受应查看 `action_ack`。这些日志包含私有信息，不要在比赛进行时公开。

可以给某个实例单独设置 `agent`、`class`、`settings`，覆盖顶层配置，例如：

```json
{"credentials": "../runtime/local/agent-02.json", "agent": "../agents/example/baseline_agent.py", "class": "BaselineAgent", "seed": 2}
```

### 6. 下次修改策略后怎么重测

保留本地凭证与配置，不要重新初始化或再次复制示例覆盖自己的设置。上一局结束后，保持终端 A 的裁判运行，在终端 B 重新执行自检和启动命令：

```powershell
python -m agents.check agents/my-team/my_agent.py
python -m multi_agent.run --config multi_agent/config.local.json --once
```

重新启动运行器才会加载修改后的 Python 代码。如果使用不带 `--once` 的持续运行模式，在两局之间按 `Ctrl+C` 退出终端 B 的旧进程，再启动。不要同时开两组使用相同凭证的实例。

| 遇到的问题 | 先检查什么 |
| --- | --- |
| 找不到凭证文件 | 是否先初始化 `runtime/local/`；配置里的路径是否相对 `multi_agent/` 填写 |
| `AUTH_FAILED` | 本地凭证是否误用于云端，或使用了其他裁判配置生成的 token |
| 找不到类或文件 | 顶层 `agent` 是否指向自己的文件，`class` 是否等于实际类名 |
| 只有一个实例在线或反复掉线 | 是否九项都误用了同一份凭证，或另有进程使用这些凭证 |
| 九人在线却没有发言 | 是否已点击开局；手动步进时是否点击“下一步”或切换自动 |
| 修改策略后行为没变 | 是否重启了终端 B 的运行器；是否仍指向示例模板而不是自己的文件 |

## 多实例与模型接入

模型能力由你加载的 Agent 类实现；设置一个 `mode` 不会让普通模板自动调用模型。要运行九个单实例模型示例，将上述配置改为：

```json
{
  "agent": "../agents/example/llm_agent.py",
  "class": "LlmAgent",
  "defaults": {
    "base_url": "https://你的服务地址/v1",
    "model": "你的模型名",
    "api_key_env": "WEREWOLF_API_KEY",
    "timeout_seconds": 20
  }
}
```

这是需要修改的字段片段，保留原配置中的 `server`、`agents` 等字段。`defaults` 会作为构造参数传给每个实例，单项 `settings` 可覆盖它；使用自己的类时应让构造函数接收所需参数。

在启动九个实例的终端里设置密钥：

```powershell
$env:WEREWOLF_API_KEY = "你的密钥"
python -m multi_agent.run --config multi_agent/config.local.json --once
```

九个实例会独立调用模型，可能遇到并发限流并产生费用。模型成功与兜底统计需要由社员自己的代码记录，通用运行器只记录最终返回动作，不会把合法动作当作模型成功。

所有实例都必须通过 `agent` 指定入口文件（顶层统一指定或逐项指定）。新示例默认加载社员模板，无需模型密钥。详细配置见 [多实例指南](multi_agent/README.md)。

## 规则摘要

固定九人局：**3 狼人、3 村民、1 预言家、1 女巫、1 猎人**。采用屠边规则：狼人全部死亡则好人胜；村民全部死亡或三位神职全部死亡则狼人胜。死亡结算先处理猎人技能，再判断胜负。

| 环节 | 当前规则 |
| --- | --- |
| 狼人刀票 | 可空刀、自刀、刀队友；唯一最高票生效。含首轮最多 3 轮，第 3 轮仍平票则从最高票选项随机选择；持续全员弃票则空刀 |
| 女巫 | 一瓶解药、一瓶毒药，每夜最多用一瓶；任意夜晚可依法自救；仅有解药时可见刀口，不可自毒 |
| 预言家 | 每晚查验一人，可以重复查验 |
| 猎人 | 符合触发条件时可开枪或放弃；被毒死不能开枪 |
| 白天投票 | 不能投自己，可弃票；最高票平票进入 PK 和一次复投，再平票则不放逐 |
| 遗言 | 首夜死者与白天被放逐者有遗言；终局后不再执行遗言 |
| 发言顺序 | 首日随机起点与方向，之后保持方向并轮换起点 |
| 默认预算 | 行动 60 秒，发言/遗言最多 500 个 Unicode 码点，最多 30 个白天 |

当前不启用警长、狼人自爆或狼人私聊。精确流程、默认动作和消息字段以 [协议文档](PROTOCOL.md) 与实现为准。

## 测试与回放验证

```powershell
# 模板及两个单 Agent 示例的接口与动作自检
python -m agents.check

# 规则、网络、SDK、Agent、看板数据等测试
python -m pytest -q

# 完整离线校验，并输出测试报告
python -m multi_agent.validate

# 校验从主持人看板导出的 JSON 记录
python -m werewolf.replay runtime/exported-game.json
```

离线校验使用临时裁判与模拟模型 HTTP 服务，不需要真实模型密钥，报告写入 `runtime/agent-validation/<本次编号>/`。系统临时目录不可写时，先创建 `runtime/`，pytest 再加 `--basetemp=runtime/pytest-temp`。

看板数据测试在检测到 Node.js 时由 pytest 自动运行，否则跳过；也可执行 `node --test tests/dashboard.test.mjs`。运行看板本身不需要 Node.js。

真实模型接入请对自己的 Agent 使用 `agents.check --settings` 并进行实际对局验证。自检只检查最终动作合法性，合法兜底也会通过；模型调用成功率应由 Agent 自己记录。

回放校验器复核角色分配、票数、死亡、终局与私有消息序列的一致性；它不重新调用 Agent，也不重新抽取随机结果。

## 目录结构

```text
agents/                   参赛 Agent 模板、示例、自检和单实例启动器
  template/               可直接复制的完整模板
  example/                启发式与模型示例
  check.py / run.py       自检 / 连接裁判
werewolf/
  engine.py               游戏规则与阶段推进
  protocol.py             协议常量与严格动作校验
  server.py               HTTP/WebSocket 服务、鉴权与比赛控制
  sdk.py                  Agent 客户端 SDK
  store.py / replay.py    SQLite 记录 / 导出记录校验
  scenarios.py            共享的 14 个动作场景
  demo.py                 裁判与九个策略 Agent 的本地演示
  static/                 中文实时看板与回放界面
multi_agent/              多实例策略/模型运行器及验证工具
tests/                    规则、网络、看板与 Agent 测试
PROTOCOL.md               完整协议、规则、管理接口与部署说明
DESIGN_REVIEW.md          看板设计与自查记录
runtime/                  本地产生的凭证、数据库和验证报告（Git 忽略）
```
