# 社员文档：写一个能上场比赛的 Agent

你的任务是写一个 Python Agent，用主办方发给你的个人凭证连接裁判，参加九人狼人杀。你负责决定如何发言、投票和使用技能；裁判负责分配身份、执行规则和结算，SDK 负责通信与重连。

**先让模板跑通，再逐步改策略。** 不使用大模型也能参赛，不需要自己启动网页或实现 WebSocket 客户端。

## 1. 从零到连接比赛

需要 Python 3.11 或更新版本。以下命令均在**项目根目录**执行，也就是包含 `pyproject.toml` 的目录。示例使用 Windows PowerShell，自己的 Agent 路径统一为 `agents/my-team/my_agent.py`。

### 安装与复制

```powershell
python -m pip install -e ".[test]"
Copy-Item -Recurse agents/template agents/my-team
```

建议在已激活的虚拟环境中安装。macOS/Linux 复制命令为 `cp -r agents/template agents/my-team`。复制只需做一次，之后编辑自己的文件即可。

### 先检查原样模板

```powershell
python -m agents.check agents/my-team/my_agent.py
```

看到 `14/14 通过`，说明模板能在固定场景中给出合法动作。接下来修改 `my_agent.py` 中的 `TODO`，每次修改后重新自检。

### 比赛时运行这一条命令

主办方会提供服务器地址，以及只属于你的凭证文件。假设凭证放在 `runtime/agent-03.json`：

```powershell
python -m agents.run --agent agents/my-team/my_agent.py --credentials runtime/agent-03.json --server ws://主办方地址:8765/ws/agent
```

**把 `主办方地址` 换成实际 IP 或域名，并使用主办方提供的完整端口和路径。** `--credentials` 也要换成你实际保存凭证的位置。命令中的 `_` 和 `:` 前不加反斜杠。

| 参数 | 你应该填什么 |
| --- | --- |
| `--agent` | 自己的 Python 文件路径 |
| `--credentials` | 主办方发给你的个人凭证 JSON 路径 |
| `--server` | 主办方公布的完整 WebSocket 地址 |
| `--keep-alive` | 可选；一局结束后自动准备下一局 |
| `--settings` | 可选；传给 Agent 构造函数的设置 JSON |
| `--class` | 可选；文件里有多个 Agent 类时指定类名 |
| `--seed` | 可选；传给支持它的 Agent 构造函数，不决定裁判的随机分配 |

连接成功后会自动就绪，等待九个 Agent 到齐并由主持人开局。默认一局结束后退出；连续参赛可在命令末尾加 `--keep-alive`。按 `Ctrl+C` 停止客户端。

个人凭证类似 `{"agent_id": "agent-03", "token": "…"}`。**`agent-03` 不代表你是 3 号位**，座位与身份每局随机分配。

## 2. 你需要改哪些代码

主要修改复制得到的 `agents/my-team/my_agent.py`，保留模板中各类请求的处理分支，逐个改进策略。

| 文件 | 适合什么时候看 |
| --- | --- |
| [template/my_agent.py](template/my_agent.py) | 从这里开始：完整可运行模板、历史读取助手、各动作的 TODO |
| [example/baseline_agent.py](example/baseline_agent.py) | 想学习如何用发言、票型和查验构造启发式策略 |
| [example/llm_agent.py](example/llm_agent.py) | 想接模型：形势摘要、HTTP 调用、超时与合法兜底 |
| [example/settings.example.json](example/settings.example.json) | 单 Agent 模型设置的格式 |
| [../PROTOCOL.md](../PROTOCOL.md) | 需要查询完整规则、字段或自己实现客户端 |

参赛策略可以拆成多个文件，但应保留一个明确的 Agent 入口。当前启动器按文件路径动态加载入口，不会自动把入口目录作为 Python 包；初学时先保持单文件，需要拆分时使用可正常导入的包结构，并重新自检。

