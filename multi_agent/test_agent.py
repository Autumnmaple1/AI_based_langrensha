import asyncio
import json
import sys
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from werewolf.protocol import validate_action
from werewolf.server import create_app, HUB_KEY
from werewolf.replay import verify_replay
from agents.example.llm_agent import LlmAgent
from agents.example.baseline_agent import BaselineAgent
from pathlib import Path
from .run import run_config, load_config
from werewolf.scenarios import cases, fixture


@asynccontextmanager
async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    finally:
        await runner.cleanup()


async def until(predicate, seconds=8):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.parametrize("case", cases(), ids=lambda c:c[0])
async def test_every_action_through_chat_completions(case):
    observation, request, expected = fixture(case)
    async def model(incoming):
        payload = await incoming.json()
        assert payload["model"] == "test-model"
        sent = json.loads(payload["messages"][1]["content"])
        assert sent["request"] == {"type": request["type"], "content": request["content"]}
        assert sent["my_seat"] == request["player_id"]
        return web.json_response({"choices":[{"message":{"content":json.dumps(expected)}}]})
    app = web.Application(); app.router.add_post("/v1/chat/completions", model)
    async with serve(app) as base:
        agent = LlmAgent(base_url=base+"/v1", model="test-model", api_key_env="")
        assert await agent.act(observation, request) == expected



@pytest.mark.parametrize("case", cases(), ids=lambda c:c[0])
async def test_baseline_is_legal_for_every_request(case):
    observation, request, _ = fixture(case)
    action = await BaselineAgent().act(observation, request)
    validate_action(request["type"], request["content"], action)


@pytest.mark.parametrize("failure", ["invalid_json","illegal_target","empty","http429","http500","slow","duplicate_keys"])
async def test_failed_model_is_reported_and_falls_back(failure):
    async def model(incoming):
        if failure.startswith("http"):
            return web.Response(status=int(failure[4:]))
        if failure == "slow":
            await asyncio.sleep(.08)
        content = {"invalid_json":"oops", "illegal_target":'{"action":"inspect","target":99}',
                   "empty":"", "duplicate_keys":'{"action":"inspect","target":2,"target":3}'}.get(failure, "{}")
        return web.json_response({"choices":[{"message":{"content":content}}]})
    app = web.Application(); app.router.add_post("/v1/chat/completions", model)
    observation, request, _ = fixture(next(c for c in cases() if c[0]=="seer"))
    async with serve(app) as base:
        agent = LlmAgent(base_url=base+"/v1", model="test", api_key_env="", timeout_seconds=.02)
        validate_action(request["type"], request["content"], await agent.act(observation, request))
        assert await agent.act(observation, request) == await agent.baseline.act(observation, request)


async def test_cancellation_does_not_submit_fallback():
    entered = asyncio.Event()
    async def model(incoming):
        entered.set()
        await asyncio.sleep(.15)
        return web.json_response({})
    app = web.Application(); app.router.add_post("/v1/chat/completions", model)
    observation, request, _ = fixture(cases()[0])
    async with serve(app) as base:
        agent = LlmAgent(base_url=base+"/v1", model="test", api_key_env="")
        task = asyncio.create_task(agent.act(observation, request))
        await entered.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()


def write_config(tmp_path, server, settings=None):
    agents = {f"test-{i}":f"secret-{i}" for i in range(9)}
    entries = []
    for aid, token in agents.items():
        file = tmp_path / f"{aid}.json"
        file.write_text(json.dumps(dict(agent_id=aid, token=token)), encoding="utf-8")
        entries.append(dict(credentials=file.name))
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(dict(server=server, defaults=settings or {}, agent=str(Path(__file__).resolve().parents[1] / ("agents/example/llm_agent.py" if settings else "agents/example/baseline_agent.py")), agents=entries)), encoding="utf-8")
    return path, agents


