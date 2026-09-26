# 多实例狼人杀 Agent

在项目根目录运行下面的命令（Python 3.11+）。本目录复用上层 `werewolf.sdk` 和协议校验器；

## 1. 先跑校验

```powershell
python -m pip install -e ".[test]"
python -m multi_agent.validate
```

不需要模型密钥，也不占用 8765 端口。脚本启动临时裁判和临时模型 HTTP 服务，真实经过 WebSocket/HTTP 通信，结束后自动关闭。退出码 0 表示通过，1 表示失败。报告位于 `runtime/agent-validation/<本次编号>/`：`summary.json`、`junit.xml`、`tests.txt`。

覆盖范围：

- 狼刀、重投、不杀、弃权；女巫自救、毒药、无药；预言家查验；猎人开枪和放弃；发言、遗言、放逐投票。
- 模型 JSON 解析、重复字段、非法目标、HTTP 429/500、超时和取消；失败不能算作模型成功。
- 九个实例连接真实裁判，连续两局、暂停、重连、继续、无超时兜底、相同结算与回放验证。
- 原项目完整规则/网络/看板数据测试：身份隔离、票数结算、猎人死亡链、鉴权、去重等。

稀有技能由明确的场景强制覆盖，不依赖随机对局碰巧触发。离线测试验证程序和协议，不代表你的外部模型服务已经通过实测，也不保证模型能赢。

## 2. 启动裁判和多个实例

首次创建凭证（已经存在时跳过，命令不会覆盖现有文件）：

```powershell
python -m werewolf.server --init --config runtime/config.json
python -m werewolf.server --config runtime/config.json
```

另开终端，准备配置：

```powershell
Copy-Item multi_agent/config.example.json multi_agent/config.local.json
python -m multi_agent.run --config multi_agent/config.local.json
```

默认启动九个无需模型的策略实例，打开 http://127.0.0.1:8765 ，用 `runtime/config.json` 中的 `admin_token` 登录并开始游戏。**不要同时运行旧 demo 中的九个 Agent 或其他使用相同 ID 的客户端。** 若旧演示占用了端口，先结束旧进程，或给服务器指定其他 `--port` 并修改配置 `server`。

默认整局结束后重新就绪，可连续测试。`--once` 让每个实例完成一局后退出；`--duration 600` 设置包含等待开局和暂停在内的总时限，超过时限退出码为 1；Ctrl+C 停止并保存报告。

`agents` 数组里有几项就启动几个实例，可以只保留 1～3 项，把其余席位分给其他社员。每项必须使用不同 ID 的独立凭证。裁判需要九个就绪实例才能开局。凭证相对路径以配置文件所在目录为基准。

## 3. 接入真实模型

编辑 `config.local.json` 的 `defaults`：

```json
{
  "mode": "llm",
  "base_url": "https://你的服务地址/v1",
  "model": "你的实际模型名",
  "api_key_env": "WEREWOLF_API_KEY",
  "timeout_seconds": 20
}
```

`base_url` 是 API 根路径，程序会追加 `/chat/completions`，不要重复填写。设置环境变量后启动：

```powershell
$env:WEREWOLF_API_KEY = "你的密钥"
python -m multi_agent.run --config multi_agent/config.local.json
```

不同实例可覆盖模型设置，例如：

```json
{"credentials":"../runtime/agent-01.json", "settings":{"model":"另一个模型", "api_key_env":"TEAM_A_KEY"}}
```

兼容接口的请求使用 `model`、`messages`，读取 `choices[0].message.content`；不强制服务端 JSON mode，以适配更多服务商。协议参考 [官方 Chat Completions 文档](https://developers.openai.com/api/reference/resources/chat)。若用无需鉴权的本地兼容服务，设置 `api_key_env` 为 `""`。远程裁判地址改为其 `/ws/agent` 地址。

每个实例只接收自身 WebSocket 历史，独立创建决策对象和随机数状态，不共享推理历史或狼人私聊。多个实例在一个 Python 进程中异步运行；需要进程隔离时分成多个配置、分别执行启动命令。模型端会收到该实例的游戏历史；Agent 不读取主持人配置。

模型超时预算会给提交动作预留 0.5 秒，并压缩到不超过裁判剩余期限；裁判默认每步 `action_timeout_ms:60000`，因此 `timeout_seconds` 设成等于或略大于裁判期限都安全，实际以裁判剩余时间为准。本版每个行动最多调用一次模型，失败会记录并使用合法策略兜底，避免无限重试增加费用。暂停会取消在途决策，继续后按裁判的新截止时间重新决策；供应商已经处理的调用仍可能计费。

## 4. 上场前验证真实模型

```powershell
python -m multi_agent.validate --live multi_agent/config.local.json
```

先运行完整离线测试，通过后对配置中的**每个实例调用 14 次真实模型**（九个实例共 126 次，可能产生费用）。不需要裁判，不向真实对局发送动作。每个场景必须由模型返回合法动作；任何超时、非法动作或兜底都判失败并返回非零退出码。女巫和猎人可依法选择放弃；具体毒药/开枪分支由离线强制场景保证编码通路覆盖。

可以单独建只含一个实例的配置，先做 14 次校验，再启动九个实例进行实际比赛。实际比赛的可用性、限流和模型策略还需结合对局报告判断。

## 5. 查看结果 / 修改策略

每次启动生成独立 `runs/<本次编号>/`，其中每个实例有一个 JSONL 动作日志和统一 `summary.json`。统计包含 `model_success`、`model_errors`、`fallback`、`baseline`、完成局数、SDK 错误和最后结算。日志不写密钥、鉴权头或模型原始错误正文，但包含私人动作，比赛中只供该成员和主持人查看。

`agent.py` 的 `SYSTEM` 是模型提示词，`WerewolfAgent.act()` 是决策入口；`run.py` 负责多实例生命周期；`validate.py` 是校验入口；校验用的 14 个固定场景在 `werewolf/scenarios.py`（社员自检 `python -m agents.check` 用的是同一套）。通信、去重、重连和暂停恢复复用项目 SDK。
