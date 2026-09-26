# 月下议事厅 · Mooncourt

一个面向社团比赛与 Agent 策略实验的 **9 人 AI 狼人杀框架**。九个 Agent 通过 WebSocket 接入同一位裁判，由服务端统一分配身份、推进阶段、校验动作和记录结果；主持人与观众可以在浏览器中观看对局，赛后查看回放。

Agent 可以使用普通 Python 策略，也可以接入大语言模型。运行本地演示不需要模型密钥。

## 从哪里开始

| 你想做什么 | 入口 |
| --- | --- |
| 先看一局完整对局 | [快速体验](#快速体验) |
| 编写自己的参赛 Agent | [开发 Agent](#开发-agent)、[详细开发指南](agents/README.md) |
| 启动裁判、组织比赛 | [组织一场比赛](#组织一场比赛) |
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

> demo 已占满九个席位。测试自己的 Agent 时，请使用下文的独立裁判，并给自己的 Agent 留出一个席位。

## 开发 Agent

### 1. 复制可运行模板

```powershell
Copy-Item -Recurse agents/template agents/team-alice
python -m agents.check agents/team-alice/my_agent.py
```

macOS/Linux 可用 `cp -r agents/template agents/team-alice`。模板默认已覆盖全部请求类型，可先自检，再逐步替换其中的策略。

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
python -m agents.check agents/team-alice/my_agent.py

# 拿到主办方提供的服务器地址与个人凭证后连接
python -m agents.run --agent agents/team-alice/my_agent.py --credentials runtime/agent-03.json --server ws://127.0.0.1:8765/ws/agent
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

## 多实例与模型接入

`multi_agent` 在一个 Python 进程中异步运行多个独立 Agent，每个实例使用自己的凭证、策略状态与可见历史。它可用于填补测试席位，或比较不同模型；需要进程隔离时可分别启动多个配置。

先按上一节启动独立裁判，再另开终端准备配置：

```powershell
Copy-Item multi_agent/config.example.json multi_agent/config.local.json
```

**示例文件当前设置为 `"mode": "llm"`，直接运行需要模型密钥。** 不调用模型时，先将 `defaults.mode` 改为 `"baseline"`，然后运行：

```powershell
python -m multi_agent.run --config multi_agent/config.local.json --once
```

默认示例包含九个实例。如果你另外启动了一个自定义 Agent，请从配置的 `agents` 数组中移除对应凭证项，只保留另外八个；不要与 demo 同时使用相同端口或席位。

需要接入模型时，配置 `defaults`，也可在各实例的 `settings` 中覆盖：

```json
{
  "mode": "llm",
  "base_url": "https://你的服务地址/v1",
  "model": "你的模型名",
  "api_key_env": "WEREWOLF_API_KEY",
  "timeout_seconds": 20
}
```

程序向 `base_url` 追加 `/chat/completions`，读取 `choices[0].message.content`；请根据服务商填写兼容接口的根地址和实际可用模型。`api_key_env` 填环境变量名称，密钥放在启动终端的环境变量中：

```powershell
$env:WEREWOLF_API_KEY = "你的密钥"
python -m multi_agent.run --config multi_agent/config.local.json --once
```

每次行动最多调用一次模型，失败后使用合法策略兜底。模型会收到该实例的游戏历史。配置中的凭证相对路径以配置文件目录为基准；示例配置的运行报告写入 `multi_agent/runs/<本次编号>/`，包含各实例 JSONL 日志和 `summary.json`。

不带 `--once` 时，实例会在一局结束后继续就绪。更多参数、日志含义和真实模型验证方式见 [多实例指南](multi_agent/README.md)。

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

离线校验使用临时裁判与模拟模型 HTTP 服务，不需要真实模型密钥，报告写入 `runtime/agent-validation/<本次编号>/`。系统临时目录不可写时，pytest 可加 `--basetemp=runtime/pytest-temp`。

看板数据测试在检测到 Node.js 时由 pytest 自动运行，否则跳过；也可执行 `node --test tests/dashboard.test.mjs`。运行看板本身不需要 Node.js。

真实模型验证是单独的可选步骤：

```powershell
python -m multi_agent.validate --live multi_agent/config.local.json
```

该命令先跑离线测试，再对配置中的每个实例发起 14 次模型调用，九个实例共 126 次，可能产生费用。真实模型验证中，超时、非法动作和策略兜底均视为失败。

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
