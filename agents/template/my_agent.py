"""Agent 模板：接口都写好了，你只需要填 TODO。

怎么用（三步）
    1. 把整个 template 文件夹复制成你自己的目录，比如 agents/team-alice/
    2. 打开这个文件，按 TODO 改判断逻辑（也可以把逻辑拆到别的文件里 import）
    3. 自检与运行：
           python -m agents.check agents/team-alice/my_agent.py
           python -m agents.run --agent agents/team-alice/my_agent.py --credentials agent-03.json
       （运行时可加 --server ws://<主办方公布的地址>:8765/ws/agent 指定服务器地址）

下面这些注释解释了这个文件里每个东西是什么。**不看也能跑**：默认逻辑虽然很笨，
但每一步都合法，能打完整局——你只需要一点一点替换成自己的策略。
"""


# ---------------------------------------------------------------- 读历史的助手
# 裁判给你的不是"当前局面"，而是**你这个座位能看到的完整消息历史**。
# 下面三个小函数演示怎么从历史里把局面读出来；可以自由改动或删除。

def read_game_history(observation):
    """历史就是一条条消息，每条长这样：

        {"type": "speech_public", "player_id": 5, "content": {"speaker_id": 5, "text": "..."}}

    type 告诉你这是什么事件（role / phase_changed / speech_public / vote_result / death ...），
    content 是这条事件的内容。你只拿得到**自己座位允许看到**的消息。
    """
    return observation.get("history", [])


def my_seat(request):
    """自己的座位号。每条请求都带 player_id，就是你自己。"""
    return request["player_id"]


def my_role(observation):
    """自己的身份和阵营。开局时裁判会单独发一条 role 消息给你。"""
    for message in read_game_history(observation):
        if message["type"] == "role":
            return message["content"]["role"], message["content"]["camp"]
    return None, None


def teammates(observation):
    """狼人队友座位；不是狼人时返回空列表。"""
    for message in read_game_history(observation):
        if message["type"] == "werewolves_info":
            return list(message["content"]["players"])
    return []


def alive_players(observation):
    """最近一次阶段变更里公布的存活座位。"""
    alive = []
    for message in read_game_history(observation):
        if message["type"] == "phase_changed":
            alive = list(message["content"].get("alive_players", []))
    return alive


def public_speeches(observation, limit=8):
    """最近的公开发言，返回 [(座位号, 发言内容), ...]。"""
    spoken = []
    for message in read_game_history(observation):
        if message["type"] == "speech_public" and message["content"].get("status") != "skipped":
            spoken.append((message["content"]["speaker_id"], message["content"].get("text", "")))
    return spoken[-limit:]


def pick(legal, wanted):
    """在合法目标里选一个：优先选你想要的，选不到就退而求其次。

    这是**最重要的一条经验**：裁判只接受 legal_targets / legal_save_targets /
    legal_poison_targets 里的值。你自己算出来的目标如果不在里面，会被判 ILLEGAL_TARGET
    打回来重填（时间还是照走）。所以永远给一个兜底。
    """
    legal = list(legal)
    if not legal:
        return None
    if wanted in legal:
        return wanted
    return legal[0]


# ---------------------------------------------------------------------- Agent