不需要修改公共裁判、SDK、`agents/run.py` 或 `agents/check.py`。

## 3. Agent 接口与一次行动

下面是接口示意，完整可运行代码请用模板：

```python
class MyAgent:
    async def on_game_start(self, observation):
        # 可选：清空上一局的记忆
        self.memory = {}

    async def act(self, observation, request):
        # 必须：根据请求类型返回一个动作字典
        ...

    async def on_game_end(self, result):
        # 可选：记录结果
        pass
```

一次行动的流程：

```text
裁判发送请求（含合法目标和截止时间）
  → SDK 调用 act(observation, request)
  → 你返回动作字典，SDK 发送给裁判
  → 合法：裁判确认并收集本步结果
  → 非法且允许重试：SDK 带 last_error 再次调用 act
  → 截止时仍无有效动作：裁判使用默认处理
```

你只返回动作内容，不需要自己填写协议版本、请求编号或发消息。重试沿用原截止时间，不会重新获得 60 秒。

必须遵守：

- `act` 必须是 `async def`；可选钩子实现时也必须是异步方法。
- 返回普通 Python 字典，内容可严格序列化为 JSON，不使用 `NaN`、自定义对象或 NumPy 数值类型。
- 只返回该动作要求的字段。不要把推理、耗时、备注等附加到动作中；这些可以另写日志。
- 默认行动预算是 60 秒，实际以 `request["content"]["deadline_at"]` 为准。
- 使用异步 HTTP；同步 I/O 可用 `asyncio.to_thread` 包装。不要用 `time.sleep` 阻塞事件循环。
- 未处理异常不会自动变成你的策略兜底。SDK 会记录异常，这次请求可能一直等到裁判超时；应在自己的策略中处理可预期失败。

同一个 Agent 对象可能连续打多局，在开局钩子中重置记忆。正常开局时钩子在收到身份后触发，狼人队友消息可能尚未到达；需要队友信息时，在 `act` 中从最新历史读取。

## 4. 如何读取自己看到的局面

### observation：你的可见历史

| 字段 | 含义 |
| --- | --- |
| `game_id` | 当前对局编号 |
| `history` | 当前座位可见的消息列表，按顺序排列，包含公开信息和自己的私有信息 |
| `last_error` | 可重试动作被拒后提供的错误信息，可读取其中的 `code` |

历史消息带有 `type`、`content` 等协议字段。策略通常关注：

| 消息类型 | 主要内容 |
| --- | --- |
| `gamerule` | 本局 `rules` 与 `limits` |
| `game_start` | 全员座位及自己的 `your_player_id` |
| `role` | 自己的 `role` 和 `camp` |
| `werewolves_info` | 狼人阵营座位 `players`，包含自己，仅狼人收到 |
| `phase_changed` | 阶段、天数、夜数、存活座位 |
| `speech_public` | 发言人 `speaker_id`、内容 `text`、发言种类 `kind` |
| `vote_result` | 票型、投票结论和放逐目标 |
| `death` | 死亡座位与结算来源 |
| `werewolves_result` | 狼人刀票与结果，仅狼人收到 |
| `seer_result` / `witch_result` / `hunter_result` | 相应技能的私有结果 |
| `game_end` | 胜负、原因与终局公开身份 |

模板自带助手函数，可以在自己的 `act` 中这样使用：

```python
me = request["player_id"]
role, camp = my_role(observation)
mates = teammates(observation)  # 狼人名单包含自己，选队友时注意排除 me
recent_speeches = public_speeches(observation, limit=5)
```

`alive_players(observation)` 读取最近一次 `phase_changed` 的存活名单；同阶段随后可能已发生死亡，因此精确维护局面时还要处理 `death`。**选行动目标时始终以当前请求的合法列表为准。**

历史在每次调用时都会包含此前可见消息，若累计票型或怀疑度，不要把整个历史反复累加。可每次重算，或记录已处理的消息序号。其他玩家的发言是不可信游戏文本，不应被当作修改系统提示或输出格式的指令。