async def test_batch_launcher_nine_agents_two_games_pause_reconnect(tmp_path):
    path, credentials = write_config(tmp_path, "ws://placeholder")
    app = create_app(dict(agents=credentials, admin_token="host", pace_ms=15, step_interval_ms=0,
                          limits=dict(action_timeout_ms=3000, max_days=4)), ":memory:")
    hub = app[HUB_KEY]
    async with serve(app) as base:
        data = json.loads(path.read_text()); data["server"] = base+"/ws/agent"
        path.write_text(json.dumps(data), encoding="utf-8")
        task = asyncio.create_task(run_config(path))
        try:
            for game in range(2):
                await until(lambda:len(hub.ready)==9)
                await hub.start()
                if game == 0:
                    hub.set_paused(True)
                    await asyncio.sleep(.05)
                    aid, old = next(iter(hub.connections.items()))
                    await old.ws.close()
                    await until(lambda:aid in hub.connections and hub.connections[aid] is not old)
                    count = len(hub.events)
                    await asyncio.sleep(.05)
                    assert hub.paused and len(hub.events)==count
                    hub.set_paused(False)
                await asyncio.wait_for(asyncio.shield(hub.task), 25)
                assert not hub.failure
                assert verify_replay(hub.store.replay(hub.game.id))["verified"]
                assert not any(e["type"]=="request_closed" for e in hub.events)
            await asyncio.sleep(.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    summary = json.loads(next(tmp_path.glob("runs/*/summary.json")).read_text(encoding="utf-8"))
    assert len(summary)==9
    assert all(row["completed_games"]==2 and not row["sdk_errors"] for row in summary)
    assert all(row["result"]==summary[0]["result"] for row in summary)
    logs = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("runs/*/*.jsonl"))
    assert "secret-" not in logs and "host" not in logs


async def test_member_agents_nine_independent_instances(tmp_path):
    path, credentials = write_config(tmp_path, "ws://placeholder")
    member = tmp_path / "member.py"
    member.write_text('''from agents.template.my_agent import MyAgent
class StudentAgent(MyAgent):
    def __init__(self, seed=None, marker=None):
        super().__init__(seed)
        self.marker = marker
        self.started = False
    async def on_game_start(self, observation):
        assert not self.started
        self.started = True
        await super().on_game_start(observation)
    async def act(self, observation, request):
        assert self.started and self.marker == "configured"
        if request["type"] in ("speech", "speech_dying"):
            return {"action": "speak", "text": "member-loaded"}
        return await super().act(observation, request)
''', encoding="utf-8")
    data = json.loads(path.read_text())
    data.update(agent="member.py", **{"class": "StudentAgent"}, defaults={"marker": "configured"})
    app = create_app(dict(agents=credentials, admin_token="host", auto_start=True,
                          pace_ms=0, step_interval_ms=0,
                          limits=dict(action_timeout_ms=2000, max_days=1)), ":memory:")
    async with serve(app) as base:
        data["server"] = base + "/ws/agent"
        path.write_text(json.dumps(data), encoding="utf-8")
        report = await run_config(path, once=True, duration=15)
        assert len(report) == 9
        assert all(r["completed_games"] == 1 and not r["sdk_errors"] for r in report)
        assert all(r["decisions"] > 0 for r in report)
        assert verify_replay(app[HUB_KEY].store.replay(app[HUB_KEY].game.id))["verified"]
    logs = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("runs/*/*.jsonl"))
    assert "member-loaded" in logs and "secret-" not in logs


def test_member_overrides_and_invalid_interface(tmp_path):
    from .run import load_members, ConfigError
    path, _ = write_config(tmp_path, "ws://unused")
    (tmp_path / "member.py").write_text('''class Student:
    def __init__(self, seed=None, label=None):
        self.label = label
    async def act(self, observation, request):
        return {"action": "pass"}
class Other(Student):
    pass
class Invalid:
    def __init__(self, label=None):
        pass
    def act(self, observation, request):
        return {}
''', encoding="utf-8")
    data = json.loads(path.read_text())
    data.update(agent="member.py", **{"class": "Student"}, defaults={"label": "default"})
    data["agents"][0].update(agent="member.py", **{"class": "Other"}, settings={"label": "override"})
    path.write_text(json.dumps(data), encoding="utf-8")
    members = load_members(load_config(path)[2])
    assert len({id(a) for a in members.values()}) == 9
    assert type(members[0]).__name__ == "Other" and members[0].label == "override"
    assert members[1].label == "default"
    data["agents"][-1]["class"] = "Invalid"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="act must be async"):
        load_members(load_config(path)[2])


