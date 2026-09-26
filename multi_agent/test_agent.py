import asyncio
import json
import sys
from contextlib import asynccontextmanager

import pytest
from aiohttp import web, ClientSession
from werewolf.protocol import validate_action
from werewolf.server import create_app, HUB_KEY
from werewolf.replay import verify_replay
from .agent import WerewolfAgent
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
        assert sent == dict(observation=observation, request=request)
        return web.json_response({"choices":[{"message":{"content":json.dumps(expected)}}]})
    app = web.Application(); app.router.add_post("/v1/chat/completions", model)
    async with serve(app) as base, ClientSession() as session:
        agent = WerewolfAgent(dict(mode="llm", base_url=base+"/v1", model="test-model", api_key_env=""), session)
        assert await agent.act(observation, request) == expected
        assert agent.stats["model_success"] == 1 and agent.stats["fallback"] == 0


@pytest.mark.parametrize("case", cases(), ids=lambda c:c[0])
async def test_baseline_is_legal_for_every_request(case):
    observation, request, _ = fixture(case)
    action = await WerewolfAgent(dict(mode="baseline")).act(observation, request)
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
    logs = []
    async with serve(app) as base, ClientSession() as session:
        agent = WerewolfAgent(dict(mode="llm", base_url=base+"/v1", model="test", api_key_env="", timeout_seconds=.02), session, log=logs.append)
        validate_action(request["type"], request["content"], await agent.act(observation, request))
        assert agent.stats["fallback"] == agent.stats["model_errors"] == 1
        assert any(e["event"] == "model_error" for e in logs)


async def test_cancellation_does_not_submit_fallback():
    entered = asyncio.Event()
    async def model(incoming):
        entered.set()
        await asyncio.sleep(.15)
        return web.json_response({})
    app = web.Application(); app.router.add_post("/v1/chat/completions", model)
    observation, request, _ = fixture(cases()[0])
    async with serve(app) as base, ClientSession() as session:
        agent = WerewolfAgent(dict(mode="llm", base_url=base+"/v1", model="test", api_key_env=""), session)
        task = asyncio.create_task(agent.act(observation, request))
        await entered.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent.stats["decisions"] == agent.stats["fallback"] == 0


def write_config(tmp_path, server, settings=None):
    agents = {f"test-{i}":f"secret-{i}" for i in range(9)}
    entries = []
    for aid, token in agents.items():
        file = tmp_path / f"{aid}.json"
        file.write_text(json.dumps(dict(agent_id=aid, token=token)), encoding="utf-8")
        entries.append(dict(credentials=file.name))
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(dict(server=server, defaults=settings or dict(mode="baseline"), agents=entries)), encoding="utf-8")
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


def test_duplicate_ids_rejected(tmp_path):
    path, _ = write_config(tmp_path, "ws://localhost/ws/agent")
    data = json.loads(path.read_text()); data["agents"][1]=data["agents"][0]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Duplicate"):
        load_config(path)


def test_missing_key_rejected_before_connections(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_TEST_KEY", raising=False)
    path, _ = write_config(tmp_path, "ws://localhost/ws/agent", dict(mode="llm", model="test", base_url="https://example.com/v1", api_key_env="MISSING_TEST_KEY"))
    with pytest.raises(ValueError, match="Missing environment"):
        load_config(path)


def test_literal_key_has_safe_actionable_error(tmp_path):
    secret = "sk-test-secret-not-an-environment-variable"
    path, _ = write_config(tmp_path, "ws://localhost/ws/agent", dict(mode="llm", model="test", base_url="https://example.com/v1", api_key_env=secret))
    from .run import ConfigError
    with pytest.raises(ConfigError) as failure:
        load_config(path)
    assert "environment variable NAME" in str(failure.value)
    assert secret not in str(failure.value)


async def test_command_line_nine_model_agents_complete_match(tmp_path):
    from werewolf.example_agent import ExampleAgent
    calls = []
    async def model(incoming):
        data = await incoming.json()
        visible = json.loads(data["messages"][1]["content"])
        calls.append(visible["request"]["type"])
        action = await ExampleAgent(seed=1).act(visible["observation"], visible["request"])
        # Send separate chunks to exercise HTTP streaming transport assembly.
        payload = json.dumps({"choices":[{"message":{"content":json.dumps(action)}}]}).encode()
        response = web.StreamResponse(headers={"Content-Type":"application/json"})
        await response.prepare(incoming)
        await response.write(payload[:20]); await asyncio.sleep(.001)
        await response.write(payload[20:]); await response.write_eof()
        return response
    mock = web.Application(); mock.router.add_post("/v1/chat/completions", model)
    async with serve(mock) as model_base:
        path, credentials = write_config(tmp_path, "ws://placeholder", dict(mode="llm",base_url=model_base+"/v1",model="test",api_key_env=""))
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
    assert all(row["completed_games"]==1 and row["fallback"]==0 and not row["sdk_errors"] for row in summary)
    assert sum(row["model_success"] for row in summary)==len(calls)
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


async def test_live_validator_does_not_count_fallback_as_model_pass(tmp_path):
    from .validate import live_check
    calls = []
    async def model(incoming):
        calls.append(1)
        return web.Response(status=429)
    app=web.Application(); app.router.add_post("/v1/chat/completions",model)
    async with serve(app) as base:
        path,_=write_config(tmp_path,"ws://unused",dict(mode="llm",base_url=base+"/v1",model="test",api_key_env=""))
        data=json.loads(path.read_text()); data["agents"]=data["agents"][:1]; path.write_text(json.dumps(data))
        rows=await live_check(path)
    assert len(calls)==len(rows)==14 and all(not row["passed"] for row in rows)
