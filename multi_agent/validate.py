"""Run the current agent, referee and dashboard regression suite."""
import argparse
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
    summary = dict(offline_passed=result.returncode==0)
    (report/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Report: {report}")
    raise SystemExit(0 if summary["offline_passed"] else 1)


if __name__ == "__main__":
    main()
