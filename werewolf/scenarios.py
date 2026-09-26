"""协议级的固定场景：把每一种请求都造出来，不需要随机对局碰运气。

用途是"校验某个 Agent 是否每个分支都实现了"，因此它同时被
`agents/check.py`（社员自检）和 `multi_agent/validate.py`（主办方全量校验）使用。
这里只依赖 `werewolf.protocol`，不涉及多实例、模型或网络。
"""
from datetime import datetime, timedelta, timezone
from .protocol import envelope, RULES


def cases():
    """14 个场景：(名字, 请求类型, 请求内容, 期望动作)。"""
    common = dict(legal_targets=[2, 3], allow_abstain=True)
    return [
        ("wolf_vote", "werewolves_act", dict(common, legal_targets=[0, 2, 3]), {"action":"kill_vote", "target":2}),
        ("wolf_revote", "werewolves_revote", dict(common, legal_targets=[0, 3]), {"action":"kill_vote", "target":3}),
        ("wolf_no_kill", "werewolves_act", dict(common, legal_targets=[0]), {"action":"kill_vote", "target":0}),
        ("wolf_abstain", "werewolves_revote", common, {"action":"kill_vote", "target":None}),
        ("witch_self_save", "witch_act", dict(antidote_remaining=True, poison_remaining=True, legal_actions=["save","poison","pass"], legal_save_targets=[1], legal_poison_targets=[2,3]), {"action":"save", "target":1}),
        ("witch_poison", "witch_act", dict(antidote_remaining=False, poison_remaining=True, legal_actions=["poison","pass"], legal_save_targets=[], legal_poison_targets=[2,3]), {"action":"poison", "target":2}),
        ("witch_pass", "witch_act", dict(antidote_remaining=False, poison_remaining=False, legal_actions=["pass"], legal_save_targets=[], legal_poison_targets=[]), {"action":"pass"}),
        ("seer", "seer_act", common, {"action":"inspect", "target":2}),
        ("hunter_shoot", "hunter_act", common, {"action":"shoot", "target":3}),
        ("hunter_pass", "hunter_act", dict(legal_targets=[]), {"action":"pass"}),
        ("speech", "speech", dict(max_codepoints=100), {"action":"speak", "text":"我会结合票型判断。"}),
        ("last_words", "speech_dying", dict(max_codepoints=100), {"action":"speak", "text":"请关注我的查验结果。"}),
        ("vote", "vote", dict(legal_targets=[0,2,3]), {"action":"vote", "target":2}),
        ("vote_abstain", "vote", dict(legal_targets=[0]), {"action":"vote", "target":0}),
    ]


def fixture(case, seconds=30):
    """把场景变成一份 (observation, request, expected)，与真实裁判的字段一致。"""
    name, kind, content, expected = case
    deadline = (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()
    request = envelope(kind, dict(content, deadline_at=deadline), "test-game", 1, name)
    role = ("werewolf" if kind.startswith("werewolves") else
            {"witch_act":"witch", "seer_act":"seer", "hunter_act":"hunter"}.get(kind, "villager"))
    # 和真实裁判的开局顺序保持一致：gamerule → game_start → role（带 camp）→ 狼人队友。
    history = [envelope("gamerule", RULES),
               envelope("game_start", dict(players=[dict(player_id=p, alive=True) for p in range(1, 10)],
                                           your_player_id=1)),
               envelope("role", dict(role=role, camp="werewolves" if role == "werewolf" else "good"))]
    if role == "werewolf":
        history.append(envelope("werewolves_info", dict(players=[1,7,9])))
    return dict(game_id="test-game", history=history), request, expected
