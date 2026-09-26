"""Remote-agent client: one socket per agent, automatic reconnect and private history."""
import asyncio
import contextlib
import random
from copy import deepcopy
from datetime import datetime, timezone

from aiohttp import ClientSession, WSMsgType, ClientError
from .protocol import REQUEST_TYPES, client_message, dumps


class AgentClient:
    def __init__(self, url, agent_id, token, agent, *, stop_after_game=True):
        self.url, self.agent_id, self.token, self.agent = url, agent_id, token, agent
        self.stop_after_game = stop_after_game
        self.history, self.actions = [], {}
        self.game_id, self.seq = None, 0
        self.worker, self.ws = None, None
        self.request = None
        self.result = None
        self.stopped = False
        self.errors = []
        self.started_games = set()
        self.finished_games = set()
        self.paused = False

    async def send(self, kind, content, request_id=None, game_id=None):
        await self.ws.send_str(dumps(client_message(kind, content, game_id, request_id)))

    async def cancel_worker(self):
        if self.worker and not self.worker.done():
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        self.worker = None

    def observation(self):
        return dict(game_id=self.game_id, history=deepcopy(self.history))

    async def callback(self, name, arg):
        method = getattr(self.agent, name, None)
        if method:
            await method(deepcopy(arg))

    async def finish(self, result):
        await self.cancel_worker()
        self.result = result
        if self.game_id not in self.finished_games:
            await self.callback("on_game_end", result)
            self.finished_games.add(self.game_id)
        if self.stop_after_game:
            self.stopped = True
        else:
            await self.send("ready", {})

    async def dispatch(self, request, error=None):
        await self.cancel_worker()
        self.request = request
        if self.paused:
            return
        rid = request["request_id"]

        async def work():
            try:
                deadline = datetime.fromisoformat(request["content"]["deadline_at"].replace("Z", "+00:00"))
                remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    return
                if rid in self.actions and error is None:
                    action = self.actions[rid]
                else:
                    observation = self.observation()
                    if error:
                        observation["last_error"] = error
                    action = await asyncio.wait_for(self.agent.act(observation, deepcopy(request)), remaining)
                    self.actions[rid] = action
                await self.send("action", action, rid, self.game_id)
            except (asyncio.TimeoutError, ClientError, ConnectionError):
                pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Local agent exceptions don't disconnect the network loop.
                self.errors.append(type(exc).__name__)
        self.worker = asyncio.create_task(work())

    async def handle(self, message):
        kind, c = message["type"], message["content"]
        if kind == "auth_result":
            if not c["ok"]:
                raise PermissionError("Agent authentication failed")
            if self.game_id is None:
                await self.send("ready", {})
            return
        if kind == "state_sync":
            await self.cancel_worker()
            previous_game = self.game_id
            self.game_id = message["game_id"]
            self.paused = c.get("paused", False)
            self.seq, self.history = c["through_seq"], c["history"]
            for receipt in c["action_receipts"]:
                self.actions[receipt["request_id"]] = receipt["accepted_action"]
            if self.game_id not in self.started_games:
                await self.callback("on_game_start", self.observation())
                self.started_games.add(self.game_id)
            if c["result"]:
                # A freshly launched client is joining the next game, not replaying
                # a previous finished match left in the server's durable state.
                if previous_game is not None:
                    await self.finish(c["result"])
            elif c["pending_request"] and not self.paused:
                await self.dispatch(c["pending_request"])
            return
        if message["seq"] is not None:
            if message["game_id"] != self.game_id:
                await self.cancel_worker()
                self.game_id, self.seq, self.history, self.actions = message["game_id"], 0, [], {}
                self.paused = False
            if message["seq"] <= self.seq:
                return
            if message["seq"] != self.seq + 1:
                await self.send("sync", {"last_seq": self.seq}, game_id=self.game_id)
                return
            self.seq = message["seq"]
            self.history.append(message)
        if kind == "role" and self.game_id not in self.started_games:
            await self.callback("on_game_start", self.observation())
            self.started_games.add(self.game_id)
        if kind in REQUEST_TYPES:
            await self.dispatch(message)
        elif kind == "game_control":
            self.paused = c["paused"]
            if self.paused:
                await self.cancel_worker()
            # Resume is followed by state_sync carrying fresh deadlines.
        elif kind in {"action_ack", "request_closed"}:
            if self.request and message["request_id"] == self.request["request_id"]:
                await self.cancel_worker()
                self.request = None
        elif kind == "action_error":
            if c["code"] not in {"NOT_READY_ALLOWED", "GAME_PAUSED"}:
                self.errors.append(c["code"])
            if c["retryable"] and self.request and message["request_id"] == self.request["request_id"]:
                await self.dispatch(self.request, c)
        elif kind == "game_end":
            await self.finish(c)

    async def run(self):
        backoff = 1
        async with ClientSession() as session:
            while not self.stopped:
                try:
                    async with session.ws_connect(self.url, heartbeat=20, max_msg_size=1048576) as ws:
                        self.ws = ws
                        await self.send("auth", dict(agent_id=self.agent_id, token=self.token, client_version="mooncourt-python/1.1"))
                        backoff = 1
                        async for frame in ws:
                            if frame.type == WSMsgType.TEXT:
                                import json
                                await self.handle(json.loads(frame.data))
                                if self.stopped:
                                    return self.result
                            elif frame.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                                break
                except PermissionError:
                    # Authentication rejection is permanent, not a network outage.
                    # PermissionError subclasses OSError and must escape before it.
                    raise
                except (ClientError, ConnectionError, OSError):
                    pass
                finally:
                    await self.cancel_worker()
                if not self.stopped:
                    await asyncio.sleep(backoff + random.random() * .2)
                    backoff = min(10, backoff * 2)
        return self.result
