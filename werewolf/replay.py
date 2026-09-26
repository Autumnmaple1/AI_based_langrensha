"""Verify a recorded match without calling any Agent or drawing randomness again."""
import argparse
import json
from collections import Counter
from pathlib import Path
from .protocol import ROLES, top_choices


def verify_replay(snapshot):
    game, events = snapshot["game"], snapshot["events"]
    roles = {int(p): r for p, r in game["roles"].items()}
    if set(roles) != set(range(1,10)) or Counter(roles.values()) != Counter(ROLES):
        raise ValueError("Invalid role assignment")
    alive, result = set(roles), None
    for event in events:
        kind, c = event["type"], event["content"]
        if kind == "death":
            dead = c["players"]
            if len(dead) != len(set(dead)) or not set(dead) <= alive:
                raise ValueError("A player died twice or does not exist")
            if result:
                raise ValueError("Death after game end")
            alive.difference_update(dead)
        elif kind == "werewolves_result":
            tops = top_choices(c["votes"], wolf=True)
            if tops != c["top_choices"]:
                raise ValueError("Incorrect wolf vote tally")
            if c["selection_method"] == "random_tie" and c["selected_target"] not in tops:
                raise ValueError("Random selection not among top choices")
        elif kind == "vote_result":
            tops = top_choices(c["votes"])
            if tops != c["top_candidates"]:
                raise ValueError("Incorrect exile tally")
            if c["decision"] == "exile" and (len(tops) != 1 or c["exiled_player"] != tops[0]):
                raise ValueError("Incorrect exile")
        elif kind == "game_end":
            if result:
                raise ValueError("Multiple endings")
            result = c
            living = [roles[p] for p in alive]
            expected = ("good_win", "all_wolves_dead") if "werewolf" not in living else (
                ("werewolves_win", "all_villagers_dead") if "villager" not in living else (
                    ("werewolves_win", "all_specials_dead") if not any(r in living for r in ("seer","witch","hunter")) else None))
            if c["outcome"] not in {"aborted","draw"} and expected != (c["outcome"],c["reason"]):
                raise ValueError("Winner does not match remaining roles")
            if c["outcome"] == "draw" and (expected or c["reason"] != "max_days" or c["day"] < game["limits"]["max_days"]):
                raise ValueError("Invalid draw")
            if {x["player_id"]: x["role"] for x in c["roles"]} != roles:
                raise ValueError("Final identities changed")
    if not result or result != game["result"] or alive != set(game["alive"]):
        raise ValueError("Event replay does not match saved final state")
    for p, history in snapshot["history"].items():
        if [m["seq"] for m in history] != list(range(1,len(history)+1)):
            raise ValueError("Noncontiguous private sequence")
        for message in history:
            if message["player_id"] != int(p) or message["game_id"] != game["id"]:
                raise ValueError("Wrong recipient or game in private history")
    return {"game_id":game["id"],"outcome":result["outcome"],"events":len(events),"alive":sorted(alive),"verified":True}


def main():
    parser=argparse.ArgumentParser(description="Verify an exported game replay")
    parser.add_argument("file",help="JSON exported from the host dashboard")
    args=parser.parse_args()
    print(json.dumps(verify_replay(json.loads(Path(args.file).read_text(encoding="utf-8"))),ensure_ascii=False))


if __name__=="__main__":main()
