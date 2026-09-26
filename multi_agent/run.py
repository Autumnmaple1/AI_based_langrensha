"""Launch separately authenticated instances; no host credentials are required."""
import argparse
import asyncio
import json
import inspect
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientSession
from werewolf.sdk import AgentClient
from .agent import WerewolfAgent
from agents.run import load_agent


class ConfigError(ValueError):
    """Safe-to-display diagnostics: never include credential values."""


def load_config(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    server = data.get("server", "ws://127.0.0.1:8765/ws/agent")
    if urlsplit(server).scheme not in {"ws", "wss", "http", "https"}:
        raise ValueError("server must be a WebSocket URL")
    specs, ids = [], set()
    for index, item in enumerate(data.get("agents", [])):
        credential = json.loads((path.parent / item["credentials"]).read_text(encoding="utf-8-sig"))
        aid, token = credential["agent_id"], credential["token"]
        if not isinstance(aid, str) or not aid or not isinstance(token, str) or not token:
            raise ValueError("Invalid credentials")
        if aid in ids:
            raise ValueError(f"Duplicate agent_id: {aid}")
        ids.add(aid)
        settings = {**data.get("defaults", {}), **item.get("settings", {})}
        agent_file = item.get("agent", data.get("agent"))
        class_name = item.get("class", data.get("class"))
        if agent_file is not None:
            agent_path = (path.parent / agent_file).resolve()
            if not agent_path.is_file():
                raise ConfigError(f"Agent file not found: {agent_path}")
            specs.append(dict(agent_id=aid, token=token, settings=settings,
                              seed=item.get("seed", index), agent=str(agent_path), class_name=class_name))
            continue
        mode = settings.get("mode", "baseline")
        if mode not in {"llm", "baseline"}:
            raise ValueError("mode must be llm or baseline")
        if float(settings.get("timeout_seconds", 20)) <= 0:
            raise ValueError("timeout_seconds must be positive")
        if mode == "llm":
            url = urlsplit(settings.get("base_url", ""))
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query:
                raise ValueError("base_url must be an HTTP(S) API root without credentials/query")
            if not settings.get("model"):
                raise ValueError("Set model for every llm agent")
            env = settings.get("api_key_env", "WEREWOLF_API_KEY")
            if not isinstance(env, str) or (env and (env.startswith("sk-") or len(env)>64 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env))):
                raise ConfigError("api_key_env must be an environment variable NAME, not an API key. "
                                  "Set it to WEREWOLF_API_KEY, then set $env:WEREWOLF_API_KEY in the same PowerShell terminal before starting.")
            if env and not os.environ.get(env):
                raise ConfigError("Missing environment variable for api_key_env. "
                                  "Set the key in the same terminal before starting; for the default name: "
                                  '$env:WEREWOLF_API_KEY = "your API key"')
        specs.append(dict(agent_id=aid, token=token, settings=settings, seed=item.get("seed", index)))
    if not specs:
        raise ValueError("Configure at least one agent")
    return data, server, specs


class MemberAgent:
    """Record decisions without requiring members to implement runner-specific APIs."""

    def __init__(self, agent, log):
        self.agent, self.log = agent, log
        self.stats = dict(decisions=0)

    async def on_game_start(self, observation):
        if hasattr(self.agent, "on_game_start"):
            await self.agent.on_game_start(observation)

    async def on_game_end(self, result):
        if hasattr(self.agent, "on_game_end"):
            await self.agent.on_game_end(result)

    async def act(self, observation, request):
        started = asyncio.get_running_loop().time()
        try:
            action = await self.agent.act(observation, request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(dict(event="decision_error", request_id=request["request_id"], error=type(exc).__name__))
            raise
        self.stats["decisions"] += 1
        self.log(dict(event="decision", game_id=request["game_id"], request_id=request["request_id"],
                      kind=request["type"], action=action,
                      elapsed_ms=round((asyncio.get_running_loop().time()-started)*1000)))
        return action


def load_members(specs):
    """Load and validate every member before opening any referee connection."""
    members = {}
    for index, spec in enumerate(specs):
        if "agent" not in spec:
            continue  # Compatibility for existing built-in model configurations.
        try:
            agent = load_agent(spec["agent"], spec["class_name"], spec["seed"], spec["settings"])
        except (Exception, SystemExit) as exc:
            raise ConfigError(f"Cannot load member {spec['agent_id']} ({type(exc).__name__}); check agent and settings") from exc
        for method in ("act", "on_game_start", "on_game_end"):
            if method == "act" or hasattr(agent, method):
                if not inspect.iscoroutinefunction(getattr(agent, method, None)):
                    raise ConfigError(f"{spec['agent_id']}: {method} must be async def")
        members[index] = agent
    return members


class LoggedClient(AgentClient):
    def __init__(self, *args, log, **kwargs):
        super().__init__(*args, **kwargs)
        self.log = log

    async def handle(self, message):
        kind = message["type"]
        if kind in {"auth_result", "state_sync", "game_control", "action_ack", "action_error", "request_closed", "game_end"}:
            content = message["content"]
            self.log(dict(event=kind, game_id=message.get("game_id"), request_id=message.get("request_id"),
                          status=content.get("status"), code=content.get("code"), paused=content.get("paused")))
        await super().handle(message)


async def run_config(path, *, once=False, duration=None, output=None):
    data, server, specs = load_config(path)
    members = load_members(specs)
    output = Path(output or (Path(path).resolve().parent / data.get("output_dir", "runs")))
    from datetime import datetime, timezone
    from uuid import uuid4
    output = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    clients, agents, handles, tasks = [], [], [], []
    print(f"Starting {len(specs)} agents. Logs: {output.resolve()}", flush=True)
    try:
        async with ClientSession() as session:
            for index, spec in enumerate(specs):
                # Numeric filenames prevent user-controlled IDs becoming filesystem paths.
                handle = (output / f"agent-{index+1:02}.jsonl").open("w", encoding="utf-8")
                handles.append(handle)
                def log(event, handle=handle, aid=spec["agent_id"]):
                    handle.write(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(), agent_id=aid, **event), ensure_ascii=False)+"\n")
                    handle.flush()
                agent = (MemberAgent(members[index], log) if index in members else
                         WerewolfAgent(spec["settings"], session, seed=spec["seed"], log=log))
                client = LoggedClient(server, spec["agent_id"], spec["token"], agent, log=log, stop_after_game=once)
                agents.append(agent); clients.append(client)
            try:
                tasks = [asyncio.create_task(client.run()) for client in clients]
                if duration:
                    async with asyncio.timeout(duration):
                        await asyncio.gather(*tasks)
                else:
                    await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        report = [dict(agent_id=c.agent_id, result=c.result, completed_games=len(c.finished_games),
                       sdk_errors=c.errors, **a.stats) for c, a in zip(clients, agents)]
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for handle in handles:
            handle.close()
    if any(c.errors for c in clients):
        raise RuntimeError("SDK errors detected; see summary.json")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true", help="Exit each instance after one game; default stays ready")
    parser.add_argument("--duration", type=float, help="Hard overall timeout in seconds; exceeding it fails")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.duration is not None and args.duration <= 0:
            raise ConfigError("--duration must be positive")
        asyncio.run(run_config(args.config, once=args.once, duration=args.duration, output=args.output))
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        print("Startup validation failed; no game or summary was created.")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Stopped. Summary saved.")
    except Exception as exc:
        print(f"Run failed ({type(exc).__name__}). Check configuration and summary.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
