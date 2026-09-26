# 模板使用说明（三步）

```sh
# 1) 复制整个文件夹，改成你自己的目录
Copy-Item -Recurse agents\template agents\team-alice     # macOS/Linux: cp -r agents/template agents/team-alice

# 2) 打开 agents/team-alice/my_agent.py，按 TODO 改逻辑

# 3) 自检
python -m agents.check agents/team-alice/my_agent.py
```

## 这个文件夹里有什么

| 文件 | 说明 |
|---|---|
| `my_agent.py` | **你要改的文件**。所有接口都写好了，默认逻辑很笨但完全合法，能打完整局 |
| `credentials.example.json` | 凭证文件的格式示例。真正要用的是主办方单独发给你的 `agent-XX.json` |

## 改哪里

`my_agent.py` 里每个 `# TODO` 都是一个可以立刻改进的点，按重要程度排：

1. `_vote_wish()` —— 白天投谁（默认弃票）。先用"谁被点名最多"这种简单规则就能打。
2. `_wolf_wish()` —— 晚上刀谁（默认空刀）。改成"刀掉最像好人的那个"立刻变强。
3. 女巫的 `witch_act` 分支 —— 默认不用药；可以先改成"自己被刀就救"。
4. 预言家的 `seer_act` 分支 —— 默认查第一个合法目标；改成优先查没查过、又最可疑的人。
5. 发言分支 —— 默认一句话；可以把自己的推理过程说出来（但别泄露只有你知道的信息）。

改完随时再跑一次 `python -m agents.check <你的文件>`：它会用 14 个固定场景把你的每个动作
送进裁判的校验器，明确指出是哪个分支、错在哪里。

## 连服务器

```sh
python -m agents.run --agent agents/team-alice/my_agent.py --credentials agent-03.json --server ws://<主办方公布的地址>:8765/ws/agent
```

服务器地址每次活动可能不同，主办方会现场公布；座位号和身份每局随机分配，运行时才会告诉你。
更详细的说明见 [`../README.md`](../README.md)。
