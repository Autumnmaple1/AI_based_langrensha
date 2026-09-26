"""Agent 自检：接口是否齐全、每种动作是否合法。

用法
    python -m agents.check                          # 检查内置模板与示例
    python -m agents.check agents/team-alice/my_agent.py
    python -m agents.check agents/team-alice/my_agent.py --connect ws://<地址>:8765/ws/agent --credentials agent-03.json

它做三件事
    1. 接口检查：是否存在 act(observation, request)，是不是 async，可选的钩子有没有。
    2. 场景检查：用 14 个固定场景把每一种请求都跑一遍（狼刀、重投、空刀、女巫救/毒/不用药、
       查验、猎人开枪/放弃、发言、遗言、投票、弃票），逐个用裁判的校验器验证合不合法；
       这个检查不连服务器、不花钱、不影响任何对局。
    3. 连线检查（可选 --connect）：用你的凭证连上真实服务器，确认凭证和网络没问题。

退出码 0 表示全部通过；有任何一项失败返回 1，并会指出是哪个场景、错在哪里。
"""
import argparse
import asyncio
import importlib.util
import inspect
import json
import sys
from pathlib import Path

from werewolf.protocol import ProtocolError, client_message, validate_action
from werewolf.scenarios import cases, fixture
from agents.run import build_agent

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("act",)
OPTIONAL = ("on_game_start", "on_game_end")


# ------------------------------------------------------------------ 加载模块

