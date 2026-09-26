"""单实例示例：把决策交给一个兼容 Chat Completions 的模型，失败就退回启发式。

本类可通过 agents.run 启动一个实例，也可由 multi_agent.run 加载多个独立实例。

要不要用模型由设置决定（见 settings.example.json）：

    python -m agents.check agents/example/llm_agent.py     # 不连模型也能通过（走兜底）
    set WEREWOLF_API_KEY=你的密钥                          # Windows: set / PowerShell: $env:
    python -m agents.run --agent agents/example/llm_agent.py \
                         --settings agents/example/settings.example.json \
                         --server ws://<主办方公布的地址>:8765/ws/agent \
                         --credentials agent-03.json

设计要点（也是给自己写 Agent 时值得照抄的几个习惯）：
1. 模型只负责"选哪一个"，合法性仍由本地校验器兜底，不合法就用启发式答案；
2. 模型超时/报错/返回不是 JSON，一律静默退回启发式，绝不让这一步卡死；
3. 预算里留出 0.5 秒给网络回传，别把裁判给的时间全花在思考上。
"""
import asyncio
import json
import os

from aiohttp import ClientSession, ClientTimeout
from werewolf.protocol import strict_loads, validate_action

from agents.example.baseline_agent import (BaselineAgent, alive, most_suspicious,
                                          my_checks, my_role, public_speeches, suspicion, teammates)

SYSTEM_PROMPT = """你是九人狼人杀里的一个玩家，正在替我做一个决定。
我会给你我的座位、身份、场上形势和当前请求；你只回答一个 JSON 动作，不要解释、不要 Markdown。
可用格式：
  刀票 {"action":"kill_vote","target":编号或0或null}
  查验 {"action":"inspect","target":编号}
  解药 {"action":"save","target":编号}   毒药 {"action":"poison","target":编号}   不用药 {"action":"pass"}
  开枪 {"action":"shoot","target":编号}   放弃 {"action":"pass"}
  发言 {"action":"speak","text":"你的公开发言"}
  投票 {"action":"vote","target":编号或0}
target 必须来自请求里给出的 legal_targets / legal_save_targets / legal_poison_targets。
发言是公开的，不要泄露只有自己知道的信息。"""


class LlmAgent:
    """接口和模板完全一样：act() 必须有，两个钩子可选。"""

    def __init__(self, seed=None, *, base_url=None, model=None, api_key_env="WEREWOLF_API_KEY",
                 timeout_seconds=20, temperature=0.7):
        self.baseline = BaselineAgent(seed=seed)     # 兜底策略，顺便也用来读历史
        self.base_url = base_url
        self.model = model
        self.api_key_env = api_key_env
        self.timeout_seconds = float(timeout_seconds)
        self.temperature = float(temperature)

    # ---- 钩子和模板一致：转交给兜底策略维护它自己的状态 ----
    async def on_game_start(self, observation):
        await self.baseline.on_game_start(observation)

    async def on_game_end(self, result):
        pass

    async def act(self, observation, request):
        fallback = await self.baseline.act(observation, request)   # 先算好保底答案
        if not (self.base_url and self.model):
            return fallback                                        # 没配模型就直接用兜底
        deadline = request["content"].get("deadline_at")
        budget = self.timeout_seconds
        if deadline:
            from datetime import datetime, timezone
            remaining = (datetime.fromisoformat(deadline.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
            budget = min(budget, remaining - 0.5)                  # 留 0.5 秒回传
        if budget <= 0:
            return fallback
        try:
            async with asyncio.timeout(budget):
                action = await self._ask_model(observation, request)
            validate_action(request["type"], request["content"], action)   # 本地先校验一遍
            return action
        except asyncio.CancelledError:
            raise                      # 暂停/退出是真的要取消，不要吞掉
        except Exception:
            # 超时、断网、HTTP 报错、返回不是 JSON、目标不合法……全部退回兜底，
            # 保证这一步一定能交出一个合法动作。
            return fallback

    # ------------------------------------------------------------------ 模型
    async def _ask_model(self, observation, request):
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        payload = {"model": self.model, "temperature": self.temperature, "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(self._situation(observation, request), ensure_ascii=False)}]}
        # 单实例场景下每次新建会话最简单；想省握手开销可以让一个 ClientSession 常驻。
        async with ClientSession(timeout=ClientTimeout(total=self.timeout_seconds)) as session:
            async with session.post(self.base_url.rstrip("/") + "/chat/completions",
                                    json=payload, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                data = await response.json()
        text = data["choices"][0]["message"]["content"]
        if isinstance(text, str) and text.strip().startswith("```"):
            text = text.strip().strip("`").removeprefix("json").strip()
        return strict_loads(text)          # 动作字段由 validate_action 校验

    def _situation(self, observation, request):
        """给模型看的"形势卡"：只放它需要的，别把原始历史整个丢过去（越长越慢）。"""
        role, camp = my_role(observation)
        score = suspicion(observation)
        return {
            "my_seat": request["player_id"],
            "my_role": role,
            "my_camp": camp,
            "teammates": sorted(teammates(observation)),
            "alive_players": alive(observation),
            "my_checks": my_checks(observation),
            "most_suspected": most_suspicious(observation, [s for s in range(1, 10)]),
            "suspicion": dict(sorted(score.items(), key=lambda item: -item[1])[:5]),
            "recent_speeches": [f"{seat} 号：{text}" for seat, text in public_speeches(observation)],
            "request": {"type": request["type"], "content": request["content"]},
        }
