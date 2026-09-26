import asyncio
import json
from contextlib import asynccontextmanager
import pytest
from aiohttp import web, ClientSession
from werewolf.server import create_app, HUB_KEY
from werewolf.protocol import client_message, dumps
from werewolf.sdk import AgentClient
from werewolf.example_agent import ExampleAgent


def config(**extra):
    base=dict(agents={f"a{i}":f"test-secret-{i}" for i in range(9)},admin_token="host-secret",pace_ms=0,step_interval_ms=0,
              limits={"action_timeout_ms":2000,"max_days":3})
    return dict(base,**extra)


@asynccontextmanager
async def running(config_data=None,db=":memory:"):
    app=create_app(config_data or config(),db)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,"127.0.0.1",0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    try:yield app[HUB_KEY],f"http://127.0.0.1:{port}"
    finally:await runner.cleanup()


async def receive(ws,kind):
    for _ in range(1000):
        data=await asyncio.wait_for(ws.receive_json(),3)
        if data["type"]==kind:return data
    raise AssertionError(f"No {kind}")


async def auth(session,base,agent="a0",token="test-secret-0"):
    ws=await session.ws_connect(base+"/ws/agent")
    await ws.send_json(client_message("auth",dict(agent_id=agent,token=token,client_version="test")))
    response=await receive(ws,"auth_result")
    return ws,response


