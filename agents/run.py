"""用自己的 agent 文件连服务器打比赛。

    python -m agents.run --agent agents/team-alice/my_agent.py --credentials agent-03.json
                        （再加 --server ws://<主办方公布的地址>:8765/ws/agent 指定服务器）

凭证文件长这样（主办方会单独发给你，里面是你自己的 agent_id 和 token）：

    {"agent_id": "agent-03", "token": "……"}

服务器地址由主办方在活动开始时公布；本地演示可以写 ws://127.0.0.1:8765/ws/agent。
脚本可以一直开着：一局结束后默认退出，加 --keep-alive 则自动准备下一局。
"""
import argparse
import asyncio
import importlib.util
import inspect
import json
import sys
from pathlib import Path

from werewolf.sdk import AgentClient

DEFAULT_SERVER = "ws://127.0.0.1:8765/ws/agent"


def build_agent(klass, seed=None, settings=None):
    """Explicit settings must match the constructor; never silently discard them."""
    kwargs = dict(settings or {})
    if seed is not None:
        kwargs["seed"] = seed
    inspect.signature(klass).bind(**kwargs)
    return klass(**kwargs)


def load_agent(path, class_name=None, seed=None, settings=None):
    """加载 .py 文件里的 Agent 类并实例化（和 agents/check.py 用的是同一套约定）。"""
    path = Path(path).resolve()
    if not path.exists():
        raise SystemExit(f"找不到 agent 文件: {path}")
    spec = importlib.util.spec_from_file_location(f"user_agent_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    classes = [obj for obj in vars(module).values()
               if inspect.isclass(obj) and obj.__module__ == module.__name__ and hasattr(obj, "act")]
    if class_name:
        klass = vars(module).get(class_name)
        if klass is None:
            raise SystemExit(f"文件里没有类 {class_name}")
    elif classes:
        klass = classes[0]
    else:
        raise SystemExit("这个文件里找不到带 act() 的类，请参考 agents/template/my_agent.py")
    return build_agent(klass, seed, settings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", required=True, help="你的 agent 文件，例如 agents/team-alice/my_agent.py")
    parser.add_argument("--class", dest="class_name", help="文件里有多个类时指定类名")
    parser.add_argument("--server", default=DEFAULT_SERVER, help=f"裁判地址，默认 {DEFAULT_SERVER}")
    parser.add_argument("--credentials", required=True, help="主办方发给你的凭证 JSON，例如 agent-03.json")
    parser.add_argument("--seed", type=int, help="随机种子（想让每局结果可复现时用）")
    parser.add_argument("--settings", help="可选：单个 agent 的设置 JSON（模型地址、模型名等），"
                                           "例如 agents/example/settings.example.json")
    parser.add_argument("--keep-alive", action="store_true", help="一局结束后继续准备下一局")
    args = parser.parse_args(argv)

    credentials = json.loads(Path(args.credentials).read_text(encoding="utf-8"))
    settings = json.loads(Path(args.settings).read_text(encoding="utf-8")) if args.settings else {}
    settings = {k: v for k, v in settings.items() if not k.startswith("_")}
    agent = load_agent(args.agent, args.class_name, args.seed, settings)
    client = AgentClient(args.server, credentials["agent_id"], credentials["token"], agent,
                         stop_after_game=not args.keep_alive)
    print(f"Agent 文件 : {Path(args.agent).as_posix()}  →  类 {type(agent).__name__}", flush=True)
    print(f"连接地址   : {args.server}", flush=True)
    print(f"身份       : {credentials['agent_id']}（座位号由裁判每局随机分配，开局后会告诉你）", flush=True)
    if settings:
        print(f"设置       : {args.settings}（{', '.join(sorted(k for k in settings if 'key' not in k))}）", flush=True)
    print("等待其他 Agent 就绪……主办方开局后开始行动。Ctrl+C 退出。", flush=True)
    try:
        result = asyncio.run(client.run())
    except KeyboardInterrupt:
        return 0
    except PermissionError:
        print("\n认证失败：凭证不对或已被替换，向主办方要一份新的 agent-XX.json。", file=sys.stderr)
        return 2
    if result:
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