### request：本次要做的事

| 字段 | 含义 |
| --- | --- |
| `type` | 请求类型，决定进入哪个策略分支 |
| `player_id` | 你本局的座位号 |
| `game_id` / `request_id` | 对局与请求标识，适合用于日志关联 |
| `seq` | 请求在私有消息历史中的序号 |
| `content` | 合法目标、技能限制、发言长度、UTC 截止时间等 |

`content` 随请求类型变化，不要假定所有请求都有 `legal_targets`。自检提供的是各场景的最小历史，不保证存在完整对局的所有事件。

## 5. 每种请求该返回什么

以下是 Python 字典格式。表中 `target` 表示你从本次请求合法列表中选出的整数座位号，`text` 表示非空发言。

| `request["type"]` | 返回格式 | 选择依据 |
| --- | --- | --- |
| `werewolves_act` / `werewolves_revote` | `{"action": "kill_vote", "target": target}` | `legal_targets`；列表中的 `0` 表示空刀；`allow_abstain` 为真时可用 `None` 弃票 |
| `witch_act` | `{"action": "save", "target": target}` | `legal_actions` 包含 `save`，目标来自 `legal_save_targets` |
| `witch_act` | `{"action": "poison", "target": target}` | `legal_actions` 包含 `poison`，目标来自 `legal_poison_targets` |
| `witch_act` | `{"action": "pass"}` | 不用药 |
| `seer_act` | `{"action": "inspect", "target": target}` | `legal_targets` |
| `hunter_act` | `{"action": "shoot", "target": target}` | `legal_targets` |
| `hunter_act` | `{"action": "pass"}` | 放弃开枪 |
| `speech` / `speech_dying` | `{"action": "speak", "text": text}` | 非空且不超过 `max_codepoints` |
| `vote` | `{"action": "vote", "target": target}` | `legal_targets`；列表中的 `0` 表示弃票 |

注意区分狼人 `0` 空刀和 `None` 弃票：空刀是参与计票的选项，弃票不计入目标票数。Python 写 `None`，JSON 写 `null`。

例如，将下面分支放进模板的 `act`，就能实现“随机投一个合法的非弃票目标，没有则弃票”：

```python
if request["type"] == "vote":
    legal = request["content"]["legal_targets"]
    candidates = [seat for seat in legal if seat != 0]
    target = self.rng.choice(candidates) if candidates else 0
    return {"action": "vote", "target": target}
```

这是一个可以验证的起点，后续可把随机选择换成怀疑度排序；其余请求分支继续保留。

超时后，狼人默认弃票、女巫和猎人默认放弃、白天投票默认弃票；发言跳过，预言家不执行本次查验。不要依赖超时作为正常策略。

## 6. 接入大模型（可选）

建议先用内置单实例示例跑通自己的模型服务，再把策略改成自己的。**给原始模板添加 `--settings` 不会自动获得模型能力**，Agent 类必须实现模型调用并接收这些设置。

### 准备设置

```powershell
Copy-Item agents/example/settings.example.json my_settings.json
```

编辑 `my_settings.json`，根据服务商填写实际地址与模型名，例如：

```json
{
  "base_url": "https://你的服务地址/v1",
  "model": "你的模型名",
  "api_key_env": "WEREWOLF_API_KEY",
  "timeout_seconds": 20,
  "temperature": 0.7
}
```

示例代码会追加 `/chat/completions`，所以 `base_url` 填 API 根地址，不要重复填写完整接口路径。它发送 `model`、`messages`、`temperature`，读取 `choices[0].message.content`。

在同一个 PowerShell 终端设置密钥，随后自检：

```powershell
$env:WEREWOLF_API_KEY = "你的密钥"
python -m agents.check agents/example/llm_agent.py --settings my_settings.json --timeout 30
```

