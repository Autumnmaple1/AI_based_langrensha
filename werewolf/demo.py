"""Local demonstration: the actual server and nine isolated SDK objects over TCP."""
import argparse
import asyncio
import contextlib
import json
from pathlib import Path
from aiohttp import web
from .server import create_app, init_config
from .sdk import AgentClient
from .example_agent import ExampleAgent


async def run(args):
    path=Path(args.config)
    if not path.exists():
        init_config(path)
    config=json.loads(path.read_text(encoding="utf-8"))
    config.update(auto_start=not args.manual,pace_ms=args.pace_ms,step_mode=args.step_mode,
                  step_interval_ms=args.step_interval_ms)
    app=create_app(config,args.db)
    runner=web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner,"127.0.0.1",args.port).start()
    clients=[AgentClient(f"ws://127.0.0.1:{args.port}/ws/agent",aid,token,ExampleAgent(seed=i,delay=args.agent_delay))
             for i,(aid,token) in enumerate(list(config["agents"].items())[:9])]
    tasks=[asyncio.create_task(c.run()) for c in clients]
    print(f"Dashboard: http://127.0.0.1:{args.port}",flush=True)
    print(f"主持人凭证：admin_token in {path.resolve()}。本演示使用真实 WebSocket，无需模型密钥。",flush=True)
    try:
        results=await asyncio.gather(*tasks)
        print(json.dumps(results[0],ensure_ascii=False),flush=True)
        if not args.exit_after_game:
            print("对局已结束，服务器保留供查看和回放。Ctrl+C 退出。",flush=True)
            await asyncio.Event().wait()
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await runner.cleanup()


def main():
    p=argparse.ArgumentParser(description="Run a complete local demo")
    p.add_argument("--port",type=int,default=8765)
    p.add_argument("--config",default="runtime/demo/config.json")
    p.add_argument("--db",default="runtime/demo/matches.sqlite3")
    p.add_argument("--pace-ms",type=int,default=650)
    p.add_argument("--agent-delay",type=float,default=.15,help="Baseline thinking delay in seconds, useful for presentations")
    p.add_argument("--manual",action="store_true",help="Wait for host to click start")
    p.add_argument("--step-mode",choices=["manual","auto"],default="manual",
                   help="manual: 主持人逐步骤放行；auto: 裁判自行推进（旧行为）")
    p.add_argument("--step-interval-ms",type=int,default=1000,dest="step_interval_ms",
                   help="步进模式下两步之间至少间隔的毫秒数")
    p.add_argument("--exit-after-game",action="store_true")
    args=p.parse_args()
    Path(args.db).parent.mkdir(parents=True,exist_ok=True)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__=="__main__":main()
