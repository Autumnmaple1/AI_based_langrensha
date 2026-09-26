"""aiohttp server: authenticated agent sockets, referee broker, protected dashboard."""
import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import random
import secrets
import time
from copy import deepcopy
from pathlib import Path

from aiohttp import web, WSMsgType

from .engine import Game
from .protocol import (VERSION, RULE_VERSION, RULES, MAX_BYTES, ProtocolError, dumps, uid, utc,
                       envelope, strict_loads, validate_client, validate_action, default_action)
from .store import Store

LOG = logging.getLogger("mooncourt")
STATIC = Path(__file__).parent / "static"


class Connection:
    def __init__(self, ws, agent_id):
        self.ws, self.agent_id, self.id = ws, agent_id, uid()
        self.queue = asyncio.Queue(maxsize=256)
        self.sender = asyncio.create_task(self.send_loop())
        self.messages = {}
        self.tokens, self.last_refill = 20.0, time.monotonic()

    def enqueue(self, message):
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            asyncio.create_task(self.ws.close(code=1013, message=b"slow consumer; reconnect"))

    async def send_loop(self):
        try:
            while True:
                message = await self.queue.get()
                await asyncio.wait_for(self.ws.send_str(dumps(message)), 5)
        except (ConnectionError, asyncio.TimeoutError, RuntimeError):
            await self.ws.close()

    def rate_ok(self):
        now = time.monotonic()
        self.tokens = min(20, self.tokens + (now - self.last_refill) * 10)
        self.last_refill = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True

    async def close(self):
        self.sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.sender
        await self.ws.close()