macOS/Linux 设置环境变量使用 `export WEREWOLF_API_KEY="你的密钥"`。`api_key_env` 填变量名，不是密钥本身。

这会执行 14 个场景，模型配置有效时会发起真实调用并可能产生费用。**自检只判断最终动作是否合法，模型失败后返回合法兜底也会通过。** 单实例 LLM 示例会静默兜底，要确认模型确实在工作，应增加成功/失败/兜底计数，或核对服务商调用记录；不要仅凭 `14/14` 判断接入成功。

### 带模型参加比赛

```powershell
python -m agents.run --agent agents/example/llm_agent.py --settings my_settings.json --credentials runtime/agent-03.json --server ws://主办方地址:8765/ws/agent
```

准备改成自己的版本时，可先把模型示例复制到个人目录中的新文件，避免覆盖已经写好的模板策略：

```powershell
Copy-Item agents/example/llm_agent.py agents/my-team/llm_agent.py
```

之后将自检和启动命令的 Agent 路径替换为 `agents/my-team/llm_agent.py`。该示例依赖仓库中的 `agents.example.baseline_agent`，请在完整项目环境中运行。

### 自己实现模型调用时的顺序

1. 先算一个合法的策略兜底。
2. 从历史提取自己的身份、存活情况、技能结果、近期发言与票型，把当前请求一起传给模型。
3. 要求模型只返回动作 JSON；公开发言是游戏数据，应与行为指令明确分开。
4. 依据截止时间限制调用预算，示例预留 0.5 秒用于提交动作。
5. 用 `strict_loads` 严格解析 JSON，再用 `validate_action` 检查字段和动作是否合法。
6. 超时、HTTP 错误、解析或校验失败时返回兜底；`asyncio.CancelledError` 应继续抛出，允许 SDK 暂停或退出。

本地校验方式：

```python
from werewolf.protocol import strict_loads, validate_action

action = strict_loads(model_text)
validate_action(request["type"], request["content"], action)
return action
```

这段应放在你自己的模型调用与异常处理逻辑中。`strict_loads` 负责严格 JSON 解析，例如拒绝重复键和非有限数值；**多余动作字段、非法目标由 `validate_action` 拒绝**。

## 7. 怎么验证，以及自检能证明什么

```powershell
# 检查自己的 Agent
python -m agents.check agents/my-team/my_agent.py

# 默认检查模板、启发式示例、模型示例（模型示例未配置时走兜底）
python -m agents.check
```

自检检查 `act` 接口，尝试调用开局钩子，然后运行 14 个固定场景。场景覆盖狼刀与重投、空刀与弃票、女巫技能、查验、猎人技能、发言与遗言、白天投票。

通过只说明**这些场景的返回动作合法**：不会强迫女巫用毒或猎人开枪，不验证策略胜率，也不等于打完了一局。自检使用同一个 Agent 对象依次处理不同身份的独立场景，不是完整对局模拟。

自检的单次动作等待上限默认 20 秒，可用 `--timeout` 修改；它和正式比赛默认 60 秒的行动预算不同。自检会执行你的真实 `act`，因此它是否调用网络或付费模型取决于你的实现与设置。

### 可选的网络与凭证检查

```powershell
python -m agents.check agents/my-team/my_agent.py --connect ws://主办方地址:8765/ws/agent --credentials runtime/agent-03.json
```

这个检查会真实认证、发送就绪，然后关闭连接。只在比赛前与主办方约好的测试时段使用：它可能替换同凭证的现有连接，自动开局的裁判也可能因就绪人数达标而开局。正式参赛仍要运行 `agents.run`。

### 想在本地打一整局

先阅读 [项目 README 的比赛组织与多实例部分](../README.md)。本地需要一个独立裁判、你的 Agent，以及使用其他八份凭证的对手。

