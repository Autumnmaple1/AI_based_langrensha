"""One-command offline verification plus optional explicitly selected live model checks."""
import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

from aiohttp import ClientSession
from .agent import WerewolfAgent
from .run import load_config
from werewolf.scenarios import cases, fixture


async def live_check(path):
    _, _, specs = load_config(path)
    results = []
    async with ClientSession() as session:
        for spec in specs:
            if spec["settings"].get("mode") != "llm":
                raise ValueError("Live validation requires every selected agent in llm mode")
            agent = WerewolfAgent(spec["settings"], session, seed=spec["seed"])
            for case in cases():
                observation, request, _ = fixture(case, seconds=float(spec["settings"].get("timeout_seconds",20))+2)
                before = agent.stats["model_success"]
                await agent.act(observation, request)
                results.append(dict(agent_id=spec["agent_id"], scenario=case[0],
                                    passed=agent.stats["model_success"]==before+1))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", metavar="CONFIG", help="Also call actual configured models (14 requests per instance; may incur API cost)")
    parser.add_argument("--report-dir", default="runtime/agent-validation")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    from uuid import uuid4
    report = Path(args.report_dir).resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid4().hex[:8])
    report.mkdir(parents=True)
    command = [sys.executable,"-m","pytest","-q","tests","multi_agent/test_agent.py", "-p","no:cacheprovider",
               f"--basetemp={report/'temp'}", f"--junitxml={report/'junit.xml'}"]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    (report/"tests.txt").write_text(result.stdout+result.stderr, encoding="utf-8")
    print(result.stdout, end="")
    summary = dict(offline_passed=result.returncode==0, live="not_requested")
    if result.returncode == 0 and args.live:
        try:
            rows = asyncio.run(live_check(args.live))
            summary.update(live=rows, live_passed=all(row["passed"] for row in rows))
        except Exception as exc:
            summary.update(live="failed", live_passed=False, error=type(exc).__name__)
    (report/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Report: {report}")
    raise SystemExit(0 if summary["offline_passed"] and summary.get("live_passed",True) else 1)


if __name__ == "__main__":
    main()