class Hub:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.connections, self.ready = {}, set()
        self.game = None
        self.seats, self.history, self.receipts, self.requests = {}, {}, {}, {}
        self.events = []
        self.revision = 0
        self.task = None
        self.failure = None
        self.started_at = None
        self.watchers = set()
        self.paused = False
        self.running = asyncio.Event()
        self.running.set()
        self.control_changed = asyncio.Event()
        # Host step control: in manual mode every step of the match waits for the
        # organizer to release it, so the dashboard decides the pace.
        self.step_mode = "manual" if config.get("step_mode") == "manual" else "auto"
        self.step_interval = max(0, int(config.get("step_interval_ms", 1000))) / 1000
        self.step_permits = 0
        self.step_event = asyncio.Event()
        self.last_step_at = None
        self.current_step = None
        self.waiting_step = None

    def snapshot(self):
        return dict(game=self.game.state(), paused=self.paused, seats=self.seats, history=self.history,
                    receipts=self.receipts, events=self.events, started_at=self.started_at,
                    pending=[dict(player=p["player"], message=p["message"]) for p in self.requests.values()
                             if not p["future"].done()])

    def persist(self, kind, body):
        self.store.commit(self.game.id if self.game else None, self.snapshot() if self.game else None, kind, body)
        self.revision += 1

    def player_for(self, agent_id):
        return next((p for p, a in self.seats.items() if a == agent_id), None)

    def emit_sync(self, kind, content, recipients=None, request_id=None):
        public = recipients is None
        recipients = sorted(self.seats) if public else recipients
        messages = []
        for p in recipients:
            message = envelope(kind, deepcopy(content), self.game.id, p, request_id, len(self.history[p]) + 1)
            self.history[p].append(message)
            messages.append((p, message))
        self.events.append(dict(index=len(self.events) + 1, at=utc(), type=kind, content=deepcopy(content),
                                recipients=list(recipients), public=public, request_id=request_id))
        if kind == "game_end":
            # The match is over: drop any step that was released but never used.
            self.step_permits = 0
        self.persist("event", self.events[-1])
        for p, message in messages:
            conn = self.connections.get(self.seats[p])
            if conn:
                conn.enqueue(message)
        return messages[0][1] if len(messages) == 1 else None

    async def emit(self, *args, **kwargs):
        if not self.game.result:
            await self.running.wait()
        return self.emit_sync(*args, **kwargs)

    def change_control(self):
        self.control_changed.set()
        self.control_changed = asyncio.Event()

    async def step_gate(self, label):
        """Wait before `label` for a host release (manual) or the step interval (auto)."""
        self.waiting_step = label
        try:
            loop = asyncio.get_running_loop()
            while True:
                if self.game and self.game.result:
                    self.step_permits = 0
                    self.current_step = label
                    return
                if self.step_permits:
                    self.step_permits -= 1
                    break
                timeout = None
                if self.step_mode == "auto":
                    # 自动模式由裁判自己放行，节奏与手动一致：至少间隔 step_interval。
                    due = (self.last_step_at or 0) + self.step_interval
                    if self.last_step_at is None or loop.time() >= due:
                        break
                    timeout = due - loop.time()
                # clear() 与 wait() 之间没有 await，HTTP 放行不会被丢掉。
                self.step_event.clear()
                try:
                    if timeout is None:
                        await self.step_event.wait()
                    else:
                        await asyncio.wait_for(self.step_event.wait(), timeout)
                except asyncio.TimeoutError:
                    continue
        finally:
            self.waiting_step = None
        self.current_step = label
        if self.last_step_at is not None and self.step_interval:
            # Keep at least `step_interval` between two steps so a held button
            # cannot hammer the agents with overlapping requests.
            gap = self.last_step_at + self.step_interval - loop.time()
            if gap > 0:
                await asyncio.sleep(gap)
        self.last_step_at = loop.time()

    def release_step(self):
        """Release exactly one step; extra requests are dropped instead of queued."""
        if not self.game or self.game.result:
            return dict(accepted=False, reason="idle", mode=self.step_mode, waiting=None)
        if self.step_mode == "auto":
            # 自动模式由裁判自行放行，不需要主持人点击。
            return dict(accepted=False, reason="auto", mode="auto", waiting=None)
        if self.step_permits:
            return dict(accepted=False, reason="queued", mode="manual", waiting=self.waiting_step)
        self.step_permits = 1
        self.step_event.set()
        return dict(accepted=True, reason="released", mode="manual", waiting=self.waiting_step)

    def set_step_mode(self, mode):
        """Switch between 手动放行 and 自动推进 for the running match."""
        if not self.game or self.game.result:
            raise web.HTTPConflict(text="只有进行中的对局可以切换推进模式")
        if mode not in {"auto", "manual"}:
            raise web.HTTPBadRequest(text="推进模式只能是 auto 或 manual")
        if mode == self.step_mode:
            return self.step_mode
        self.step_mode = mode
        self.step_permits = 0
        wake, self.step_event = self.step_event, asyncio.Event()
        wake.set()
        self.emit_sync("step_control", dict(mode=mode, reason="host"))
        return mode

    def set_paused(self, paused):
        if not self.game or self.game.result:
            raise web.HTTPConflict(text="只有进行中的对局可以暂停或继续")
        if self.paused == paused:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        self.paused = paused
        for request in self.requests.values():
            if request["future"].done():
                continue
            if paused:
                request["frozen_remaining"] = max(0, request["deadline"] - now)
            else:
                remaining = request.pop("frozen_remaining", 0)
                request["deadline"] = now + remaining
                # Keep the original immutable request in history. The current
                # request carries the refreshed deadline in the resume snapshot.
                request["message"] = deepcopy(request["message"])
                request["message"]["content"]["deadline_at"] = utc(time.time() + remaining)
        self.running.clear() if paused else self.running.set()
        self.change_control()
        self.emit_sync("game_control", {"paused": paused, "reason": "host"})
        if not paused:
            for agent_id, connection in self.connections.items():
                if self.player_for(agent_id):
                    self.sync(connection)

    def error(self, conn, code, request_id=None, retryable=False):
        p = self.player_for(conn.agent_id)
        req = self.requests.get(request_id)
        if not req or req["player"] != p:
            request_id, req, retryable = None, None, False
        content = dict(code=code, message=code, retryable=retryable,
                       deadline_at=req["message"]["content"]["deadline_at"] if req else None)
        if self.game and p:
            self.emit_sync("action_error", content, [p], request_id)
        else:
            conn.enqueue(envelope("action_error", content))

    async def ask_many(self, specs):
        if not specs:
            return {}
        if self.game.result:
            raise asyncio.CancelledError
        await self.running.wait()
        loop = asyncio.get_running_loop()
        timeout = self.game.limits["action_timeout_ms"] / 1000
        deadline = time.time() + timeout
        monotonic_deadline = loop.time() + timeout
        group = []
        # No await during creation: all participants get the same deadline and a fixed state.
        for p, kind, content in specs:
            rid = uid()
            request = dict(player=p, future=loop.create_future(), deadline=monotonic_deadline,
                           kind=kind, message=None, state="pending")
            self.requests[rid] = request
            content = dict(content, deadline_at=utc(deadline))
            # snapshot requires the message; construct it before emit's durable write.
            request["message"] = envelope(kind, content, self.game.id, p, rid, len(self.history[p]) + 1)
            request["message"] = self.emit_sync(kind, content, [p], rid)
            group.append((p, rid, request))

        async def wait_one(p, rid, request):
            while True:
                await self.running.wait()
                if request["future"].done():
                    return p, (request["future"].result(), rid)
                remaining = request["deadline"] - loop.time()
                if remaining <= 0:
                    action = default_action(request["kind"])
                    request["state"] = "timeout"
                    request["future"].set_result(action)
                    self.emit_sync("request_closed", dict(reason="timeout", default_action=action), [p], rid)
                    return p, (action, rid)
                changed = asyncio.create_task(self.control_changed.wait())
                try:
                    # Pause wakes the timeout wait immediately, without cancelling
                    # the action future. At a deadline race the future wins above.
                    await asyncio.wait({request["future"], changed}, timeout=remaining,
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    changed.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await changed

        results = await asyncio.gather(*(wait_one(*item) for item in group))
        delay = self.config.get("pace_ms", 0) / 1000
        if delay:
            await asyncio.sleep(delay)
        await self.running.wait()
        return dict(results)

    def submit(self, conn, message, received_at=None):
        if self.connections.get(conn.agent_id) is not conn:
            return
        p, rid = self.player_for(conn.agent_id), message["request_id"]
        if not self.game or message["game_id"] != self.game.id:
            raise ProtocolError("GAME_MISMATCH")
        request = self.requests.get(rid)
        if not request or request["player"] != p:
            raise ProtocolError("UNKNOWN_REQUEST")
        action = message["content"]
        receipt = self.receipts.get(rid)
        if receipt:
            if dumps(receipt["accepted_action"]) != dumps(action):
                raise ProtocolError("ACTION_ALREADY_ACCEPTED")
            self.emit_sync("action_ack", dict(status="already_accepted", accepted_action=action), [p], rid)
            return
        if self.game.result or request["state"] == "cancelled":
            raise ProtocolError("REQUEST_CLOSED")
        if self.paused:
            raise ProtocolError("GAME_PAUSED")
        if request["state"] == "timeout" or (received_at if received_at is not None else asyncio.get_running_loop().time()) >= request["deadline"]:
            raise ProtocolError("REQUEST_EXPIRED")
        validate_action(request["kind"], request["message"]["content"], action)
        self.receipts[rid] = dict(player=p, request_id=rid, accepted_action=deepcopy(action))
        request["state"] = "accepted"
        # The accepted intent is durable before ACK. A crash aborts this match;
        # accepted intents are never re-executed after restarting.
        request["future"].set_result(deepcopy(action))
        self.persist("accepted_action", dict(player=p, request_id=rid, action=action))
        self.emit_sync("action_ack", dict(status="accepted", accepted_action=action), [p], rid)

    def sync(self, conn):
        p = self.player_for(conn.agent_id)
        if not self.game or p is None:
            return
        game = self.game
        role = game.roles[p]
        private = {}
        if role == "werewolf":
            private = dict(werewolf_players=[p for p, r in game.roles.items() if r == "werewolf"])
        elif role == "witch":
            private = dict(antidote_remaining=game.antidote, poison_remaining=game.poison)
        elif role == "seer":
            private = dict(checks=game.checks)
        pending = next((r["message"] for r in self.requests.values() if r["player"] == p and
                        not r["future"].done() and (self.paused or r["deadline"] > asyncio.get_running_loop().time())), None)
        content = dict(through_seq=len(self.history[p]), rule_version=RULE_VERSION, rules=RULES, limits=game.limits,
                       status="ended" if game.result else "running", paused=self.paused, period=game.period, day=game.day, night=game.night,
                       players=game.players(), self=dict(player_id=p, role=role, camp=game.camp(p)),
                       private_state=private, history=self.history[p], pending_request=pending,
                       action_receipts=[{k: r[k] for k in ("request_id", "accepted_action")}
                                        for r in self.receipts.values() if r["player"] == p], result=game.result)
        message = envelope("state_sync", deepcopy(content), game.id, p)
        if len(dumps(message).encode("utf-8")) > MAX_BYTES:
            asyncio.create_task(self.abort("platform_error"))
            self.error(conn, "INTERNAL_ERROR")
            return
        conn.enqueue(message)

    async def start(self):
        if self.task and not self.task.done() or self.game and not self.game.result:
            raise web.HTTPConflict(text="已有正在进行的对局")
        agents = sorted(a for a in self.ready if a in self.connections)
        if len(agents) < 9:
            raise web.HTTPConflict(text="至少需要 9 个已连接且就绪的 Agent")
        agents = agents[:9]
        random.SystemRandom().shuffle(agents)
        self.seats = dict(enumerate(agents, 1))
        self.ready.difference_update(agents)
        self.history = {p: [] for p in self.seats}
        self.receipts, self.requests, self.events = {}, {}, []
        self.failure = None
        self.paused = False
        self.running.set()
        self.step_permits, self.last_step_at = 0, None
        self.current_step = self.waiting_step = None
        self.step_event = asyncio.Event()
        self.started_at = utc()
        self.game = Game(self.emit, self.ask_many, limits=self.config.get("limits"), gate=self.step_gate)
        self.persist("game_created", {"seats": self.seats})
        self.task = asyncio.create_task(self.run_game())
        return self.game.id

    async def run_game(self):
        try:
            await self.game.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Game failed")
            self.failure = "platform_error"
            self.cancel_pending("platform_abort")
            self.paused = False
            self.running.set()
            await self.game.finish("aborted", "platform_error")

    def cancel_pending(self, reason):
        for rid, request in self.requests.items():
            if not request["future"].done():
                request["state"] = "cancelled"
                request["future"].cancel()
                self.emit_sync("request_closed", dict(reason=reason, default_action=None), [request["player"]], rid)

    async def abort(self, reason="organizer_abort"):
        if not self.game or self.game.result:
            return
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.cancel_pending("platform_abort" if reason == "platform_error" else "game_ended")
        self.paused = False
        self.running.set()
        await self.game.finish("aborted", reason)

    async def recover(self):
        saved = self.store.latest()
        if not saved:
            return
        self.game = Game(self.emit, self.ask_many)
        self.game.restore(saved["game"])
        self.seats = {int(p): a for p, a in saved["seats"].items()}
        self.history = {int(p): h for p, h in saved["history"].items()}
        self.receipts, self.events = saved["receipts"], saved["events"]
        self.started_at = saved["started_at"]
        if not self.game.result:
            for pending in saved.get("pending", []):
                self.emit_sync("request_closed", dict(reason="platform_abort", default_action=None),
                               [pending["player"]], pending["message"]["request_id"])
            await self.game.finish("aborted", "platform_error")

    def view(self, admin=False):
        game = self.game
        lobby = [dict(agent_id=a, connected=a in self.connections, ready=a in self.ready)
                 for a in sorted(self.config["agents"])]
        view = dict(revision=self.revision, server_time=utc(), mode="host" if admin else "public", lobby=lobby,
                    game=None, events=[], requests=[])
        if admin:
            # Step labels reveal who is still alive, so they stay host-only.
            view["step"] = dict(mode=self.step_mode, interval_ms=round(self.step_interval * 1000),
                                current=self.current_step, waiting=self.waiting_step, queued=bool(self.step_permits))
        else:
            # 公开视图只说明“是否在等主持人放行”，不含会泄露角色的步骤名。
            view["step"] = dict(waiting=bool(self.waiting_step))
        if not game:
            return view
        revealed = bool(game.result)
        players = []
        for p, role in game.roles.items():
            player = dict(player_id=p, agent_id=self.seats[p], alive=p in game.alive,
                          connected=self.seats[p] in self.connections)
            if admin or revealed:
                player["role"] = role
            players.append(player)
        view["game"] = dict(id=game.id, day=game.day, night=game.night, period=game.period,
                            paused=self.paused,
                            phase=game.phase if admin else game.period, players=players, result=game.result,
                            started_at=self.started_at, limits=game.limits, rule_version=RULE_VERSION)
        selected = self.events if admin else [e for e in self.events if e["public"]]
        # Public stream gets its own indices, never the global hidden-event index.
        view["events"] = [dict(index=i + 1, at=e["at"], type=e["type"], content=e["content"],
                               **({"recipients": e["recipients"], "public": e["public"]} if admin else {}))
                          for i, e in enumerate(selected)]
        if admin:
            view["requests"] = [dict(player_id=r["player"], type=r["kind"], state=r["state"],
                                request_id=rid, deadline_at=r["message"]["content"]["deadline_at"],
                                remaining_ms=round(r.get("frozen_remaining", 0) * 1000) if self.paused and r["state"] == "pending" else None,
                                action=self.receipts.get(rid, {}).get("accepted_action"))
                                for rid, r in self.requests.items()]
        # Hidden event count must not leak via revision.
        if not admin:
            view["revision"] = None
        return view


HUB_KEY = web.AppKey("hub", Hub)


async def agent_socket(request):
    hub = request.app[HUB_KEY]
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_BYTES)
    await ws.prepare(request)
    conn = None
    try:
        first = await asyncio.wait_for(ws.receive(), 10)
        if first.type != WSMsgType.TEXT:
            await ws.close(code=1008)
            return ws
        try:
            message = validate_client(strict_loads(first.data))
            if message["type"] != "auth":
                raise ProtocolError("AUTH_FAILED")
            c = message["content"]
            expected = hub.config["agents"].get(c["agent_id"])
            if not expected or not secrets_equal(expected, c["token"]):
                raise ProtocolError("AUTH_FAILED")
        except ProtocolError as exc:
            code = "UNSUPPORTED_VERSION" if exc.code == "UNSUPPORTED_VERSION" else "AUTH_FAILED"
            await ws.send_str(dumps(envelope("auth_result", dict(ok=False, agent_id=None, connection_id=None,
                                                                 code=code, message="认证失败"))))
            await ws.close(code=1008)
            return ws
        conn = Connection(ws, c["agent_id"])
        old = hub.connections.get(conn.agent_id)
        hub.connections[conn.agent_id] = conn
        conn.enqueue(envelope("auth_result", dict(ok=True, agent_id=conn.agent_id, connection_id=conn.id,
                                                  code=None, message="connected")))
        hub.sync(conn)
        hub.revision += 1
        if old:
            await old.close()
        async for frame in ws:
            received_at = asyncio.get_running_loop().time()
            if hub.connections.get(conn.agent_id) is not conn:
                break
            if frame.type != WSMsgType.TEXT:
                if frame.type == WSMsgType.BINARY:
                    await ws.close(code=1003)
                break
            message = None
            try:
                if not conn.rate_ok():
                    raise ProtocolError("RATE_LIMITED")
                message = validate_client(strict_loads(frame.data))
                canonical = dumps(message)
                if not hub.store.check_message_id(conn.agent_id, message["message_id"], canonical):
                    raise ProtocolError("INVALID_MESSAGE")
                kind = message["type"]
                if kind == "auth":
                    raise ProtocolError("INVALID_MESSAGE")
                # No auth/token contents are ever journaled.
                hub.persist("client_message", dict(agent_id=conn.agent_id, message=message))
                if kind == "ready":
                    if hub.game and not hub.game.result and hub.player_for(conn.agent_id):
                        raise ProtocolError("NOT_READY_ALLOWED")
                    hub.ready.add(conn.agent_id)
                    conn.enqueue(envelope("ready_result", {"ready": True}))
                    hub.revision += 1
                    if hub.config.get("auto_start") and len(hub.ready & hub.connections.keys()) >= 9:
                        if not hub.game or hub.game.result:
                            await hub.start()
                elif kind == "sync":
                    if not hub.game or message["game_id"] != hub.game.id or hub.player_for(conn.agent_id) is None:
                        raise ProtocolError("GAME_MISMATCH")
                    hub.sync(conn)
                elif kind == "action":
                    hub.submit(conn, message, received_at)
            except ProtocolError as exc:
                rid = message.get("request_id") if message else None
                pending = hub.requests.get(rid)
                retry = bool(pending and not pending["future"].done() and asyncio.get_running_loop().time() < pending["deadline"])
                hub.error(conn, exc.code, rid, retry and exc.code not in {"GAME_MISMATCH", "UNKNOWN_REQUEST", "GAME_PAUSED"})
    except (asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        if conn:
            if hub.connections.get(conn.agent_id) is conn:
                del hub.connections[conn.agent_id]
                hub.revision += 1
            await conn.close()
    return ws


def secrets_equal(left, right):
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def is_admin(request):
    expected = request.app[HUB_KEY].config["admin_token"]
    header = request.headers.get("Authorization", "")
    return bool(header.startswith("Bearer ") and secrets_equal(header[7:], expected))


def require_admin(request):
    if not is_admin(request):
        raise web.HTTPUnauthorized(text="需要主持人凭证")


async def state(request):
    if request.headers.get("Authorization") and not is_admin(request):
        raise web.HTTPUnauthorized(text="主持人凭证无效")
    return web.json_response(request.app[HUB_KEY].view(is_admin(request)))


async def watch(request):
    hub = request.app[HUB_KEY]
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
    await ws.prepare(request)
    hub.watchers.add(ws)
    sender = None
    try:
        frame = await asyncio.wait_for(ws.receive(), 10)
        data = strict_loads(frame.data) if frame.type == WSMsgType.TEXT else {}
        if not isinstance(data, dict) or set(data) != {"token"} or not isinstance(data["token"], str):
            await ws.close(code=1008)
            return ws
        admin = bool(data["token"] and secrets_equal(data["token"], hub.config["admin_token"]))
        if data["token"] and not admin:
            await ws.close(code=1008)
            return ws

        async def push():
            try:
                previous = None
                while not ws.closed:
                    view = hub.view(admin)
                    # No timestamp-only pushes. Public view cannot observe private revision increments.
                    compare = dict(view, server_time=None)
                    serialized = dumps(compare)
                    if serialized != previous:
                        await asyncio.wait_for(ws.send_str(dumps(view)), 5)
                        previous = serialized
                    await asyncio.sleep(.25)
            except (ConnectionError, RuntimeError, asyncio.TimeoutError):
                await ws.close()
        sender = asyncio.create_task(push())
        async for _ in ws:
            pass
    except (ProtocolError, asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        hub.watchers.discard(ws)
        if sender:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionError, RuntimeError, asyncio.TimeoutError):
                await sender
    return ws


async def start_game(request):
    require_admin(request)
    game_id = await request.app[HUB_KEY].start()
    return web.json_response({"game_id": game_id})


async def abort_game(request):
    require_admin(request)
    await request.app[HUB_KEY].abort()
    return web.json_response({"ok": True})


async def pause_game(request):
    require_admin(request)
    request.app[HUB_KEY].set_paused(True)
    return web.json_response({"paused": True})


async def resume_game(request):
    require_admin(request)
    request.app[HUB_KEY].set_paused(False)
    return web.json_response({"paused": False})


async def step_game(request):
    require_admin(request)
    return web.json_response(request.app[HUB_KEY].release_step())


async def step_mode(request):
    require_admin(request)
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="请求体必须是 JSON")
    if not isinstance(body, dict) or set(body) != {"mode"} or not isinstance(body["mode"], str):
        raise web.HTTPBadRequest(text="请求体只能是 {\"mode\":\"auto|manual\"}")
    return web.json_response({"mode": request.app[HUB_KEY].set_step_mode(body["mode"])})