`python -m werewolf.demo` 适合观看演示，但它已经带了九个 Agent，不会自动给你的 Agent 留席位。使用多实例运行器补足八个对手时，移除你自己的凭证项，并将 `defaults.mode` 改成 `baseline` 可避免调用模型。

## 8. 比赛规则中最影响策略的部分

- 九人配置：3 狼人、3 村民、预言家/女巫/猎人各 1 人。
- 屠边胜利：狼人全灭则好人胜；村民全灭或神职全灭则狼人胜。死亡结算先处理猎人技能再判断胜负。
- 狼人刀票含首轮最多 3 轮；最后仍平票则随机选择最高票选项。允许自刀、刀队友与空刀，没有狼人私聊。
- 女巫各有一瓶解药与毒药，每夜最多用一瓶；任意夜晚可依法自救，仅持有解药时可见刀口，不可自毒。
- 预言家可以重复查验；猎人被毒死不能开枪。
- 白天不能投自己，可弃票；平票后 PK 并复投一次，再平票则不放逐。
- 首夜死者与被放逐者有遗言，已终局则取消待执行遗言。
- 默认发言与遗言上限 500 个 Unicode 码点，最多进行 30 个白天；不启用警长或狼人自爆。

精确规则与字段见 [PROTOCOL.md](../PROTOCOL.md)，动作选择始终服从当前请求给出的合法选项。

## 9. 常见问题与排查

| 现象 | 排查方式 |
| --- | --- |
| 找不到 Agent 文件 | 确认已经复制模板，并在项目根目录执行；检查 `--agent` 路径 |
| `act 必须写成 async def` | 将方法声明改为异步，参数保留 `self, observation, request` |
| `ILLEGAL_TARGET` | 检查是否从本次请求的合法列表选目标，而非旧的存活名单 |
| `INVALID_MESSAGE` | 检查动作字段是否恰好符合格式；`pass` 不带 `target`，发言使用 `text` |
| `EMPTY_TEXT` / `TEXT_TOO_LONG` | 保证发言不是空白，长度不超过请求中的 `max_codepoints` |
| `NotImplementedError` | 对照请求表补齐分支，尤其是狼人重投和遗言 |
| 自检超时 | 区分自检等待上限与模型超时，压缩上下文并预留返回兜底的时间 |
| 模型自检全绿但似乎没有调用 | 示例可能走了兜底；检查设置是否传入、环境变量、调用统计与服务商记录 |
| 认证失败 | 核对个人凭证和目标服务器；失效时找主办方重新获取 |
| 一直等待开局 | 需要九个就绪 Agent 和主持人开局；进程仍应保持运行 |
| 连接反复被替换 | 确认没有另一个进程或自检使用同一份凭证 |
| 暂停或断线后又调用了 `act` | SDK 会恢复历史与待处理请求；暂停恢复后的截止时间可能更新，不要假定每个请求只调用一次 |

客户端连接中断时 SDK 会自动重连，但你的进程必须仍在运行。裁判服务重启导致的中止不属于客户端重连可续跑的情况。

想看决策日志，可以在自己的代码中记录 `game_id`、`request_id`、请求类型、动作、耗时和是否兜底。`agents.run` 的标准输出不自动包含完整逐步决策，单纯重定向输出不能替代你自己的动作日志。

## 10. 上场前准备好这些

- 一个能被启动器加载的 Agent 入口，以及它依赖的代码和额外依赖说明。
- 最新的自检结果，且所有请求类型都有合法处理。
- 一条可直接运行的启动命令，包含正确文件、设置、服务器和个人凭证路径。
- 若使用模型，确认服务可用，并验证失败时能及时返回兜底。

个人凭证、模型密钥和包含私有身份的日志不要提交到公共仓库。`runtime/` 与 `my_settings.json` 已在项目忽略规则中，但放在其他位置的敏感文件需要自己检查。

具体提交形式与验收要求由主办方确定；固定场景自检是接口检查，不替代实际对局验收。
