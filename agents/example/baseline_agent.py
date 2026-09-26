"""完整示例 Agent：不调用任何模型，用最简单的启发式打满一局。

它做的事（每个分支都对应模板里的一个 TODO，可以作为参考）：

* 从公开历史里统计"谁被点名最多"，当成嫌疑度；被投票放逐过的人权重更高。
* 狼人：刀掉嫌疑度最高的存活者；没有合适对象就空刀。
* 女巫：自己被刀就救，否则不用药；有人嫌疑度很高且是第 2 夜以后才下毒（避免误伤）。
* 预言家：先查没查过的、嫌疑度最高的人。
* 猎人：开枪带走嫌疑度最高的人，没有就放弃。
* 发言：报自己的座位号 + 一句自己的判断；狼人不会暴露队友。
* 投票：投嫌疑度最高的合法目标，没有就弃票。

运行方式（先按 README 拿到自己的凭证文件）：

    python -m agents.check agents/example/baseline_agent.py
    python -m agents.run   --agent agents/example/baseline_agent.py \
                           --server ws://127.0.0.1:8765/ws/agent \
                           --credentials agent-01.json
"""
import random


# ------------------------------------------------------------------ 读历史

def events(observation, kind):
    return [m["content"] for m in observation.get("history", []) if m["type"] == kind]


def my_role(observation):
    for content in events(observation, "role"):
        return content["role"], content["camp"]
    return None, None


def teammates(observation):
    for content in events(observation, "werewolves_info"):
        return set(content["players"])
    return set()


def alive(observation):
    latest = []
    for content in events(observation, "phase_changed"):
        latest = list(content.get("alive_players", []))
    return latest


def public_speeches(observation, limit=8):
    """最近的公开发言：返回 [(座位号, 发言内容), ...]。"""
    spoken = [(content["speaker_id"], content.get("text", ""))
              for content in events(observation, "speech_public")
              if content.get("status") != "skipped"]
    return spoken[-limit:]


def my_checks(observation):
    """预言家自己的查验记录：{座位: "werewolf"/"good"}。"""
    return {c["target"]: c["alignment"] for c in events(observation, "seer_result") if c.get("target")}


def suspicion(observation):
    """一个很粗糙的嫌疑度：被公开点名 +1，被投过票 +2。谁都会算，够用就行。"""
    score = {}
    for content in events(observation, "speech_public"):
        text = content.get("text") or ""
        speaker = content.get("speaker_id")
        for seat in range(1, 10):
            if seat != speaker and f"{seat} 号" in text:
                score[seat] = score.get(seat, 0) + 1
    for content in events(observation, "vote_result"):
        for vote in content.get("votes", []):
            if vote.get("target"):
                score[vote["target"]] = score.get(vote["target"], 0) + 2
    return score


def pick(legal, wanted):
    legal = list(legal)
    if not legal:
        return None
    return wanted if wanted in legal else legal[0]


def most_suspicious(observation, legal, avoid=()):
    score = suspicion(observation)
    candidates = [seat for seat in legal if seat not in avoid]
    if not candidates:
        return None
    return max(candidates, key=lambda seat: (score.get(seat, 0), -seat))


# ----------------------------------------------------------------- Agent

class BaselineAgent:
    """和模板一样的接口：act() 必须有，两个钩子可选。"""

    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        # 不依赖 on_game_start 也先给个默认值，避免忘记调用钩子时直接报错。
        self.role, self.camp = None, None

    async def on_game_start(self, observation):
        self.role, self.camp = my_role(observation)

    async def on_game_end(self, result):
        pass

    async def act(self, observation, request):
        kind, content = request["type"], request["content"]
        me = request["player_id"]
        night = content.get("night")
        camp = self.camp or my_role(observation)[1]

        if kind in ("werewolves_act", "werewolves_revote"):
            legal = [t for t in content["legal_targets"] if t not in teammates(observation)]
            target = most_suspicious(observation, legal)
            if target is None:
                if 0 in content["legal_targets"]:
                    return {"action": "kill_vote", "target": 0}      # 空刀
                if content.get("allow_abstain"):
                    return {"action": "kill_vote", "target": None}   # 弃票
                return {"action": "kill_vote", "target": content["legal_targets"][0]}
            return {"action": "kill_vote", "target": target}

        if kind == "witch_act":
            legal_actions = content["legal_actions"]
            kill = content.get("kill_target")
            if "save" in legal_actions and kill == me:
                return {"action": "save", "target": pick(content["legal_save_targets"], me)}
            if "poison" in legal_actions and (night or 0) >= 2:
                target = most_suspicious(observation, content["legal_poison_targets"], avoid={me})
                if target is not None and suspicion(observation).get(target, 0) >= 3:
                    return {"action": "poison", "target": target}
            return {"action": "pass"}

        if kind == "seer_act":
            checked = my_checks(observation)
            legal = [seat for seat in content["legal_targets"] if seat not in checked]
            return {"action": "inspect", "target": pick(legal or content["legal_targets"],
                                                        most_suspicious(observation, legal or content["legal_targets"]))}

        if kind == "hunter_act":
            target = most_suspicious(observation, content["legal_targets"], avoid={me})
            if target is None:
                return {"action": "pass"}
            return {"action": "shoot", "target": target}

        if kind in ("speech", "speech_dying"):
            limit = content["max_codepoints"]
            target = most_suspicious(observation, [s for s in range(1, 10) if s != me])
            if camp == "werewolves":
                text = f"我是 {me} 号，我认为 {target} 号的发言前后矛盾，建议今天重点关注。" if target \
                    else f"我是 {me} 号，目前还没有明确的方向。"
            else:
                text = f"我是 {me} 号，{target} 号的发言最可疑，我倾向今天放逐他。" if target \
                    else f"我是 {me} 号，我先听大家的判断。"
            return {"action": "speak", "text": text[:limit]}

        if kind == "vote":
            target = most_suspicious(observation, content["legal_targets"], avoid={me})
            if target is None:
                target = pick(content["legal_targets"], 0)   # 没思路就弃票（0）
            return {"action": "vote", "target": target}

        raise NotImplementedError(f"未处理的请求类型: {kind}")