class MyAgent:
    """SDK 会 await 这个类的 act()；另外两个钩子可选，写了就会调用。"""

    def __init__(self, seed=None):
        # 你想记住的任何状态都放这里。注意：同一个对象会连续打多局，
        # 每局开始请用 on_game_start 重置。需要固定随机数时给 seed 传一个值。
        import random
        self.rng = random.Random(seed)
        self.seat = None
        self.role = None

    # ---- 开局钩子（可选）：每局开始时调用一次 ----
    async def on_game_start(self, observation):
        self.seat = my_seat_of(observation)
        self.role, _camp = my_role(observation)
        # TODO(可选)：重置怀疑度、清空上一局的记忆

    # ---- 终局钩子（可选）：对局结束时调用一次 ----
    async def on_game_end(self, result):
        # result = {"outcome": "good_win|werewolves_win|draw|aborted", "reason": ..., "roles": [...]}
        # TODO(可选)：把战绩写到自己的文件里，方便复盘
        pass

    # ---- 必须实现：**每次需要你行动时**裁判都会调用它 ----
    async def act(self, observation, request):
        """参数

        observation: {"game_id": "...", "history": [ ...只属于你这个座位的消息... ]}
                     重试时还会多一个 "last_error"（上次动作为什么被拒）
        request:     {"type": "vote", "request_id": "...", "player_id": 我自己的座位,
                      "content": { ...这一轮能用什么目标、截止时间等... }}

        返回值：一个动作字典，例如 {"action": "vote", "target": 3}
        可以返回 dict 里的值必须是 JSON 能表达的类型（int / str / bool / None / list / dict）。
        """
        me = my_seat(request)
        kind = request["type"]
        content = request["content"]

        # -------- 狼人刀票（type: werewolves_act / werewolves_revote）--------
        if kind in ("werewolves_act", "werewolves_revote"):
            # 合法：target 取 content["legal_targets"] 里的值；0 = 空刀；
            # content["allow_abstain"] 为真时还可以用 None 表示弃票。
            # 注意：可以刀自己人（规则允许），但通常不会这么做。
            # TODO：把"最该刀的座位"算出来，下面只是保底。
            target = pick(content["legal_targets"], self._wolf_wish(observation, content))
            if target is None and content.get("allow_abstain"):
                return {"action": "kill_vote", "target": None}
            return {"action": "kill_vote", "target": target if target is not None else 0}

        # -------- 女巫（type: witch_act）--------
        if kind == "witch_act":
            # 合法动作在 content["legal_actions"] 里：save / poison / pass，
            # 救人用 legal_save_targets，毒人用 legal_poison_targets，每夜最多一瓶。
            legal = content["legal_actions"]
            # TODO：想救/想毒就在这里返回，例如
            #     return {"action": "save", "target": content["legal_save_targets"][0]}
            if "pass" in legal:
                return {"action": "pass"}
            return {"action": legal[0], "target": content[f"legal_{legal[0]}_targets"][0]}

        # -------- 预言家查验（type: seer_act）--------
        if kind == "seer_act":
            # TODO：优先查还没查过、又比较可疑的人
            return {"action": "inspect", "target": pick(content["legal_targets"], None)}

        # -------- 猎人开枪（type: hunter_act）--------
        if kind == "hunter_act":
            # 可以 pass（放弃开枪）。开枪目标必须在 legal_targets 里。
            # TODO：想清楚带走谁再改成 {"action": "shoot", "target": ...}
            return {"action": "pass"}

        # -------- 发言 / 遗言（type: speech / speech_dying）--------
        if kind in ("speech", "speech_dying"):
            # text 不能为空，长度上限是 content["max_codepoints"]（默认 500 个字）。
            # 发言是**公开**的：所有人都会看到，别在这里泄露只有你知道的私有信息。
            # TODO：换成你自己的发言生成（可以调用模型，注意别超过时限）
            text = f"我是 {me} 号。目前信息有限，我会结合后面的发言和票型判断。"
            return {"action": "speak", "text": text[: content["max_codepoints"]]}

        # -------- 白天放逐投票（type: vote）--------
        if kind == "vote":
            # 合法：target 取 legal_targets；0 = 弃票；不能投自己。
            # TODO：把"最该放逐的人"算出来
            return {"action": "vote", "target": pick(content["legal_targets"], self._vote_wish(observation, content))}

        # 理论上不会走到这里：裁判只会发上面这几种请求。
        raise NotImplementedError(f"未处理的请求类型: {kind}")

    # ---- 下面两个是"我想投谁"的示意实现，替换成你的策略 ----
    def _wolf_wish(self, observation, content):
        """狼人想刀的座位：先给一个安全默认（空刀）。"""
        return 0

    def _vote_wish(self, observation, content):
        """白天想放逐的座位：先给一个安全默认（弃票）。"""
        return 0


def my_seat_of(observation):
    """备用：从历史里的 game_start 取自己的座位（和 request["player_id"] 一致）。"""
    for message in read_game_history(observation):
        if message["type"] == "game_start":
            return message["content"].get("your_player_id")
    return None