async def wait_until(predicate,timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():await asyncio.sleep(.01)


async def connect_nine(session,base):
    sockets=[]
    for i in range(9):
        ws,result=await auth(session,base,f"a{i}",f"test-secret-{i}")
        assert result["content"]["ok"]
        await ws.send_json(client_message("ready",{}))
        await receive(ws,"ready_result")
        sockets.append(ws)
    return sockets


async def test_auth_and_dashboard_access():
    async with running() as (hub,base),ClientSession() as session:
        ws,result=await auth(session,base,token="bad")
        assert not result["content"]["ok"]
        await ws.close()
        for path in ("/api/games/start","/api/games/abort"):
            async with session.post(base+path) as r:assert r.status==401
        async with session.get(base+"/api/replays") as r:assert r.status==401
        async with session.get(base+"/") as r:
            assert r.status==200 and "AI 狼人杀" in await r.text()
            assert "frame-ancestors" in r.headers["Content-Security-Policy"]
        async with session.get(base+"/api/state",headers={"Authorization":"Bearer bad"}) as r:assert r.status==401
        async with session.get(base+"/health") as r:assert (await r.json())["status"]=="ok"


async def test_nine_real_sdk_clients_finish_and_private_history(tmp_path):
    async with running(db=tmp_path/"test.sqlite3") as (hub,base),ClientSession() as session:
        clients=[AgentClient(base+"/ws/agent",f"a{i}",f"test-secret-{i}",ExampleAgent(seed=i)) for i in range(9)]
        tasks=[asyncio.create_task(c.run()) for c in clients]
        try:
            await wait_until(lambda:len(hub.ready)==9)
            async with session.post(base+"/api/games/start",headers={"Authorization":"Bearer host-secret"}) as r:
                assert r.status==200
            results=await asyncio.wait_for(asyncio.gather(*tasks),20)
            assert all(r==results[0] for r in results)
            assert results[0]["outcome"] in {"good_win","werewolves_win","draw"}
            assert not hub.failure
            for client in clients:
                own=next(m["content"]["role"] for m in client.history if m["type"]=="role")
                assert [m["seq"] for m in client.history]==list(range(1,len(client.history)+1))
                assert sum(m["type"]=="role" for m in client.history)==1
                assert any(m["type"]=="werewolves_info" for m in client.history)==(own=="werewolf")
                assert any(m["type"]=="witch_act" for m in client.history)<= (own=="witch")
                assert not client.errors
            public=hub.view()
            assert not public["requests"]
            assert all(e["type"] not in {"role","witch_act","seer_result","action_ack"} for e in public["events"])
            saved=hub.store.replay(hub.game.id)
            assert saved["game"]["result"]==results[0]
            assert saved["events"][-1]["type"]=="game_end"
            from werewolf.replay import verify_replay
            assert verify_replay(saved)["verified"]
            serialized=dumps(saved)
            assert "host-secret" not in serialized and "test-secret" not in serialized
        finally:
            for t in tasks:t.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


async def test_timeout_match_progress_and_no_public_identity_leak():
    c=config();c["limits"]={"action_timeout_ms":15,"max_days":1}
    async with running(c) as (hub,base),ClientSession() as session:
        sockets=await connect_nine(session,base)
        await hub.start()
        view=hub.view()
        assert all("role" not in p for p in view["game"]["players"])
        assert view["revision"] is None
        await asyncio.wait_for(hub.task,5)
        assert hub.game.result["outcome"]=="draw"
        assert sum(e["type"]=="werewolves_result" for e in hub.events)==3
        assert any(e["type"]=="request_closed" for e in hub.events)
        for ws in sockets:await ws.close()


async def test_duplicates_invalid_correction_barrier_and_reconnect():
    c=config();c["limits"]["action_timeout_ms"]=3000
    async with running(c) as (hub,base),ClientSession() as session:
        sockets=await connect_nine(session,base)
        await hub.start()
        await wait_until(lambda:len(hub.requests)==3)
        rid,request=next(iter(hub.requests.items()))
        p=request["player"];aid=hub.seats[p];ws=sockets[int(aid[1:])]
        illegal=client_message("action",{"action":"kill_vote","target":99},hub.game.id,rid)
        await ws.send_json(illegal)
        error=await receive(ws,"action_error")
        assert error["content"]["code"]=="ILLEGAL_TARGET" and error["content"]["retryable"]
        assert not request["future"].done()
        good=client_message("action",{"action":"kill_vote","target":0},hub.game.id,rid)
        await ws.send_json(good)
        assert (await receive(ws,"action_ack"))["content"]["status"]=="accepted"
        assert not any(e["type"]=="werewolves_result" for e in hub.events)
        await ws.send_json(good)
        assert (await receive(ws,"action_ack"))["content"]["status"]=="already_accepted"
        changed=client_message("action",{"action":"kill_vote","target":4},hub.game.id,rid)
        await ws.send_json(changed)
        assert (await receive(ws,"action_error"))["content"]["code"]=="ACTION_ALREADY_ACCEPTED"
        await ws.close()
        ws2,_=await auth(session,base,aid,c["agents"][aid])
        sync=await receive(ws2,"state_sync")
        assert sync["content"]["pending_request"] is None
        assert sync["content"]["action_receipts"][0]["request_id"]==rid
        other_rid,other=next((i,r) for i,r in hub.requests.items() if r["player"]!=p)
        oid=hub.seats[other["player"]]
        old_deadline=other["message"]["content"]["deadline_at"]
        replacement,_=await auth(session,base,oid,c["agents"][oid])
        sync2=await receive(replacement,"state_sync")
        assert sync2["content"]["pending_request"]["request_id"]==other_rid
        assert sync2["content"]["pending_request"]["content"]["deadline_at"]==old_deadline
        await ws2.send_json(client_message("action",{"action":"kill_vote","target":0},hub.game.id,other_rid))
        assert (await receive(ws2,"action_error"))["content"]["code"]=="UNKNOWN_REQUEST"
        await hub.abort()
        assert hub.game.result["outcome"]=="aborted"
        await ws2.close();await replacement.close()
        for sock in sockets:await sock.close()


async def test_malformed_json_does_not_disconnect():
    async with running() as (hub,base),ClientSession() as session:
        ws,_=await auth(session,base)
        await ws.send_str('{"x":1,"x":2}')
        assert (await receive(ws,"action_error"))["content"]["code"]=="INVALID_JSON"
        await ws.send_json(client_message("ready",{}))
        assert (await receive(ws,"ready_result"))["content"]["ready"]
        await ws.close()


async def test_restart_marks_interrupted_match_aborted(tmp_path):
    db=tmp_path/"recovery.sqlite3"
    # Simulate a hard crash by storing a live snapshot separately before graceful cleanup.
    from werewolf.store import Store
    async with running() as (hub,base),ClientSession() as session:
        sockets=await connect_nine(session,base)
        await hub.start();await wait_until(lambda:bool(hub.requests))
        snapshot=hub.snapshot();game_id=hub.game.id
        store=Store(db);store.commit(game_id,snapshot,"crash_snapshot",{});store.close()
        for ws in sockets:await ws.close()
    async with running(db=db) as (hub,base),ClientSession() as session:
        assert hub.game.id==game_id
        assert hub.game.result["outcome"]=="aborted"
        assert hub.game.result["reason"]=="platform_error"
        aid=hub.seats[1]
        ws,_=await auth(session,base,aid,hub.config["agents"][aid])
        sync=await receive(ws,"state_sync")
        assert sync["content"]["result"]["outcome"]=="aborted"
        assert sync["content"]["pending_request"] is None
        await ws.close()


async def test_watch_private_stream_requires_host_credential():
    async with running() as (hub,base),ClientSession() as session:
        public=await session.ws_connect(base+"/ws/watch")
        await public.send_json({"token":""})
        assert (await public.receive_json())["mode"]=="public"
        host=await session.ws_connect(base+"/ws/watch")
        await host.send_json({"token":"host-secret"})
        assert (await host.receive_json())["mode"]=="host"
        bad=await session.ws_connect(base+"/ws/watch")
        await bad.send_json({"token":"bad"})
        msg=await bad.receive()
        assert msg.type.name in {"CLOSE","CLOSED"}
        await public.close();await host.close();await bad.close()


async def test_late_reply_and_reconnect_after_deadline():
    c=config();c["limits"]={"action_timeout_ms":35,"max_days":1}
    async with running(c) as (hub,base),ClientSession() as session:
        sockets=await connect_nine(session,base)
        await hub.start();await wait_until(lambda:bool(hub.requests))
        rid,req=next(iter(hub.requests.items()));aid=hub.seats[req["player"]]
        await wait_until(lambda:req["state"]=="timeout")
        ws,_=await auth(session,base,aid,c["agents"][aid])
        sync=await receive(ws,"state_sync")
        assert not sync["content"]["pending_request"] or sync["content"]["pending_request"]["request_id"]!=rid
        await ws.send_json(client_message("action",{"action":"kill_vote","target":4},hub.game.id,rid))
        assert (await receive(ws,"action_error"))["content"]["code"]=="REQUEST_EXPIRED"
        assert rid not in hub.receipts
        await ws.close()
        for s in sockets:await s.close()


async def test_new_clients_can_join_after_completed_match():
    async with running() as (hub,base):
        results=[]
        for _ in range(2):
            clients=[AgentClient(base+"/ws/agent",f"a{i}",f"test-secret-{i}",ExampleAgent(seed=i)) for i in range(9)]
            tasks=[asyncio.create_task(c.run()) for c in clients]
            try:
                await wait_until(lambda:len(hub.ready)==9)
                await hub.start()
                finished=await asyncio.wait_for(asyncio.gather(*tasks),20)
                results.append(hub.game.id)
                assert all(r==hub.game.result for r in finished)
                assert not hub.failure
            finally:
                for t in tasks:t.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        assert results[0]!=results[1]
        assert len(hub.store.list_games())==2


async def test_keep_alive_clients_play_two_games():
    async with running() as (hub,base):
        clients=[AgentClient(base+"/ws/agent",f"a{i}",f"test-secret-{i}",ExampleAgent(seed=i),stop_after_game=False) for i in range(9)]
        tasks=[asyncio.create_task(c.run()) for c in clients]
        try:
            for _ in range(2):
                await wait_until(lambda:len(hub.ready)==9)
                await hub.start()
                await asyncio.wait_for(hub.task,15)
                await wait_until(lambda:len(hub.ready)==9)
            assert all(len(c.finished_games)==2 for c in clients)
            assert all(not c.errors for c in clients)
        finally:
            for t in tasks:t.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


async def test_sdk_reconnect_during_pending_action():
    c=config();c["limits"]["action_timeout_ms"]=4000
    async with running(c) as (hub,base):
        clients=[AgentClient(base+"/ws/agent",f"a{i}",f"test-secret-{i}",ExampleAgent(seed=i,delay=.04)) for i in range(9)]
        tasks=[asyncio.create_task(c.run()) for c in clients]
        try:
            await wait_until(lambda:len(hub.ready)==9)
            await hub.start();await wait_until(lambda:bool(hub.requests))
            req=next(iter(hub.requests.values()));aid=hub.seats[req["player"]]
            await hub.connections[aid].ws.close()
            results=await asyncio.wait_for(asyncio.gather(*tasks),20)
            assert all(r==hub.game.result for r in results)
            assert req["state"]=="accepted"
            assert not hub.failure
        finally:
            for t in tasks:t.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


async def test_message_id_reuse_across_reconnect_rejected():
    async with running() as (hub,base),ClientSession() as session:
        ws,_=await auth(session,base)
        original=client_message("ready",{})
        await ws.send_json(original);await receive(ws,"ready_result");await ws.close()
        ws,_=await auth(session,base)
        modified=client_message("sync",{"last_seq":0},"nonexistent")
        modified["message_id"]=original["message_id"]
        await ws.send_json(modified)
        assert (await receive(ws,"action_error"))["content"]["code"]=="INVALID_MESSAGE"
        await ws.close()


async def test_unicode_invalid_token_is_rejected_not_server_error():
    async with running() as (hub,base),ClientSession() as session:
        ws,result=await auth(session,base,token="无效凭证")
        assert not result["content"]["ok"]
        await ws.close()


async def test_host_api_start_conflict_and_replay_permissions():
    async with running() as (hub,base),ClientSession() as session:
        headers={"Authorization":"Bearer host-secret"}
        async with session.post(base+"/api/games/start",headers=headers) as r:assert r.status==409
        sockets=await connect_nine(session,base)
        await hub.start()
        async with session.post(base+"/api/games/start",headers=headers) as r:assert r.status==409
        async with session.get(base+f"/api/replays/{hub.game.id}") as r:assert r.status==401
        async with session.get(base+f"/api/replays/{hub.game.id}",headers=headers) as r:
            assert r.status==200 and (await r.json())["game"]["id"]==hub.game.id
        for ws in sockets:await ws.close()


async def test_timeout_callback_does_not_overwrite_accepted_action(monkeypatch):
    from werewolf.server import Hub
    from werewolf.store import Store
    from werewolf.engine import Game
    store=Store(":memory:");hub=Hub(config(),store)
    hub.game=Game(hub.emit,hub.ask_many)
    hub.seats={1:"a0"};hub.history={1:[]}
    async def racing_timeout(awaitables, timeout, return_when):
        req=next(iter(hub.requests.values()))
        req["state"]="accepted"
        req["future"].set_result({"action":"vote","target":2})
        return set(), set(awaitables)
    monkeypatch.setattr(asyncio,"wait",racing_timeout)
    try:
        result=await hub.ask_many([(1,"vote",{"legal_targets":[0,2]})])
        assert result[1][0]=={"action":"vote","target":2}
        assert not any(e["type"]=="request_closed" for e in hub.events)
    finally:store.close()


async def test_pause_freezes_deadlines_and_blocks_settlement():
    c=config();c["limits"]["action_timeout_ms"]=250
    async with running(c) as (hub,base),ClientSession() as session:
        sockets=await connect_nine(session,base)
        await hub.start();await wait_until(lambda:len(hub.requests)==3)
        headers={"Authorization":"Bearer host-secret"}
        async with session.post(base+"/api/games/pause") as r:assert r.status==401
        async with session.post(base+"/api/games/pause",headers=headers) as r:assert r.status==200
        rid,req=next(iter(hub.requests.items()));deadline=req["deadline"]
        remaining=hub.view(True)["requests"][0]["remaining_ms"]
        count=len(hub.events)
        await asyncio.sleep(.35)
        assert len(hub.events)==count and req["state"]=="pending"
        assert hub.view(True)["requests"][0]["remaining_ms"]==remaining
        aid=hub.seats[req["player"]];ws=sockets[int(aid[1:])]
        await ws.send_json(client_message("action",{"action":"kill_vote","target":0},hub.game.id,rid))
        error=await receive(ws,"action_error")
        assert error["content"]["code"]=="GAME_PAUSED"
        assert rid not in hub.receipts
        events=len(hub.events);hub.set_paused(True);assert len(hub.events)==events
        async with session.post(base+"/api/games/resume",headers=headers) as r:assert r.status==200
        sync=await receive(ws,"state_sync")
        assert not sync["content"]["paused"]
        assert sync["content"]["pending_request"]["request_id"]==rid
        assert req["deadline"]-deadline>=.35
        await ws.send_json(client_message("action",{"action":"kill_vote","target":0},hub.game.id,rid))
        assert (await receive(ws,"action_ack"))["content"]["status"]=="accepted"
        assert req["state"]=="accepted"
        for s in sockets:await s.close()


async def test_pause_keeps_accepted_votes_and_can_abort():
    c=config();c["pace_ms"]=250
    async with running(c) as (hub,base),ClientSession() as session:
        sockets=await connect_nine(session,base)
        await hub.start();await wait_until(lambda:len(hub.requests)==3)
        for rid,req in list(hub.requests.items()):
            aid=hub.seats[req["player"]];ws=sockets[int(aid[1:])]
            await ws.send_json(client_message("action",{"action":"kill_vote","target":0},hub.game.id,rid))
            await receive(ws,"action_ack")
        hub.set_paused(True)
        await asyncio.sleep(.3)
        assert not any(e["type"]=="werewolves_result" for e in hub.events)
        assert len(hub.receipts)==3
        await hub.abort()
        assert not hub.paused and hub.game.result["outcome"]=="aborted"
        for s in sockets:await s.close()


async def test_sdk_pause_reconnect_resume_completes_game():
    c=config();c["limits"]["action_timeout_ms"]=3000
    async with running(c) as (hub,base):
        clients=[AgentClient(base+"/ws/agent",f"a{i}",f"test-secret-{i}",ExampleAgent(seed=i,delay=.07)) for i in range(9)]
        tasks=[asyncio.create_task(client.run()) for client in clients]
        try:
            await wait_until(lambda:len(hub.ready)==9)
            await hub.start();await wait_until(lambda:len(hub.requests)==3)
            hub.set_paused(True)
            await wait_until(lambda:all(client.paused for client in clients))
            req=next(iter(hub.requests.values()));aid=hub.seats[req["player"]]
            old=hub.connections[aid]
            await old.ws.close()
            await wait_until(lambda:aid in hub.connections and hub.connections[aid] is not old)
            await asyncio.sleep(.1)
            assert all(client.paused for client in clients)
            assert len(hub.requests)==3
            hub.set_paused(False)
            results=await asyncio.wait_for(asyncio.gather(*tasks),25)
            assert all(result==hub.game.result for result in results)
            assert not hub.failure and all(not client.errors for client in clients)
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


async def test_pause_not_available_outside_running_game():
    async with running() as (hub,base),ClientSession() as session:
        headers={"Authorization":"Bearer host-secret"}
        for endpoint in ("pause","resume"):
            async with session.post(base+f"/api/games/{endpoint}",headers=headers) as r:assert r.status==409


async def test_host_step_control_gates_and_finishes_the_match():
    c=config();c["step_mode"]="manual";c["step_interval_ms"]=60
    c["limits"]={"action_timeout_ms":15,"max_days":1}
    async with running(c) as (hub,base),ClientSession() as session:
        headers={"Authorization":"Bearer host-secret"}
        sockets=await connect_nine(session,base)
        async with session.post(base+"/api/games/start",headers=headers) as r:assert r.status==200
        await wait_until(lambda:hub.waiting_step is not None)
        assert hub.waiting_step.startswith("第 1 夜") and not hub.requests
        host_view=hub.view(True)
        assert host_view["step"]==dict(mode="manual",interval_ms=60,current=None,waiting=hub.waiting_step,queued=False)
        public_view=hub.view()
        assert public_view["step"]==dict(waiting=True) and not public_view["requests"]
        assert "入夜" not in json.dumps(public_view) and "女巫" not in json.dumps(public_view)
        async with session.post(base+"/api/games/step") as r:assert r.status==401
        first_step=hub.waiting_step
        async with session.post(base+"/api/games/step",headers=headers) as r:
            assert r.status==200 and (await r.json())["accepted"]
        await wait_until(lambda:any(e["type"]=="phase_changed" for e in hub.events))
        await wait_until(lambda:hub.waiting_step is not None and hub.waiting_step!=first_step)
        assert hub.waiting_step=="第 1 夜 · 狼人刀票" and hub.current_step==first_step
        # 切换自动后不需要再点击，裁判按 step_interval 自行推进
        async with session.post(base+"/api/games/mode",json={"mode":"auto"},headers=headers) as r:
            assert r.status==200 and (await r.json())["mode"]=="auto"
        async with session.post(base+"/api/games/mode",json={"mode":"nope"},headers=headers) as r:assert r.status==400
        async with session.post(base+"/api/games/mode",json={"mode":"auto"}) as r:assert r.status==401
        before=len(hub.events)
        await wait_until(lambda:len(hub.events)>before or hub.game.result)
        async with session.post(base+"/api/games/step",headers=headers) as r:
            assert r.status==200 and (await r.json())["reason"]=="auto"
        # 切回手动后停下，直到主持人再次放行
        async with session.post(base+"/api/games/mode",json={"mode":"manual"},headers=headers) as r:
            assert r.status==200 and (await r.json())["mode"]=="manual"
        await wait_until(lambda:hub.waiting_step is not None)
        parked=list(hub.events)
        await asyncio.sleep(.5)
        assert hub.events==parked
        for _ in range(6000):
            if hub.game.result:break
            hub.release_step()
            await asyncio.sleep(.002)
        assert hub.game.result["outcome"]=="draw"
        assert hub.view(True)["step"]["queued"] is False
        assert [e["type"] for e in hub.events if e["type"]=="step_control"]==["step_control","step_control"]
        for ws in sockets:await ws.close()


async def test_step_gate_paces_manual_steps_and_auto_releases_itself():
    from werewolf.server import Hub
    from werewolf.store import Store
    from werewolf.engine import Game
    store=Store(":memory:")
    try:
        hub=Hub(config(step_mode="manual",step_interval_ms=120),store)
        with pytest.raises(web.HTTPConflict):
            hub.set_step_mode("auto")
        assert hub.step_mode=="manual" and hub.step_interval==.12
        hub.game=Game(hub.emit,hub.ask_many,gate=hub.step_gate)
        hub.seats={p:f"a{p}" for p in range(1,10)};hub.history={p:[] for p in range(1,10)}
        assert hub.release_step()["accepted"] and hub.release_step()==dict(accepted=False,reason="queued",mode="manual",waiting=None)
        assert hub.view(True)["step"]["queued"] is True
        start=asyncio.get_running_loop().time()
        await hub.step_gate("第一步")
        first=asyncio.get_running_loop().time()-start
        hub.release_step();await hub.step_gate("第二步")
        assert asyncio.get_running_loop().time()-start-first>=.1
        assert hub.view(True)["step"]["current"]=="第二步"
        # 自动模式：没人点击也会按 step_interval 自行放行
        assert hub.set_step_mode("auto")=="auto"
        assert hub.release_step()==dict(accepted=False,reason="auto",mode="auto",waiting=None)
        assert [e["type"] for e in hub.events]==["step_control"]
        before=asyncio.get_running_loop().time()
        await asyncio.wait_for(hub.step_gate("第三步"),1)
        assert .05<=asyncio.get_running_loop().time()-before<.6
        # 切回手动：没有放行就一直等
        assert hub.set_step_mode("manual")=="manual"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(hub.step_gate("第四步"),.25)
        hub.release_step()
        await asyncio.wait_for(hub.step_gate("第四步"),1)
        assert hub.view(True)["step"]["current"]=="第四步"
    finally:store.close()


async def test_legacy_protocol_version_is_rejected_explicitly():
    async with running() as (hub,base),ClientSession() as session:
        ws=await session.ws_connect(base+"/ws/agent")
        message=client_message("auth",dict(agent_id="a0",token="test-secret-0",client_version="old"))
        message["protocol_version"]="1.0"
        await ws.send_json(message)
        result=await receive(ws,"auth_result")
        assert result["content"]["code"]=="UNSUPPORTED_VERSION"
        await ws.close()
