"""A model-free baseline. Replace ExampleAgent.act with your own model/strategy."""
import argparse
import asyncio
import json
import random
from pathlib import Path
from .sdk import AgentClient


class ExampleAgent:
    def __init__(self, seed=None, delay=0):
        self.rng, self.delay = random.Random(seed), delay

    async def act(self, observation, request):
        if self.delay:
            await asyncio.sleep(self.delay)
        history, c, kind = observation["history"], request["content"], request["type"]
        wolves = next((m["content"]["players"] for m in history if m["type"] == "werewolves_info"), [])
        checks = {m["content"]["target"]: m["content"]["alignment"] for m in history
                  if m["type"] == "seer_result" and m["content"]["status"] == "checked"}
        if kind.startswith("werewolves_"):
            choices = [p for p in c["legal_targets"] if p and p not in wolves]
            # Identical rule aligns wolves without chatting or sharing local state.
            return {"action": "kill_vote", "target": min(choices) if choices else 0}
        if kind == "witch_act":
            if c["legal_save_targets"]:
                return {"action": "save", "target": c["legal_save_targets"][0]}
            return {"action": "pass"}
        if kind == "seer_act":
            choices = [p for p in c["legal_targets"] if p not in checks] or c["legal_targets"]
            return {"action": "inspect", "target": self.rng.choice(choices)}
        if kind == "hunter_act":
            return {"action": "shoot", "target": self.rng.choice(c["legal_targets"])} if c["legal_targets"] else {"action": "pass"}
        if kind in {"speech", "speech_dying"}:
            known = "；".join(f"{p}号是{'狼人' if r == 'werewolf' else '好人'}" for p, r in checks.items())
            text = f"我是{request['player_id']}号。" + (f"我的查验信息：{known}。" if known else "目前信息有限，我会结合公开票型和后续发言判断。")
            return {"action": "speak", "text": text[:c["max_codepoints"]]}
        if kind == "vote":
            choices = [p for p in c["legal_targets"] if p and p not in wolves]
            known = [p for p in choices if checks.get(p) == "werewolf"]
            return {"action": "vote", "target": self.rng.choice(known or choices) if choices else 0}
        raise ValueError(f"Unknown request: {kind}")


async def run(args):
    credentials = json.loads(Path(args.credentials).read_text(encoding="utf-8"))
    client = AgentClient(args.server, credentials["agent_id"], credentials["token"],
                         ExampleAgent(args.seed, args.delay), stop_after_game=not args.keep_alive)
    print(f"Connecting {credentials['agent_id']} to {args.server}")
    result = await client.run()
    print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Remote Werewolf baseline agent")
    parser.add_argument("--server", default="ws://127.0.0.1:8765/ws/agent")
    parser.add_argument("--credentials", required=True, help="Only this agent's credentials JSON")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--delay", type=float, default=.3)
    parser.add_argument("--keep-alive", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
