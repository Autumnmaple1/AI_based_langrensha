"""agents/ 的自检闭环：模板与示例必须全过，坏 agent 必须被明确指出来。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.check import find_agent_class, inspect_interface, load_module, main, run_scenarios  # noqa: E402
from agents.run import load_agent  # noqa: E402

GOOD = ("agents/template/my_agent.py", "agents/example/baseline_agent.py", "agents/example/llm_agent.py")


def agent_class(path):
    return find_agent_class(load_module(ROOT / path))


@pytest.mark.parametrize("path", GOOD)
async def test_shipped_agents_pass_interface_and_every_scenario(path):
    problems, notes = inspect_interface(agent_class(path))
    assert problems == [], problems
    assert any("act(observation, request) ✓" in note for note in notes)
    results = await run_scenarios(agent_class(path)(), timeout=5)
    assert len(results) == 14
    broken = [entry for entry in results if not entry["ok"]]
    assert broken == [], broken


async def test_scenarios_cover_every_request_type():
    kinds = {entry["kind"] for entry in await run_scenarios(agent_class(GOOD[0])(), timeout=5)}
    assert kinds == {"werewolves_act", "werewolves_revote", "witch_act", "seer_act",
                     "hunter_act", "speech", "speech_dying", "vote"}


def write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_sync_act_is_reported(tmp_path):
    path = write(tmp_path, "sync_agent.py",
                 "class SyncAgent:\n"
                 "    def act(self, observation, request):\n"
                 "        return {'action': 'vote', 'target': 0}\n")
    problems, _notes = inspect_interface(find_agent_class(load_module(path)))
    assert any("async def" in problem for problem in problems)


def test_class_without_act_is_reported(tmp_path):
    path = write(tmp_path, "empty_agent.py", "class Empty:\n    pass\n")
    with pytest.raises(SystemExit) as error:
        find_agent_class(load_module(path))
    assert "act()" in str(error.value)


async def test_illegal_target_and_unimplemented_branch_are_reported(tmp_path):
    path = write(tmp_path, "bad_agent.py",
                 "class BadAgent:\n"
                 "    async def act(self, observation, request):\n"
                 "        if request['type'] == 'vote':\n"
                 "            return {'action': 'vote', 'target': 99}\n"
                 "        raise NotImplementedError(request['type'])\n")
    results = {entry["name"]: entry for entry in
               await run_scenarios(find_agent_class(load_module(path))(), timeout=5)}
    assert results["vote"]["ok"] is False and results["vote"]["error"] == "ILLEGAL_TARGET"
    assert results["speech"]["ok"] is False and "NotImplementedError" in results["speech"]["error"]
    assert results["vote_abstain"]["ok"] is False


def test_cli_exit_codes(tmp_path, capsys):
    assert main(["agents/template/my_agent.py", "--quiet"]) == 0
    broken = write(tmp_path, "broken_agent.py",
                   "class Broken:\n"
                   "    async def act(self, observation, request):\n"
                   "        raise RuntimeError('boom')\n")
    assert main([str(broken), "--quiet"]) == 1
    assert main([str(tmp_path / "missing.py")]) == 1
    output = capsys.readouterr().out
    assert "有检查未通过" in output and "找不到文件" in output


def test_run_loader_passes_settings_and_tolerates_simple_classes(tmp_path):
    llm = load_agent(ROOT / "agents/example/llm_agent.py", seed=1,
                     settings={"model": "m", "base_url": "http://example.invalid", "temperature": 0.2})
    assert llm.model == "m" and llm.temperature == 0.2
    template = load_agent(ROOT / "agents/template/my_agent.py", seed=2, settings={"whatever": 1})
    assert template.seat is None      # 不认识设置项时会退回更简单的构造
    plain = write(tmp_path, "plain_agent.py",
                  "class Plain:\n"
                  "    async def act(self, observation, request):\n"
                  "        return {'action': 'pass'}\n")
    assert type(load_agent(plain)).__name__ == "Plain"