async def replays(request):
    require_admin(request)
    return web.json_response(request.app[HUB_KEY].store.list_games())


async def replay(request):
    require_admin(request)
    saved = request.app[HUB_KEY].store.replay(request.match_info["game_id"])
    if not saved:
        raise web.HTTPNotFound()
    return web.json_response(saved)


@web.middleware
async def security_headers(request, handler):
    response = await handler(request)
    if not isinstance(response, web.WebSocketResponse):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
    return response


def create_app(config, db_path=":memory:"):
    if len(config.get("agents", {})) < 9 or not config.get("admin_token"):
        raise ValueError("Configuration needs at least 9 agent credentials and an admin_token")
    for value in config.get("limits", {}).values():
        if type(value) is not int or value <= 0:
            raise ValueError("Limits must be positive integers")
    from .protocol import LIMITS
    if set(config.get("limits", {})) - set(LIMITS):
        raise ValueError("Unknown limits")
    if config.get("limits", {}).get("max_message_bytes", MAX_BYTES) != MAX_BYTES:
        raise ValueError("Protocol v1 fixes max_message_bytes at 1048576")
    if type(config.get("pace_ms", 0)) is not int or config.get("pace_ms", 0) < 0:
        raise ValueError("pace_ms must be a nonnegative integer")
    if config.get("step_mode", "auto") not in {"auto", "manual"}:
        raise ValueError("step_mode must be 'auto' or 'manual'")
    if type(config.get("step_interval_ms", 1000)) is not int or config.get("step_interval_ms", 1000) < 0:
        raise ValueError("step_interval_ms must be a nonnegative integer")
    if any(not isinstance(v, str) or not v for v in [config["admin_token"], *config["agents"].values()]):
        raise ValueError("Credentials must be nonempty strings")
    store = Store(db_path)
    hub = Hub(config, store)
    app = web.Application(middlewares=[security_headers], client_max_size=MAX_BYTES)
    app[HUB_KEY] = hub
    app.router.add_get("/ws/agent", agent_socket)
    app.router.add_get("/ws/watch", watch)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/games/start", start_game)
    app.router.add_post("/api/games/abort", abort_game)
    app.router.add_post("/api/games/pause", pause_game)
    app.router.add_post("/api/games/resume", resume_game)
    app.router.add_post("/api/games/step", step_game)
    app.router.add_post("/api/games/mode", step_mode)
    app.router.add_get("/api/replays", replays)
    app.router.add_get("/api/replays/{game_id}", replay)
    async def health(_):
        return web.json_response({"status": "ok", "protocol_version": VERSION})

    async def index(_):
        return web.FileResponse(STATIC / "index.html")
    app.router.add_get("/health", health)
    app.router.add_get("/", index)
    app.router.add_static("/static/", STATIC)

    async def startup(_):
        await hub.recover()

    async def shutdown(_):
        await hub.abort("platform_error")
        for conn in list(hub.connections.values()):
            await conn.close()
        for ws in list(hub.watchers):
            await ws.close(code=1001, message=b"server shutdown")

    async def cleanup(_):
        store.close()
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(cleanup)
    return app


def init_config(path):
    path = Path(path)
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing credentials: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    config = dict(admin_token=secrets.token_urlsafe(32),
                  agents={f"agent-{i:02}": secrets.token_urlsafe(32) for i in range(1, 10)},
                  auto_start=False, step_mode="manual", step_interval_ms=1000, pace_ms=350)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    # Separate credential files so the organizer can distribute only one per member.
    for agent_id, token in config["agents"].items():
        (path.parent / f"{agent_id}.json").write_text(json.dumps(dict(agent_id=agent_id, token=token), indent=2), encoding="utf-8")
    print(f"Created {path} and 9 individual credential files. Keep the server config private.")


def main():
    parser = argparse.ArgumentParser(description="Mooncourt referee")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--config", default="runtime/config.json")
    parser.add_argument("--db", default="runtime/matches.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.init:
        init_config(args.config)
        return
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(config, args.db), host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