def load_module(path):
    """把成员写的 .py 文件当模块加载，返回模块对象。"""
    path = Path(path).resolve()
    if not path.exists():
        raise SystemExit(f"找不到文件: {path}")
    spec = importlib.util.spec_from_file_location(f"user_agent_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_agent_class(module, name=None):
    """找出要检查的类：默认取文件里定义了 act() 的类（可以有多个，取第一个）。"""
    found = [obj for obj in vars(module).values()
             if inspect.isclass(obj) and obj.__module__ == module.__name__ and hasattr(obj, "act")]
    if name:
        if name not in vars(module):
            raise SystemExit(f"文件里没有类 {name}")
        return vars(module)[name]
    if not found:
        raise SystemExit("这个文件里找不到带 act() 方法的类。请参考 agents/template/my_agent.py。")
    return found[0]


# ------------------------------------------------------------------ 检查

def inspect_interface(cls):
    """检查必须实现的方法和签名，返回 (问题列表, 发现列表)。"""
    problems, notes = [], []
    for name in REQUIRED:
        method = getattr(cls, name, None)
        if method is None:
            problems.append(f"缺少必须实现的方法 {name}(observation, request)")
            continue
        if not inspect.iscoroutinefunction(method):
            problems.append(f"{name} 必须写成 async def（SDK 会用 await 调用它）")
            continue
        parameters = [p for p in inspect.signature(method).parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        if len(parameters) < 3:            # self + observation + request
            problems.append(f"{name} 的参数应该是 (self, observation, request)，当前是 {inspect.signature(method)}")
        else:
            notes.append(f"{name}(observation, request) ✓")
    for name in OPTIONAL:
        if inspect.iscoroutinefunction(getattr(cls, name, None)):
            notes.append(f"{name} 钩子已实现 ✓")
        else:
            notes.append(f"{name} 钩子未实现（可选）")
    return problems, notes


async def run_scenarios(agent, *, timeout=20.0, verbose=False):
    """跑一遍所有场景，返回每个场景的结果。不会连服务器。"""
    results = []
    for case in cases():
        name, kind, _content, _expected = case
        observation, request, _ = fixture(case, seconds=timeout + 2)
        entry = dict(name=name, kind=kind)
        started = asyncio.get_running_loop().time()
        try:
            action = await asyncio.wait_for(agent.act(observation, request), timeout)
            entry["action"] = action
            json.dumps(action, ensure_ascii=False, allow_nan=False)   # 必须能发成 JSON
            validate_action(kind, request["content"], action)          # 裁判会这样校验
            entry["ok"] = True
        except asyncio.TimeoutError:
            entry.update(ok=False, error=f"超过 {timeout:g} 秒没有返回")
        except ProtocolError as exc:
            entry.update(ok=False, error=f"{exc.code}")
        except NotImplementedError:
            entry.update(ok=False, error="这个分支还没实现（NotImplementedError）")
        except Exception as exc:
            entry.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        entry["elapsed_ms"] = round((asyncio.get_running_loop().time() - started) * 1000)
        results.append(entry)
        if verbose:
            mark = "✓" if entry["ok"] else "✗"
            detail = json.dumps(entry.get("action"), ensure_ascii=False) if entry["ok"] else entry.get("error")
            print(f"    {mark} {name:14s} {kind:18s} {entry['elapsed_ms']:5d}ms  {detail}")
    return results


async def check_path(path, *, class_name=None, timeout=20.0, verbose=True, settings=None):
    """检查一个文件，返回 (是否通过, 摘要文本)。"""
    module = load_module(path)
    cls = find_agent_class(module, class_name)
    problems, notes = inspect_interface(cls)
    print(f"\n▌{Path(path).as_posix()}  →  类 {cls.__name__}")
    for note in notes:
        print(f"  接口  {note}")
    if problems:
        for problem in problems:
            print(f"  接口  ✗ {problem}")
        return False

    agent = build_agent(cls, settings=settings)
    start_hook = getattr(agent, "on_game_start", None)
    if start_hook:
        # 和真实流程一致：先调用开局钩子，再跑场景（场景自带最小历史）。
        observation = fixture(cases()[0], seconds=timeout + 2)[0]
        try:
            await asyncio.wait_for(start_hook(observation), timeout)
            print("  钩子  on_game_start 调用成功 ✓")
        except Exception as exc:
            print(f"  钩子  ✗ on_game_start 抛错：{type(exc).__name__}: {exc}")
            return False

    print("  场景  （14 个固定场景，逐个用裁判的校验器验证）")
    results = await run_scenarios(agent, timeout=timeout, verbose=verbose)
    failed = [r for r in results if not r["ok"]]
    slowest = max(results, key=lambda r: r["elapsed_ms"])
    print(f"  结果  {len(results) - len(failed)}/{len(results)} 通过"
          f"（最慢 {slowest['name']} {slowest['elapsed_ms']}ms）")
    if failed:
        for entry in failed:
            print(f"  ✗ {entry['name']}（{entry['kind']}）：{entry['error']}")
        print("  提示  常见原因：返回的目标不在 legal_targets 里；act 写成同步函数；"
              "某个分支直接 raise NotImplementedError；发言为空或超过 max_codepoints。")
        return False
    return True


# ------------------------------------------------------------------ 连线检查

async def check_connection(url, credentials, *, timeout=10.0):
    """用真实凭证连一次服务器，确认认证与 ready 能走通。"""
    from aiohttp import ClientSession, WSMsgType
    data = json.loads(Path(credentials).read_text(encoding="utf-8"))
    agent_id, token = data["agent_id"], data["token"]
    print(f"\n▌连线 {url}  身份 {agent_id} / 凭证 {Path(credentials).name}")
    async with ClientSession() as session:
        async with session.ws_connect(url, heartbeat=20, max_msg_size=1048576) as ws:
            await ws.send_str(json.dumps(client_message(
                "auth", dict(agent_id=agent_id, token=token, client_version="agents-check/1.0"))))
            async with asyncio.timeout(timeout):
                while True:
                    frame = await ws.receive()
                    if frame.type != WSMsgType.TEXT:
                        print("  ✗ 连接被服务器关闭")
                        return False
                    message = json.loads(frame.data)
                    if message["type"] == "auth_result":
                        if not message["content"]["ok"]:
                            print(f"  ✗ 认证失败（{message['content'].get('code')}）："
                                  f"凭证不对或已被替换，向主办方要一份新的 agent-XX.json")
                            return False
                        print("  认证  成功 ✓")
                        await ws.send_str(json.dumps(client_message("ready", {})))
                    elif message["type"] == "ready_result":
                        print("  就绪  服务器已接受，等主办方开局即可 ✓")
                        return True
                    elif message["type"] == "action_error":
                        code = message["content"]["code"]
                        if code == "NOT_READY_ALLOWED":
                            print("  就绪  服务器正在比赛中，稍后会接受 ✓")
                            return True
                        print(f"  ✗ 服务器返回 {code}")
                        return False


# ------------------------------------------------------------------ 入口

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("agent", nargs="*", help="要检查的 agent 文件；留空则检查内置模板与示例")
    parser.add_argument("--class", dest="class_name", help="文件里有多个类时指定类名")
    parser.add_argument("--timeout", type=float, default=20.0, help="单个动作的等待上限（秒），默认 20")
    parser.add_argument("--quiet", action="store_true", help="只打印结论，不逐个场景打印")
    parser.add_argument("--settings", help="可选：单个 agent 的设置 JSON（模型地址、模型名等）。"
                                           "给了它就会真的调用模型，14 个场景共 14 次调用")
    parser.add_argument("--connect", metavar="WS_URL", help="额外连一次真实服务器，例如 ws://1.2.3.4:8765/ws/agent")
    parser.add_argument("--credentials", help="配合 --connect 使用的凭证文件，例如 agent-03.json")
    args = parser.parse_args(argv)
    if args.connect and not args.credentials:
        parser.error("--connect 需要同时给出 --credentials")

    settings = {}
    if args.settings:
        settings = json.loads(Path(args.settings).read_text(encoding="utf-8"))
        settings = {k: v for k, v in settings.items() if not k.startswith("_")}
    targets = args.agent or ["agents/template/my_agent.py", "agents/example/baseline_agent.py",
                             "agents/example/llm_agent.py"]
    passed = True
    for target in targets:
        try:
            ok = asyncio.run(check_path(target, class_name=args.class_name,
                                        timeout=args.timeout, verbose=not args.quiet,
                                        settings=settings))
        except SystemExit as exc:
            print(f"\n▌{target}\n  ✗ {exc}")
            ok = False
        passed = passed and ok

    if args.connect:
        try:
            passed = asyncio.run(check_connection(args.connect, args.credentials)) and passed
        except Exception as exc:
            print(f"\n▌连线 {args.connect}\n  ✗ {type(exc).__name__}: {exc}")
            passed = False

    print("\n" + ("全部通过：你的 agent 可以在比赛里跑。" if passed else
                  "有检查未通过，先按上面的提示修好再连服务器。"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
