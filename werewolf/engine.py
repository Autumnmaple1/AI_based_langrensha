"""Game rules. No sockets, model calls, or dashboard dependencies."""
import random
from collections import Counter
from copy import deepcopy
from .protocol import ROLES, RULES, LIMITS, RULE_VERSION, uid, top_choices


class Game:
    def __init__(self, emit, ask_many, *, seed=None, roles=None, limits=None, game_id=None, gate=None):
        self.emit_callback, self.ask_many = emit, ask_many
        # Host step control: when the referee runs in step mode every call of
        # step() waits for the organizer to release that step. None means the
        # match runs on its own, which is what tests and free-run configurations use.
        self.gate = gate
        self.rng = random.Random(seed)
        deck = list(ROLES) if roles is None else list(roles)
        if Counter(deck) != Counter(ROLES):
            raise ValueError("A game requires exactly 3 wolves, 3 villagers, 1 seer, 1 witch, 1 hunter")
        if roles is None:
            self.rng.shuffle(deck)
        self.roles = dict(enumerate(deck, 1))
        self.alive = set(self.roles)
        self.id = game_id or uid()
        self.limits = dict(LIMITS, **(limits or {}))
        self.day = self.night = 0
        self.period, self.phase = "night", "setup"
        self.antidote = self.poison = 1
        self.checks = []
        self.result = None
        self.order = []
        self.first_speaker = None
        self.direction = self.rng.choice([-1, 1])

    def players(self):
        return [{"player_id": p, "alive": p in self.alive} for p in self.roles]

    def camp(self, player):
        return "werewolves" if self.roles[player] == "werewolf" else "good"

    def role_player(self, role):
        return next(p for p, r in self.roles.items() if r == role)

    async def emit(self, kind, content, recipients=None, request_id=None):
        await self.emit_callback(kind, content, recipients, request_id)

    async def step(self, label):
        """Announce the upcoming step and wait for the host to release it."""
        if self.gate:
            await self.gate(label)

    async def phase_event(self):
        await self.emit("phase_changed", dict(period=self.period, day=self.day, night=self.night,
                                             alive_players=sorted(self.alive), speech_order=self.order if self.period == "day" else []))

    async def ask(self, player, kind, content):
        return (await self.ask_many([(player, kind, content)]))[player]

    async def initialize(self):
        await self.emit("gamerule", dict(rule_version=RULE_VERSION, rules=RULES, limits=self.limits,
                                        description="9人预女猎；屠边；女巫每夜可自救；无警长、自爆、狼聊；狼刀最多三轮。完整规则见 PROTOCOL.md。"))
        for p in self.roles:
            await self.emit("game_start", dict(players=self.players(), your_player_id=p), [p])
        for p, role in self.roles.items():
            await self.emit("role", dict(role=role, camp=self.camp(p)), [p])
        wolves = [p for p, r in self.roles.items() if r == "werewolf"]
        await self.emit("werewolves_info", {"players": wolves}, wolves)

    def victory(self):
        living = [self.roles[p] for p in self.alive]
        if "werewolf" not in living:
            return "good_win", "all_wolves_dead"
        if "villager" not in living:
            return "werewolves_win", "all_villagers_dead"
        if not any(role in living for role in ("seer", "witch", "hunter")):
            return "werewolves_win", "all_specials_dead"
        return None

    async def finish(self, outcome, reason):
        if self.result:
            return
        self.result = dict(outcome=outcome, reason=reason,
                           roles=[dict(player_id=p, role=r) for p, r in self.roles.items()], day=self.day, night=self.night)
        self.phase = self.period = "ended"
        await self.phase_event()
        await self.emit("game_end", self.result)

    async def check_victory(self):
        win = self.victory()
        if win:
            await self.finish(*win)
            return True
        return False

    async def wolf_vote(self):
        self.phase = "wolf_vote"
        wolves = sorted(p for p in self.alive if self.roles[p] == "werewolf")
        previous = []
        for round_no in range(1, 4):
            kind = "werewolves_act" if round_no == 1 else "werewolves_revote"
            content = dict(night=self.night, round=round_no, max_rounds=3, previous_votes=previous,
                           legal_targets=[0] + sorted(self.alive), allow_abstain=True)
            replies = await self.ask_many([(p, kind, deepcopy(content)) for p in wolves])
            votes = [dict(voter=p, target=replies[p][0]["target"]) for p in wolves]
            tops = top_choices(votes, wolf=True)
            selected, method = None, "pending"
            if len(tops) == 1:
                selected, method = tops[0], "unique_plurality"
            elif round_no == 3:
                selected = self.rng.choice(tops) if tops else 0
                method = "random_tie" if tops else "all_abstain"
            decision = "revote" if selected is None else "kill" if selected else "no_kill"
            await self.emit("werewolves_result", dict(night=self.night, round=round_no, votes=votes,
                            top_choices=tops, decision=decision, selected_target=selected, selection_method=method), wolves)
            if selected is not None:
                return selected
            previous = votes
        raise AssertionError("unreachable")

    def witch_request(self, kill):
        witch = self.role_player("witch")
        saves = [kill] if self.antidote and kill else []
        poisons = sorted(self.alive - {witch}) if self.poison else []
        return dict(night=self.night, antidote_remaining=self.antidote, poison_remaining=self.poison,
                    kill_target_visible=bool(self.antidote), kill_target=kill or None if self.antidote else None,
                    legal_actions=["pass"] + (["save"] if saves else []) + (["poison"] if poisons else []),
                    legal_save_targets=saves, legal_poison_targets=poisons)

    @staticmethod
    def night_deaths(kill, saved, poison):
        return ({kill} if kill and not saved else set()) | ({poison} if poison else set())

    async def kill(self, players, source):
        deaths = sorted(set(players) & self.alive)
        self.alive.difference_update(deaths)
        await self.emit("death", dict(period="night" if source == "night_resolution" else "day",
                                     day=self.day, night=self.night, source=source, players=deaths))
        return deaths

    async def hunter(self, deaths, poisoned=None, trigger="night"):
        hunter = self.role_player("hunter")
        if hunter not in deaths or hunter == poisoned:
            return
        self.phase = "hunter_action"
        await self.step("猎人开枪")
        action, rid = await self.ask(hunter, "hunter_act", dict(trigger=trigger, legal_targets=sorted(self.alive), allow_pass=True))
        target = action.get("target")
        await self.emit("hunter_result", dict(action=action["action"], target=target), [hunter], rid)
        if action["action"] == "shoot":
            await self.emit("hunter_shoot", dict(hunter=hunter, target=target))
            await self.kill([target], "hunter")

    def day_order(self):
        if not self.alive:
            return []
        if self.first_speaker is None:
            start = self.rng.choice(sorted(self.alive))
        else:
            start = self.first_speaker
            for _ in range(9):
                start = (start - 1 + self.direction) % 9 + 1
                if start in self.alive:
                    break
        self.first_speaker = start
        return [p for step in range(9) if (p := (start - 1 + step * self.direction) % 9 + 1) in self.alive]

    async def speak(self, players, kind):
        self.phase = {"normal": "day_speech", "pk": "pk_speech", "last_words": "last_words"}[kind]
        label = {"normal": "白天发言", "pk": "PK 发言", "last_words": "遗言"}[kind]
        for p in players:
            await self.step(f"{label} · {p} 号")
            request_type = "speech_dying" if kind == "last_words" else "speech"
            content = dict(day=self.day, kind=kind, max_codepoints=self.limits["speech_max_codepoints"])
            content.update({"night": self.night} if kind == "last_words" else {"order": list(players)})
            action, _ = await self.ask(p, request_type, content)
            await self.emit("speech_public", dict(speaker_id=p, kind=kind, text=action["text"] if action else "",
                                                 status="spoken" if action else "skipped"))

    async def night_turn(self):
        self.night += 1
        self.period, self.phase = "night", "night_start"
        await self.step(f"第 {self.night} 夜 · 入夜")
        await self.phase_event()
        self.phase = "wolf_vote"
        await self.step(f"第 {self.night} 夜 · 狼人刀票")
        kill = await self.wolf_vote()
        saved, poisoned = False, None
        witch = self.role_player("witch")
        if witch in self.alive:
            self.phase = "witch_action"
            await self.step("女巫行动")
            action, rid = await self.ask(witch, "witch_act", self.witch_request(kill))
            if action["action"] == "save":
                self.antidote, saved = 0, True
            if action["action"] == "poison":
                self.poison, poisoned = 0, action["target"]
            await self.emit("witch_result", dict(night=self.night, action=action["action"], target=action.get("target"),
                            antidote_remaining=self.antidote, poison_remaining=self.poison), [witch], rid)
        seer = self.role_player("seer")
        if seer in self.alive:
            self.phase = "seer_action"
            await self.step("预言家查验")
            action, rid = await self.ask(seer, "seer_act", dict(night=self.night, legal_targets=sorted(self.alive - {seer})))
            target = action["target"] if action else None
            alignment = ("werewolf" if self.roles[target] == "werewolf" else "good") if target else None
            if action:
                self.checks.append(dict(night=self.night, target=target, alignment=alignment))
            await self.emit("seer_result", dict(night=self.night, status="checked" if action else "skipped",
                                               target=target, alignment=alignment), [seer], rid)
        self.phase = "night_resolution"
        await self.step("夜间结算")
        deaths = self.night_deaths(kill, saved, poisoned) & self.alive
        self.alive.difference_update(deaths)
        self.day += 1
        self.period = "day"
        self.order = self.day_order()
        await self.phase_event()
        await self.emit("death", dict(period="night", day=self.day, night=self.night,
                                      source="night_resolution", players=sorted(deaths)))
        await self.hunter(deaths, poisoned)
        self.order = [p for p in self.order if p in self.alive]
        if self.order:
            self.first_speaker = self.order[0]
        if await self.check_victory():
            return
        if self.night == 1:
            await self.speak(sorted(deaths), "last_words")

    async def day_vote(self):
        candidates = sorted(self.alive)
        for round_no in (1, 2):
            self.phase = "day_vote" if round_no == 1 else "pk_vote"
            await self.step("放逐投票" if round_no == 1 else "平票复投")
            voters = sorted(self.alive if round_no == 1 else self.alive - set(candidates))
            replies = await self.ask_many([(p, "vote", dict(day=self.day, round=round_no, candidates=candidates,
                        legal_targets=[0] + [t for t in candidates if t != p], allow_abstain=True)) for p in voters])
            votes = [dict(voter=p, target=replies[p][0]["target"]) for p in voters]
            tops = top_choices(votes)
            exiled = tops[0] if len(tops) == 1 else None
            decision = "exile" if exiled else "pk" if len(tops) > 1 and round_no == 1 else "no_exile"
            await self.emit("vote_result", dict(day=self.day, round=round_no, votes=votes,
                            top_candidates=tops, decision=decision, exiled_player=exiled))
            if decision != "pk":
                return exiled
            candidates = tops
            await self.speak([p for p in self.order if p in candidates], "pk")

    async def day_turn(self):
        await self.speak(self.order, "normal")
        exiled = await self.day_vote()
        self.phase = "day_resolution"
        if exiled:
            await self.step("放逐结算")
            deaths = await self.kill([exiled], "exile")
            await self.hunter(deaths, trigger="exile")
        if await self.check_victory():
            return
        if exiled:
            await self.speak([exiled], "last_words")
        if self.day >= self.limits["max_days"]:
            await self.finish("draw", "max_days")

    async def run(self):
        await self.initialize()
        while not self.result:
            await self.night_turn()
            if not self.result:
                await self.day_turn()

    def state(self):
        return dict(id=self.id, roles=self.roles, alive=sorted(self.alive), limits=self.limits,
                    day=self.day, night=self.night, period=self.period, phase=self.phase,
                    antidote=self.antidote, poison=self.poison, checks=self.checks,
                    result=self.result, order=self.order, first_speaker=self.first_speaker, direction=self.direction)

    def restore(self, state):
        for key, value in state.items():
            setattr(self, key, value)
        self.roles = {int(p): role for p, role in self.roles.items()}
        self.alive = set(self.alive)
