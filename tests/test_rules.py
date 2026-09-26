import random
from copy import deepcopy
import pytest
from werewolf.engine import Game
from werewolf.protocol import (ROLES, ProtocolError, client_message, default_action, strict_loads,
                               top_choices, validate_action, validate_client)


class Harness:
    def __init__(self, policy=None, seed=42, limits=None):
        self.events, self.requests = [], []
        self.policy = policy or self.defaults
        self.game = Game(self.emit, self.ask, roles=ROLES, seed=seed, limits=limits)

    def defaults(self, p, kind, c):
        if kind == "seer_act":
            return {"action": "inspect", "target": c["legal_targets"][0]}
        if kind in {"speech", "speech_dying"}:
            return {"action": "speak", "text": "测试发言"}
        return default_action(kind)

    async def emit(self, kind, content, recipients=None, request_id=None):
        self.events.append((kind, deepcopy(content), recipients, request_id))

    async def ask(self, specs):
        answers = {}
        for p, kind, c in specs:
            self.requests.append((p, kind, deepcopy(c)))
            a = self.policy(p, kind, c)
            if a is not None:
                validate_action(kind, c, a)
            answers[p] = (a, f"req-{len(self.requests)}")
        return answers


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', 'not json', '{', b'{}', r'{"text":"\ud800"}'])
def test_strict_json(raw):
    with pytest.raises(ProtocolError):
        strict_loads(raw)


def test_client_strict_envelope():
    good = client_message("action", {"action": "vote", "target": 2}, "g", "r")
    assert validate_client(good) is good
    for key, value in [("player_id", 1), ("seq", 2), ("extra", 0)]:
        with pytest.raises(ProtocolError):
            validate_client(dict(good, **{key: value}))
    with pytest.raises(ProtocolError):
        validate_client(client_message("ready", {}, "g"))


@pytest.mark.parametrize("action,code", [
    ({"action":"vote","target":True}, "ILLEGAL_TARGET"),
    ({"action":"vote","target":"2"}, "ILLEGAL_TARGET"),
    ({"action":"vote","target":None}, "ILLEGAL_TARGET"),
    ({"action":"vote","target":1}, "ILLEGAL_TARGET"),
    ({"action":"vote","target":2,"text":"hi"}, "INVALID_MESSAGE"),
    ({"action":"poison","target":2}, "ILLEGAL_ACTION"),
    ({"action":"vote"}, "INVALID_MESSAGE"),
])
def test_invalid_votes(action, code):
    with pytest.raises(ProtocolError) as err:
        validate_action("vote", {"legal_targets": [0,2,3]}, action)
    assert err.value.code == code


@pytest.mark.parametrize("text,valid", [("你好",True),("😀😀",True),("😀😀😀",False),("  ",False),(123,False)])
def test_speech_unicode(text, valid):
    if valid:
        validate_action("speech", {"max_codepoints":2}, {"action":"speak","text":text})
    else:
        with pytest.raises(ProtocolError):
            validate_action("speech", {"max_codepoints":2}, {"action":"speak","text":text})


@pytest.mark.parametrize("votes,wolf,expected", [
    ([0,0,3],True,[0]),([0,0,3],False,[3]),([None,None],True,[]),
    ([1,2,3],True,[1,2,3]),([2,2,3],True,[2]),([0,0],False,[])
])
def test_plurality(votes,wolf,expected):
    assert top_choices([{"target":v} for v in votes],wolf=wolf)==expected


@pytest.mark.parametrize("mode,rounds,method,targets", [
    ("unique",1,"unique_plurality",[4]),("tie",3,"random_tie",[4,5,6]),
    ("abstain",3,"all_abstain",[0]),("no_kill",1,"unique_plurality",[0]),
    ("zero_tie",3,"random_tie",[0,4,5]),("resolve_second",2,"unique_plurality",[9]),
])
async def test_wolf_rounds(mode,rounds,method,targets):
    def policy(p,k,c):
        target={"unique":4,"tie":p+3,"abstain":None,"no_kill":0,"zero_tie":{1:0,2:4,3:5}[p],
                "resolve_second":p+3 if c["round"]==1 else 9}[mode]
        return {"action":"kill_vote","target":target}
    h=Harness(policy)
    result=await h.game.wolf_vote()
    assert result in targets
    assert len(h.events)==rounds
    assert h.events[-1][1]["selection_method"]==method
    assert all(e[2]==[1,2,3] for e in h.events)
    if rounds>1:
        assert h.requests[3][2]["previous_votes"]==h.events[0][1]["votes"]


@pytest.mark.parametrize("night", [1,2,10])
def test_witch_can_self_save_every_night(night):
    h=Harness();g=h.game;g.night=night
    c=g.witch_request(8)
    validate_action("witch_act",c,{"action":"save","target":8})
    assert c["legal_save_targets"]==[8]


def test_witch_visibility_and_validation():
    g=Harness().game
    c=g.witch_request(0)
    assert c["kill_target_visible"] and c["kill_target"] is None and not c["legal_save_targets"]
    with pytest.raises(ProtocolError):
        validate_action("witch_act",c,{"action":"poison","target":8})
    with pytest.raises(ProtocolError):
        validate_action("witch_act",c,{"action":"save","target":8,"poison":3})
    g.antidote=0
    c=g.witch_request(4)
    assert c["kill_target_visible"] is False and c["kill_target"] is None
    with pytest.raises(ProtocolError) as err:
        validate_action("witch_act",c,{"action":"save","target":4})
    assert err.value.code=="NO_ANTIDOTE"


@pytest.mark.parametrize("kill,saved,poison,expected",[(4,False,None,{4}),(4,True,None,set()),(4,True,4,{4}),(4,False,5,{4,5}),(0,False,5,{5}),(0,False,None,set())])
def test_simultaneous_deaths(kill,saved,poison,expected):
    assert Game.night_deaths(kill,saved,poison)==expected


async def test_dying_seer_still_checks_and_double_death():
    h=Harness()
    def policy(p,k,c):
        if k.startswith("werewolves_"):return {"action":"kill_vote","target":7}
        if k=="witch_act":return {"action":"poison","target":4}
        return h.defaults(p,k,c)
    h.policy=policy
    await h.game.night_turn()
    assert any(k=="seer_act" for _,k,_ in h.requests)
    assert 7 not in h.game.alive and 4 not in h.game.alive
    death=next(c for k,c,_,_ in h.events if k=="death")
    assert death["players"]==[4,7]
    assert "cause" not in death
    assert [p for p,k,_ in h.requests if k=="speech_dying"]==[4,7]


@pytest.mark.parametrize("poisoned,shot", [(None,True),(9,False)])
async def test_hunter_poison_blocks_shot(poisoned,shot):
    h=Harness(lambda p,k,c:{"action":"shoot","target":1})
    h.game.alive.remove(9)
    await h.game.hunter([9],poisoned)
    assert (1 not in h.game.alive)==shot
    assert bool(h.requests)==shot


async def test_last_hunter_kills_last_wolf_before_win():
    h=Harness(lambda p,k,c:{"action":"shoot","target":1})
    h.game.alive={1,4,5}
    assert h.game.victory()==("werewolves_win","all_specials_dead")
    await h.game.hunter([9])
    await h.game.check_victory()
    assert h.game.result["outcome"]=="good_win"
    assert [e[0] for e in h.events]==["hunter_result","hunter_shoot","death","phase_changed","game_end"]


@pytest.mark.parametrize("alive,outcome,reason", [({4,7},"good_win","all_wolves_dead"),({1,7},"werewolves_win","all_villagers_dead"),({1,4},"werewolves_win","all_specials_dead"),(set(),"good_win","all_wolves_dead")])
def test_victory(alive,outcome,reason):
    g=Harness().game;g.alive=alive
    assert g.victory()==(outcome,reason)


@pytest.mark.parametrize("mode,expected",[("tie",None),("resolve",1),("abstain",None),("all_candidates",None)])
async def test_day_pk(mode,expected):
    h=Harness();h.game.alive={1,2,4,5};h.game.order=[5,4,2,1]
    def policy(p,k,c):
        if k=="vote":
            if mode=="abstain":t=0
            elif mode=="all_candidates":t={1:2,2:4,4:5,5:1}[p]
            elif c["round"]==1:t={1:2,2:1,4:1,5:2}[p]
            else:t=1 if mode=="resolve" or p==4 else 2
            return {"action":"vote","target":t}
        return h.defaults(p,k,c)
    h.policy=policy
    assert await h.game.day_vote()==expected
    second=[p for p,k,c in h.requests if k=="vote" and c["round"]==2]
    if mode in {"tie","resolve"}:assert second==[4,5]
    if mode=="all_candidates":assert second==[]


async def test_max_days_and_no_late_last_words():
    h=Harness(limits={"max_days":1})
    await h.game.run()
    assert h.game.result["outcome"]=="draw"
    assert h.game.day==h.game.night==1
    assert h.events[-1][0]=="game_end"


def test_speech_rotation_uses_previous_start_even_if_dead():
    g=Harness().game;g.first_speaker=3;g.direction=1;g.alive={1,2,5,9}
    assert g.day_order()==[5,9,1,2]
    g.alive.remove(5)
    assert g.day_order()==[9,1,2]


@pytest.mark.parametrize("seed",range(20))
async def test_random_full_matches_invariants(seed):
    rng=random.Random(seed)
    h=Harness(seed=seed,limits={"max_days":8})
    def policy(p,k,c):
        if k.startswith("werewolves_"):return {"action":"kill_vote","target":rng.choice(c["legal_targets"]+[None])}
        if k=="vote":return {"action":"vote","target":rng.choice(c["legal_targets"])}
        if k=="witch_act":
            a=rng.choice(c["legal_actions"])
            return {"action":"pass"} if a=="pass" else {"action":a,"target":rng.choice(c[f"legal_{a}_targets"])}
        if k=="hunter_act":return {"action":"shoot","target":rng.choice(c["legal_targets"])} if c["legal_targets"] else {"action":"pass"}
        return h.defaults(p,k,c)
    h.policy=policy
    await h.game.run()
    assert h.game.result and 1<=h.game.day<=8
    assert h.game.antidote in (0,1) and h.game.poison in (0,1)
    deaths=[p for k,c,_,_ in h.events if k=="death" for p in c["players"]]
    assert len(deaths)==len(set(deaths))
    assert set(deaths)|h.game.alive==set(range(1,10))
    assert sum(k=="game_end" for k,*_ in h.events)==1
