# 两个完整示例（可以直接跑）

这两个文件不是让你复制的，是让你**读**的：每个分支都对应模板里的一个 TODO，
你可以照着自己的需要抄某一段。改自己的 agent 时请改 `agents/team-<名字>/` 里的文件。

## 1. `baseline_agent.py`：不调模型的启发式

只看公开信息就能打：

- 嫌疑度 = 被公开点名次数 + 被投过票的次数×2；
- 狼人刀最可疑的人；女巫被刀就自救；预言家查没查过的人；猎人开枪带走最可疑的人；
- 发言报座位号 + 一句判断（狼人不暴露队友）；投票投最可疑的合法目标。

```sh
python -m agents.check agents/example/baseline_agent.py       # 14/14 通过，0ms 级
python -m agents.run --agent agents/example/baseline_agent.py --credentials agent-01.json --server ws://127.0.0.1:8765/ws/agent
```

## 2. `llm_agent.py`：把决策交给模型（单实例）

它参考了主办方批量跑九实例用的 `multi_agent/agent.py`，但只保留**一个连接、一份设置**：

- 先算好启发式的兜底答案，再去问模型；
- 只把"形势卡"（我的身份、存活名单、最近发言摘要、当前请求）发给模型，不给原始历史——
  上下文越长模型越慢，很容易撞上每步 60 秒的上限；
- 模型超时、报错、返回不是 JSON、目标不合法，一律静默退回兜底答案，保证这一步交得出动作。

先复制一份设置并填好自己的模型与密钥环境变量：

```sh
Copy-Item agents\example\settings.example.json my_settings.json
# 编辑 my_settings.json：base_url / model / api_key_env
set WEREWOLF_API_KEY=你的密钥            # PowerShell: $env:WEREWOLF_API_KEY="你的密钥"

# 不连模型也能自检（走兜底路径）
python -m agents.check agents/example/llm_agent.py

# 连真实模型自检：14 个场景 = 14 次模型调用，会消耗额度
python -m agents.check agents/example/llm_agent.py --settings my_settings.json --timeout 60

# 上场比赛
python -m agents.run --agent agents/example/llm_agent.py --settings my_settings.json \
                     --credentials agent-03.json --server ws://<主办方公布的地址>:8765/ws/agent
```

（`agents.run --settings` 会把 JSON 里的键作为关键字参数传给你的 Agent 构造函数；
你的类可以只挑选自己认识的键，`llm_agent.py` 就是通过 `**_ignored` 忽略多余项的。）