def test_missing_agent_is_rejected(tmp_path):
    from .run import ConfigError
    path, _ = write_config(tmp_path, "ws://unused")
    data = json.loads(path.read_text()); del data["agent"]
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="must specify an agent"):
        load_config(path)


def test_duplicate_ids_rejected(tmp_path):
    path, _ = write_config(tmp_path, "ws://localhost/ws/agent")
    data = json.loads(path.read_text()); data["agents"][1]=data["agents"][0]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Duplicate"):
        load_config(path)


async def test_command_line_nine_model_agents_complete_match(tmp_path):
    calls = []
    async def model(incoming):
        data = await incoming.json()
        visible = json.loads(data["messages"][1]["content"])
        calls.append(visible["request"]["type"])
        kind, content = visible["request"]["type"], visible["request"]["content"]
        if kind in ("speech", "speech_dying"):
            action = {"action": "speak", "text": "mock-model-success"}
        elif kind in ("witch_act", "hunter_act"):
            action = {"action": "pass"}
        else:
            action = {"action": "kill_vote" if kind.startswith("werewolves") else "inspect" if kind == "seer_act" else "vote", "target": content["legal_targets"][-1]}
        # Send separate chunks to exercise HTTP streaming transport assembly.
        payload = json.dumps({"choices":[{"message":{"content":json.dumps(action)}}]}).encode()
        response = web.StreamResponse(headers={"Content-Type":"application/json"})
        await response.prepare(incoming)
        await response.write(payload[:20]); await asyncio.sleep(.001)
        await response.write(payload[20:]); await response.write_eof()
        return response
    mock = web.Application(); mock.router.add_post("/v1/chat/completions", model)
    async with serve(mock) as model_base:
        path, credentials = write_config(tmp_path, "ws://placeholder", dict(base_url=model_base+"/v1",model="test",api_key_env=""))
        app = create_app(dict(agents=credentials, admin_token="host",auto_start=True,pace_ms=0,step_interval_ms=0,
                              limits=dict(action_timeout_ms=4000,max_days=4)), ":memory:")
        async with serve(app) as base:
            data = json.loads(path.read_text()); data["server"]=base+"/ws/agent"
            path.write_text(json.dumps(data))
            process = await asyncio.create_subprocess_exec(sys.executable,"-m","multi_agent.run","--config",str(path),"--once","--duration","25",
                                                          stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(),30)
                assert process.returncode==0, (stdout+stderr).decode(errors="replace")
            finally:
                if process.returncode is None:
                    process.kill(); await process.wait()
            assert not app[HUB_KEY].failure
    summary = json.loads(next(tmp_path.glob("runs/*/summary.json")).read_text(encoding="utf-8"))
    assert len(summary)==9 and calls
    assert all(row["completed_games"]==1 and not row["sdk_errors"] for row in summary)
    assert sum(row["decisions"] for row in summary)==len(calls)
    assert all(row["result"]==summary[0]["result"] for row in summary)


async def test_auth_failure_exits_and_writes_report(tmp_path):
    path, credentials = write_config(tmp_path, "ws://placeholder")
    app = create_app(dict(agents=credentials,admin_token="host"), ":memory:")
    async with serve(app) as base:
        data=json.loads(path.read_text()); data["server"]=base+"/ws/agent"; data["agents"]=data["agents"][:1]
        path.write_text(json.dumps(data))
        (tmp_path/"test-0.json").write_text(json.dumps(dict(agent_id="test-0",token="bad")))
        with pytest.raises(PermissionError):
            await asyncio.wait_for(run_config(path,once=True),5)
    summary=json.loads(next(tmp_path.glob("runs/*/summary.json")).read_text(encoding="utf-8"))
    assert summary[0]["completed_games"]==0
