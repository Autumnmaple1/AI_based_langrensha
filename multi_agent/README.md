# 多实例运行器：同时启动社员的 Agent

`python -m multi_agent.run` 从配置加载社员的 Python 文件和类，为每份凭证创建一个独立对象，并复用 `werewolf.sdk` 连接裁判。所有实例在一个进程内异步运行，分别持有自己的历史与实例状态；需要进程隔离时拆成多个配置分别运行。不要通过全局变量或共享文件交换私有状态。

## 本地九人测试

完整的凭证说明、配置 JSON 和看板步骤见 [根 README](../README.md#本地同时运行九个自己的-agent)。以下在项目根目录执行，Python 3.11+。

终端 A：

```powershell
python -m pip install -e ".[test]"
if (-not (Test-Path runtime/local/config.json)) {
    python -m werewolf.server --init --config runtime/local/config.json
}
python -m werewolf.server --config runtime/local/config.json --db runtime/local/matches.sqlite3 --host 127.0.0.1 --port 8766
```

终端 B 使用同一 Python 环境，首次复制配置并编辑：

```powershell
Copy-Item multi_agent/config.example.json multi_agent/config.local.json
```

示例默认加载 `../agents/template/my_agent.py` 中的 `MyAgent`，九份凭证位于 `../runtime/local/`，不调用模型。使用自己的代码时修改顶层 `agent` 和 `class`，例如 `../agents/my-team/my_agent.py`、`MyAgent`。然后启动：

```powershell
python -m multi_agent.run --config multi_agent/config.local.json --once
```

打开 [本地看板](http://127.0.0.1:8766)，用 `runtime/local/config.json` 中的 `admin_token` 登录。九人就绪后点击开局，手动推进或切换自动。不要同时运行 demo 或其他使用同一凭证的客户端。

## 配置字段

| 位置 | 字段 | 含义 |
| --- | --- | --- |
| 顶层 | `server` | 裁判完整 WebSocket 地址 |
| 顶层 | `agent` | 默认社员 Python 入口文件 |
| 顶层 | `class` | 可选类名；省略时自动选择文件内首个带 `act` 的类 |
| 顶层 | `defaults` | 默认构造参数，例如模型配置；可以为空对象 |
| 顶层 | `output_dir` | 相对配置目录的日志路径，默认 `runs` |
| 顶层 | `agents` | 实例数组，每项一份不同凭证 |
| 实例 | `credentials` | 必需：个人凭证 JSON，含 `agent_id`、`token` |
| 实例 | `seed` | 可选：传给支持它的构造函数的随机种子 |
| 实例 | `agent` / `class` | 可选：覆盖顶层的文件和类名；换文件时注意同时修改或清除类名 |
| 实例 | `settings` | 可选：覆盖 `defaults` 中同名构造参数 |

Agent、凭证和默认输出目录的相对路径均以**配置文件所在目录**为基准。CLI `--output` 则按当前工作目录解析。无需复制九份代码。

入口必须提供 `async def act(self, observation, request)`；`on_game_start`、`on_game_end` 可选，实现时必须是异步方法。加载器与 `agents.run` 共用：构造函数尝试接收 `seed` 和设置，不兼容时尝试仅设置及无参数构造。因此务必让自己的构造函数明确接收需要的设置，避免因不匹配退回默认值。

启动器会在连接任何裁判之前加载并检查全部社员对象；错误路径、错误类名或同步接口会阻止整批启动。用户模块及构造函数此时会执行，请勿在其中直接连接比赛或执行长时间阻塞操作。

## 模型与不同策略

模型调用由社员类实现，运行器不强制供应商、模型接口或设置字段。使用仓库中的单实例模型示例时，将顶层入口改为：

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

这只是配置片段，需要保留 `server` 和九项 `agents`。启动终端设置密钥：

```powershell
$env:WEREWOLF_API_KEY = "你的密钥"
python -m multi_agent.run --config multi_agent/config.local.json --once
```

此示例追加 `/chat/completions`，失败后返回合法启发式动作。九实例调用可能产生费用和并发限流，合法结果不一定来自模型。

每个实例可以选择不同策略，例如某项配置：

```json
{"credentials": "../runtime/local/agent-02.json", "agent": "../agents/example/baseline_agent.py", "class": "BaselineAgent", "seed": 2}
```

## 运行选项与结果

- `--once`：每个实例完成一局后退出；省略则结束后继续就绪，等待主持人再次开局。
- `--duration 600`：总时限，包含连接、等开局及暂停；超时返回非零退出码。
- `--output runtime/my-runs`：覆盖输出目录，每次仍创建独立编号子目录。
- `Ctrl+C`：取消所有客户端并保存当前报告。

每次运行产生每个实例的 JSONL 日志与统一 `summary.json`。社员模式记录返回动作、耗时、决策次数、SDK 事件和错误、完成局数及最后结果。模型成功、失败和兜底次数由社员代码自行统计，运行器不作推断。日志中的动作必须结合 `action_ack` 判断是否被裁判接受。暂停导致的取消不会计为一次完成决策。

日志不主动记录凭证或异常正文，但包含私人动作；社员自己返回的文本也会写入日志，不要在动作中包含密钥。

## 自检与旧配置兼容

```powershell
python -m agents.check agents/template/my_agent.py
python -m pytest -q
python -m multi_agent.validate
```

`agents.check` 接受自己的入口和 `--settings`，检查固定场景的合法性；合法兜底也能通过。`multi_agent.validate` 运行离线回归测试，并输出 `runtime/agent-validation/<编号>/` 报告，不调用真实模型。

为保留现有模型评测，未指定 `agent` 的旧配置仍使用 `multi_agent.agent.WerewolfAgent`，其 `defaults.mode` 支持 `baseline` 或 `llm`，保留原来的模型统计与 `--live` 校验。新配置示例默认使用社员模板。

`python -m multi_agent.validate --live <旧配置>` 仅支持所有实例均未指定 `agent` 的内置 LLM 配置，每个实例执行 14 次真实模型调用；任何兜底视为失败。社员配置会明确拒绝该选项，避免误用内置模型替代自己的代码进行验证。自定义模型是否成功调用，需要自己的统计和真实对局确认。
